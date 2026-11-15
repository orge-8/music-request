# -*- coding: utf-8 -*-
"""VCPedia 联动失败五分类 + 内存计数 + /点歌联动自检（v1.8.0 P1-C）。

此前所有失败都只有一条 INFO 日志，真机排查分不清「插件没装」「超时」
还是「库里没这首歌」。现在按五分类计数（内存态，重启清零），自检命令
把可见性 / 探测 / 计数一次说清。

纪律不变：任何分类都只回退搜索，绝不打断点歌主链路。
"""

from __future__ import annotations

import asyncio

import pytest

import plugin as plugin_module
from plugin import MusicRequestPlugin, _classify_vcpedia_exception
from url_parser import PLATFORM_BILI

from contract_cv_music import sample_result, validate_result


def _hit(**overrides) -> dict:
    base = sample_result(
        "get_song_bilibili",
        found=True, name="普通DISCO", aid=2129461, bvid="", match="exact",
    )
    base.update(overrides)
    validate_result("get_song_bilibili", base)
    return base


class _FakeApi:
    def __init__(self, result=None, *, exc: Exception | None = None,
                 listing=None, list_exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.listing = listing
        self.list_exc = list_exc
        self.calls: list[tuple[str, dict]] = []
        self.list_calls: list[dict] = []

    async def call(self, api_name: str, **kwargs):
        self.calls.append((api_name, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result

    async def list(self, *, plugin_id: str = ""):
        self.list_calls.append({"plugin_id": plugin_id})
        if self.list_exc is not None:
            raise self.list_exc
        return self.listing


def _prepare(*, vcpedia_lookup: bool = True):
    from fakehost import FakeHost, bind_context, build_context, get_default_config

    plug = MusicRequestPlugin()
    host = FakeHost(plugin_id="github.orge-8.music-request")
    ctx = build_context("github.orge-8.music-request", rpc_call=host.rpc_call)
    config = get_default_config(plugin_module.MusicRequestConfig)
    config["bilibili"]["vcpedia_lookup"] = vcpedia_lookup
    bind_context(plug, ctx, config)

    searches: list[str] = []

    async def fake_search(keyword: str, platform: str):
        searches.append(keyword)
        return []

    async def fake_duration(_video_id):
        return 200

    plug._search = fake_search  # type: ignore[method-assign]
    plug._probe_bili_duration = fake_duration  # type: ignore[method-assign]
    return plug, ctx, searches


# ---------- 异常分类助手 ----------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AttributeError("api"), "no_api_proxy"),
        (asyncio.TimeoutError(), "timeout"),
        (RuntimeError("插件未安装或未启用"), "not_installed"),
        (RuntimeError("api not found: xxx"), "not_installed"),
        (ValueError("响应格式不对"), "error"),
    ],
)
def test_classify_exception(exc: Exception, expected: str) -> None:
    assert _classify_vcpedia_exception(exc) == expected


# ---------- 查询结果分类与计数 ----------


def test_hit_counts_hit() -> None:
    plug, ctx, _ = _prepare()
    ctx.api = _FakeApi(_hit())
    song = asyncio.run(plug._vcpedia_pinned_song("普通DISCO"))
    assert song is not None
    assert plug._vcpedia_stats["hit"] == 1
    assert sum(plug._vcpedia_stats.values()) == 1


def test_not_found_counts_not_found_or_no_bvid() -> None:
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(sample_result("get_song_bilibili", found=False, reason="曲库里没有这首歌"))
    songs = asyncio.run(plug._search_ranked("不存在的歌", PLATFORM_BILI))
    assert songs == []
    assert searches == ["不存在的歌"]
    assert plug._vcpedia_stats["not_found_or_no_bvid"] == 1


def test_found_without_id_counts_not_found_or_no_bvid() -> None:
    plug, ctx, _ = _prepare()
    ctx.api = _FakeApi(_hit(name="旧歌", aid=None, reason="该曲目尚未采集 B站标识"))
    assert asyncio.run(plug._vcpedia_pinned_song("旧歌")) is None
    assert plug._vcpedia_stats["not_found_or_no_bvid"] == 1


def test_timeout_counts_timeout() -> None:
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi(exc=asyncio.TimeoutError())
    songs = asyncio.run(plug._search_ranked("普通DISCO", PLATFORM_BILI))
    assert songs == []
    assert searches == ["普通DISCO"]
    assert plug._vcpedia_stats["timeout"] == 1


def test_not_installed_counts_not_installed() -> None:
    plug, ctx, _ = _prepare()
    ctx.api = _FakeApi(exc=RuntimeError("插件未安装或未启用"))
    assert asyncio.run(plug._vcpedia_pinned_song("普通DISCO")) is None
    assert plug._vcpedia_stats["not_installed"] == 1


def test_no_api_proxy_counts_no_api_proxy() -> None:
    """ctx.api 代理整个不存在（老 Host）：AttributeError 也要计数。"""
    plug, ctx, _ = _prepare()
    # build_context 的真实 PluginContext 带 api 代理；删掉模拟老 Host
    del ctx.api
    assert asyncio.run(plug._vcpedia_pinned_song("普通DISCO")) is None
    assert plug._vcpedia_stats["no_api_proxy"] == 1


def test_garbage_result_counts_schema_mismatch() -> None:
    plug, ctx, searches = _prepare()
    ctx.api = _FakeApi({"status": "ok", "data": {}})
    songs = asyncio.run(plug._search_ranked("普通DISCO", PLATFORM_BILI))
    assert songs == []
    # 空候选可能触发「按歌名重搜」，只检查首条查询
    assert searches and searches[0] == "普通DISCO"
    assert plug._vcpedia_stats["schema_mismatch"] == 1


def test_future_schema_version_rejected() -> None:
    """G4 版本协商：schema_version=2 是未来不兼容契约，不能按 v1 误读。

    故意构造契约外输入（契约只定义 v1），不能被 sample_result 补齐。
    """
    plug, ctx, searches = _prepare()
    future = _hit()
    future["schema_version"] = 2
    ctx.api = _FakeApi(future)
    songs = asyncio.run(plug._search_ranked("普通DISCO", PLATFORM_BILI))
    assert songs == []
    assert searches and searches[0] == "普通DISCO"
    assert plug._vcpedia_stats["schema_mismatch"] == 1
    assert plug._vcpedia_stats["hit"] == 0


def test_missing_schema_version_tolerated_as_v1() -> None:
    """兼容未升级的提供方：缺 schema_version 键按 v1 宽容处理，照常采纳。"""
    plug, ctx, _ = _prepare()
    legacy = _hit()
    del legacy["schema_version"]
    ctx.api = _FakeApi(legacy)
    song = asyncio.run(plug._vcpedia_pinned_song("普通DISCO"))
    assert song is not None
    assert plug._vcpedia_stats["hit"] == 1
    assert plug._vcpedia_stats["schema_mismatch"] == 0


def test_other_error_counts_error() -> None:
    plug, ctx, _ = _prepare()
    ctx.api = _FakeApi(exc=ValueError("响应格式不对"))
    assert asyncio.run(plug._vcpedia_pinned_song("普通DISCO")) is None
    assert plug._vcpedia_stats["error"] == 1


def test_disabled_or_blank_does_not_count() -> None:
    """没发起查询（开关关 / 空关键词）不计数——计数只反映真实 RPC。"""
    plug, ctx, _ = _prepare(vcpedia_lookup=False)
    ctx.api = _FakeApi(_hit())
    assert asyncio.run(plug._vcpedia_pinned_song("普通DISCO")) is None
    assert sum(plug._vcpedia_stats.values()) == 0

    plug2, ctx2, _ = _prepare()
    ctx2.api = _FakeApi(_hit())
    assert asyncio.run(plug2._vcpedia_pinned_song("   ")) is None
    assert sum(plug2._vcpedia_stats.values()) == 0


def test_counters_accumulate_across_calls() -> None:
    plug, ctx, _ = _prepare()
    api = _FakeApi(_hit())
    ctx.api = api
    for _ in range(3):
        asyncio.run(plug._vcpedia_pinned_song("普通DISCO"))
    api.exc = asyncio.TimeoutError()
    asyncio.run(plug._vcpedia_pinned_song("普通DISCO"))
    assert plug._vcpedia_stats["hit"] == 3
    assert plug._vcpedia_stats["timeout"] == 1


# ---------- /点歌联动自检 命令 ----------


def _run_check(plug, ctx):
    return asyncio.run(
        plug.cmd_点歌联动自检(matched_groups={"pfx": "/"}, stream_id="s1")
    )


def test_check_command_reports_visible_api() -> None:
    plug, ctx, _ = _prepare()
    entry = {
        "name": "get_song_bilibili",
        "full_name": "org.mai-mai.cv-lyric-context.get_song_bilibili",
        "plugin_id": "org.mai-mai.cv-lyric-context",
        "version": "1",
        "public": True,
        "enabled": True,
        "metadata": {"timeout_ms": 3000},
    }
    ctx.api = _FakeApi(
        sample_result("get_song_bilibili", found=False, reason="曲库里没有这首歌"),
        listing=[entry],
    )
    ok, text, level = _run_check(plug, ctx)
    assert ok is True and level == 2
    assert "✅ 可见" in text
    assert "timeout_ms=3000" in text
    assert "not_found_or_no_bvid" in text and "链路正常" in text
    assert "运行计数" in text
    # 探测不污染运行计数
    assert sum(plug._vcpedia_stats.values()) == 0


def test_check_command_reports_missing_provider() -> None:
    plug, ctx, _ = _prepare()
    ctx.api = _FakeApi(
        sample_result("get_song_bilibili", found=False, reason="曲库里没有这首歌"),
        listing=[],
    )
    ok, text, _ = _run_check(plug, ctx)
    assert ok is True
    assert "❌ 不可见" in text


def test_check_command_survives_old_host() -> None:
    """无 ctx.api 代理：自检照常输出，不抛异常。"""
    plug, ctx, _ = _prepare()
    del ctx.api
    ok, text, _ = _run_check(plug, ctx)
    assert ok is True
    assert "老版本" in text or "无 ctx.api" in text


def test_check_command_skips_probe_when_disabled() -> None:
    plug, ctx, _ = _prepare(vcpedia_lookup=False)
    api = _FakeApi(_hit(), listing=[])
    ctx.api = api
    ok, text, _ = _run_check(plug, ctx)
    assert ok is True
    assert "跳过" in text
    assert api.calls == [], "开关关闭时自检也不该发起查询"


def test_check_command_prefix_guard() -> None:
    """前缀不匹配 → 不处理不拦截（与本插件其它命令同一纪律）。"""
    plug, ctx, _ = _prepare()
    ctx.api = _FakeApi(_hit())
    ok, text, level = asyncio.run(
        plug.cmd_点歌联动自检(matched_groups={"pfx": "。"}, stream_id="s1")
    )
    assert ok is False and text == "" and level == 0


def test_check_command_pattern_matches() -> None:
    import re

    info = getattr(MusicRequestPlugin.cmd_点歌联动自检, "__maibot_component_info__")
    pattern = info.command_pattern
    for text in ("/点歌联动自检", "／点歌联动自检", "/点歌联动自检  "):
        assert re.match(pattern, text), text
    for text in ("/点歌自检", "/点歌联动", "/点歌联动自检x", "/点歌 普通DISCO"):
        assert not re.match(pattern, text), text
