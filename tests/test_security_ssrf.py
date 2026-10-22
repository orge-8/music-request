"""短链解析的 SSRF / 凭据外发守卫（上线前全检固化）。

审计做法：不读代码猜结论，而是**拦截每一次出站请求**，断言
「越界主机有没有真的收到请求」以及「它拿到的是什么 cookie」。

这是证明此类漏洞的唯一可靠手段 —— 旧版 `follow_redirects=True` 的写法
照样会抛出同样的 `ValueError("短链重定向到非白名单域")`，只看返回值
完全正常，只有出站记录能暴露越界访问。

覆盖：
  1. 入口闸门：内网字面量 / 非白名单域 → 零出站
  2. 子串绕过：`evil.com/?x=y.qq.com` 不能骗过 host 判定
  3. 逐跳守卫：302 到外部域时，外部域不得收到请求
  4. 凭据不外发：即便真去请求白名单域，Cookie 也只发给该域
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import music_api as api_mod
import plugin as plugin_module
from music_api import MusicSearchClient


def _make_plugin():
    """装好 FakeHost 上下文的插件实例。"""
    from fakehost import FakeHost, bind_context, build_context, get_default_config

    plug = plugin_module.MusicRequestPlugin()
    host = FakeHost(plugin_id="github.cateye.music-request")
    ctx = build_context("github.cateye.music-request", rpc_call=host.rpc_call)
    bind_context(plug, ctx, get_default_config(plugin_module.MusicRequestConfig))
    return plug, host


class _Recorder:
    """记录每次出站的 (host, cookie)。"""

    def __init__(self, handler=None) -> None:
        self.seen: list[tuple[str, str]] = []
        self._handler = handler

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((
            httpx.URL(str(request.url)).host,
            request.headers.get("cookie", ""),
        ))
        if self._handler is not None:
            return await self._handler(request)
        return httpx.Response(200, text="", request=request)


def _api_with(recorder: _Recorder, *, cookie: bool = True) -> MusicSearchClient:
    """构造一个出站被拦截的 client。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    if cookie:
        # 刻意用「无 domain 限定」的 jar —— 这正是凭据最容易外泄的形态
        client.cookies.set("uin", "1234567890")
        client.cookies.set("qqmusic_key", "FAKE_SECRET_VALUE")
    api = MusicSearchClient(
        netease_cookie={"MUSIC_U": "FAKE_NETEASE"},
        qq_cookie={"uin": "1234567890", "qqmusic_key": "FAKE_SECRET_VALUE"},
    )
    api._netease = client
    api._qq = client
    return api


# ---------- 入口闸门：零出站 ----------


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://192.168.1.1/x",
    "http://10.0.0.5/x",
    "http://localhost/x",
    "https://evil.example.com/y.qq.com",
    "https://evil.example.com/?x=y.qq.com",   # 子串绕过（旧实现的坑）
    "https://y.qq.com.evil.example.com/x",    # 后缀伪装
])
def test_entry_gate_rejects_without_any_request(url: str) -> None:
    rec = _Recorder()
    api = _api_with(rec)
    assert asyncio.run(api.resolve_short_url(url)) is None
    assert rec.seen == [], f"不该发出任何请求，实际: {rec.seen}"


# ---------- 逐跳守卫：302 到外部域 ----------


def _redirect_to(evil: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        if httpx.URL(str(request.url)).host == "163cn.tv":
            return httpx.Response(
                302, headers={"location": evil}, request=request
            )
        return httpx.Response(200, text="", request=request)

    return handler


@pytest.mark.parametrize("evil", [
    "https://evil.example.com/steal",
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8080/x",
])
def test_redirect_out_of_whitelist_never_reaches(evil: str) -> None:
    """302 到越界域：请求必须止步，越界域不得收到任何请求。"""
    rec = _Recorder(_redirect_to(evil))
    api = _api_with(rec)
    assert asyncio.run(api.resolve_short_url("https://163cn.tv/abc")) is None
    hosts = [host for host, _ in rec.seen]
    assert hosts == ["163cn.tv"], f"越界域不应收到请求: {rec.seen}"


def test_cookie_never_leaks_to_redirect_target() -> None:
    """即便真的请求了白名单域，凭据也只能发给该域本身。"""
    evil = "https://evil.example.com/steal"
    rec = _Recorder(_redirect_to(evil))
    api = _api_with(rec)
    asyncio.run(api.resolve_short_url("https://163cn.tv/abc"))
    for host, cookie in rec.seen:
        if host == "evil.example.com":
            pytest.fail(f"越界域收到了请求: {host}")
        assert "FAKE_SECRET_VALUE" not in cookie or host in {"163cn.tv"}, (
            f"凭据出现在非白名单域 {host}: {cookie}"
        )


# ---------- 正常路径不被误伤 ----------


def test_legitimate_short_link_resolves() -> None:
    """白名单内的跳转链应正常解析（防护不能把正常功能杀掉）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        host = httpx.URL(str(request.url)).host
        if host == "163cn.tv":
            return httpx.Response(
                302, headers={"location": "https://music.163.com/song?id=1"},
                request=request,
            )
        return httpx.Response(200, text="", request=request)

    rec = _Recorder(handler)
    api = _api_with(rec)
    out = asyncio.run(api.resolve_short_url("https://163cn.tv/abc"))
    assert out == "https://music.163.com/song?id=1", out
    assert [h for h, _ in rec.seen] == ["163cn.tv", "music.163.com"]


def test_whitelist_is_not_substring_matched() -> None:
    """白名单判定必须按 host，不是子串（旧实现的漏洞形态）。"""
    assert api_mod.is_fetchable_link("https://y.qq.com/x") is True
    assert api_mod.is_fetchable_link("https://evil.example.com/?x=y.qq.com") is False
    assert api_mod.is_fetchable_link("https://y.qq.com.evil.example.com/x") is False


# ---------- 日志泄露：响应体 vs 异常消息（清单第 7 项）----------
#
# 插件手握三套凭据（网易云 MUSIC_U、QQ uin+qqmusic_key、B站 SESSDATA）。
# 实战坑：以为「只打 status/message 就够了」，却在 `except Exception as e:` 里
# 写了 `logger.warning(f"...: {e}")`，而异常消息里内嵌了请求用的 cookie 串。
# 这里分别验证「响应体」与「异常消息」两条路径。


from music_api import MusicAPIResponseError, _preview, _redact


def test_response_body_is_redacted() -> None:
    """结构化响应体里的凭据字段必须被遮蔽（已有防护，固化防回归）。"""
    payload = {
        "code": 0,
        "data": {"song": {"name": "春游"}},
        "cookie": "uin=1234567890; p_skey=SECRET123",
        "token": "SECRET456",
    }
    text = _preview(payload)
    assert "SECRET123" not in text, text
    assert "SECRET456" not in text, text
    assert "[REDACTED]" in text, text
    assert "春游" in text, "非敏感字段应保留，便于排障"


def test_redact_keeps_diagnostics() -> None:
    """脱敏要保住诊断信息（键名、结构），只吃掉值。"""
    out = _redact({"code": -460, "message": "Cheating", "p_skey": "SECRET"})
    assert out["code"] == -460
    assert out["message"] == "Cheating"
    assert out["p_skey"] == "[REDACTED]"


def test_diagnostic_text_has_no_credentials() -> None:
    err = MusicAPIResponseError(
        "QQ音乐搜索业务失败", detail=_preview({"p_skey": "SECRET123", "code": 2001}),
    )
    text = err.diagnostic()
    assert "SECRET123" not in text, text
    assert "2001" in text, "状态码应保留"


def test_exception_message_with_credentials_is_scrubbed(caplog) -> None:
    """异常**消息**里夹带凭据时也不能写进日志。

    这是实战炸过的形态：adapter 抛的异常消息内嵌 cookie 串
    （`adapter error: cookies=uin=...; p_skey=...`），日志照样明文落盘。
    """
    import logging

    plugin, _host = _make_plugin()
    asyncio.run(plugin.on_load())   # 真机路径：脱敏 Filter 在这里装上

    class _BoomApi:
        async def search(self, *_args, **_kwargs):
            raise RuntimeError(
                "adapter error: cookies=uin=1234567890; p_skey=SECRET123"
            )

    plugin._api = _BoomApi()
    caplog.set_level(logging.DEBUG)
    asyncio.run(
        plugin.cmd_点歌(
            matched_groups={"pfx": "/", "query": "春游"}, stream_id="s1"
        )
    )
    assert "SECRET123" not in caplog.text, "凭据不得出现在日志里"
