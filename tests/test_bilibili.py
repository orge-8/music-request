"""B站平台（bilibili_api + plugin 集成）的回归测试。

这组用例锚定 v1.6.0 新增的 B站视频音频能力，三条测试铁律都适用：

1. WBI 签名向量：混淆表与算法以独立实现（timerring/bilitool wbi_sign.py）
   交叉核对过——测试里内置一份**独立抄录**的混淆表与手工 md5 推导，
   实现改坏（表改错、排序丢了、特殊字符没剔除）任何一处都会 FAIL。
2. CDN 白名单是 SSRF 闸门：必须拒绝伪装域名（`bilivideo.com.evil.com`）、
   字面量内网 IP 与公网 IP；反向验证 = 放宽后缀匹配用例必须红。
3. B站固定 file 形态 + 文本降级：请求 voice/card 也会被覆盖成 file，
   NapCat 不可用时绝不静默换成卡片（构不了平台卡片）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import sys
from urllib.parse import urlencode

import httpx
import pytest

from bilibili_api import (
    BilibiliClient,
    audio_stream_from_playurl_payload,
    av2bv,
    bv2av,
    cid_from_pagelist,
    cid_from_view,
    clean_search_title,
    get_mixin_key,
    is_bili_audio_url,
    is_valid_bvid,
    normalize_video_id,
    parse_duration_seconds,
    resolve_video_bvid,
    sign_wbi,
    songs_from_search_payload,
)
from music_api import MusicAPIResponseError, MusicSearchClient, SongInfo
from url_parser import PLATFORM_BILI

import plugin as plugin_module


# ---------- WBI 签名 ----------

# 测试内**独立抄录**的混淆表（来源与实现不同：按官方前端 JS 逐行抄录），
# 实现里的表被改坏时这里必须 FAIL
_TEST_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_IMG_KEY = "7cd084941338484aae1ad942dcab20b1"
_SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"


def _expected_mixin_key() -> str:
    full = _IMG_KEY + _SUB_KEY
    return "".join(full[i] for i in _TEST_TAB)[:32]


def test_mixin_key_matches_independent_table() -> None:
    """混淆表重排必须与独立抄录的表一致。"""
    assert get_mixin_key(_IMG_KEY, _SUB_KEY) == _expected_mixin_key()
    assert len(get_mixin_key(_IMG_KEY, _SUB_KEY)) == 32


def test_sign_wbi_deterministic_vector() -> None:
    """固定 wts 下签名可独立推导：md5(urlencode(排序参数) + mixin_key)。"""
    signed = sign_wbi(
        {"keyword": "洛天依", "page": 1},
        _IMG_KEY, _SUB_KEY, wts=1702204800,
    )
    assert signed["wts"] == "1702204800"
    # 用测试自己的实现推导期望值（不经 sign_wbi 的代码路径）
    items = sorted(
        [("keyword", "洛天依"), ("page", "1"), ("wts", "1702204800")],
    )
    query = urlencode(items)
    expected = hashlib.md5((query + _expected_mixin_key()).encode()).hexdigest()
    assert signed["w_rid"] == expected


def test_sign_wbi_strips_special_chars_and_sorts() -> None:
    """value 里的 !'()* 只从**签名 query** 里剔除；返回参数保留原值（官方行为）。"""
    a = sign_wbi({"z": "one", "a": "two!"}, _IMG_KEY, _SUB_KEY, wts=1)
    b = sign_wbi({"a": "two", "z": "one"}, _IMG_KEY, _SUB_KEY, wts=1)
    assert a["w_rid"] == b["w_rid"], "剔除特殊字符后两者应同签名"
    c = sign_wbi({"a": "two", "z": "one", "wts": "1"}, _IMG_KEY, _SUB_KEY, wts=1)
    assert a["w_rid"] == c["w_rid"], "wts 覆盖与合并顺序无关"
    # 官方前端行为：签名 query 用剔除后的值，但随请求发出的参数保留原值
    assert a["a"] == "two!"
    # 签名确实基于剔除后的 query：手工重算对比
    stripped_query = urlencode(sorted([("a", "two"), ("z", "one"), ("wts", "1")]))
    expected = hashlib.md5((stripped_query + _expected_mixin_key()).encode()).hexdigest()
    assert a["w_rid"] == expected


def test_sign_wbi_does_not_mutate_input() -> None:
    params = {"keyword": "x"}
    sign_wbi(params, _IMG_KEY, _SUB_KEY, wts=5)
    assert params == {"keyword": "x"}, "实现不得改动调用方的参数 dict"


# ---------- 时长解析 / 标题清洗 / BV 号 ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("05:30", 330),
        ("1:02:03", 3723),
        ("45", 45),
        ("330", 330),
        ("", 0),
        ("abc", 0),
        ("1:2:3:4", 0),
        (None, 0),
    ],
)
def test_parse_duration_seconds(raw, expected: int) -> None:
    assert parse_duration_seconds(raw) == expected


def test_clean_search_title_strips_em_tags() -> None:
    assert clean_search_title('<em class="keyword">洛天依</em> 普通DISCO') == "洛天依 普通DISCO"
    assert clean_search_title("无标签标题") == "无标签标题"
    assert clean_search_title("") == ""


@pytest.mark.parametrize(
    ("bvid", "expected"),
    [
        ("BV1xx411c7mD", True),
        ("BV1GJ411x7h7", True),
        ("BV1", False),
        ("av170001", False),
        ("", False),
        ("BV1xx411c7mDextra", False),
    ],
)
def test_is_valid_bvid(bvid: str, expected: bool) -> None:
    assert is_valid_bvid(bvid) is expected


# ---------- CDN 白名单（SSRF 闸门） ----------


@pytest.mark.parametrize(
    "url",
    [
        "https://upos-sz-mirror08c.bilivideo.com/xxx/30280.m4s?e=1",
        "https://upos-sz-mirrorakam.akamaized.net/xxx.m4s",
        "https://www.bilivideo.com/a.m4s",
        "https://xy.bilivideo.cn/a.m4s",
    ],
)
def test_cdn_whitelist_accepts_known_hosts(url: str) -> None:
    assert is_bili_audio_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/x.m4s",
        "https://bilivideo.com.evil.com/x.m4s",  # 后缀伪装
        "http://127.0.0.1/x.m4s",
        "http://169.254.169.254/latest/meta-data",
        "http://192.168.1.10/x.m4s",
        "https://93.184.216.34/x.m4s",  # 字面量公网 IP 也拒绝（CDN 不会是 IP）
        "",
        "not-a-url",
    ],
)
def test_cdn_whitelist_rejects_evil_and_private(url: str) -> None:
    assert is_bili_audio_url(url) is False


# ---------- 搜索 payload 解析 ----------


def _search_item(bvid: str, title: str, author: str, duration: str, play: int) -> dict:
    return {
        "type": "video",
        "bvid": bvid,
        "title": title,
        "author": author,
        "duration": duration,
        "play": play,
    }


def test_search_payload_parses_and_presorts_by_play() -> None:
    payload = {
        "code": 0,
        "data": {
            "result": [
                _search_item("BV1aaaaaaaaa", "《歌A》翻唱", "路人UP", "05:00", 100),
                _search_item("BV1bbbbbbbbb", "歌A 原曲", "官方号", "04:30", 90000),
                {"type": "live_room", "title": "直播中"},  # 非视频条目要跳过
                _search_item("BAD_ID", "坏数据", "x", "03:00", 500),  # 非法 bvid 要跳过
            ]
        },
    }
    songs = songs_from_search_payload(payload, limit=5, max_duration_seconds=0)
    assert [s.song_id for s in songs] == ["BV1bbbbbbbbb", "BV1aaaaaaaaa"], "应按播放量降序"
    top = songs[0]
    assert top.platform == PLATFORM_BILI
    assert top.name == "歌A 原曲" and top.artists == "官方号"


def test_search_payload_filters_overlong_duration() -> None:
    payload = {
        "code": 0,
        "data": {
            "result": [
                _search_item("BV1aaaaaaaaa", "短的", "a", "05:00", 10),
                _search_item("BV1bbbbbbbbb", "超长的", "b", "31:00", 90000),
            ]
        },
    }
    songs = songs_from_search_payload(payload, limit=10, max_duration_seconds=600)
    assert [s.song_id for s in songs] == ["BV1aaaaaaaaa"], "超过时长上限的候选必须被剔除"


def test_search_payload_empty_result_is_not_protocol_error() -> None:
    assert songs_from_search_payload({"code": 0, "data": {}}, limit=5) == []
    assert songs_from_search_payload({"code": 0, "data": {"result": []}}, limit=5) == []


def test_search_payload_bad_structure_raises() -> None:
    with pytest.raises(MusicAPIResponseError):
        songs_from_search_payload({"code": 0}, limit=5)  # 缺 data
    with pytest.raises(MusicAPIResponseError):
        songs_from_search_payload({"code": 0, "data": {"result": "oops"}}, limit=5)


# ---------- playurl 音频轨挑选 ----------


def _stream(aid: int, bandwidth: int, host: str = "upos-sz-mirror08c.bilivideo.com") -> dict:
    return {"id": aid, "bandwidth": bandwidth, "baseUrl": f"https://{host}/x-{aid}.m4s"}


def test_pick_best_audio_by_bandwidth() -> None:
    payload = {
        "code": 0,
        "data": {
            "dash": {
                "audio": [
                    _stream(30280, 198000),
                    _stream(30216, 66000),
                    _stream(30232, 132000),
                ],
                "video": [{"id": 80}],
            }
        },
    }
    url = audio_stream_from_playurl_payload(payload)
    assert url is not None and "30280" in url, "必须取带宽最高的音频轨"


def test_pick_audio_rejects_non_whitelist_host() -> None:
    payload = {
        "code": 0,
        "data": {
            "dash": {
                "audio": [
                    _stream(30280, 198000, host="evil.com"),
                    _stream(30216, 66000),
                ]
            }
        },
    }
    url = audio_stream_from_playurl_payload(payload)
    assert url is not None and "30216" in url, "白名单外的直链要被剔除、退而取次优"


def test_pick_audio_none_cases() -> None:
    assert audio_stream_from_playurl_payload(None) is None
    assert audio_stream_from_playurl_payload({"code": 0}) is None
    assert audio_stream_from_playurl_payload({"code": 0, "data": {"dash": {}}}) is None
    # dash 缺 audio（部分旧视频走 durl 形态）→ None，由上层记日志
    assert audio_stream_from_playurl_payload(
        {"code": 0, "data": {"dash": {"video": []}}}
    ) is None
    # 所有直链都不在白名单 → None
    assert audio_stream_from_playurl_payload(
        {"code": 0, "data": {"dash": {"audio": [_stream(30280, 1, host="evil.com")]}}}
    ) is None


# ---------- Cookie 域限定与未启用行为 ----------


def test_sessdata_cookie_domain_bound() -> None:
    """SESSDATA 必须带 .bilibili.com 域限定（无 domain cookie 会发往所有请求）。"""
    client = BilibiliClient(sessdata="test-sess", buvid3="test-b3")
    jar = client._client.cookies.jar
    by_name = {c.name: c for c in jar}
    assert by_name["SESSDATA"].domain == ".bilibili.com"
    assert by_name["buvid3"].domain == ".bilibili.com"


def test_music_client_bili_disabled_raises_on_search() -> None:
    api = MusicSearchClient(bili_enabled=False)
    with pytest.raises(MusicAPIResponseError, match="未启用"):
        asyncio.run(api.search("x", PLATFORM_BILI))
    with pytest.raises(MusicAPIResponseError, match="未启用"):
        asyncio.run(api.get_song_url("BV1xxxxxxxxxx", PLATFORM_BILI))


def test_music_client_bili_enabled_builds_subclient() -> None:
    api = MusicSearchClient(bili_enabled=True, bili_sessdata="abc")
    assert api.bili_available is True
    assert api._bili.sessdata_configured is True
    asyncio.run(api.close())


# ---------- 插件层：强制 file 形态与文本降级 ----------


class _ScenarioAPI:
    """搜索/直链/上传替身（不联网）。"""

    def __init__(self, songs, *, upload_ok: bool = True) -> None:
        self._songs = list(songs)
        self.upload_ok = upload_ok
        self.searches: list[str] = []
        self.uploads: list[dict] = []
        self.timeout_used: float | None = None

    async def search(self, query, platform, limit=5):
        self.searches.append(f"{platform}:{query}")
        return list(self._songs)

    async def get_song_url(self, song_id, platform, media_id="", *, mp3_only=False):
        return "https://upos-sz-mirror08c.bilivideo.com/x-30280.m4s?e=1"

    async def get_qq_song_detail(self, song_mid):
        return None

    async def napcat_upload_file(self, file_path, *, name="", group_id="", user_id="",
                                 timeout_seconds=None):
        self.uploads.append({"file_path": file_path, "name": name,
                             "group_id": group_id, "user_id": user_id})
        self.timeout_used = timeout_seconds
        return self.upload_ok, {"status": "ok"}

    async def close(self):
        return None


class _FakeCache:
    def __init__(self, suffix: str = ".m4a") -> None:
        self._suffix = suffix
        self.calls: list[dict] = []

    async def get_or_download(self, platform, song_id, url, suffix=""):
        self.calls.append({"platform": platform, "song_id": song_id, "url": url, "suffix": suffix})
        return pathlib.Path(f"C://tmp//cache//{platform}_{song_id}{self._suffix}")

    def retain(self, path):
        return None

    def release(self, path):
        return None

    def napcat_path_checked(self, path):
        return (f"/app/music_cache/{path.name}", True)


def _prepare_plugin(*, play_mode: str = "card"):
    """装好 FakeHost 上下文的插件实例（B站默认启用）。"""
    from fakehost import FakeHost, bind_context, build_context, get_default_config

    plugin = plugin_module.MusicRequestPlugin()
    host = FakeHost(plugin_id="github.orge-8.music-request")
    ctx = build_context("github.orge-8.music-request", rpc_call=host.rpc_call)
    config = get_default_config(plugin_module.MusicRequestConfig)
    config["music"]["play_mode"] = play_mode
    bind_context(plugin, ctx, config)
    return plugin, host


def test_bili_song_forces_file_mode() -> None:
    """配置 play_mode=card 时，B站歌曲也必须强制走文件上传。"""
    plugin, host = _prepare_plugin(play_mode="card")
    song = SongInfo(song_id="BV1xxxxxxxxxx", name="普通DISCO", artists="ILEmotions",
                    platform=PLATFORM_BILI)
    api = _ScenarioAPI([song])
    plugin._api = api
    plugin._cache = _FakeCache()
    plugin._qq_targets["s1"] = {"group_id": "12345"}

    outcome: dict[str, str] = {}
    sent = asyncio.run(plugin._send_song(song, "s1", outcome=outcome))

    assert sent is True
    assert outcome.get("kind") == "file"
    assert api.uploads and api.uploads[0]["name"].endswith(".m4a"), api.uploads
    cards = host.calls_of("send.custom")
    assert not cards, "B站不该发任何卡片"


def test_bili_download_uses_m4a_suffix() -> None:
    """B站直链以 .m4s 结尾，落盘后缀必须显式指定为 .m4a（URL 推断会拿 .mp3）。"""
    plugin, _host = _prepare_plugin()
    song = SongInfo(song_id="BV1xxxxxxxxxx", name="普通DISCO", artists="ILEmotions",
                    platform=PLATFORM_BILI)
    api = _ScenarioAPI([song])
    cache = _FakeCache()
    plugin._api = api
    plugin._cache = cache
    plugin._qq_targets["s1"] = {"group_id": "12345"}

    asyncio.run(plugin._send_song(song, "s1"))

    assert cache.calls and cache.calls[0]["suffix"] == ".m4a", cache.calls
    assert cache.calls[0]["platform"] == PLATFORM_BILI


def test_bili_upload_timeout_budget_in_tool_path() -> None:
    """工具路径的 B站文件上传必须收到 45s 超时预算（Host RPC 60s 之内）。"""
    plugin, _host = _prepare_plugin()
    song = SongInfo(song_id="BV1xxxxxxxxxx", name="普通DISCO", artists="ILEmotions",
                    platform=PLATFORM_BILI)
    api = _ScenarioAPI([song])
    plugin._api = api
    plugin._cache = _FakeCache()
    plugin._qq_targets["s1"] = {"group_id": "12345"}

    outcome: dict[str, str] = {}
    asyncio.run(plugin._send_song(
        song, "s1", silent=True, outcome=outcome,
        upload_timeout_seconds=plugin_module._TOOL_NAPCAT_UPLOAD_BUDGET,
    ))
    assert api.timeout_used == plugin_module._TOOL_NAPCAT_UPLOAD_BUDGET


def test_bili_degrades_to_text_not_card() -> None:
    """NapCat 不可用时 B站必须发文本提示并报失败，绝不降级成卡片。"""
    plugin, host = _prepare_plugin()
    song = SongInfo(song_id="BV1xxxxxxxxxx", name="普通DISCO", artists="ILEmotions",
                    platform=PLATFORM_BILI)
    api = _ScenarioAPI([song])
    plugin._api = api
    plugin._cache = _FakeCache()
    # 不放 _qq_targets：_resolve_qq_target 走反查也拿不到 → target=None

    outcome: dict[str, str] = {}
    sent = asyncio.run(plugin._send_song(song, "s1", outcome=outcome))

    assert sent is False
    assert outcome.get("kind") == "text_degraded", outcome
    assert outcome.get("reason"), "文本降级必须带原因"
    assert not host.calls_of("send.custom"), "B站不得发卡片"
    assert any("暂时发不了音频文件" in (t or "") for t in host.sent_texts), host.sent_texts


def test_describe_outcome_text_degraded() -> None:
    """text_degraded 的工具文案必须如实说明「没有发送」，禁止谎报。"""
    describe = plugin_module.MusicRequestPlugin._describe_outcome
    song = SongInfo(song_id="BV1xxxxxxxxxx", name="普通DISCO", artists="ILEmotions",
                    platform=PLATFORM_BILI)
    text = describe(song, "file", {"kind": "text_degraded", "reason": "NapCat 上传失败"})
    assert "NapCat 上传失败" in text
    assert "没有发送" in text or "未发送" in text or "没有发送".replace("没", "未") in text
    assert "已发送文件" not in text


def test_bili_platform_resolution_aliases() -> None:
    plugin, _host = _prepare_plugin()
    for alias in ("bili", "bilibili", "B站", "b站", "哔哩哔哩"):
        assert plugin._resolve_platform(alias) == PLATFORM_BILI, alias
    assert plugin._platform_name(PLATFORM_BILI) == "B站"


def test_bili_command_regex_matches() -> None:
    """/点歌 bili <query> 的平台分组必须能被命令正则捕获。"""
    import re

    info = getattr(plugin_module.MusicRequestPlugin.cmd_点歌, "__maibot_component_info__")
    pattern = info.command_pattern
    m = re.search(pattern, "/点歌 bili 普通DISCO")
    assert m is not None, "/点歌 bili 应命中"
    assert m.group("platform") == "bili" and m.group("query") == "普通DISCO"
    m2 = re.search(pattern, "/点歌 B站 普通DISCO")
    assert m2 is not None and m2.group("platform") == "B站"
    # 反向：163/qq 不受影响
    m3 = re.search(pattern, "/点歌 qq 稻香")
    assert m3 is not None and m3.group("platform") == "qq"


def test_tool_fallback_chain_appends_bili_last(caplog) -> None:
    """启用 B站时，工具的换平台顺序必须是 163 → qq → bili（B站垫底）。"""
    import logging

    plugin, host = _prepare_plugin()
    api = _ScenarioAPI([])
    plugin._api = api
    # 库未启用 → 直接平台链

    with caplog.at_level(logging.WARNING, logger="plugin"):
        result = asyncio.run(
            plugin.search_and_play_music(query="不存在的歌xyz", stream_id="s1")
        )
    # 顺序看实际搜索调用——这才是「B站垫底」的定义
    assert api.searches == [
        "163:不存在的歌xyz", "qq:不存在的歌xyz", "bili:不存在的歌xyz"
    ], api.searches
    # 明细留在日志里（返回给模型的文案刻意不再罗列平台名，
    # 否则会把平台名带进下一轮上下文，诱导模型主动填 platform）
    assert "B站" in caplog.text, caplog.text
    assert "B站" not in result["content"], "文案不该罗列平台名"
    assert "均未取到可播放音频" in result["content"]


def test_tool_fallback_chain_skips_bili_when_disabled() -> None:
    plugin, host = _prepare_plugin()
    from fakehost import get_default_config

    config = get_default_config(plugin_module.MusicRequestConfig)
    config["bilibili"]["enabled"] = False
    plugin.set_plugin_config(config)
    api = _ScenarioAPI([])
    plugin._api = api

    result = asyncio.run(
        plugin.search_and_play_music(query="不存在的歌xyz", stream_id="s1")
    )
    assert all(not s.startswith("bili:") for s in api.searches), api.searches
    assert "B站" not in result["content"], result


# ---------- 取流链路：多通路与 412 归因（v1.6.1 / v1.6.2） ----------
#
# 真机事故（2026-10-21），两轮定位结论不同，都记在这里：
#
#   第一轮（v1.6.1）：`get_video_cid` 走裸 GET，绕过了 `_signed_get` 的 412 分支，
#     且 `except httpx.HTTPError` 把 412 / 404 / 网络异常折叠成同一句
#     「查询视频信息失败」—— 属诊断塌陷，已修。
#   第二轮（v1.6.2）：修复部署后**仍然 412**，且 nav 明确回显 isLogin=true
#     （凭据有效，排除了「SESSDATA 过期」这个假设）。逐条实测，同一 IP、
#     同一份 cookie、相隔数秒：
#         x/web-interface/view      → HTTP 412
#         x/player/pagelist         → HTTP 200（data[0].cid）
#         x/web-interface/wbi/view  → HTTP 200
#     ⇒ 不是 IP 风控、不是凭据过期，是这个**接口路径本身**对该客户端稳定 412。
#     结论：换接口，不在风控上硬磕。主通路改 pagelist，兜底 wbi/view。
#
# 下面九条用例把通路选择与四条错误岔路全部钉死。
# 特别地：stub 不认识 `x/web-interface/view`，任何把主通路改回去的改动都会
# 直接以「未预期请求」失败，而不是悄悄换个接口继续绿。

_WBI_IMG = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad942dcab20b1.png"
_WBI_SUB = "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"
_PAGELIST_OK = {"code": 0, "data": [{"cid": 3307690, "page": 1, "duration": 206}]}
_VIEW_OK = {"code": 0, "data": {"cid": 2129461, "title": "洛天依，言和原创《普通DISCO》"}}


def _nav_payload(is_login: bool = True) -> dict:
    return {
        "code": 0,
        "data": {
            "isLogin": is_login,
            "wbi_img": {"img_url": _WBI_IMG, "sub_url": _WBI_SUB},
        },
    }


def _spi_payload(seq: int) -> dict:
    return {"code": 0, "data": {"b_3": f"buvid3-{seq}", "b_4": f"buvid4-{seq}"}, "message": "ok"}


class _BiliStub:
    """按 path 分派的假 B站后端；script 元素 200=正常，412/404=对应状态码。"""

    def __init__(
        self,
        *,
        pagelist_script: list[int],
        wbi_view_script: list[int] | None = None,
        nav_payload: dict | None = None,
        pagelist_payload: dict | None = None,
    ) -> None:
        self.pagelist_script = list(pagelist_script)
        self.wbi_view_script = list(wbi_view_script or [200])
        self.nav_payload = nav_payload or _nav_payload()
        self.pagelist_payload = pagelist_payload or _PAGELIST_OK
        self.spi_calls = 0
        self.pagelist_calls: list[dict] = []
        self.wbi_view_calls: list[dict] = []
        self.playurl_calls: list[dict] = []
        self.playurl_payload = {"code": 0, "data": {"dash": {"audio": [_stream(30280, 198000)]}}}

    @staticmethod
    def _respond(script: list[int], calls: list[dict], params: dict, ok: dict) -> httpx.Response:
        calls.append(params)
        step = script[min(len(calls), len(script)) - 1]
        if step == 412:
            return httpx.Response(
                412, text="<html><body>412 Precondition Failed</body></html>"
            )
        if step == 404:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json=ok)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path.endswith("/spi"):
            self.spi_calls += 1
            return httpx.Response(200, json=_spi_payload(self.spi_calls))
        if path.endswith("/nav"):
            return httpx.Response(200, json=self.nav_payload)
        if path.endswith("/player/pagelist"):
            return self._respond(
                self.pagelist_script, self.pagelist_calls, params, self.pagelist_payload
            )
        if path.endswith("/web-interface/wbi/view"):
            return self._respond(self.wbi_view_script, self.wbi_view_calls, params, _VIEW_OK)
        if path.endswith("/player/playurl"):
            self.playurl_calls.append(params)
            return httpx.Response(200, json=self.playurl_payload)
        # 刻意不支持 x/web-interface/view：主通路若被改回去，这里立刻炸
        raise AssertionError(f"未预期的请求（疑似通路回退）: {path}")

    def client(self, **kwargs) -> BilibiliClient:
        return BilibiliClient(transport=httpx.MockTransport(self.handler), **kwargs)


def _run_with_stub(stub: "_BiliStub", coro_factory, *, sessdata: str = ""):
    """在同一个事件循环里建 client、跑协程、关连接，返回 (结果, client)。"""

    async def runner():
        client = stub.client(sessdata=sessdata)
        try:
            return await coro_factory(client), client
        finally:
            await client.close()

    return asyncio.run(runner())


def test_primary_cid_path_is_pagelist() -> None:
    """主通路必须是 pagelist：带签名、且成功时完全不触碰 view。"""
    stub = _BiliStub(pagelist_script=[200])
    cid, _ = _run_with_stub(stub, lambda c: c.get_video_cid("BV1zs411S7sz"))
    assert cid == 3307690
    assert len(stub.pagelist_calls) == 1
    assert stub.wbi_view_calls == [], "主通路成功时不应触碰兜底通路"
    assert "w_rid" in stub.pagelist_calls[0], "pagelist 请求必须带 WBI 签名"
    assert "wts" in stub.pagelist_calls[0]


def test_pagelist_412_refreshes_buvid_and_retries_once() -> None:
    """412 后刷新 buvid3 重试一次；只一次，不反复打设备指纹。"""
    stub = _BiliStub(pagelist_script=[412, 200])
    cid, _ = _run_with_stub(stub, lambda c: c.get_video_cid("BV1zs411S7sz"))
    assert cid == 3307690
    assert len(stub.pagelist_calls) == 2
    assert stub.spi_calls == 2, "第 1 次初始领取，第 2 次 412 自救刷新"
    assert stub.wbi_view_calls == []


def test_pagelist_failure_falls_back_to_wbi_view() -> None:
    """pagelist 两跳都被风控时必须换兜底通路，而不是直接把失败抛给用户。"""
    stub = _BiliStub(pagelist_script=[412, 412], wbi_view_script=[200])
    cid, _ = _run_with_stub(stub, lambda c: c.get_video_cid("BV1zs411S7sz"))
    assert cid == 2129461, "应从 wbi/view 拿到 cid"
    assert len(stub.pagelist_calls) == 2
    assert len(stub.wbi_view_calls) == 1


def test_missing_cid_in_pagelist_falls_back() -> None:
    """pagelist 返回空列表（无分P）时视为失败，继续走兜底。"""
    stub = _BiliStub(pagelist_script=[200], pagelist_payload={"code": 0, "data": []})
    cid, _ = _run_with_stub(stub, lambda c: c.get_video_cid("BV1zs411S7sz"))
    assert cid == 2129461
    assert len(stub.wbi_view_calls) == 1


def test_all_paths_failing_reports_each_reason() -> None:
    """两条通路都挂时必须同时报出各自原因，不能折叠成一句话。"""
    stub = _BiliStub(pagelist_script=[412, 412], wbi_view_script=[412, 412])
    with pytest.raises(MusicAPIResponseError) as ei:
        _run_with_stub(stub, lambda c: c.get_video_cid("BV1zs411S7sz"))
    text = ei.value.diagnostic()
    assert "412" in text
    assert "条通路均不可用" in text
    assert "分P列表" in text and "视频信息" in text, f"两条通路的原因都要在: {text}"
    assert "匿名" in text, "凭据诊断要带出来"


def test_view_404_is_distinguishable_from_412() -> None:
    """视频被删（404）与风控（412）必须能区分。"""
    stub = _BiliStub(pagelist_script=[404], wbi_view_script=[404])
    with pytest.raises(MusicAPIResponseError) as ei:
        _run_with_stub(stub, lambda c: c.get_video_cid("BV1zs411S7sz"))
    text = ei.value.diagnostic()
    assert "404" in text
    assert "风控" not in text


def test_stale_sessdata_is_detected_via_nav_islogin() -> None:
    """配了 SESSDATA 但 nav.isLogin=false 必须被记下（静默退化为匿名的唯一线索）。"""
    stub = _BiliStub(pagelist_script=[200], nav_payload=_nav_payload(is_login=False))
    _, client = _run_with_stub(
        stub, lambda c: c.get_video_cid("BV1zs411S7sz"), sessdata="stale-cookie"
    )
    assert client.login_state is False


def test_get_audio_url_records_last_error_instead_of_swallowing() -> None:
    """取流失败必须把原因留在 last_error，否则上层只能报「未返回可用音频」。"""
    stub = _BiliStub(pagelist_script=[412, 412], wbi_view_script=[412, 412])
    url, client = _run_with_stub(stub, lambda c: c.get_audio_url("BV1zs411S7sz"))
    assert url is None
    assert "412" in client.last_error


def test_cid_extractors_handle_edges() -> None:
    """两条通路的 cid 提取都要抗异常结构（多P取 page=1、cid=0 视为无效）。"""
    assert cid_from_pagelist({"data": [{"cid": 2, "page": 2}, {"cid": 1, "page": 1}]}) == 1
    assert cid_from_pagelist({"data": [{"cid": 9}]}) == 9, "无 page 字段时退回第一条"
    assert cid_from_pagelist({"data": []}) is None
    assert cid_from_pagelist({"data": [{"cid": 0, "page": 1}]}) is None
    assert cid_from_pagelist({}) is None
    assert cid_from_pagelist({"data": "oops"}) is None
    assert cid_from_view({"data": {"cid": 2129461}}) == 2129461
    assert cid_from_view({"data": {}}) is None
    assert cid_from_view({"data": None}) is None
    assert cid_from_view(None) is None


# ---------- PCDN 与官方备份的择优（v1.6.3） ----------
#
# 真机 2026-10-21 20:17 的日志：
#   `B站音频直链不在 CDN 白名单，已拒绝: https://b-iba....edge.mountaintoys.cn:4483/...`
# 三条音频轨（30216 / 30232 / 30280）全被拒，于是报「未返回可用音频轨」。
#
# 事实核对（第三方交叉验证，非自证）：
#   - `mountaintoys.cn` 的 WHOIS 注册邮箱是 email@bilibili.com，注册人为
#     杭州不同凡想网络科技有限公司 ⇒ 是 B站**自建 PCDN**，不是第三方；
#   - PCDN URL 的特征是**带非标准端口**（4483）且参数含 `os=mcdn`；
#   - 同一 stream 的 `backupUrl` 仍指向官方 `*.bilivideo.com`。
#
# 因此修法不是放宽白名单，而是**把 backupUrl 纳入候选**：安全边界不变，
# 只是让早就存在的官方备份地址有机会被选中。下面四条用例钉死这条路。

_PCDN_HOST = "b-iba20ag82j4h3alp2dhdrg8fe7rc48za.edge.mountaintoys.cn:4483"
_OFFICIAL_HOST = "upos-sz-mirrorcoso1.bilivideo.com"


def _stream_full(
    aid: int,
    bandwidth: int,
    base_host: str,
    backup_hosts: tuple[str, ...] = (),
) -> dict:
    """构造带 backupUrl 的 dash stream（同时给驼峰与下划线两套字段名）。"""
    return {
        "id": aid,
        "bandwidth": bandwidth,
        "baseUrl": f"https://{base_host}/x-{aid}.m4s",
        "base_url": f"https://{base_host}/x-{aid}.m4s",
        "backupUrl": [f"https://{h}/x-{aid}.m4s" for h in backup_hosts],
        "backup_url": [f"https://{h}/x-{aid}.m4s" for h in backup_hosts],
    }


def test_pcdn_base_url_falls_back_to_official_backup() -> None:
    """baseUrl 是 PCDN 时必须改用 backupUrl 的官方 CDN，不能判「无音频轨」。"""
    payload = {
        "code": 0,
        "data": {"dash": {"audio": [
            _stream_full(30280, 319160, _PCDN_HOST, (_OFFICIAL_HOST,)),
        ]}},
    }
    url = audio_stream_from_playurl_payload(payload)
    assert url is not None, "官方备份可用，不应返回 None"
    assert "mountaintoys" not in url, f"不能选 PCDN: {url}"
    assert _OFFICIAL_HOST in url


def test_high_bandwidth_pcdn_falls_back_to_lower_official_stream() -> None:
    """最高码率流只有 PCDN 时，退到次高但有官方地址的流。"""
    payload = {
        "code": 0,
        "data": {"dash": {"audio": [
            _stream_full(30280, 319160, _PCDN_HOST, (_PCDN_HOST,)),
            _stream_full(30232, 132842, _OFFICIAL_HOST),
        ]}},
    }
    url = audio_stream_from_playurl_payload(payload)
    assert url is not None
    assert "x-30232" in url, f"应退到 30232 官方流: {url}"


def test_base_url_preferred_over_backup_when_both_ok() -> None:
    """两者都合法时优先 baseUrl（尊重 B站的调度意图）。"""
    payload = {
        "code": 0,
        "data": {"dash": {"audio": [
            _stream_full(30280, 319160, "upos-sz-a.bilivideo.com",
                         ("upos-sz-b.bilivideo.com",)),
        ]}},
    }
    url = audio_stream_from_playurl_payload(payload)
    assert url is not None and "upos-sz-a.bilivideo.com" in url


def test_pcdn_everywhere_reports_hosts_in_log(caplog) -> None:
    """baseUrl 与 backupUrl 全被拒时，日志必须列出 host，供下一轮直接定案。"""
    payload = {
        "code": 0,
        "data": {"dash": {"audio": [
            _stream_full(30280, 319160, _PCDN_HOST, (_PCDN_HOST,)),
        ]}},
    }
    with caplog.at_level(logging.WARNING, logger="plugin.music-request.bilibili"):
        assert audio_stream_from_playurl_payload(payload) is None
    assert "mountaintoys.cn:4483" in caplog.text, caplog.text
    assert "backupUrl" in caplog.text


# ---------- av 号支持（v1.6.4） ----------
#
# VCPedia 词条信息框里的 `bb_id` 大多是 av 号（实测 11/13），只有少数写 BV。
# 而 B站的 `player/pagelist` 与 `player/playurl` 都原生接受 `aid=` 参数，
# 所以不做 av↔BV 转换（那需要编码算法或额外请求），直接把形态透传给接口。
# 这两条用例保证「av 号不会被当成非法标识挡在门外」。


def test_aid_converted_to_bvid_for_requests() -> None:
    """av 号必须先转 BV 再发请求——playurl 不接受 aid（实测 code=-400）。"""
    stub = _BiliStub(pagelist_script=[200])
    cid, _ = _run_with_stub(stub, lambda c: c.get_video_cid("av2129461"))
    assert cid == 3307690
    params = stub.pagelist_calls[0]
    assert params.get("bvid") == "BV1zs411S7sz", params
    assert "aid" not in params, "转换后不应再带 aid 参数"


def test_bare_digits_treated_as_aid() -> None:
    """纯数字（VCPedia 也有这种写法）当 av 号处理并转成 BV。"""
    stub = _BiliStub(pagelist_script=[200])
    _, _ = _run_with_stub(stub, lambda c: c.get_video_cid("2129461"))
    assert stub.pagelist_calls[0].get("bvid") == "BV1zs411S7sz"


def test_cid_cache_normalizes_av_prefix() -> None:
    """`av2129461` 与 `2129461` 指向同一视频，缓存必须共用一条。"""
    stub = _BiliStub(pagelist_script=[200])

    async def two(client):
        first = await client.get_video_cid("av2129461")
        second = await client.get_video_cid("2129461")
        return first, second

    result, _ = _run_with_stub(stub, two)
    assert result == (3307690, 3307690)
    assert len(stub.pagelist_calls) == 1, "归一化后应命中同一条缓存"


@pytest.mark.parametrize("bad", ["BV123", "abc", "", "av", "1234567890123", "BV1zs411S7s"])
def test_invalid_video_id_rejected(bad: str) -> None:
    """非法标识必须在发请求之前就挡掉。"""
    stub = _BiliStub(pagelist_script=[200])
    with pytest.raises(MusicAPIResponseError):
        _run_with_stub(stub, lambda c: c.get_video_cid(bad))
    assert stub.pagelist_calls == [], "非法标识不应产生任何网络请求"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BV1zs411S7sz", ("bvid", "BV1zs411S7sz")),
        ("av2075941", ("aid", "2075941")),
        ("AV2075941", ("aid", "2075941")),
        ("2075941", ("aid", "2075941")),
        ("  av9372087  ", ("aid", "9372087")),
        ("", ("", "")),
        ("None", ("", "")),
        ("BV1zs411S7szx", ("", "")),
    ],
)
def test_normalize_video_id(raw: str, expected: tuple[str, str]) -> None:
    assert normalize_video_id(raw) == expected


# ---------- av↔BV 编码（v1.6.4） ----------
#
# 转换是必须的：`player/playurl` **不接受 aid 参数**（实测 code=-400 请求错误），
# 而 VCPedia 里 11/13 的原曲标识是 av 号。算法经两重验证，不是照抄：
#   1. av2129461 → BV1zs411S7sz 与已知对应精确吻合；
#   2. 下面 5 组用 aid 与转换出的 bvid 分别查 cid，结果完全一致。


def test_av2bv_matches_known_pair() -> None:
    """av2129461 ↔ BV1zs411S7sz：两个形态查 cid 都是 3307690（实测确认）。"""
    assert av2bv(2129461) == "BV1zs411S7sz"
    assert bv2av("BV1zs411S7sz") == 2129461


@pytest.mark.parametrize(
    ("aid", "bvid"),
    [
        (2075941, "BV1fx411A7hz"),   # 权御天下
        (9372087, "BV1sx411k7ug"),   # 达拉崩吧
        (78977256, "BV1KJ411C7CW"),  # 万古生香
        (45020985, "BV14b411b7Vr"),  # 大氿歌
        (3905462, "BV1es41197ai"),   # 九九八十一(乐正绫)
    ],
)
def test_av2bv_cross_checked_pairs(aid: int, bvid: str) -> None:
    """这 5 组经 B站接口交叉验证：aid 与 bvid 查到的 cid 完全一致。"""
    assert av2bv(aid) == bvid
    assert bv2av(bvid) == aid


def test_resolve_video_bvid() -> None:
    assert resolve_video_bvid("BV1zs411S7sz") == "BV1zs411S7sz"
    assert resolve_video_bvid("av2129461") == "BV1zs411S7sz"
    assert resolve_video_bvid("2129461") == "BV1zs411S7sz"
    assert resolve_video_bvid("  av2129461  ") == "BV1zs411S7sz"
    assert resolve_video_bvid("bad") == ""
    assert resolve_video_bvid(None) == ""


def test_bv2av_rejects_invalid() -> None:
    assert bv2av("BV123") is None
    assert bv2av("") is None
    assert bv2av("BV1zs411S7!z") is None
