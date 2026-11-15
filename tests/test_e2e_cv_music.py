# -*- coding: utf-8 -*-
"""cv_music 契约端到端测试（消费方视角，v1.8.0 联动加固）。

与 cv_lyric_context/tests/test_e2e_cv_music.py 互为镜像：那边验证
「真实 API → 真实消费方」的短路/回退判定，这边额外验证**完整点歌链路**
——`_search_ranked` 与 `search_and_play_music` 工具路径在真实对端 API
后端下的行为（假数据测试里这两条链路靠 sample_result，这里换真库）。

对端目录不存在时整文件 skip（单仓库 CI 可跑）；对端存在但导入失败则**报错**
——那是真实回归，不能静默跳过。全程零网络：搜索/发送/时长探测全被替换。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
_PEER_DIR = _PLUGIN_DIR.parent / "cv_lyric_context"

if not (_PEER_DIR / "plugin.py").is_file():
    pytest.skip(
        "对端插件 cv_lyric_context 不在本机（单仓库 CI 属正常）",
        allow_module_level=True,
    )

# 对端目录入 path 供包式加载；本插件目录**显式挪到最前**——pytest 可能已把
# 它放进 sys.path（去重插入会跳过），若对端目录抢先，`from plugin import ...`
# 会解析到对端的 plugin.py。
for _p in (str(_PEER_DIR), str(_TESTS_DIR), str(_PLUGIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
for _p in (str(_PLUGIN_DIR), str(_TESTS_DIR)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import plugin as plugin_module  # noqa: E402
from plugin import MusicRequestPlugin  # noqa: E402
from url_parser import PLATFORM_BILI  # noqa: E402
from contract_cv_music import validate_result  # noqa: E402

# 对端（提供方）：包式加载，辅助模块挂到 cv_lyric_context.* 命名空间，
# 避免与本插件的顶层模块重名（真机 Runner 也是包式加载）。
from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module  # noqa: E402

_CV_MOD = load_plugin_module(_PEER_DIR)
from vcpedia_store import SongStore  # noqa: E402  （对端目录已入 sys.path）

_MUSIC_PLUGIN_ID = "github.orge-8.music-request"
_CV_PLUGIN_ID = "org.mai-mai.cv-lyric-context"
_BILI_API = f"{_CV_PLUGIN_ID}.get_song_bilibili"


class _Ctx:
    def __init__(self) -> None:
        self.logger = logging.getLogger("test-e2e-music")


def _cv_plugin(tmp_path, records=None, *, started=True):
    """真实 cv 插件实例 + 临时曲库（不跑 on_load，零网络零写盘）。"""
    plug = _CV_MOD.create_plugin()
    plug.set_plugin_config({})
    plug._ctx = _Ctx()
    plug._store = SongStore(Path(tmp_path) / "e2e.db") if started else None
    for record in records or []:
        plug._store.upsert(record)
    return plug


class _RealApiBridge:
    """模拟 Host 的 api.call 转发：按全名路由到真实 cv API 方法。"""

    def __init__(self, cv_plug) -> None:
        self._cv = cv_plug
        self.calls: list[tuple[str, dict]] = []

    async def call(self, api_name: str, **kwargs):
        self.calls.append((api_name, kwargs))
        if api_name == _BILI_API:
            return await self._cv.api_get_song_bilibili(**kwargs)
        raise RuntimeError(f"未注册的 API: {api_name}")


def _music_plug(cv_plug):
    """真实 music 插件实例，ctx.api 接到真实 cv API 上；搜索/发送/时长全假。"""
    plug = MusicRequestPlugin()
    host = FakeHost(plugin_id=_MUSIC_PLUGIN_ID)
    ctx = build_context(_MUSIC_PLUGIN_ID, rpc_call=host.rpc_call)
    config = get_default_config(plugin_module.MusicRequestConfig)
    config["bilibili"]["vcpedia_lookup"] = True
    bind_context(plug, ctx, config)
    bridge = _RealApiBridge(cv_plug)
    ctx.api = bridge

    searches: list[str] = []
    sent: list = []

    async def fake_search(keyword: str, platform: str):
        searches.append(keyword)
        return []

    async def fake_send(song, stream_id, *, silent=False, mode=None, outcome=None,
                        upload_timeout_seconds=None):
        sent.append(song)
        if outcome is not None:
            outcome["kind"] = "file"
        return True

    async def fake_duration(_video_id):
        return 200  # 常规曲目时长，不触发直连时长闸；零网络

    plug._search = fake_search  # type: ignore[method-assign]
    plug._send_song = fake_send  # type: ignore[method-assign]
    plug._probe_bili_duration = fake_duration  # type: ignore[method-assign]
    return plug, bridge, searches, sent


# ---------- 真实链路：命令/搜索路径 ----------


def test_e2e_search_ranked_pins_original(tmp_path) -> None:
    """B站搜索路径：真实曲库命中 → 直连原曲，一次搜索都不发。"""
    cv = _cv_plugin(tmp_path, [{"name": "普通DISCO", "bilibili_aid": 2129461}])
    music, bridge, searches, _ = _music_plug(cv)
    songs = asyncio.run(music._search_ranked("普通DISCO", PLATFORM_BILI))
    assert len(songs) == 1
    assert songs[0].song_id == "av2129461"
    assert songs[0].platform == PLATFORM_BILI
    assert searches == []
    assert bridge.calls == [(_BILI_API, {"name": "普通DISCO"})]


def test_e2e_search_ranked_falls_back(tmp_path) -> None:
    """未收录 → 回退 B站搜索（真实 found=False 返回）。"""
    cv = _cv_plugin(tmp_path, [])
    music, _, searches, _ = _music_plug(cv)
    songs = asyncio.run(music._search_ranked("根本不存在的歌", PLATFORM_BILI))
    assert songs == []
    assert searches == ["根本不存在的歌"]


# ---------- 真实链路：工具路径 ----------


def test_e2e_tool_path_plays_original(tmp_path) -> None:
    """自然语言点歌：真实曲库命中 → 跳过整条平台链，直接发原曲。"""
    cv = _cv_plugin(tmp_path, [
        {"name": "世末歌者", "bilibili_bvid": "BV1Qs411k7Qv"},
    ])
    music, _, searches, sent = _music_plug(cv)
    out = asyncio.run(music.search_and_play_music(query="世末歌者", stream_id="s1"))
    assert searches == [], "命中真实曲库后不该再走平台链"
    assert len(sent) == 1
    assert sent[0].song_id == "BV1Qs411k7Qv"
    assert sent[0].platform == PLATFORM_BILI
    assert out["content"]


def test_e2e_tool_path_prefix_original_singer(tmp_path) -> None:
    """「世末积雨云 洛天依」：真实 singers 判定原曲歌手 → 采纳直连。"""
    cv = _cv_plugin(tmp_path, [
        {"name": "世末积雨云", "bilibili_aid": 1579079, "singers": "洛天依"},
    ])
    music, _, searches, sent = _music_plug(cv)
    asyncio.run(
        music.search_and_play_music(query="世末积雨云 洛天依", stream_id="s1")
    )
    assert searches == []
    assert sent and sent[0].song_id == "av1579079"


def test_e2e_tool_path_cover_singer_falls_back(tmp_path) -> None:
    """「普通DISCO 三无」+ 原话点名三无：真实 singers 不含三无 → 平台链。"""
    cv = _cv_plugin(tmp_path, [
        {"name": "普通DISCO", "bilibili_aid": 2129461, "singers": "洛天依、言和"},
    ])
    music, _, searches, sent = _music_plug(cv)
    asyncio.run(
        music.search_and_play_music(
            query="普通DISCO 三无", text="放普通DISCO 三无", stream_id="s1"
        )
    )
    assert sent == [], "点名翻唱者时不该播原曲"
    assert searches and searches[0] == "普通DISCO 三无"


def test_e2e_real_api_shape_along_the_way(tmp_path) -> None:
    """顺带全量校验真实 API 形状（与镜像文件同判据，防单侧漂移）。"""
    cv = _cv_plugin(tmp_path, [
        {"name": "普通DISCO", "bilibili_aid": 2129461},
        {"name": "旧歌"},
    ])
    for query in ("普通DISCO", "根本不存在的歌", "旧歌", "   "):
        validate_result(
            "get_song_bilibili", asyncio.run(cv.api_get_song_bilibili(name=query))
        )
