"""冒烟测试：不启动 MaiBot，用 FakeHost 跑完生命周期与三个入口。

运行: python tests/smoke_test.py

用离线替身（StubAPI）挡住网络：真实音乐接口在开发机上可能被墙/被代理拦，
让冒烟依赖外网就会变成"时好时坏"，失去回归价值。这里只验证插件的编排逻辑：
命令→搜索→选歌→发送、Tool 直发、Hook 解析与拦截、卸载清理。
"""

from __future__ import annotations

import asyncio
import hashlib
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_ID = "github.cateye.music-request"


class StubAPI:
    """MusicSearchClient 的离线替身：不发任何网络请求。"""

    def __init__(self, songs) -> None:
        self._songs = list(songs)
        self.closed = False
        self.uploads = []
        self.voices = []

    async def search(self, query, platform, limit=5):
        return self._songs[:limit]

    async def get_song_url(self, song_id, platform, media_id="", *, mp3_only=False):
        return "https://example.invalid/audio.mp3"

    async def get_qq_song_detail(self, song_mid):
        return None

    async def qq_music_card(self, song):
        return {
            "type": "custom",
            "url": f"https://y.qq.com/n/ryqq/songDetail/{song.song_id}",
            "audio": "",
            "title": song.name or "未知歌曲",
            "image": "https://y.qq.com/music/photo_new/T002R300x300M000x.jpg",
            "content": song.artists,
        }

    async def probe_audio(self, url, timeout=5.0):
        return False

    async def resolve_short_url(self, url):
        return None

    async def get_raw_message(self, message_id):
        return None

    async def napcat_send_message(self, message, *, group_id="", user_id=""):
        return False, {}

    async def napcat_upload_file(self, file_path, *, name="", group_id="", user_id="", timeout_seconds=None):
        self.uploads.append({"file_path": file_path, "name": name, "user_id": user_id})
        return True, {"status": "ok"}

    async def napcat_send_voice(self, file_path, *, group_id="", user_id=""):
        self.voices.append(file_path)
        return True, {"status": "ok"}

    async def close(self):
        self.closed = True


def main() -> int:
    try:
        import maibot_sdk  # noqa: F401
    except Exception:
        print("SKIP: 未安装 maibot-plugin-sdk，跳过冒烟测试（这不代表通过）")
        return 0

    from fakehost import (
        FakeHost,
        bind_context,
        build_context,
        get_default_config,
        load_plugin_module,
    )

    module = load_plugin_module(PLUGIN_DIR)
    plugin = module.create_plugin()
    host = FakeHost(plugin_id=PLUGIN_ID)
    ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
    # 默认配置是 voice_source=local，而冒烟不开本地缓存 → 会刷「缓存未就绪」告警。
    # 冒烟不测下载，改用 remote 让输出干净。
    config = get_default_config(getattr(type(plugin), "config_model", None))
    config["music"]["voice_source"] = "remote"
    bind_context(plugin, ctx, config)

    songs = [
        # 三首**同档相关**的候选（同曲不同版本）。
        # 注意：相关度过滤会把「与最佳匹配不在同一档」的候选剔掉，
        # 所以这里不能拿互不相关的假数据凑数——那样只会剩一首、直接发送，
        # 就走不到「列候选 → 选歌」这条流程了。
        module.SongInfo(
            song_id="1", name="测试歌曲", artists="测试歌手", album="测试专辑", platform="163"
        ),
        module.SongInfo(song_id="2", name="测试歌曲 (Live)", artists="测试歌手", platform="163"),
        module.SongInfo(song_id="3", name="测试歌曲 (Remix)", artists="测试歌手", platform="163"),
    ]
    # 明显不相关的干扰项：必须被过滤掉，不能被播出（真机「放错歌」的根因）
    noise = module.SongInfo(
        song_id="9", name="嘘嘘声+羊水声+胎心音+八音盒 星星糖果罐", artists="晚安宝贝", platform="163"
    )

    async def run() -> None:
        await plugin.on_load()
        assert plugin.config.plugin.enabled is True, "默认配置未生效"

        # 组件注册链路：唯一能在本地覆盖 Runner 注册的手段
        names = {item["name"] for item in plugin.get_components()}
        assert names == {
            "点歌", "选歌", "点歌状态", "点歌自检",
            "本地歌", "本地库",
            "search_and_play_music", "play_local_music", "parse_music_link",
        }, f"组件集合异常: {names}"

        plugin._api = StubAPI(songs)  # 挡掉网络

        # ── 命令：多首结果 → 列出候选 ──
        ok, _resp, level = await plugin.cmd_点歌(
            matched_groups={"pfx": "/", "query": "测试歌曲"}, stream_id="fake-stream"
        )
        assert ok is True and level == 2, f"点歌命令返回异常: {(ok, _resp, level)}"
        assert "选歌" in (host.sent_texts[-1] or ""), "点歌未列出候选"

        # ── 命令：选歌 → 发音乐卡片 ──
        ok, _resp, _level = await plugin.cmd_选歌(
            matched_groups={"pfx": "/", "index": "2"}, stream_id="fake-stream"
        )
        assert ok is True, f"选歌失败: {(ok, _resp)}"
        assert host.calls_of("send.custom"), "选歌未发送音乐卡片"

        # ── 命令：状态自检 ──
        ok, _resp, _level = await plugin.cmd_点歌状态(
            matched_groups={"pfx": "/"}, stream_id="fake-stream"
        )
        assert ok is True and "点歌插件 v" in (host.sent_texts[-1] or ""), "状态命令输出异常"

        # ── 命令：缓存目录自检 → 探针真的落盘且摘要与内容一致 ──
        ok, _resp, _level = await plugin.cmd_点歌自检(
            matched_groups={"pfx": "/"}, stream_id="fake-stream"
        )
        assert ok is True, "自检命令失败"
        report = host.sent_texts[-1] or ""
        assert "点歌缓存目录自检" in report, f"自检输出异常: {report}"
        probe_name = re.search(r"探针文件: (\S+)", report)
        probe_digest = re.search(r"内容摘要: ([0-9a-f]{8})", report)
        assert probe_name and probe_digest, f"自检输出缺少探针信息: {report}"
        storage_dir, _napcat_dir = plugin._cache_dirs()
        probe_file = pathlib.Path(storage_dir) / probe_name.group(1)
        assert probe_file.is_file(), f"探针文件没有落盘: {probe_file}"
        assert hashlib.md5(probe_file.read_bytes()).hexdigest()[:8] == probe_digest.group(1), (
            "自检报出的摘要与磁盘内容不符"
        )
        assert "md5sum" in report or "certutil" in report, "自检没有给出核对命令"

        # ── 命令：前缀不匹配 → 不处理也不拦截 ──
        ok, _resp, level = await plugin.cmd_点歌(
            matched_groups={"pfx": "#", "query": "x"}, stream_id="fake-stream"
        )
        assert ok is False and level == 0, "前缀不匹配时不应拦截消息"

        # ── Tool：直发最佳匹配 ──
        result = await plugin.search_and_play_music(query="测试歌曲", stream_id="fake-stream")
        assert isinstance(result, dict) and "已播放" in result.get("content", ""), (
            f"工具返回异常: {result}"
        )

        # ── 相关度过滤：混入无关干扰项时仍必须播最佳匹配 ──
        # （真机「放错歌」的根因就是干扰项被当成候选依次尝试）
        plugin._api = StubAPI([noise, songs[0]])
        result = await plugin.search_and_play_music(query="测试歌曲", stream_id="fake-stream")
        assert "测试歌曲" in result.get("content", ""), f"未播最佳匹配: {result}"
        assert "嘘嘘声" not in result.get("content", ""), f"播出了无关候选: {result}"

        # ── 本地歌曲库：状态、搜索列候选、选歌发文件、工具直发 ──
        # 库默认关闭；这里临时开一个指向临时目录的库验证端到端编排
        import tempfile

        tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="mr-smoke-local-lib"))
        lib_dir = tmp_root / "music"
        lib_dir.mkdir()
        (lib_dir / "夜曲.flac").write_bytes(b"local-a")
        (lib_dir / "夜曲 (Live).mp3").write_bytes(b"local-b")
        (lib_dir / "晴天.flac").write_bytes(b"local-c")

        config["library"]["enabled"] = True
        config["library"]["music_dir"] = str(lib_dir)
        config["library"]["send_mode"] = "file"
        plugin.set_plugin_config(config)
        stub = plugin._api

        ok, _resp, _level = await plugin.cmd_本地库(matched_groups={"pfx": "/"}, stream_id="fake-stream")
        assert ok is True and "3 首" in (host.sent_texts[-1] or ""), "本地库状态输出异常"

        # 多首命中 → 列候选（与平台点歌共用 /选歌 机制）
        ok, _resp, level = await plugin.cmd_本地歌(
            matched_groups={"pfx": "/", "query": "夜曲"},
            stream_id="fake-stream",
            message={"platform": "qq", "message_info": {"user_info": {"user_id": "10001"}}},
        )
        assert ok is True and "选歌" in (host.sent_texts[-1] or ""), "本地歌未列出候选"
        assert not stub.uploads, "列候选阶段不应发文件"

        ok, _resp, _level = await plugin.cmd_选歌(matched_groups={"pfx": "/", "index": "1"}, stream_id="fake-stream")
        assert ok is True and len(stub.uploads) == 1 and stub.uploads[0]["name"] == "夜曲.flac", (
            f"选歌未发本地文件: {stub.uploads}"
        )

        # 工具直发单首命中（send_as 留空 → 库配置 file 形态）
        result = await plugin.play_local_music(query="晴天", stream_id="fake-stream")
        assert result.get("content", "").startswith("已发送文件"), f"本地库工具返回异常: {result}"
        assert len(stub.uploads) == 2, stub.uploads

        plugin.set_plugin_config({**config, "library": {**config["library"], "enabled": False}})

        # ── Hook：音乐链接 → 发送并拦截 ──
        hook_result = await plugin.parse_music_link(
            message={
                "session_id": "fake-stream",
                "message_id": "1",
                "platform": "qq",
                "processed_plain_text": "https://music.163.com/song?id=12345",
            }
        )
        assert hook_result.get("action") == "abort", f"音乐链接未被拦截: {hook_result}"

        # ── Hook：普通文本 → 放行 ──
        hook_result = await plugin.parse_music_link(
            message={
                "session_id": "fake-stream",
                "message_id": "2",
                "processed_plain_text": "今天天气不错",
            }
        )
        assert hook_result.get("action") == "continue", f"普通消息被误拦: {hook_result}"

        # ── Hook：缺少 session_id → 放行且不抛异常 ──
        hook_result = await plugin.parse_music_link(message={"processed_plain_text": "x"})
        assert hook_result.get("action") == "continue"

        await plugin.on_unload()
        assert plugin._api is None, "卸载后未关闭 API 客户端"

    asyncio.run(run())
    print("smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
