"""B站点歌的「VCPedia 原曲优先」链路（v1.6.4）。

背景：B站搜索会返回翻唱 / 现场 / 串烧 / 搬运等一堆同名片源，`search_rank.py`
的相关度排序只能剔掉明显无关的，**保证不了播的是原曲**。cv_lyric_context
v2.14.0 起会把 VCPedia 词条信息框里的 `bb_id`（原曲投稿）采集进曲库，并通过
只读 API `get_song_bilibili` 暴露。

本插件在 B站搜索前先查它，命中就跳过搜索。这条链路的四条纪律：

1. **只在整串关键词命中时短路**：用户说「普通DISCO 三无」时库里没有这个组合，
   自然回退搜索——不能把想听翻唱的人钉在原曲上；
2. **任何异常都回退**：插件未安装 / 未启用 / 老 Host 没有 api 代理 / 超时，
   一律静默走搜索，绝不打断点歌；
3. **`found=True` 但没标识也要回退**：曲库有歌、B站标识还没采集到，是常态；
4. **开关可关**：`bilibili.vcpedia_lookup=false` 时完全不发跨插件调用。
"""

from __future__ import annotations

import asyncio

import pytest

import plugin as plugin_module
from plugin import MusicRequestPlugin
from music_api import SongInfo
from url_parser import PLATFORM_BILI

_BILI_API = "org.mai-mai.cv-lyric-context.get_song_bilibili"


class _FakeApi:
    """模拟跨插件 api 代理，记录调用参数。"""

    def __init__(self, result=None, *, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    async def call(self, api_name: str, **kwargs):
        self.calls.append((api_name, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


def _prepare(*, vcpedia_lookup: bool = True):
    """装好 FakeHost 上下文 + 一个记录搜索调用的假搜索函数。"""
    from fakehost import FakeHost, bind_context, build_context, get_default_config

    plug = MusicRequestPlugin()
    host = FakeHost(plugin_id="github.cateye.music-request")
    ctx = build_context("github.cateye.music-request", rpc_call=host.rpc_call)
    config = get_default_config(plugin_module.MusicRequestConfig)
    config["bilibili"]["vcpedia_lookup"] = vcpedia_lookup
    bind_context(plug, ctx, config)

    searches: list[str] = []

    async def fake_search(keyword: str, platform: str):
        searches.append(keyword)
        return []

    plug._search = fake_search  # type: ignore[method-assign]
    return plug, ctx, searches


# ---------- 短路命中 ----------


def test_hit_with_aid_skips_search() -> None:
    """av 号命中：直接构造 iv 前缀的 song_id，且**完全不调用搜索**。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(
        {"schema_version": 1, "reason": "", "found": True, "name": "普通DISCO",
         "aid": 2129461, "bvid": ""}
    )
    songs = asyncio.run(plug._search_ranked("普通DISCO", PLATFORM_BILI))
    assert searches == [], "命中曲库时不应再走搜索（省一次风控暴露）"
    assert len(songs) == 1
    assert songs[0].song_id == "av2129461"
    assert songs[0].name == "普通DISCO"
    assert songs[0].platform == PLATFORM_BILI


def test_hit_with_bvid_prefers_bvid() -> None:
    """bvid 与 aid 同时存在时用 bvid（更稳定，不怕 av 号未来退役）。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(
        {"found": True, "name": "世末歌者", "aid": 13787072, "bvid": "BV1Qs411k7Qv"}
    )
    songs = asyncio.run(plug._search_ranked("世末歌者", PLATFORM_BILI))
    assert searches == []
    assert songs[0].song_id == "BV1Qs411k7Qv"


def test_api_called_with_expected_name() -> None:
    """调用名必须是 `<plugin_id>.<api_name>`，参数用 name=。"""
    plug, ctx, _ = _prepare()
    api = _FakeApi({"found": True, "name": "大氿歌", "aid": 45020985, "bvid": ""})
    ctx.api = api
    asyncio.run(plug._search_ranked("大氿歌", PLATFORM_BILI))
    assert api.calls == [(_BILI_API, {"name": "大氿歌"})]


# ---------- 回退搜索 ----------


def test_not_found_falls_back_to_search() -> None:
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"found": False, "reason": "曲库里没有这首歌", "name": "", "aid": None, "bvid": ""})
    asyncio.run(plug._search_ranked("不存在的歌", PLATFORM_BILI))
    assert searches == ["不存在的歌"]


def test_found_without_id_falls_back() -> None:
    """曲库有歌但 B站标识还没采集：必须回退搜索，不能当作失败。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(
        {"found": True, "reason": "该曲目尚未采集 B站标识", "name": "旧歌",
         "aid": None, "bvid": ""}
    )
    asyncio.run(plug._search_ranked("旧歌", PLATFORM_BILI))
    assert searches == ["旧歌"]


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("插件未安装或未启用"),   # Host 侧找不到提供方
        AttributeError("api"),              # 老 Host 没有 ctx.api 代理
        asyncio.TimeoutError(),              # 跨插件调用超时
        ValueError("响应格式不对"),
    ],
)
def test_any_exception_falls_back( exc: Exception) -> None:
    """任何异常都只能回退搜索——点歌主链路不能被跨插件调用拖死。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(exc=exc)
    songs = asyncio.run(plug._search_ranked("普通DISCO", PLATFORM_BILI))
    assert songs == []
    assert searches == ["普通DISCO"]


def test_garbage_result_falls_back() -> None:
    """Host 对未知能力返回占位结构（没有 found 字段）时也要回退。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"status": "ok", "data": {}})
    asyncio.run(plug._search_ranked("普通DISCO", PLATFORM_BILI))
    assert searches == ["普通DISCO"]


# ---------- 开关与作用域 ----------


def test_disabled_by_config_never_calls_api() -> None:
    plug, ctx, searches = _prepare(vcpedia_lookup=False)
    api = _FakeApi({"found": True, "name": "普通DISCO", "aid": 2129461, "bvid": ""})
    ctx.api = api
    asyncio.run(plug._search_ranked("普通DISCO", PLATFORM_BILI))
    assert api.calls == [], "关闭开关后不应发出任何跨插件调用"
    assert searches == ["普通DISCO"]


def test_other_platforms_unaffected() -> None:
    """VCPedia 只对 B站生效：网易云/QQ音乐路径一次都不该查。"""
    plug, ctx, searches = _prepare()
    api = _FakeApi({"found": True, "name": "普通DISCO", "aid": 2129461, "bvid": ""})
    ctx.api = api
    asyncio.run(plug._search_ranked("普通DISCO", "163"))
    assert api.calls == []
    assert searches == ["普通DISCO"]


def test_blank_keyword_skips_lookup() -> None:
    plug, ctx, searches = _prepare()
    api = _FakeApi({"found": True, "name": "x", "aid": 1, "bvid": ""})
    ctx.api = api
    asyncio.run(plug._search_ranked("   ", PLATFORM_BILI))
    assert api.calls == []


# ---------- 自然语言点歌（工具路径）也要优先原曲 ----------
#
# 产品决策（2026-10-22 用户确认）：**翻唱版算放错歌**。所以工具路径在平台链
# 之前就要查曲库——如果只在兜底位生效，用户听到的永远是网易云给的第一条。
# 边界同样明确：带歌手的多 token 查询走 prefix 命中，不算原曲（见 _vcpedia_pinned_song）。


def _install_fake_send(plug, *, ok: bool = True):
    """替换 _send_song，记录被发送的歌曲；不联网。"""
    sent = []

    async def fake_send(song, stream_id, *, silent=False, mode=None, outcome=None,
                        upload_timeout_seconds=None):
        sent.append(song)
        if outcome is not None:
            outcome["kind"] = "file"
        return ok

    plug._send_song = fake_send
    return sent


def _exact_hit() -> dict:
    return {
        "schema_version": 1, "reason": "", "found": True, "name": "普通DISCO",
        "aid": 2129461, "bvid": "", "match": "exact",
    }


def test_tool_prefers_vcpedia_over_platform_search() -> None:
    """曲库命中就直连原曲，连 163 都不搜。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(_exact_hit())
    sent = _install_fake_send(plug)
    out = asyncio.run(plug.search_and_play_music(query="普通DISCO", stream_id="s1"))
    assert searches == [], "命中曲库后不应再走平台链"
    assert len(sent) == 1
    assert sent[0].song_id == "av2129461"
    assert sent[0].platform == PLATFORM_BILI
    assert out["content"]


def test_tool_falls_back_to_platforms_when_send_fails() -> None:
    """原曲取流失败不能断死路，必须回退平台链。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(_exact_hit())
    _install_fake_send(plug, ok=False)
    asyncio.run(plug.search_and_play_music(query="普通DISCO", stream_id="s1"))
    assert searches, "VCPedia 发送失败后应回退平台搜索"


def test_tool_ignores_prefix_match() -> None:
    """「普通DISCO 三无」想听三无的翻唱版，不能靠剥 token 命中原曲。"""
    plug, ctx, searches = _prepare()
    hit = {**_exact_hit(), "match": "prefix"}
    ctx.api = _FakeApi(hit)
    sent = _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="普通DISCO 三无", stream_id="s1"))
    assert sent == [], "prefix 命中不该直接播原曲"
    assert searches, "应交给平台搜索（它的歌手校验能正确处理）"


def test_tool_skips_vcpedia_when_bilibili_disabled() -> None:
    plug, ctx, searches = _prepare()
    plug.config.bilibili.enabled = False
    api = _FakeApi(_exact_hit())
    ctx.api = api
    _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="普通DISCO", stream_id="s1"))
    assert api.calls == [], "B站未启用时不该发起 VCPedia 查询"
    assert searches


# ---------- 命令路径（/点歌 <歌名> 未指定平台）----------


def test_command_prefers_vcpedia_when_platform_omitted() -> None:
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(_exact_hit())
    sent = _install_fake_send(plug)
    ok, message = asyncio.run(plug._search_and_send("普通DISCO", "", "s1"))
    assert searches == []
    assert ok is True
    assert sent and sent[0].song_id == "av2129461"
    assert message


@pytest.mark.parametrize("platform", ["163", "qq"])
def test_command_respects_explicit_platform(platform: str) -> None:
    """显式 /点歌 163|qq 时尊重用户，不查曲库。"""
    plug, ctx, searches = _prepare()
    api = _FakeApi(_exact_hit())
    ctx.api = api
    _install_fake_send(plug)
    asyncio.run(plug._search_and_send("普通DISCO", platform, "s1"))
    assert api.calls == [], "显式指定平台时不该查 VCPedia"
    assert searches == ["普通DISCO"]


# ---------- 显式平台（v1.6.6）与歌手感知的 prefix（v1.6.6）----------
#
# 真机 2026-10-22 19:49 的完整事故链：
#   用户「用b站放世末积雨云」
#     → 工具没有 platform 参数，平台意图被整个丢掉
#     → Planner 只能把它塞进 query：query="世末积雨云 洛天依"
#     → VCPedia 的 prefix 层本可命中（原曲 av1579079），但被「带歌手就拒绝」误杀
#     → 网易云返回「世末积雨云(洛天依ACE Studio Cover)」翻唱版 → 播了
#
# 两个修复各自对应一个缺口，都要有用例。


def test_explicit_bili_restricts_platforms() -> None:
    """用户点名 B站时，只在 B站找，不跨平台。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"found": False, "reason": "", "name": "", "aid": None, "bvid": "", "match": ""})
    _install_fake_send(plug, ok=False)
    asyncio.run(
        plug.search_and_play_music(query="世末积雨云", platform="bili", stream_id="s1")
    )
    assert searches == ["世末积雨云"], f"只应在 B站搜一次: {searches}"


def test_explicit_163_skips_vcpedia() -> None:
    """显式 163 时完全不查曲库，尊重用户选择。"""
    plug, ctx, searches = _prepare()
    api = _FakeApi(_exact_hit())
    ctx.api = api
    _install_fake_send(plug, ok=False)
    asyncio.run(
        plug.search_and_play_music(query="普通DISCO", platform="163", stream_id="s1")
    )
    assert api.calls == []
    assert searches == ["普通DISCO"]


@pytest.mark.parametrize("alias", ["bili", "B站", "哔哩哔哩", "bilibili"])
def test_platform_alias_recognized(alias: str) -> None:
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"found": False, "reason": "", "name": "", "aid": None, "bvid": "", "match": ""})
    _install_fake_send(plug, ok=False)
    asyncio.run(plug.search_and_play_music(query="某歌", platform=alias, stream_id="s1"))
    assert searches == ["某歌"]


def test_unknown_platform_string_falls_back_to_chain() -> None:
    """无法识别的平台写法不能假装「用户指定了平台」（不能被兜底成 163）。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"found": False, "reason": "", "name": "", "aid": None, "bvid": "", "match": ""})
    _install_fake_send(plug, ok=False)
    asyncio.run(plug.search_and_play_music(query="某歌", platform="kugou", stream_id="s1"))
    assert len(searches) == 3, f"应走完整平台链: {searches}"


def test_explicit_bili_when_disabled_reports_clearly() -> None:
    plug, ctx, searches = _prepare()
    plug.config.bilibili.enabled = False
    _install_fake_send(plug)
    out = asyncio.run(
        plug.search_and_play_music(query="某歌", platform="bili", stream_id="s1")
    )
    assert searches == []
    assert "未启用" in out["content"]


# ---------- prefix + 原曲歌手 → 采纳 ----------


def _prefix_hit(singers: list[str]) -> dict:
    return {
        "schema_version": 1, "reason": "", "found": True,
        "name": "世末积雨云", "aid": 1579079, "bvid": "",
        "match": "prefix", "singers": singers,
    }


def test_prefix_with_original_singer_is_accepted() -> None:
    """「世末积雨云 洛天依」——洛天依是原曲歌手，用户要的就是原曲。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(_prefix_hit(["洛天依"]))
    sent = _install_fake_send(plug)
    asyncio.run(
        plug.search_and_play_music(query="世末积雨云 洛天依", stream_id="s1")
    )
    assert searches == [], "点名原曲歌手时不该再去平台搜"
    assert sent and sent[0].song_id == "av1579079"


def test_prefix_with_cover_singer_is_rejected() -> None:
    """「普通DISCO 三无」——三无不是原曲歌手，用户点名的是翻唱版。"""
    plug, ctx, searches = _prepare()
    hit = {**_prefix_hit(["洛天依", "言和"]), "name": "普通DISCO", "aid": 2129461}
    ctx.api = _FakeApi(hit)
    sent = _install_fake_send(plug)
    asyncio.run(
        plug.search_and_play_music(query="普通DISCO 三无", stream_id="s1")
    )
    assert sent == [], "点名翻唱者时不该播原曲"
    # 双 token 查询会触发「按歌名重搜」，所以搜索次数 >1，只检查首条
    assert searches and searches[0] == "普通DISCO 三无"


def test_prefix_without_singers_in_api_response_is_rejected() -> None:
    """上游没给 singers（旧版 cv_lyric_context）时保持保守：回退搜索。"""
    plug, ctx, searches = _prepare()
    hit = {"schema_version": 1, "reason": "", "found": True, "name": "普通DISCO",
           "aid": 2129461, "bvid": "", "match": "prefix"}
    ctx.api = _FakeApi(hit)
    sent = _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="普通DISCO 三无", stream_id="s1"))
    assert sent == []
    assert searches


# ---------- 查询串里的歌手是「模型补的」还是「用户说的」（v1.6.7）----------
#
# 真机 2026-10-22 21:48：用户「放一首世末歌者」（原话无歌手），Planner 传的
# query 却是「世末歌者 洛天依」。洛天依既不是原曲歌手（VCPedia 实测：乐正绫），
# 用户也没提——但一刀切否决后，三个平台都搜不到「世末歌者 洛天依」而全部失败。
#
# 判据：查 `trigger_text`（用户原话）里有没有那个 token。
#   不在 → 模型/上游注入补的噪声，不该否决原曲；
#   在   → 用户真的点名了某个非原曲歌手，尊重。


def _prefix_bv() -> dict:
    return {
        "schema_version": 1, "reason": "", "found": True, "name": "世末歌者",
        "aid": None, "bvid": "BV1Qs411k7Qv", "match": "prefix",
        "singers": ["乐正绫"],
    }


def test_prefix_noise_singer_not_in_user_text_is_accepted() -> None:
    """用户没说歌手、query 却被补了歌手 → 采纳原曲。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(_prefix_bv())
    sent = _install_fake_send(plug)
    asyncio.run(
        plug.search_and_play_music(
            query="世末歌者 洛天依", text="放一首世末歌者", stream_id="s1"
        )
    )
    assert searches == [], "不该再去平台搜这条带噪声的查询串"
    assert sent and sent[0].song_id == "BV1Qs411k7Qv"


def test_prefix_rejected_when_user_really_named_cover_singer() -> None:
    """用户原话里确实说了非原曲歌手 → 尊重，回退搜索。"""
    plug, ctx, searches = _prepare()
    hit = {
        "schema_version": 1, "reason": "", "found": True, "name": "普通DISCO",
        "aid": 2129461, "bvid": "", "match": "prefix", "singers": ["洛天依", "言和"],
    }
    ctx.api = _FakeApi(hit)
    sent = _install_fake_send(plug)
    asyncio.run(
        plug.search_and_play_music(
            query="普通DISCO 三无", text="放普通DISCO 三无", stream_id="s1"
        )
    )
    assert sent == [], "点名翻唱者时不该播原曲"
    assert searches


def test_prefix_noise_rejected_when_trigger_text_missing() -> None:
    """拿不到用户原话时保持保守：无法判断就回退搜索，不赌。"""
    plug, ctx, searches = _prepare()
    hit = {
        "schema_version": 1, "reason": "", "found": True, "name": "普通DISCO",
        "aid": 2129461, "bvid": "", "match": "prefix", "singers": ["洛天依", "言和"],
    }
    ctx.api = _FakeApi(hit)
    sent = _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="普通DISCO 三无", stream_id="s1"))
    assert sent == []
    assert searches


def test_failure_detail_lists_every_platform(caplog) -> None:
    """全部失败时明细要逐一列出每个平台的结局。

    真机 2026-10-22 21:48 的日志只有 `QQ音乐: 登录态失效` 一条，实际上 163 与
    B站 也都参与了并失败——只记「搜索抛异常」会让排查者误以为只试了一个平台。
    """
    import logging

    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"found": False, "reason": "", "name": "", "aid": None,
                        "bvid": "", "match": ""})
    _install_fake_send(plug, ok=False)
    with caplog.at_level(logging.WARNING, logger="plugin"):
        out = asyncio.run(plug.search_and_play_music(query="某歌", stream_id="s1"))
    text = caplog.text
    for name in ("网易云音乐", "QQ音乐", "B站"):
        assert name in text, f"明细缺少 {name}: {text}"
    assert "无法发送" in text or "无搜索结果" in text
    assert out["content"]


# ---------- platform 参数的可信度校验（v1.6.8）----------
#
# 真机 2026-10-22 22:14：用户只说「放一首世末告白」，Planner 却传了
# platform='qq'。那一步按设计绕过了 VCPedia 优先，于是被绑死在 QQ 一个平台，
# 而 QQ 登录态失效 → 全链路失败。可曲库里《世末告白》是有原曲标识的
# （BV1qonXz2EtU），这条路径本来能成功。
#
# 判据同 prefix：有用户原话就比对原话，没原话才信任显式参数。


def _shimo_gaobai() -> dict:
    return {
        "schema_version": 1, "reason": "", "found": True, "name": "世末告白",
        "aid": None, "bvid": "BV1qonXz2EtU", "match": "exact", "singers": ["洛天依"],
    }


def test_hallucinated_platform_is_ignored() -> None:
    """原话没提平台，LLM 自填的 platform 必须被忽略，回到正常优先链路。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(_shimo_gaobai())
    sent = _install_fake_send(plug)
    asyncio.run(
        plug.search_and_play_music(
            query="世末告白 洛天依", platform="qq", text="放一首世末告白", stream_id="s1"
        )
    )
    assert searches == [], "忽略假平台后应走 VCPedia 优先"
    assert sent and sent[0].song_id == "BV1qonXz2EtU"


@pytest.mark.parametrize(
    ("text", "platform"),
    [
        ("用B站放世末告白", "bili"),
        ("去网易云搜世末告白", "163"),
        ("用QQ音乐放世末告白", "qq"),
        ("bilibili 上放世末告白", "bili"),
    ],
)
def test_platform_respected_when_user_mentioned_it(text: str, platform: str) -> None:
    """用户原话确实提到平台时，参数生效（不查曲库、只走该平台）。"""
    plug, ctx, searches = _prepare()
    api = _FakeApi({"found": False, "reason": "", "name": "", "aid": None,
                    "bvid": "", "match": ""})
    ctx.api = api
    _install_fake_send(plug, ok=False)
    asyncio.run(
        plug.search_and_play_music(
            query="世末告白", platform=platform, text=text, stream_id="s1"
        )
    )
    assert searches == ["世末告白"], f"应只走指定平台: {searches}"


def test_platform_trusted_when_no_trigger_text() -> None:
    """非聊天触发（拿不到原话）时信任显式参数——那是唯一的信息来源。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"found": False, "reason": "", "name": "", "aid": None,
                        "bvid": "", "match": ""})
    _install_fake_send(plug, ok=False)
    asyncio.run(
        plug.search_and_play_music(query="世末告白", platform="qq", stream_id="s1")
    )
    assert searches == ["世末告白"]


def test_platform_ignored_when_user_mentioned_another() -> None:
    """用户说「用B站」但 LLM 传了 qq：以用户原话为准，忽略错误参数。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"found": False, "reason": "", "name": "", "aid": None,
                        "bvid": "", "match": ""})
    _install_fake_send(plug, ok=False)
    asyncio.run(
        plug.search_and_play_music(
            query="世末告白", platform="qq", text="用B站放世末告白", stream_id="s1"
        )
    )
    # 忽略 qq 后回到完整平台链（3 个平台），而不是只搜 QQ
    assert len(searches) >= 2, f"不该被绑死在 QQ: {searches}"


# ---------- 工具载荷里没有原话，必须回退到入站缓存（v1.7.0）----------
#
# 真机 2026-10-22 22:27 暴露的根因：`search_and_play_music` 的工具载荷只有
# query / send_as / platform / stream_id，**没有用户消息**。于是
# `trigger_text` 恒为空，前两轮加的原话校验（platform 与 prefix 歌手）全部
# 静默失效——日志里只有 `工具入参指定平台: 'qq' -> qq`，却没有我加的
# 「用户原话未提及平台，忽略该参数」那一行。
#
# 修法：`chat.receive.after_process` 钩子里按会话登记原话，工具路径拿不到就回退。


def _chunyou() -> dict:
    return {
        "schema_version": 1, "reason": "", "found": True, "name": "春游",
        "aid": 26553386, "bvid": "", "match": "exact", "singers": ["洛天依"],
    }


def test_platform_guard_uses_inbound_cache() -> None:
    """工具载荷无原话时，从入站缓存取，platform 校验照样生效。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(_chunyou())
    sent = _install_fake_send(plug)
    plug._inbound_text.remember("s1", "放一首春游")
    # 关键：**不传 text**，模拟真实工具调用载荷
    asyncio.run(
        plug.search_and_play_music(query="春游 洛天依", platform="qq", stream_id="s1")
    )
    assert searches == [], "应忽略 QQ 并走 VCPedia 优先"
    assert sent and sent[0].name == "春游"


def test_inbound_cache_is_per_session() -> None:
    """缓存按会话隔离：别的会话的原话不能拿来校验本会话。"""
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"found": False, "reason": "", "name": "", "aid": None,
                        "bvid": "", "match": ""})
    _install_fake_send(plug, ok=False)
    plug._inbound_text.remember("other", "用B站放世末告白")
    asyncio.run(
        plug.search_and_play_music(query="世末告白", platform="qq", stream_id="s1")
    )
    # 拿不到本会话原话 → 信任显式参数 → 只搜 qq
    assert searches == ["世末告白"]


def test_recent_inbound_text_ttl() -> None:
    cache = plugin_module._RecentInboundText(ttl=0.01)
    cache.remember("s1", "放一首春游")
    assert cache.get("s1") == "放一首春游"
    import time as _time

    _time.sleep(0.03)
    assert cache.get("s1") == "", "过期后应失效"


def test_recent_inbound_text_ignores_blank() -> None:
    cache = plugin_module._RecentInboundText()
    cache.remember("s1", "   ")
    assert cache.get("s1") == ""
    cache.remember("", "放一首春游")
    assert cache.get("") == ""


def test_inbound_cache_overwrites_with_latest() -> None:
    """每个会话只保留最近一条：点歌总是紧跟触发它的消息。"""
    cache = plugin_module._RecentInboundText()
    cache.remember("s1", "放一首春游")
    cache.remember("s1", "放一首狼狈")
    assert cache.get("s1") == "放一首狼狈"


# ---------- 直连时长闸（v1.7.1）----------
#
# 曲库 `duration` 字段实测 100% 为空，`bilibili.max_duration_seconds` 在直连
# 路径上完全失效。实测数据：音频 ≈38.5KB/s、上传 ≈318KB/s ⇒ 50 秒预算约覆盖
# 413 秒；「洛天依带你铁道唱歌」系列单P 8.3 分钟（约 19MB）必然传不完。
#
# 决策（用户 2026-10-22 确认）：**超时长就回退平台搜索**，不硬传；
# 预算从 45s 提到 50s（不能再高，会给 Host 的 60s RPC 预算留不出回落时间）。


def _install_fake_duration(plug, seconds):
    """替换 _probe_bili_duration 的返回值。"""

    async def fake(_video_id):
        return seconds

    plug._probe_bili_duration = fake


def test_long_video_is_not_played_directly() -> None:
    """时长超过直连上限 → 不直连，回去搜平台（避免白等一次上传超时）。"""
    plug, ctx, searches = _prepare()
    hit = {
        "schema_version": 1, "reason": "", "found": True, "name": "铁道唱歌",
        "aid": None, "bvid": "BV1xx411c7mD", "match": "exact", "singers": ["洛天依"],
    }
    ctx.api = _FakeApi(hit)
    _install_fake_duration(plug, 500)  # 8.3 分钟
    sent = _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="铁道唱歌", stream_id="s1"))
    assert sent == [], "超时长的歌不该直连"
    assert searches, "应回退平台搜索"


def test_short_video_plays_directly() -> None:
    """常规曲目（3-5 分钟）照常直连。"""
    plug, ctx, searches = _prepare()
    hit = {
        "schema_version": 1, "reason": "", "found": True, "name": "创作",
        "aid": None, "bvid": "BV1dG4y1o7Ji", "match": "exact", "singers": ["洛天依"],
    }
    ctx.api = _FakeApi(hit)
    _install_fake_duration(plug, 231)
    sent = _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="创作", stream_id="s1"))
    assert searches == []
    assert sent and sent[0].song_id == "BV1dG4y1o7Ji"


def test_unknown_duration_still_plays() -> None:
    """探测失败（None）时放行——不能因为时长接口抽风就把能播的歌挡掉。"""
    plug, ctx, searches = _prepare()
    hit = {
        "schema_version": 1, "reason": "", "found": True, "name": "创作",
        "aid": None, "bvid": "BV1dG4y1o7Ji", "match": "exact", "singers": ["洛天依"],
    }
    ctx.api = _FakeApi(hit)
    _install_fake_duration(plug, None)
    sent = _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="创作", stream_id="s1"))
    assert sent, "拿不到时长时应照常直连"


def test_budget_and_gate_constants_consistent() -> None:
    """预算与时长闸必须自洽：预算 50s 只能覆盖约 413s，闸设 420s 刚好。"""
    assert plugin_module._TOOL_NAPCAT_UPLOAD_BUDGET == 50
    assert plugin_module._VCPEDIA_MAX_DIRECT_SECONDS == 420
    # 预算必须给 Host 的 60s RPC 留出回落时间
    assert plugin_module._TOOL_NAPCAT_UPLOAD_BUDGET < 60


# ---------- 直连时长闸（v1.7.1）----------
#
# 曲库 `duration` 字段实测 100% 为空，`bilibili.max_duration_seconds` 在直连
# 路径上完全失效。实测数据：音频 ≈38.5KB/s、上传 ≈318KB/s ⇒ 50 秒预算约覆盖
# 413 秒；「洛天依带你铁道唱歌」系列单P 8.3 分钟（约 19MB）必然传不完。
#
# 决策（用户 2026-10-22 确认）：**超时长就回退平台搜索**，不硬传；
# 预算从 45s 提到 50s（不能再高，会给 Host 的 60s RPC 预算留不出回落时间）。


def _install_fake_duration(plug, seconds):
    """替换 _probe_bili_duration 的返回值。"""

    async def fake(_video_id):
        return seconds

    plug._probe_bili_duration = fake


def test_long_video_is_not_played_directly() -> None:
    """时长超过直连上限 → 不直连，回去搜平台（避免白等一次上传超时）。"""
    plug, ctx, searches = _prepare()
    hit = {
        "schema_version": 1, "reason": "", "found": True, "name": "铁道唱歌",
        "aid": None, "bvid": "BV1xx411c7mD", "match": "exact", "singers": ["洛天依"],
    }
    ctx.api = _FakeApi(hit)
    _install_fake_duration(plug, 500)  # 8.3 分钟
    sent = _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="铁道唱歌", stream_id="s1"))
    assert sent == [], "超时长的歌不该直连"
    assert searches, "应回退平台搜索"


def test_short_video_plays_directly() -> None:
    """常规曲目（3-5 分钟）照常直连。"""
    plug, ctx, searches = _prepare()
    hit = {
        "schema_version": 1, "reason": "", "found": True, "name": "创作",
        "aid": None, "bvid": "BV1dG4y1o7Ji", "match": "exact", "singers": ["洛天依"],
    }
    ctx.api = _FakeApi(hit)
    _install_fake_duration(plug, 231)
    sent = _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="创作", stream_id="s1"))
    assert searches == []
    assert sent and sent[0].song_id == "BV1dG4y1o7Ji"


def test_unknown_duration_still_plays() -> None:
    """探测失败（None）时放行——不能因为时长接口抽风就把能播的歌挡掉。"""
    plug, ctx, searches = _prepare()
    hit = {
        "schema_version": 1, "reason": "", "found": True, "name": "创作",
        "aid": None, "bvid": "BV1dG4y1o7Ji", "match": "exact", "singers": ["洛天依"],
    }
    ctx.api = _FakeApi(hit)
    _install_fake_duration(plug, None)
    sent = _install_fake_send(plug)
    asyncio.run(plug.search_and_play_music(query="创作", stream_id="s1"))
    assert sent, "拿不到时长时应照常直连"


def test_budget_and_gate_constants_consistent() -> None:
    """预算与时长闸必须自洽：预算 50s 只能覆盖约 413s，闸设 420s 刚好。"""
    assert plugin_module._TOOL_NAPCAT_UPLOAD_BUDGET == 50
    assert plugin_module._VCPEDIA_MAX_DIRECT_SECONDS == 420
    # 预算必须给 Host 的 60s RPC 留出回落时间
    assert plugin_module._TOOL_NAPCAT_UPLOAD_BUDGET < 60


# ---------- 上传槽位有等待上限（v1.7.2）----------
#
# 前提核查结论：`_send_file` 里的 `async with guard:`（`_cache_guard`）**已经**
# 串行化了「取直链 → 下载 → 上传」整个流程，所以并发上传本来就不会同时发生。
# 真正缺的是**等待上限**：前一个上传用满 50s，后来者再死等 50s 才轮到，
# 叠加必然超 Host 的 60s RPC 预算，触发 2026-09-26 那种僵尸调用。
# 所以这次补的不是锁，而是「等不到就让路」。


def test_upload_slot_free_is_acquired_immediately() -> None:
    plug, ctx, _ = _prepare()
    guard = plug._run_lock("_cache_guard")

    async def scenario():
        ok = await plug._acquire_upload_slot(guard, 50.0)
        if ok:
            guard.release()
        return ok

    assert asyncio.run(scenario()) is True
    assert guard.locked() is False, "获取后必须能正常释放"


def test_upload_slot_timeout_gives_way() -> None:
    """槽位被占时超时放弃（不无限等）。等待上限最小 3 秒。"""
    plug, ctx, _ = _prepare()
    guard = plug._run_lock("_cache_guard")

    async def scenario():
        await guard.acquire()
        try:
            return await plug._acquire_upload_slot(guard, 1.0)
        finally:
            guard.release()

    assert asyncio.run(scenario()) is False


def test_send_file_returns_false_when_slot_unavailable() -> None:
    """拿不到槽位时 _send_file 快速返回 False（不降级、不发消息）。"""
    plug, ctx, _ = _prepare()

    async def no_slot(*_args, **_kwargs):
        return False

    plug._acquire_upload_slot = no_slot
    song = SongInfo(
        song_id="BV1dG4y1o7Ji", name="创作", platform=PLATFORM_BILI
    )
    assert asyncio.run(plug._send_song(song, "s1", silent=True)) is False


def test_upload_slot_wait_is_capped() -> None:
    """等待上限必须被夹在 [3, 15] 秒——太小会让正常排队白失败，太大就超预算。"""
    plug, ctx, _ = _prepare()
    guard = plug._run_lock("_cache_guard")

    async def probe(budget):
        await guard.acquire()
        import time as _t

        started = _t.monotonic()
        try:
            ok = await plug._acquire_upload_slot(guard, budget)
        finally:
            guard.release()
        return ok, _t.monotonic() - started

    ok, elapsed = asyncio.run(probe(1.0))
    assert ok is False
    # 预算 1s × 0.3 = 0.3s → 被抬到下限 3s
    assert 2.9 <= elapsed <= 4.5, f"实际等待 {elapsed:.1f}s"
