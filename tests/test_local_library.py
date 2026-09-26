"""本地歌曲库（v1.5.0）的模块级与插件级测试。

覆盖三层：
    - LocalSongLibrary 纯逻辑：扫描、打分搜索、NapCat 路径映射、失效刷新
    - 插件编排：/本地歌 与 /选歌 串通（platform=local 路由）、/本地库 状态
    - @Tool play_local_music：形态决策（file 默认 / voice 覆写）与失败口径
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from local_library import LocalSongLibrary, LocalTrack, PLATFORM_LOCAL


def make_library(tmp_path: pathlib.Path, napcat_dir: str = "", refresh_minutes: int = 0):
    music_dir = tmp_path / "music"
    music_dir.mkdir(exist_ok=True)
    return LocalSongLibrary(
        str(music_dir), napcat_dir, refresh_minutes=refresh_minutes
    ), music_dir


def make_files(music_dir: pathlib.Path, names: list[str]) -> None:
    for name in names:
        path = music_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-audio-" + name.encode())


# ---------- 纯逻辑：扫描 ----------


def test_scan_recursive_and_extension_whitelist(tmp_path: pathlib.Path) -> None:
    """递归扫描 + 扩展名白名单：歌词/文本/可执行文件不入库。"""
    lib, music_dir = make_library(tmp_path)
    make_files(music_dir, ["夜曲.flac", "sub/晴天.mp3", "deep/deeper/以父之名.m4a", "说明.txt", "坏东西.exe"])

    lib.rescan()
    names = sorted(t.name for t in lib._tracks)
    assert names == ["以父之名", "夜曲", "晴天"], names
    assert all(t.suffix in {".flac", ".mp3", ".m4a"} for t in lib._tracks)


def test_scan_error_keeps_old_index(tmp_path: pathlib.Path) -> None:
    """目录消失时重扫失败必须保留旧索引并留痕，不能把库里清成 0。"""
    lib, music_dir = make_library(tmp_path)
    make_files(music_dir, ["夜曲.flac"])
    lib.rescan()
    assert len(lib._tracks) == 1

    import shutil
    shutil.rmtree(music_dir)
    count = lib.rescan()
    assert count == 1, "重扫失败不应清空旧索引"
    assert lib.scan_error, "失败原因未记录"


def test_refresh_zero_disables_auto_rescan(tmp_path: pathlib.Path) -> None:
    """refresh_minutes=0：索引只在显式 rescan 时更新。"""
    lib, music_dir = make_library(tmp_path, refresh_minutes=0)
    make_files(music_dir, ["夜曲.flac"])
    lib.rescan()

    make_files(music_dir, ["晴天.mp3"])
    lib.ensure_index()
    assert len(lib._tracks) == 1, "自动刷新应被禁用"
    lib.rescan()
    assert len(lib._tracks) == 2


# ---------- 纯逻辑：搜索 ----------


def test_search_exact_beats_variant(tmp_path: pathlib.Path) -> None:
    """整串命中 > 变体：搜「夜曲」时原版排在 Live 前面。"""
    lib, music_dir = make_library(tmp_path)
    make_files(music_dir, ["夜曲 (Live).mp3", "夜曲.flac", "晴天.mp3"])

    tracks = lib.search("夜曲")
    assert [t.name for t in tracks] == ["夜曲", "夜曲 (Live)"], [t.name for t in tracks]


def test_search_tokens_match_in_any_order(tmp_path: pathlib.Path) -> None:
    """「歌手 歌名」与「歌名 歌手」都能命中：token 分词不区分顺序。"""
    lib, music_dir = make_library(tmp_path)
    make_files(music_dir, ["周杰伦-夜曲.flac", "晴天.mp3"])

    tracks = lib.search("夜曲 周杰伦")
    assert tracks and tracks[0].name == "周杰伦-夜曲", [t.name for t in tracks]
    tracks = lib.search("周杰伦 夜曲")
    assert tracks and tracks[0].name == "周杰伦-夜曲"


def test_search_no_match_and_empty(tmp_path: pathlib.Path) -> None:
    lib, music_dir = make_library(tmp_path)
    make_files(music_dir, ["夜曲.flac"])

    assert lib.search("晴天") == []
    assert lib.search("") == []
    assert lib.search("   ") == []


def test_search_limit(tmp_path: pathlib.Path) -> None:
    lib, music_dir = make_library(tmp_path)
    make_files(music_dir, [f"夜曲 {i}.mp3" for i in range(5)] + ["夜曲.flac"])

    assert len(lib.search("夜曲", limit=3)) == 3
    assert len(lib.search("夜曲")) == 6


# ---------- 纯逻辑：NapCat 路径映射 ----------


def test_napcat_mapping_posix_root(tmp_path: pathlib.Path) -> None:
    """Docker 场景：宿主路径映射成容器内 POSIX 路径。"""
    lib, music_dir = make_library(tmp_path, napcat_dir="/music")
    make_files(music_dir, ["sub/夜曲.flac"])
    track = lib.search("夜曲")[0]

    napcat_path, mapped = lib.napcat_path_checked(track.path)
    assert mapped is True
    assert napcat_path.startswith("/music/sub/"), napcat_path
    assert napcat_path.endswith(".flac")


def test_napcat_mapping_same_dir(tmp_path: pathlib.Path) -> None:
    lib, music_dir = make_library(tmp_path)
    make_files(music_dir, ["夜曲.flac"])
    track = lib.search("夜曲")[0]

    napcat_path, mapped = lib.napcat_path_checked(track.path)
    assert mapped is True
    assert pathlib.Path(napcat_path) == track.path


def test_napcat_mapping_outside_root_fails(tmp_path: pathlib.Path) -> None:
    """路径不在歌曲目录下时必须报映射失败，不能静默交宿主路径。"""
    lib, music_dir = make_library(tmp_path, napcat_dir="/music")
    outside = tmp_path / "elsewhere.flac"
    outside.write_bytes(b"x")

    _napcat_path, mapped = lib.napcat_path_checked(outside)
    assert mapped is False


# ---------- 插件编排 ----------


class _LocalStubAPI:
    """挡网络的 MusicSearchClient 替身：记录文件上传与语音发送。"""

    def __init__(self, *, upload_ok: bool = True, voice_ok: bool = True) -> None:
        self.uploads: list[dict] = []
        self.voices: list[str] = []
        self.upload_ok = upload_ok
        self.voice_ok = voice_ok

    async def napcat_upload_file(self, file_path, *, name="", group_id="", user_id=""):
        self.uploads.append({"file_path": file_path, "name": name, "group_id": group_id, "user_id": user_id})
        return self.upload_ok, {"status": "ok"}

    async def napcat_send_voice(self, file_path, *, group_id="", user_id=""):
        self.voices.append(file_path)
        return self.voice_ok, {"status": "ok"}

    async def close(self):
        return None


def _qq_message(user_id: str = "10001") -> dict:
    """让 _remember_qq_target 记下私聊目标的最小消息载荷。"""
    return {
        "platform": "qq",
        "message_info": {"user_info": {"user_id": user_id}},
    }


def _prepare_plugin_with_library(tmp_path: pathlib.Path, *, enabled: bool = True, send_mode: str = "file"):
    from fakehost import FakeHost, bind_context, build_context, get_default_config

    import plugin as plugin_module

    music_dir = tmp_path / "music"
    music_dir.mkdir(exist_ok=True)
    plugin = plugin_module.MusicRequestPlugin()
    host = FakeHost(plugin_id="github.cateye.music-request")
    ctx = build_context("github.cateye.music-request", rpc_call=host.rpc_call)
    config = get_default_config(plugin_module.MusicRequestConfig)
    config["music"]["play_mode"] = "card"  # 库发送形态不受全局 play_mode 影响
    config["music"]["voice_source"] = "remote"
    config["library"]["enabled"] = enabled
    config["library"]["music_dir"] = str(music_dir)
    config["library"]["napcat_dir"] = ""
    config["library"]["send_mode"] = send_mode
    config["library"]["refresh_minutes"] = 0
    bind_context(plugin, ctx, config)
    return plugin, host, music_dir


def test_cmd_local_single_match_sends_file(tmp_path: pathlib.Path) -> None:
    """单首命中直发文件：上传名带原扩展名，路径已映射。"""
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path)
    make_files(music_dir, ["夜曲.flac", "晴天.mp3"])
    stub = _LocalStubAPI()
    plugin._api = stub

    ok, resp, level = asyncio.run(
        plugin.cmd_本地歌(
            matched_groups={"pfx": "/", "query": "夜曲"},
            stream_id="s1",
            message=_qq_message(),
        )
    )

    assert ok is True and level == 2, (ok, resp, level)
    assert len(stub.uploads) == 1, stub.uploads
    upload = stub.uploads[0]
    assert upload["name"] == "夜曲.flac", upload
    assert upload["user_id"] == "10001", upload
    assert pathlib.Path(upload["file_path"]) == (music_dir / "夜曲.flac").resolve()
    assert not host.calls_of("send.custom"), "本地歌不应走平台卡片通道"


def test_cmd_local_multiple_lists_candidates_and_select(tmp_path: pathlib.Path) -> None:
    """多首命中列候选；/选歌 复用同一套待选机制发本地文件。"""
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path)
    make_files(music_dir, ["夜曲.flac", "夜曲 (Live).mp3"])
    stub = _LocalStubAPI()
    plugin._api = stub

    ok, _resp, _level = asyncio.run(
        plugin.cmd_本地歌(
            matched_groups={"pfx": "/", "query": "夜曲"},
            stream_id="s1",
            message=_qq_message(),
        )
    )
    assert ok is True
    assert "选歌" in (host.sent_texts[-1] or ""), "未列出候选"
    assert not stub.uploads, "列候选阶段不该发文件"

    ok, _resp, _level = asyncio.run(
        plugin.cmd_选歌(matched_groups={"pfx": "/", "index": "2"}, stream_id="s1")
    )
    assert ok is True, "选歌失败"
    assert len(stub.uploads) == 1 and stub.uploads[0]["name"] == "夜曲 (Live).mp3", stub.uploads


def test_cmd_local_no_match_and_disabled(tmp_path: pathlib.Path) -> None:
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path)
    make_files(music_dir, ["夜曲.flac"])
    stub = _LocalStubAPI()
    plugin._api = stub

    ok, _resp, _level = asyncio.run(
        plugin.cmd_本地歌(matched_groups={"pfx": "/", "query": "晴天"}, stream_id="s1")
    )
    assert ok is False and not stub.uploads

    # 未启用：命令如实提示，不抛异常
    plugin2, host2, _dir2 = _prepare_plugin_with_library(tmp_path, enabled=False)
    ok, resp, level = asyncio.run(
        plugin2.cmd_本地歌(matched_groups={"pfx": "/", "query": "夜曲"}, stream_id="s2")
    )
    assert ok is False and "未启用" in resp, (ok, resp)
    ok, resp, _level = asyncio.run(
        plugin2.cmd_本地库(matched_groups={"pfx": "/"}, stream_id="s2")
    )
    assert ok is True and "未启用" in resp, (ok, resp)


def test_cmd_local_missing_napcat_target(tmp_path: pathlib.Path) -> None:
    """拿不到 QQ 直连目标时必须报可操作的原因，而不是静默失败。"""
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path)
    make_files(music_dir, ["夜曲.flac"])
    plugin._api = _LocalStubAPI()

    ok, _resp, _level = asyncio.run(
        plugin.cmd_本地歌(matched_groups={"pfx": "/", "query": "夜曲"}, stream_id="s1")
    )
    assert ok is False
    text = host.sent_texts[-1] or ""
    assert "NapCat" in text, text


def test_cmd_local_upload_failure_reports(tmp_path: pathlib.Path) -> None:
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path)
    make_files(music_dir, ["夜曲.flac"])
    plugin._api = _LocalStubAPI(upload_ok=False)

    ok, _resp, _level = asyncio.run(
        plugin.cmd_本地歌(
            matched_groups={"pfx": "/", "query": "夜曲"},
            stream_id="s1",
            message=_qq_message(),
        )
    )
    assert ok is False
    text = host.sent_texts[-1] or ""
    assert "上传失败" in text, text


def test_cmd_local_status_and_refresh(tmp_path: pathlib.Path) -> None:
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path)
    make_files(music_dir, ["夜曲.flac", "晴天.mp3"])

    ok, resp, _level = asyncio.run(
        plugin.cmd_本地库(matched_groups={"pfx": "/"}, stream_id="s1")
    )
    assert ok is True
    text = host.sent_texts[-1] or ""
    assert "本地歌曲库状态" in text and "2 首" in text, text
    assert str(music_dir) in text, text

    make_files(music_dir, ["以父之名.flac"])
    ok, _resp, _level = asyncio.run(
        plugin.cmd_本地库(matched_groups={"pfx": "/", "action": "刷新"}, stream_id="s1")
    )
    text = host.sent_texts[-1] or ""
    assert ok is True and "3 首" in text, text


def test_cmd_local_status_line_in_overall_status(tmp_path: pathlib.Path) -> None:
    """/点歌状态 里必须能看到本地库一行。"""
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path)
    make_files(music_dir, ["夜曲.flac"])

    ok, _resp, _level = asyncio.run(
        plugin.cmd_点歌状态(matched_groups={"pfx": "/"}, stream_id="s1")
    )
    text = host.sent_texts[-1] or ""
    assert ok is True and "本地歌曲库" in text and "1 首" in text, text


# ---------- @Tool play_local_music ----------


def test_tool_local_default_uses_library_send_mode(tmp_path: pathlib.Path) -> None:
    """send_as 留空时按库配置形态（file）发送，返回文本必须与实际一致。"""
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path, send_mode="file")
    make_files(music_dir, ["夜曲.flac"])
    stub = _LocalStubAPI()
    plugin._api = stub

    result = asyncio.run(
        plugin.play_local_music(query="夜曲", stream_id="s1", message=_qq_message())
    )

    assert result["content"].startswith("已发送文件"), result
    assert len(stub.uploads) == 1
    assert not stub.voices, "file 形态不应发语音"


def test_tool_local_send_as_voice_overrides(tmp_path: pathlib.Path) -> None:
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path, send_mode="file")
    make_files(music_dir, ["夜曲.flac"])
    stub = _LocalStubAPI()
    plugin._api = stub

    result = asyncio.run(
        plugin.play_local_music(query="夜曲", send_as="voice", stream_id="s1", message=_qq_message())
    )

    assert result["content"].startswith("已播放本地歌曲"), result
    assert len(stub.voices) == 1 and not stub.uploads, (stub.voices, stub.uploads)


def test_tool_local_no_result_and_disabled(tmp_path: pathlib.Path) -> None:
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path)
    make_files(music_dir, ["夜曲.flac"])

    result = asyncio.run(plugin.play_local_music(query="晴天", stream_id="s1"))
    assert "没有找到" in result["content"], result
    assert "不要重复调用" in result["content"], result

    plugin2, _host2, _dir2 = _prepare_plugin_with_library(tmp_path, enabled=False)
    result = asyncio.run(plugin2.play_local_music(query="夜曲", stream_id="s2"))
    assert "未启用" in result["content"], result


def test_tool_local_voice_failure_honest_report(tmp_path: pathlib.Path) -> None:
    plugin, host, music_dir = _prepare_plugin_with_library(tmp_path, send_mode="voice")
    make_files(music_dir, ["夜曲.flac"])
    plugin._api = _LocalStubAPI(voice_ok=False)

    result = asyncio.run(
        plugin.play_local_music(query="夜曲", stream_id="s1", message=_qq_message())
    )

    assert "发送失败" in result["content"], result
    assert "不要重复调用" in result["content"], result


# ---------- music_api.napcat_send_voice（真实 CQ 构造） ----------


def test_napcat_send_voice_builds_record_uri() -> None:
    """真实客户端的 CQ 构造：Windows/POSIX 路径都要转成合法 file:// URI。"""
    import httpx

    from music_api import MusicSearchClient

    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"path": request.url.path, "json": request.read()})
        return httpx.Response(200, json={"status": "ok", "retcode": 0})

    api = MusicSearchClient()
    api._napcat = httpx.AsyncClient(
        base_url="http://127.0.0.1:9999", transport=httpx.MockTransport(handler)
    )

    try:
        for file_path, expect_uri in [
            (r"C:\music\夜曲.flac", "file:///C:/music/夜曲.flac"),
            ("/music/夜曲.flac", "file:///music/夜曲.flac"),
        ]:
            ok, _data = asyncio.run(api.napcat_send_voice(file_path, user_id="10001"))
            assert ok is True, (file_path, ok)
            assert captured[-1]["path"] == "/send_private_msg"
            body = captured[-1]["json"].decode("utf-8")
            assert expect_uri in body, (file_path, body)
    finally:
        asyncio.run(api._napcat.aclose())


def test_local_track_display_and_conversion() -> None:
    track = LocalTrack(path=pathlib.Path("D:/music/夜曲.flac"), name="夜曲", suffix=".flac", size_bytes=32 * 1024 * 1024)
    assert "32.0MB" in track.display()

    from plugin import SongInfo, track_to_song_info_fields

    fields = track_to_song_info_fields(track)
    song = SongInfo(**fields)
    assert song.platform == PLATFORM_LOCAL
    assert song.song_id == str(pathlib.Path("D:/music/夜曲.flac"))  # str(Path) 随平台分隔符
    assert pathlib.Path(song.song_id) == pathlib.Path("D:/music/夜曲.flac")
    assert song.display() == "夜曲"
