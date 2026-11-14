"""入站解析作用域回归：`auto_parse_card=off` 时不得借「纯链接」通道解析卡片。

真机报障（2026-11-04，SnowLuma 适配器接 NapCat 客户端）：
关闭「自动解析音乐卡片」、开启「自动解析音乐链接」后，群里分享的音乐卡片
仍被解析并发歌。

根因：MaiBot 统一 QQ 连接器（MaiBot-SnowLuma-Adapter，NapCat / SnowLuma
两类画像共用同一套 inbound codec）在 `codecs/inbound/cards.py::
_build_music_card_segments` 里把卡片跳转链接拼回正文：

    [<tag>] <title> - <singer> 链接: <jumpUrl>

于是 `processed_plain_text` 同时带着卡片标记与卡片自带链接，纯链接分支
对整段跑 `extract_urls` 就把卡片链接当成用户手打的链接解析了——开关形同虚设。

断言取向：每一条都能反向验证。把 `strip_card_injected_text` 从纯链接分支摘掉，
前两条必然失败。
"""

from __future__ import annotations

import asyncio

import pytest

import plugin as plugin_module
from plugin import MusicRequestPlugin
from url_parser import parse_music_url

#: 适配器对 QQ 音乐卡片的真实渲染形态（jumpUrl 拼在「链接: 」之后）
_QQ_CARD_TEXT = (
    "[QQ音乐] 小城夏天 - LBI利比 "
    "链接: https://i.y.qq.com/v8/playsong.html?songmid=003QWE&from=card"
)

#: 网易云卡片的渲染形态（tag 由卡片 meta.tag 决定，可能是「音乐分享」）
_NETEASE_CARD_TEXT = (
    "[音乐分享] 我的悲伤是水做的 - ChiliChill "
    "链接: https://y.music.163.com/m/song?id=987654"
)

#: 用户手打的音乐链接
_USER_LINK_TEXT = "这首歌好听 https://music.163.com/song?id=12345"


def _plugin_with_stubs(*, auto_parse_card: bool, auto_parse_url: bool = True):
    """构造带 FakeHost 上下文的插件，并把「取 ref / 发送」换成可观测桩。

    只桩两处：`_ref_from_url`（免联网、记录被解析的链接）与 `_send_by_ref`
    （记录实际发歌）。判定逻辑本身走真实实现。
    """
    from fakehost import FakeHost, bind_context, build_context, get_default_config

    plugin = MusicRequestPlugin()
    host = FakeHost(plugin_id="github.orge-8.music-request")
    ctx = build_context("github.orge-8.music-request", rpc_call=host.rpc_call)
    config = get_default_config(plugin_module.MusicRequestConfig)
    config["music"]["auto_parse_card"] = auto_parse_card
    config["music"]["auto_parse_url"] = auto_parse_url
    bind_context(plugin, ctx, config)

    resolved: list[str] = []
    sent: list[tuple[str, str]] = []

    async def _fake_ref_from_url(url: str):
        resolved.append(url)
        return parse_music_url(url)

    async def _fake_send_by_ref(ref, stream_id: str, *, card=None) -> bool:
        sent.append((ref.platform, ref.song_id))
        return True

    plugin._ref_from_url = _fake_ref_from_url  # type: ignore[method-assign]
    plugin._send_by_ref = _fake_send_by_ref  # type: ignore[method-assign]
    return plugin, resolved, sent


def _run_hook(plugin: MusicRequestPlugin, text: str):
    return asyncio.run(
        plugin.parse_music_link(
            message={
                "session_id": "fake-stream",
                "platform": "qq",
                "processed_plain_text": text,
            }
        )
    )


@pytest.mark.parametrize("text", [_QQ_CARD_TEXT, _NETEASE_CARD_TEXT])
def test_card_is_not_parsed_when_card_parsing_disabled(text: str) -> None:
    """报障主场景：卡片解析关闭 + 链接解析开启，卡片不得被解析。"""
    plugin, resolved, sent = _plugin_with_stubs(auto_parse_card=False)

    result = _run_hook(plugin, text)

    assert result.get("action") == "continue", f"关闭卡片解析后仍拦下了卡片: {result}"
    assert resolved == [], f"卡片内附链接被当成用户链接解析了: {resolved}"
    assert sent == [], f"关闭卡片解析后仍发出了歌曲: {sent}"


def test_user_link_still_parsed_when_card_parsing_disabled() -> None:
    """正向对照：同一开关组合下，用户手打的音乐链接必须照旧解析。"""
    plugin, resolved, sent = _plugin_with_stubs(auto_parse_card=False)

    result = _run_hook(plugin, _USER_LINK_TEXT)

    assert result.get("action") == "abort", f"用户链接未被解析: {result}"
    assert sent == [("163", "12345")], sent


def test_card_still_parsed_when_card_parsing_enabled() -> None:
    """反向对照：开关打开时卡片必须照旧解析（回归护栏，防修过头）。"""
    plugin, resolved, sent = _plugin_with_stubs(auto_parse_card=True)

    result = _run_hook(plugin, _QQ_CARD_TEXT)

    assert result.get("action") == "abort", f"开启卡片解析后卡片未被处理: {result}"
    assert sent == [("qq", "003QWE")], sent


def test_card_link_and_user_link_mixed_keeps_user_link() -> None:
    """用户正文在前、卡片在后：只认正文里的链接，卡片内附链接仍被丢掉。"""
    plugin, resolved, sent = _plugin_with_stubs(auto_parse_card=False)

    result = _run_hook(plugin, f"听听这个 https://music.163.com/song?id=555 {_QQ_CARD_TEXT}")

    assert result.get("action") == "abort", result
    assert sent == [("163", "555")], f"解析到了卡片链接而不是用户链接: {sent}"
    assert all("y.qq.com" not in url for url in resolved), f"卡片链接仍被解析: {resolved}"


def test_cq_music_code_not_parsed_when_card_parsing_disabled() -> None:
    """CQ 码形态（自定义音乐卡片）里的 url= 参数同样属于卡片载荷。"""
    plugin, resolved, sent = _plugin_with_stubs(auto_parse_card=False)
    text = (
        "[CQ:music,type=custom,url=https://i.y.qq.com/v8/playsong.html?songmid=009ZXC,"
        "audio=https://example.invalid/a.mp3,title=小城夏天]"
    )

    result = _run_hook(plugin, text)

    assert result.get("action") == "continue", f"CQ 卡片被解析了: {result}"
    assert resolved == [] and sent == [], (resolved, sent)


def test_url_parsing_disabled_still_wins() -> None:
    """两个开关都关：任何文本都不解析（防止新增分支把旧开关架空）。"""
    plugin, resolved, sent = _plugin_with_stubs(auto_parse_card=False, auto_parse_url=False)

    result = _run_hook(plugin, _USER_LINK_TEXT)

    assert result.get("action") == "continue", result
    assert resolved == [] and sent == [], (resolved, sent)
