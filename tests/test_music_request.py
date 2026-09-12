"""插件级单测：manifest 契约、组件绑定、命令正则、配置模型、纯函数。

这些用例的存在意义是「本地就能发现真机才会炸的问题」：
    - 能力漏声明 → 真机 `[E_CAPABILITY_DENIED]`
    - 装饰器绑错方法 → 真机 `unexpected keyword argument`
    - 命令正则失配 → 命令石沉大海
每一条都能反向验证：把实现改回错误写法，对应用例必然失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import pathlib
import re

import pytest

import plugin as plugin_module
from audio_cache import (
    PROBE_PREFIX,
    MusicAudioCache,
    looks_like_mp3,
    map_to_napcat,
    write_cache_probe,
)
from music_api import MusicSearchClient, SongInfo, eapi_encrypt
import music_api as client_module
from plugin import MusicRequestPlugin

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent

# Host 在调用 @Tool 时会注入这批上下文字段，参数名撞车会产生覆盖歧义
_INJECTED_CONTEXT_FIELDS = {
    "user_id", "group_id", "stream_id", "session_id",
    "message", "chat_id", "raw_message", "is_local_operator",
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))


@pytest.fixture()
def plugin() -> MusicRequestPlugin:
    instance = MusicRequestPlugin()
    instance.set_plugin_config(plugin_module.MusicRequestConfig().model_dump())
    return instance


# ---------- manifest 契约 ----------


def test_manifest_declares_required_capabilities(manifest: dict) -> None:
    """源码用到的 ctx 能力必须全部声明，漏一个真机就 E_CAPABILITY_DENIED。"""
    caps = set(manifest["capabilities"])
    assert {"send.text", "send.custom", "chat.get_all_streams"} <= caps, f"漏声明能力: {caps}"


def test_manifest_matches_plugin_version(manifest: dict) -> None:
    """manifest.version 与代码里的 SUPPORTED_CONFIG_VERSION 必须同步。"""
    assert manifest["version"] == plugin_module.SUPPORTED_CONFIG_VERSION


def test_manifest_capabilities_are_exact_dotted_names(manifest: dict) -> None:
    """粗粒度能力名（如 send_message）在注册表里不存在，等于未声明。"""
    for cap in manifest["capabilities"]:
        assert re.fullmatch(r"[a-z_]+\.[a-z_]+", cap), f"能力名不是精确点分名: {cap}"


def test_manifest_declares_cryptography_dependency(manifest: dict) -> None:
    """eapi 加密通道依赖 cryptography，必须声明否则真机不会安装。"""
    packages = {
        dep.get("name") for dep in manifest["dependencies"] if dep.get("type") == "python_package"
    }
    assert {"httpx", "cryptography"} <= packages, f"缺少依赖声明: {packages}"


# ---------- 组件绑定（装饰器绑错方法的唯一本地防线） ----------


def test_get_components_exposes_all_six() -> None:
    """覆盖 Runner 注册链路：漏调 super().__init__() 时这里会 AttributeError。"""
    components = MusicRequestPlugin().get_components()
    names = {item["name"] for item in components}
    assert names == {
        "点歌", "选歌", "点歌状态", "点歌自检", "search_and_play_music", "parse_music_link",
    }, names


@pytest.mark.parametrize(
    ("method_name", "component_name"),
    [
        ("cmd_点歌", "点歌"),
        ("cmd_选歌", "选歌"),
        ("cmd_点歌状态", "点歌状态"),
        ("cmd_点歌自检", "点歌自检"),
        ("search_and_play_music", "search_and_play_music"),
        ("parse_music_link", "parse_music_link"),
    ],
)
def test_decorator_bound_to_expected_method(method_name: str, component_name: str) -> None:
    """辅助方法插队到装饰器与组件函数之间时，这里会立刻失败。"""
    method = getattr(MusicRequestPlugin, method_name)
    info = getattr(method, "__maibot_component_info__", None)
    assert info is not None, f"{method_name} 上没有组件元数据（装饰器绑错人了）"
    assert info.name == component_name, f"{method_name} 绑定到了 {info.name!r}"


# ---------- 命令正则 ----------


def _command_pattern(method_name: str) -> str:
    info = getattr(getattr(MusicRequestPlugin, method_name), "__maibot_component_info__")
    return info.command_pattern


@pytest.mark.parametrize(
    "text",
    [
        "/点歌 晴天",
        "#点歌 163 晴天",
        "/点歌 qq 稻香",
        "/点歌 网易云音乐 起风了",
        "!点歌 netease 孤勇者",
    ],
)
def test_点歌_pattern_matches(text: str) -> None:
    assert re.search(_command_pattern("cmd_点歌"), text), f"点歌正则失配: {text}"


@pytest.mark.parametrize("text", ["点歌 晴天", "/点歌", "/点歌状态", "我想 点歌 晴天"])
def test_点歌_pattern_rejects(text: str) -> None:
    """必须要求前缀，且不能吞掉「点歌状态」和句中的「点歌」。"""
    assert re.search(_command_pattern("cmd_点歌"), text) is None, f"点歌正则误匹配: {text}"


@pytest.mark.parametrize(
    "text",
    [
        "发一首万能处方的文件",
        "发晚安糖果罐的文件",
        "发个文件",
        "我要无损",
        "要好音质",
        "这歌太长发文件吧",
        "来个无损音质的",
        "来无损",
        "要flac",
        "给我发ape",
        "发一份无损",
    ],
)
def test_file_intent_pattern_matches(text: str) -> None:
    """用户明确要文件/无损时，必须能识别（含中间夹歌名的情况）。"""
    assert plugin_module._RE_FILE_INTENT.search(text), f"文件意图失配: {text}"


@pytest.mark.parametrize(
    "text",
    [
        "放一首万能处方",
        "放一下万能处方",
        "来一首万能处方",
        "放歌",
        "随便放点音乐",
        "我想听万能处方",
        "放个歌吧",
    ],
)
def test_file_intent_pattern_rejects(text: str) -> None:
    """普通点歌话术绝不能命中文件意图（真机实锤：误判会让用户收到文件）。"""
    assert plugin_module._RE_FILE_INTENT.search(text) is None, f"文件意图误匹配: {text}"


@pytest.mark.parametrize("text", ["/选歌 1", "#选歌 12"])
def test_选歌_pattern_matches(text: str) -> None:
    assert re.search(_command_pattern("cmd_选歌"), text)


@pytest.mark.parametrize("text", ["选歌 1", "/选歌", "/选歌 abc"])
def test_选歌_pattern_rejects(text: str) -> None:
    assert re.search(_command_pattern("cmd_选歌"), text) is None


def test_点歌状态_pattern() -> None:
    pattern = _command_pattern("cmd_点歌状态")
    assert re.search(pattern, "/点歌状态")
    assert re.search(pattern, "#点歌状态")
    assert re.search(pattern, "/点歌状态 x") is None


def test_点歌自检_pattern() -> None:
    pattern = _command_pattern("cmd_点歌自检")
    assert re.search(pattern, "/点歌自检")
    assert re.search(pattern, "#点歌自检")
    assert re.search(pattern, "/点歌自检 x") is None


def test_点歌自检_does_not_swallow_点歌状态() -> None:
    """两个命令只差两个字，必须各匹配各的，不能被彼此吃掉。"""
    assert re.search(_command_pattern("cmd_点歌"), "/点歌自检") is None
    assert re.search(_command_pattern("cmd_点歌自检"), "/点歌状态") is None


# ---------- 前缀归一化 ----------


def test_prefix_accepts_full_width(plugin: MusicRequestPlugin) -> None:
    """全角斜杠／ 应等价于配置里的 /，否则手机输入法下命令会失联。"""
    assert plugin._prefix_ok("/") is True
    assert plugin._prefix_ok("／") is True
    assert plugin._prefix_ok("#") is False
    assert plugin._prefix_ok("") is False


def test_prefix_follows_config(plugin: MusicRequestPlugin) -> None:
    config = plugin_module.MusicRequestConfig()
    config.music.command_prefix = "#"
    plugin.set_plugin_config(config.model_dump())
    assert plugin._prefix_ok("#") is True
    assert plugin._prefix_ok("＃") is True
    assert plugin._prefix_ok("/") is False


# ---------- 配置模型 ----------


def test_config_section_requires_config_version_hidden() -> None:
    """1.2.3 硬性要求：plugin.config_version 必须存在且对用户隐藏。"""
    field = plugin_module.PluginSectionConfig.model_fields["config_version"]
    assert field.json_schema_extra == {"hidden": True, "disabled": True}


def test_all_config_fields_have_defaults() -> None:
    """任何字段缺默认值都会让 Runner 无法生成默认配置。"""
    config = plugin_module.MusicRequestConfig()
    dumped = config.model_dump()
    assert set(dumped) == {"plugin", "music", "netease", "qq", "napcat", "cache"}
    assert dumped["music"]["play_mode"] == "card"
    assert dumped["music"]["voice_source"] == "local"
    assert dumped["music"]["command_prefix"] == "/"


@pytest.mark.parametrize(("field", "bad"), [("play_mode", "video"), ("voice_source", "cloud")])
def test_config_rejects_invalid_enum(field: str, bad: str) -> None:
    """Literal 字段应拒绝非法取值，而不是静默走默认分支。"""
    with pytest.raises(Exception):
        plugin_module.MusicConfig(**{field: bad})


def test_config_default_platform_rejects_unknown_platform() -> None:
    with pytest.raises(Exception):
        plugin_module.MusicConfig(default_platform="kugou")


# ---------- Tool 参数命名 ----------


def test_tool_parameters_avoid_injected_context_fields() -> None:
    signature = inspect.signature(MusicRequestPlugin.search_and_play_music)
    params = set(signature.parameters) - {"self", "kwargs"}
    assert params == {"query", "send_as"}, f"工具参数集合异常: {params}"
    assert not (params & _INJECTED_CONTEXT_FIELDS), "工具参数名与 Host 注入字段撞车"


def test_tool_description_covers_lyric_chat_trigger() -> None:
    """工具描述必须覆盖「贴了歌词 / 在聊某首歌并表示想听」这条触发路径。

    cv_lyric_context 会把歌名语境注入规划器，但如果本工具的描述只写「用户点歌」，
    规划器仍可能不选它——描述是规划器做工具选择的唯一依据。
    """
    info = MusicRequestPlugin.search_and_play_music.__maibot_component_info__
    text = f"{info.description}\n{info.detailed_description}"
    for keyword in ("歌词", "想听", "可点播查询"):
        assert keyword in text, f"工具描述缺少联动关键词「{keyword}」: {text}"


# ---------- QQ 卡片 CQ 码 ----------


def test_build_qq_card_cq_escapes_display_text_only() -> None:
    """展示文本要转义 & , [ ]；URL/audio 绝不能转义（转了就打不开）。"""
    cq = plugin_module._build_qq_card_cq({
        "url": "https://y.qq.com/n/ryqq/songDetail/a&b",
        "audio": "https://cdn.example.com/a.m4a?x=1&y=2",
        "title": "A&B, C [D]",
        "image": "https://y.qq.com/cover.jpg",
        "content": "歌手, 另一位",
    })
    assert cq.startswith("[CQ:music,type=custom,url=https://y.qq.com/n/ryqq/songDetail/a&b,")
    assert "audio=https://cdn.example.com/a.m4a?x=1&y=2" in cq
    assert "title=A&amp;B&#44; C &#91;D&#93;" in cq
    assert "content=歌手&#44; 另一位" in cq


def test_build_qq_card_cq_omits_audio_when_missing() -> None:
    """拿不到直链时生成跳转卡（不带 audio），而不是发不出去的坏卡。"""
    cq = plugin_module._build_qq_card_cq({
        "url": "https://y.qq.com/n/ryqq/songDetail/a",
        "audio": "",
        "title": "t",
        "image": "https://y.qq.com/c.jpg",
        "content": "",
    })
    assert "audio=" not in cq
    assert cq.endswith("]")


# ---------- 网易云 eapi 加密 ----------


def test_eapi_encrypt_is_deterministic_uppercase_hex() -> None:
    """网易云要求大写 hex，且密文长度必须是 16 字节的整数倍。"""
    pytest.importorskip("cryptography")
    first = eapi_encrypt("/api/song/enhance/player/url", {"ids": "[1]", "br": 320000})
    second = eapi_encrypt("/api/song/enhance/player/url", {"ids": "[1]", "br": 320000})
    assert first == second, "同参数必须得到同一密文"
    assert re.fullmatch(r"[0-9A-F]+", first), "密文不是大写 hex"
    assert len(first) % 32 == 0, "密文长度不是 16 字节的整数倍"


def test_eapi_encrypt_changes_with_params() -> None:
    pytest.importorskip("cryptography")
    a = eapi_encrypt("/api/song/enhance/player/url", {"ids": "[1]"})
    b = eapi_encrypt("/api/song/enhance/player/url", {"ids": "[2]"})
    assert a != b


# ---------- 音频格式校验 ----------


@pytest.mark.parametrize("head", [b"ID3\x03\x00", b"\xff\xfb\x90\x00", b"\xff\xf3\x00"])
def test_looks_like_mp3_accepts(head: bytes) -> None:
    assert looks_like_mp3(head) is True


@pytest.mark.parametrize(
    "head",
    [b"fLaC\x00", b"OggS\x00", b"<html><b", b"", b"\x00\x00"],
)
def test_looks_like_mp3_rejects(head: bytes) -> None:
    """FLAC/M4A/错误页必须被拒，否则语音会静默损坏。"""
    assert looks_like_mp3(head) is False


# ---------- 网易云多通道回退（协程必须惰性创建） ----------


def _stub_netease_channels(
    client: MusicSearchClient,
    results: dict[str, str | None],
    created: list[str],
    awaited: list[str],
) -> None:
    """把三条网易云通道换成假的实现，分别记录「创建」与「被 await」的通道。

    关键：`created` 记录必须写在**同步包装函数**里。如果把记录写在协程体内，
    记录动作要等 await 才发生，就无法区分「惰性创建」与「提前创建」——
    这正是本组用例要守的不变量。
    """

    def make(channel: str):
        def _factory(song_id: str, bitrate: int = 0):
            created.append(channel)

            async def _coro() -> str | None:
                awaited.append(channel)
                return results.get(channel)

            return _coro()

        return _factory

    client._netease_eapi_url = make("eapi")
    client._netease_web_url = make("web")
    client._netease_outer_url = make("outer")


def _run_netease_song_url(client: MusicSearchClient) -> str | None:
    async def run() -> str | None:
        try:
            return await client._netease_song_url("1")
        finally:
            await client.close()

    return asyncio.run(run())


def test_first_channel_hit_creates_only_one_coroutine() -> None:
    """不变量：**创建的协程数必须等于被 await 的协程数**。

    真机日志实锤（2026-09-11）：写成
    `for coro in (self._a(...), self._b(...), self._c(...))` 时三个协程在循环开始前
    就全部创建；命中 eapi 后 web / outer 永不 await，于是每个请求都往 stderr 刷
    `RuntimeWarning: coroutine 'MusicSearchClient._netease_web_url' was never awaited`。

    只断言「被 await 的通道」是抓不到这个 bug 的——必须同时看创建数。
    """
    client = MusicSearchClient()
    created: list[str] = []
    awaited: list[str] = []
    _stub_netease_channels(client, {"eapi": "https://cdn.example.com/a.mp3"}, created, awaited)

    assert _run_netease_song_url(client) == "https://cdn.example.com/a.mp3"
    assert created == awaited == ["eapi"], (
        f"存在未 await 的协程（created={created} awaited={awaited}）"
    )


def test_middle_channel_hit_creates_only_two_coroutines() -> None:
    """中间通道命中也要立刻停，且不留孤儿协程。"""
    client = MusicSearchClient()
    created: list[str] = []
    awaited: list[str] = []
    _stub_netease_channels(client, {"web": "https://cdn.example.com/b.mp3"}, created, awaited)

    assert _run_netease_song_url(client) == "https://cdn.example.com/b.mp3"
    assert created == awaited == ["eapi", "web"], (
        f"命中后未停止或留下未 await 的协程（created={created} awaited={awaited}）"
    )


def test_all_channels_fail_falls_back_in_order() -> None:
    """三条通道全部失败时，按 eapi → web → outer 顺序依次尝试，不留下孤儿协程。"""
    client = MusicSearchClient()
    created: list[str] = []
    awaited: list[str] = []
    _stub_netease_channels(client, {}, created, awaited)

    assert _run_netease_song_url(client) is None
    assert created == awaited == ["eapi", "web", "outer"], (
        f"回退顺序异常（created={created} awaited={awaited}）"
    )


# ---------- 缓存目录映射与探针（voice_source=local 的前提） ----------

def test_map_to_napcat_same_dir_returns_original(tmp_path: pathlib.Path) -> None:
    storage = tmp_path / "cache"
    storage.mkdir()
    target = storage / "163_1.mp3"
    target.write_bytes(b"x")
    mapped, ok = map_to_napcat(str(storage), str(storage), target)
    assert ok is True
    assert pathlib.Path(mapped) == target


def test_map_to_napcat_docker_uses_posix_join(tmp_path: pathlib.Path) -> None:
    """napcat_dir 是容器内路径时必须拼出纯正斜杠路径，不能混进反斜杠。"""
    storage = tmp_path / "cache"
    storage.mkdir()
    target = storage / "163_1.mp3"
    target.write_bytes(b"x")
    mapped, ok = map_to_napcat(str(storage), "/app/music_cache", target)
    assert ok is True
    assert mapped == "/app/music_cache/163_1.mp3"
    assert "\\" not in mapped


def test_map_to_napcat_empty_napcat_dir_falls_back_to_storage(tmp_path: pathlib.Path) -> None:
    storage = tmp_path / "cache"
    storage.mkdir()
    target = storage / "a.mp3"
    target.write_bytes(b"x")
    mapped, ok = map_to_napcat(str(storage), "", target)
    assert ok is True and pathlib.Path(mapped) == target


def test_map_to_napcat_flags_path_outside_storage(tmp_path: pathlib.Path) -> None:
    """映射失败必须被标记出来——静默回退会让 NapCat 收到宿主机路径而打不开。"""
    storage = tmp_path / "cache"
    storage.mkdir()
    foreign = tmp_path / "outside.mp3"
    foreign.write_bytes(b"x")
    mapped, ok = map_to_napcat(str(storage), "/app/music_cache", foreign)
    assert ok is False
    assert mapped == str(foreign)


def test_cache_napcat_path_checked_reports_failure(tmp_path: pathlib.Path) -> None:
    cache = MusicAudioCache(
        str(tmp_path / "cache"),
        "/app/music_cache",
        max_size_bytes=1024,
        expire_seconds=60,
        max_file_size_bytes=1024,
        download_timeout_seconds=5,
    )
    # 注意：容器路径不能在 Windows 上再过一遍 pathlib.Path（会把 / 反转成 \），
    # 直接比字符串——插件交给 NapCat 的也是字符串
    mapped_inside, ok_inside = cache.napcat_path_checked(tmp_path / "cache" / "a.mp3")
    assert ok_inside is True
    assert mapped_inside == "/app/music_cache/a.mp3"
    _mapped, ok = cache.napcat_path_checked(tmp_path / "elsewhere.mp3")
    assert ok is False


def test_write_cache_probe_digest_matches_disk(tmp_path: pathlib.Path) -> None:
    """摘要必须真的是写下去那份内容的 MD5——否则用户在 NapCat 侧永远比对不上。"""
    storage = tmp_path / "cache"
    probe = write_cache_probe(str(storage), str(storage))
    on_disk = pathlib.Path(probe.storage_path)
    assert on_disk.is_file(), "探针文件没有落盘"
    assert probe.filename.startswith(PROBE_PREFIX)
    assert hashlib.md5(on_disk.read_bytes()).hexdigest()[:8] == probe.digest
    assert probe.mapped is True
    assert probe.napcat_path == probe.storage_path


def test_write_cache_probe_creates_missing_dir(tmp_path: pathlib.Path) -> None:
    """缓存目录还没建（比如从没走过 voice 模式）时自检也要能用。"""
    storage = tmp_path / "not" / "yet" / "there"
    probe = write_cache_probe(str(storage), "/app/music_cache")
    assert pathlib.Path(probe.storage_path).is_file()
    assert probe.napcat_path.startswith("/app/music_cache/")


def test_write_cache_probe_is_unique_per_call(tmp_path: pathlib.Path) -> None:
    """每次自检换一个文件名与摘要，避免读到上一次的探针造成误判。"""
    storage = tmp_path / "cache"
    first = write_cache_probe(str(storage), str(storage))
    second = write_cache_probe(str(storage), str(storage))
    assert first.filename != second.filename
    assert first.digest != second.digest


def test_probe_files_are_not_mp3_and_survive_cleanup_glob(tmp_path: pathlib.Path) -> None:
    """探针不能被 *.mp3 的过期/容量清理扫到，否则自检结果会自己消失。"""
    probe = write_cache_probe(str(tmp_path), "")
    assert not probe.filename.endswith(".mp3")


# ---------- 状态命令的目录提示 ----------


@pytest.mark.parametrize(
    ("storage", "napcat", "expected"),
    [
        ("E:\\cache", "E:\\cache", True),
        ("E:/cache/", "E:\\cache", True),
        ("E:\\cache", "/app/music_cache", False),
    ],
)
def test_same_dir_hint(storage: str, napcat: str, expected: bool) -> None:
    assert MusicRequestPlugin._same_dir_hint(storage, napcat) is expected


# ---------- 真机场景回归：最佳匹配不可播时，不许改播无关的歌 ----------

# 日志里的实际候选（2026-09-11 12:21，网易云搜索「晚安糖果罐」）
_TARGET_SONG = SongInfo(
    song_id="2010064341", name="晚安糖果罐", artists="洛天依", platform="163"
)
_NOISE_SONG = SongInfo(
    song_id="1909999",
    name="嘘嘘声+羊水声+胎心音+八音盒 星星糖果罐",
    artists="晚安宝贝",
    platform="163",
)


class _ScenarioAPI:
    """模拟真机的搜索与取直链行为（不联网）。

    `unplayable` 里的 song_id 视为「平台有结果、但版权受限拿不到音频」，
    对应日志里的 `网易云 eapi/web 返回空直链: song_code=-110`。
    """

    def __init__(
        self, songs: list[SongInfo], unplayable: set[str], *, upload_ok: bool = True
    ) -> None:
        self._songs = list(songs)
        self._unplayable = set(unplayable)
        self.searches: list[str] = []
        self.upload_ok = upload_ok
        self.uploads: list[dict] = []

    async def search(self, query, platform, limit=5):
        self.searches.append(f"{platform}:{query}")
        return list(self._songs)

    async def get_song_url(self, song_id, platform, media_id="", *, mp3_only=False):
        if song_id in self._unplayable:
            return None
        return f"https://example.invalid/{song_id}.mp3"

    async def get_qq_song_detail(self, song_mid):
        return None

    async def qq_music_card(self, song):
        return None

    async def probe_audio(self, url, timeout=5.0):
        return True

    async def resolve_short_url(self, url):
        return None

    async def get_raw_message(self, message_id):
        return None

    async def napcat_send_message(self, message, *, group_id="", user_id=""):
        return False, {}

    async def napcat_upload_file(
        self, file_path: str, *, name: str = "", group_id: str = "", user_id: str = ""
    ) -> tuple[bool, dict]:
        self.uploads.append({
            "file_path": file_path,
            "name": name,
            "group_id": group_id,
            "user_id": user_id,
        })
        return self.upload_ok, {"status": "ok"}

    async def close(self):
        return None


def _prepare_plugin(*, play_mode: str = "voice", voice_source: str = "remote"):
    """装好 FakeHost 上下文与指定播放模式的插件实例。"""
    from fakehost import FakeHost, bind_context, build_context, get_default_config

    plugin = MusicRequestPlugin()
    host = FakeHost(plugin_id="github.cateye.music-request")
    ctx = build_context("github.cateye.music-request", rpc_call=host.rpc_call)
    config = get_default_config(plugin_module.MusicRequestConfig)
    config["music"]["play_mode"] = play_mode
    config["music"]["voice_source"] = voice_source
    bind_context(plugin, ctx, config)
    return plugin, host


def test_tool_never_plays_irrelevant_fallback() -> None:
    """真机实锤回归（2026-09-11 12:21）。

    日志：query='晚安糖果罐' → 网易云 eapi 与标准接口都返回 `song_code=-110`
    （版权受限）→ **旧实现继续往下试候选** → 播出了
    「嘘嘘声+羊水声+胎心音+八音盒 星星糖果罐 - 晚安宝贝」。

    正确曲目不可播是平台问题；改播一首完全无关的歌是逻辑问题。
    断言：一条歌曲消息都不该发出去，工具如实报告失败。
    """
    plugin, host = _prepare_plugin()
    plugin._api = _ScenarioAPI([_TARGET_SONG, _NOISE_SONG], unplayable={_TARGET_SONG.song_id})

    result = asyncio.run(plugin.search_and_play_music(query="晚安糖果罐", stream_id="s1"))

    assert "已播放" not in result["content"], f"把无关候选当成结果播了: {result}"
    assert not host.calls_of("send.custom"), (
        f"不应发出任何歌曲消息，实际: {host.calls_of('send.custom')}"
    )
    assert "均未取到可播放音频" in result["content"], result["content"]


def test_tool_still_falls_back_within_same_song() -> None:
    """正向对照：同一首歌的其它版本必须仍被尝试——过滤不能过头。"""
    live = SongInfo(
        song_id="1888888", name="晚安糖果罐 (Live)", artists="洛天依", platform="163"
    )
    plugin, host = _prepare_plugin()
    plugin._api = _ScenarioAPI(
        [_TARGET_SONG, live, _NOISE_SONG], unplayable={_TARGET_SONG.song_id}
    )

    result = asyncio.run(plugin.search_and_play_music(query="晚安糖果罐", stream_id="s1"))

    assert "已播放" in result["content"], f"同曲版本被误过滤: {result}"
    assert "Live" in result["content"], f"播放的不是同曲版本: {result}"
    sent_urls = [(kw.get("data") or {}).get("url") for kw in host.calls_of("send.custom")]
    assert sent_urls == ["https://example.invalid/1888888.mp3"], sent_urls


def test_relevance_floor_one_tries_only_best_match() -> None:
    """relevance_floor=1.0 时只尝试最佳匹配，不做任何候选回退。"""
    live = SongInfo(
        song_id="1888888", name="晚安糖果罐 (Live)", artists="洛天依", platform="163"
    )
    plugin, host = _prepare_plugin()
    config = plugin_module.MusicRequestConfig()
    config.music.play_mode = "voice"
    config.music.voice_source = "remote"
    config.music.relevance_floor = 1.0
    plugin.set_plugin_config(config.model_dump())
    plugin._api = _ScenarioAPI(
        [_TARGET_SONG, live], unplayable={_TARGET_SONG.song_id}
    )

    result = asyncio.run(plugin.search_and_play_music(query="晚安糖果罐", stream_id="s1"))

    assert "已播放" not in result["content"], f"floor=1.0 不该回退候选: {result}"
    assert not host.calls_of("send.custom")


def test_command_plays_best_match_and_drops_noise_from_listing() -> None:
    """命令路径也要过排序+过滤：干扰项被剔除后只剩一首，直接播正确的那首。"""
    plugin, host = _prepare_plugin(play_mode="card")
    plugin._api = _ScenarioAPI([_NOISE_SONG, _TARGET_SONG], unplayable=set())

    ok, _resp, _level = asyncio.run(
        plugin.cmd_点歌(matched_groups={"pfx": "/", "query": "晚安糖果罐"}, stream_id="s1")
    )

    assert ok is True
    cards = host.calls_of("send.custom")
    assert cards, "未发送任何卡片"
    assert cards[0].get("data") == {"type": "163", "id": _TARGET_SONG.song_id}, cards[0]
    assert not any("嘘嘘声" in text for text in host.sent_texts if text), host.sent_texts


def test_command_lists_only_in_band_candidates() -> None:
    """正向对照：真有多个同档候选（同曲不同版本）时仍要列候选，且不含干扰项。"""
    live = SongInfo(
        song_id="1888888", name="晚安糖果罐 (Live)", artists="洛天依", platform="163"
    )
    plugin, host = _prepare_plugin(play_mode="card")
    plugin._api = _ScenarioAPI([_NOISE_SONG, _TARGET_SONG, live], unplayable=set())

    ok, _resp, _level = asyncio.run(
        plugin.cmd_点歌(matched_groups={"pfx": "/", "query": "晚安糖果罐"}, stream_id="s1")
    )

    assert ok is True
    assert not host.calls_of("send.custom"), "多候选时不该直接播"
    listing = next((t for t in reversed(host.sent_texts) if t and "搜索结果" in t), None)
    assert listing, f"未列出候选: {host.sent_texts}"
    assert "嘘嘘声" not in listing, f"干扰项混进了候选列表: {listing}"
    assert listing.index("晚安糖果罐") < listing.index("Live"), f"未按相关度排序: {listing}"


# ---------- 发送形态（voice / file）与 NapCat 文件上传 ----------


class _FakeCache:
    """缓存替身：文件形态真正需要的是「拿到一个 NapCat 可见路径」。"""

    def __init__(self, suffix: str = ".flac") -> None:
        self._suffix = suffix

    async def get_or_download(self, platform, song_id, url, suffix=""):
        return pathlib.Path(f"C://tmp//cache//{platform}_{song_id}{self._suffix}")

    def retain(self, path):
        return None

    def release(self, path):
        return None

    def napcat_path_checked(self, path):
        return (f"/app/music_cache/{path.name}", True)


def test_resolve_send_as_defaults_to_voice() -> None:
    """需求口径：工具没指定形态时默认发语音；无效取值回落默认而不是乱猜。"""
    plugin, _host = _prepare_plugin()
    plugin.set_plugin_config(plugin_module.MusicRequestConfig().model_dump())
    assert plugin._resolve_send_as("") == "voice"
    assert plugin._resolve_send_as("voice") == "voice"
    assert plugin._resolve_send_as("file") == "file"
    # card 未在工具参数里宣传，但传了就照做——静默吞掉等于「用户要 A 给了 B」
    assert plugin._resolve_send_as("card") == "card"
    assert plugin._resolve_send_as("随便") == "voice"


def test_resolve_send_as_follows_config() -> None:
    plugin = MusicRequestPlugin()
    config = plugin_module.MusicRequestConfig()
    config.music.tool_default_mode = "file"
    plugin.set_plugin_config(config.model_dump())
    assert plugin._resolve_send_as("") == "file"


def test_tool_send_file_uploads_via_napcat() -> None:
    """send_as=file：下载到本地后走 NapCat upload_*_file，且不走 voiceurl。"""
    plugin, host = _prepare_plugin()
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = True
    plugin._api = api
    plugin._cache = _FakeCache()
    plugin._qq_targets["s1"] = {"group_id": "12345"}

    result = asyncio.run(
        plugin.search_and_play_music(query="晚安糖果罐", send_as="file", stream_id="s1")
    )

    assert "已发送文件" in result["content"], result
    assert api.uploads, "没有调用 NapCat 上传"
    upload = api.uploads[0]
    assert upload["group_id"] == "12345" and upload["user_id"] == ""
    assert upload["file_path"] == "/app/music_cache/163_2010064341.flac"
    assert upload["name"].startswith("晚安糖果罐")
    assert not [
        kw for kw in host.calls_of("send.custom") if kw.get("custom_type") == "voiceurl"
    ], "文件形态不应改发语音"


def test_tool_defaults_to_voice_when_send_as_omitted() -> None:
    """默认发语音：LLM 不填 send_as 时绝不该偷偷上传文件。"""
    plugin, host = _prepare_plugin()
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = True
    plugin._api = api
    plugin._cache = _FakeCache()

    result = asyncio.run(plugin.search_and_play_music(query="晚安糖果罐", stream_id="s1"))

    assert "已播放" in result["content"], result
    customs = host.calls_of("send.custom")
    assert customs and customs[0].get("custom_type") == "voiceurl", customs
    assert api.uploads == [], "默认形态不应上传文件"


def test_file_mode_degrades_to_card_without_napcat() -> None:
    """拿不到 NapCat 直连目标时降级为卡片，并且必须让用户知道换形态了。

    刻意不降级为语音：用户选文件图的就是音质。
    """
    plugin, host = _prepare_plugin(play_mode="file")
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = True
    plugin._api = api
    plugin._cache = _FakeCache()

    sent = asyncio.run(plugin._send_song(_TARGET_SONG, "s1"))

    assert sent is True
    cards = host.calls_of("send.custom")
    assert cards and cards[0].get("data") == {"type": "163", "id": _TARGET_SONG.song_id}, cards
    assert api.uploads == [], "不该在拿不到目标时上传文件"
    assert any("已改为发送音乐卡片" in (t or "") for t in host.sent_texts), host.sent_texts


def test_tool_reports_degrade_when_upload_fails() -> None:
    """工具返回文本必须如实说明「已降级为卡片」。

    真机实锤（2026-09-11）：上传失败降级后工具仍返回「已发送文件」，
    reply 模型据此对用户说「文件版《哑巴》你看看收到没」，实际收到的是卡片。
    """
    plugin, host = _prepare_plugin(play_mode="file")
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = False  # NapCat 上传失败 → 应走降级
    plugin._api = api
    plugin._cache = _FakeCache()

    result = asyncio.run(
        plugin.search_and_play_music(query="晚安糖果罐", send_as="file", stream_id="s1")
    )

    content = result["content"]
    assert "音乐卡片" in content, content
    assert "已发送文件" not in content, f"降级后不该说已发送文件: {content}"
    assert "不要再说" in content, f"应明确禁止 reply 谎报文件: {content}"
    # 卡片确实发出去了
    cards = host.calls_of("send.custom")
    assert cards and cards[0].get("data") == {"type": "163", "id": _TARGET_SONG.song_id}, cards


def test_tool_reports_file_when_upload_succeeds() -> None:
    """正向对照：上传成功时仍报「已发送文件」，不能被降级文案污染。"""
    plugin, host = _prepare_plugin(play_mode="file")
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = True
    plugin._api = api
    plugin._cache = _FakeCache()
    # 工具拿不到 message 时按 stream_id 反查聊天流；测试里直接注入缓存目标
    plugin._qq_targets["s1"] = {"user_id": "3816023959"}

    result = asyncio.run(
        plugin.search_and_play_music(query="晚安糖果罐", send_as="file", stream_id="s1")
    )

    assert result["content"].startswith("已发送文件"), result
    assert "音乐卡片" not in result["content"], result
    assert api.uploads, "成功路径应真的上传了文件"


def test_send_file_outcome_reflects_degrade() -> None:
    """_send_file 的 outcome 出参必须写回真实形态与降级原因。"""
    plugin, host = _prepare_plugin(play_mode="file")
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = False
    plugin._api = api
    plugin._cache = _FakeCache()

    outcome: dict[str, str] = {}
    asyncio.run(plugin._send_file(_TARGET_SONG, "s1", silent=True, outcome=outcome))

    assert outcome.get("kind") == "card_degraded", outcome
    assert outcome.get("reason"), "降级必须带原因"


def test_describe_outcome_variants() -> None:
    """文案分派：三种形态 + 取不到音频，各有独立话术。"""
    describe = plugin_module.MusicRequestPlugin._describe_outcome
    song = _TARGET_SONG

    assert describe(song, "file", {"kind": "file"}) == f"已发送文件: {song.display()}"
    assert describe(song, "file", {}).startswith("已发送文件"), "缺 outcome 时按 mode 兜底"
    assert "已播放" in describe(song, "voice", {"kind": "voice"})
    assert "音乐卡片" in describe(song, "card", {"kind": "card"})
    assert "未返回可用音频" in describe(song, "file", {"kind": "unavailable"})
    degraded = describe(song, "file", {"kind": "card_degraded", "reason": "NapCat 上传失败"})
    assert "NapCat 上传失败" in degraded and "音乐卡片" in degraded, degraded


def test_llm_send_as_file_without_user_intent_falls_back() -> None:
    """LLM 自作主张传 file、但用户没提 → 回落配置默认形态。

    真机实锤（2026-09-12）：配置 tool_default_mode=voice，用户只说
    「放一首万能处方」，却因模型传了 send_as=file 收到了文件（而非语音）。
    """
    plugin, host = _prepare_plugin()  # tool_default_mode 默认 voice
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = True
    plugin._api = api
    plugin._cache = _FakeCache()
    plugin._qq_targets["s1"] = {"user_id": "3816023959"}

    result = asyncio.run(
        plugin.search_and_play_music(
            query="万能处方", send_as="file", stream_id="s1",
            processed_plain_text="放一首万能处方",
        )
    )

    assert result["content"].startswith("已播放"), f"应回落语音而非发文件: {result}"
    assert api.uploads == [], "用户没要文件时不该上传文件"
    customs = host.calls_of("send.custom")
    assert customs and customs[0].get("custom_type") == "voiceurl", customs


def test_user_file_intent_still_wins() -> None:
    """反向对照：用户明说要文件时，即使 LLM 没传 send_as 也要发文件。"""
    plugin, host = _prepare_plugin()
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = True
    plugin._api = api
    plugin._cache = _FakeCache()
    plugin._qq_targets["s1"] = {"user_id": "3816023959"}

    result = asyncio.run(
        plugin.search_and_play_music(
            query="万能处方", stream_id="s1",
            processed_plain_text="发一首万能处方的文件",
        )
    )

    assert result["content"].startswith("已发送文件"), result
    assert api.uploads, "用户明确要文件时必须上传"


def test_llm_send_as_file_kept_when_user_intent_present() -> None:
    """LLM 传 file 且用户确实要文件 → 保持一致，回落后不该误伤。"""
    plugin, host = _prepare_plugin()
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = True
    plugin._api = api
    plugin._cache = _FakeCache()
    plugin._qq_targets["s1"] = {"user_id": "3816023959"}

    result = asyncio.run(
        plugin.search_and_play_music(
            query="万能处方", send_as="file", stream_id="s1",
            processed_plain_text="要无损的，发个文件",
        )
    )

    assert result["content"].startswith("已发送文件"), result


def test_no_trigger_text_does_not_fall_back() -> None:
    """拿不到触发消息文本时不做回落判断（避免误伤命令/卡片解析路径）。"""
    plugin, host = _prepare_plugin()
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set())
    api.upload_ok = True
    plugin._api = api
    plugin._cache = _FakeCache()
    plugin._qq_targets["s1"] = {"user_id": "3816023959"}

    result = asyncio.run(
        plugin.search_and_play_music(query="万能处方", send_as="file", stream_id="s1")
    )

    assert result["content"].startswith("已发送文件"), f"无触发文本时应尊重 LLM 入参: {result}"


class _FakeNapCat:
    """假 NapCat HTTP 客户端：只记录调用。"""

    def __init__(self, payload=None) -> None:
        self.payload = payload or {"status": "ok"}
        self.calls: list[tuple[str, dict]] = []
        self.last_timeout = None

    async def post(self, path, json=None, timeout=None):
        self.calls.append((path, json or {}))
        self.last_timeout = timeout
        payload = self.payload

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return payload

        return _Resp()


def test_napcat_upload_file_group_payload() -> None:
    client = MusicSearchClient()
    napcat = _FakeNapCat()
    client._napcat = napcat

    ok, data = asyncio.run(
        client.napcat_upload_file("/app/music_cache/a.flac", name="歌.flac", group_id="123")
    )

    assert ok is True and data["status"] == "ok"
    path, payload = napcat.calls[0]
    assert path == "/upload_group_file"
    assert payload == {
        "group_id": 123, "file": "/app/music_cache/a.flac", "name": "歌.flac", "folder": "",
    }


def test_napcat_upload_file_private_uses_default_name() -> None:
    client = MusicSearchClient()
    napcat = _FakeNapCat()
    client._napcat = napcat

    asyncio.run(client.napcat_upload_file("/app/music_cache/a.flac", user_id="888"))

    path, payload = napcat.calls[0]
    assert path == "/upload_private_file"
    assert payload == {"user_id": 888, "file": "/app/music_cache/a.flac", "name": "a.flac"}


def test_napcat_upload_file_rejects_bad_input() -> None:
    """目标不是数字 / 未配置 NapCat / 缺目标，都必须返回 False 而不是抛异常。"""
    client = MusicSearchClient()
    assert asyncio.run(client.napcat_upload_file("/a.flac", group_id="abc"))[0] is False
    assert asyncio.run(client.napcat_upload_file("/a.flac"))[0] is False


def test_napcat_upload_file_reports_business_failure() -> None:
    client = MusicSearchClient()
    napcat = _FakeNapCat({"status": "failed", "retcode": 1200})
    client._napcat = napcat

    ok, _data = asyncio.run(client.napcat_upload_file("/a.flac", group_id="123"))

    assert ok is False


def test_napcat_upload_file_uses_extended_timeout() -> None:
    """上传动作必须用放宽后的超时（120s），不能用通用 10s。
    真机实锤（2026-09-11）：私聊文件上传 10s 处 ReadTimeout。"""
    client = MusicSearchClient(napcat_url="http://127.0.0.1:9999")
    napcat = _FakeNapCat()
    client._napcat = napcat

    ok, _data = asyncio.run(client.napcat_upload_file("/a.flac", user_id="888"))

    assert ok is True
    assert napcat.calls[0][0] == "/upload_private_file"
    assert napcat.last_timeout == client_module.NAPCAT_UPLOAD_TIMEOUT


def test_napcat_url_without_scheme_gets_http_prefix() -> None:
    """真机实锤（2026-09-11）：http_url 漏写 http:// 时 httpx 抛 UnsupportedProtocol，
    上传文件/直连发送全部失败。构造客户端时必须自动补全协议头。"""
    for raw, expected in (
        ("127.0.0.1:9999", "http://127.0.0.1:9999"),
        ("  127.0.0.1:9999  ", "http://127.0.0.1:9999"),
        ("127.0.0.1:9999/", "http://127.0.0.1:9999"),
        ("http://127.0.0.1:9999/", "http://127.0.0.1:9999"),
        ("https://napcat.example.com", "https://napcat.example.com"),
    ):
        client = MusicSearchClient(napcat_url=raw)
        assert client._napcat is not None, raw
        base = str(client._napcat.base_url).rstrip("/")
        assert base == expected, f"napcat_url={raw!r}: got {base!r}, want {expected!r}"
        asyncio.run(client.close())


def test_napcat_url_empty_disables_direct_client() -> None:
    """http_url 留空时不创建直连客户端（保持原语义）。"""
    client = MusicSearchClient(napcat_url="   ")
    assert client._napcat is None
