# -*- coding: utf-8 -*-
"""music-request v1.4.6 安全回归测试。

覆盖上线前全检确认的高危项修复：
1. QQ 登录态 cookie 必须限定到 .y.qq.com —— 无 domain 的 cookie 会被 httpx
   附加到该 client 的所有请求上，请求一旦指向白名单外主机就会外发凭据；
2. 短链解析入口 host 必须白名单化（旧实现用 `"y.qq.com" in url` 子串判断，
   `https://evil.com/?x=y.qq.com` 可绕过）；
3. 重定向必须手动逐跳校验 host —— `follow_redirects=True` 会在跨主机跳转时
   继续携带 cookie，一条 302 就能把凭据送出白名单；
4. NapCat URL 归一化的两个边界：`rstrip("/")` 会吃掉 `http://` 的斜杠、
   协议头判定必须大小写不敏感。

真实性与性质：httpx.MockTransport 拦截出站请求并断言实际请求头。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from music_api import (  # noqa: E402
    MusicSearchClient,
    is_fetchable_link,
    is_private_host,
    normalize_napcat_url,
)

_QQ_SECRET = "QQ_SECRET_TOKEN_XYZ"


def _pin_transport(client: httpx.AsyncClient, handler) -> None:
    """把 client 的出站请求钉到 MockTransport。

    注意：httpx 在 `trust_env=True`（默认）且环境里存在代理变量时会填充
    `client._mounts`，`_transport_for_url` 优先走 mounts，直接赋值 `_transport`
    会被忽略、请求仍打到真实代理。测试里必须同时清空 mounts。
    """
    client._mounts = {}
    client._transport = httpx.MockTransport(handler)


# ================================================================ 1. 凭据外发

def test_qq_cookie_not_sent_to_foreign_host() -> None:
    """QQ 登录态只能发给 .y.qq.com，绝不能跟着请求走到其它主机。

    这是本次全检的最高危项：未限定 domain 时，任何一次指向白名单外主机的
    请求都会把 uin / qqmusic_key 原样送出。
    """
    client = MusicSearchClient(qq_cookie={"uin": "12345", "qqmusic_key": _QQ_SECRET})
    try:
        seen: dict[str, str] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen[request.url.host] = request.headers.get("cookie", "")
            return httpx.Response(200, json={"ok": True})

        _pin_transport(client._qq, handler)
        asyncio.run(client._qq.get("https://evil.example.com/steal"))
        asyncio.run(client._qq.get("https://u.y.qq.com/cgi-bin/musicu.fcg"))
    finally:
        asyncio.run(client.close())

    assert "evil.example.com" in seen, "测试未真正发出请求"
    assert _QQ_SECRET not in seen["evil.example.com"], (
        f"凭据被外发到白名单外主机: {seen['evil.example.com']!r}"
    )
    assert _QQ_SECRET in seen["u.y.qq.com"], (
        f"合法域名没拿到 cookie，功能会坏: {seen['u.y.qq.com']!r}"
    )


def test_qq_cookie_all_have_domain_scope() -> None:
    """cookie jar 里每条 cookie 都必须带 domain（不依赖上层调用是否正确）。"""
    client = MusicSearchClient(qq_cookie={"uin": "1", "qqmusic_key": _QQ_SECRET})
    try:
        domains = [c.domain for c in client._qq.cookies.jar]
    finally:
        asyncio.run(client.close())
    assert domains, "QQ cookie 未装载"
    assert all(d and "qq.com" in d for d in domains), f"存在无 domain 的 cookie: {domains}"


def test_netease_cookie_still_scoped() -> None:
    """回归：网易云 cookie 的 domain 限定不能被改坏。"""
    client = MusicSearchClient(netease_cookie={"MUSIC_U": "NETEASE_SECRET", "__csrf": "csrf"})
    try:
        seen: dict[str, str] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen[request.url.host] = request.headers.get("cookie", "")
            return httpx.Response(200, json={})

        _pin_transport(client._netease, handler)
        asyncio.run(client._netease.get("https://evil.example.com/steal"))
        asyncio.run(client._netease.get("https://music.163.com/api/x"))
    finally:
        asyncio.run(client.close())

    assert "NETEASE_SECRET" not in seen["evil.example.com"]
    assert "NETEASE_SECRET" in seen["music.163.com"]


# ================================================================ 2. 短链入口闸门

@pytest.mark.parametrize(
    "url",
    [
        "https://163cn.tv/abc123",
        "https://y.qq.com/n/ryqq/songDetail/xyz",
        "https://i.y.qq.com/v8/playsong.html?songmid=x",
        "https://music.163.com/song?id=123",
        "https://y.music.163.com/m/song?id=1",
        # QQ 短链主机带数字前缀：必须与 url_parser._QQ_SHORT_RE 的 `c\d+` 对齐，
        # 白名单只列 c6 会把 c5/c7 这类合法短链静默丢掉
        "https://c5.y.qq.com/base/fcgi-bin/u?__=abc",
        "https://c6.y.qq.com/base/fcgi-bin/u?__=abc",
        "https://c7.y.qq.com/base/fcgi-bin/u?__=abc",
        "https://C6.Y.QQ.COM/base/fcgi-bin/u?__=abc",
    ],
)
def test_fetchable_link_accepts_whitelisted(url: str) -> None:
    assert is_fetchable_link(url), f"白名单内 URL 被误拒: {url}"


def test_short_link_whitelist_matches_parser() -> None:
    """交叉一致性：凡解析器认的 QQ 短链，入口闸门必须也放行。

    两侧正则脱节会造成「解析得到、却发不出请求」的静默失败。
    """
    from url_parser import parse_music_url

    for host in ("c5", "c6", "c7", "c12"):
        url = f"https://{host}.y.qq.com/base/fcgi-bin/u?__=abc"
        assert parse_music_url(url) is not None, f"解析器不认: {url}"
        assert is_fetchable_link(url), f"入口闸门拦掉了解析器认的短链: {url}"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/?next=y.qq.com",   # 子串绕过（旧实现会中招）
        "https://y.qq.com.evil.example.com/x",        # 前缀伪装
        "https://evil.example.com/163cn.tv",          # 路径里带白名单域名
        "http://127.0.0.1:8080/short",
        "http://localhost/short",
        "http://10.0.0.5/short",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/short",
        "http://[::1]/short",
        "not-a-url",
        "",
    ],
)
def test_fetchable_link_rejects_foreign_and_internal(url: str) -> None:
    """白名单外主机与内网地址一律拒绝——这是 SSRF + 凭据外发的主防线。"""
    assert not is_fetchable_link(url), f"危险 URL 未被拒绝: {url!r}"


def test_private_host_detection() -> None:
    assert is_private_host("http://127.0.0.1/x")
    assert is_private_host("http://169.254.169.254/x")
    assert is_private_host("http://10.1.2.3/x")
    assert is_private_host("http://172.16.0.1/x")
    assert is_private_host("http://localhost/x")
    assert not is_private_host("https://y.qq.com/x")


# ================================================================ 3. 重定向逐跳校验

def test_resolve_short_url_rejects_foreign_entry_without_request() -> None:
    """入口不在白名单 → **连请求都不发**（否则凭据/host 信息已泄露）。"""
    client = MusicSearchClient()
    asked: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    try:
        _pin_transport(client._netease, handler)
        _pin_transport(client._qq, handler)
        result = asyncio.run(client.resolve_short_url("https://evil.example.com/abc"))
    finally:
        asyncio.run(client.close())

    assert result is None
    assert asked == [], f"不该向白名单外主机发请求，实际请求了: {asked}"


def test_resolve_short_url_does_not_follow_foreign_redirect() -> None:
    """白名单入口 302 到白名单外主机 → 必须中止，不得跟随。"""
    client = MusicSearchClient()
    asked: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        if request.url.host == "163cn.tv":
            return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})
        return httpx.Response(200, text="<html>evil</html>")

    try:
        _pin_transport(client._netease, handler)
        _pin_transport(client._qq, handler)
        result = asyncio.run(client.resolve_short_url("https://163cn.tv/abc"))
    finally:
        asyncio.run(client.close())

    assert result is None
    assert "evil.example.com" not in asked, f"跟随到了白名单外主机: {asked}"


def test_resolve_short_url_follows_whitelisted_hop() -> None:
    """正向对照：白名单内的逐跳跳转仍要能解析出来（别把功能拦死）。"""
    client = MusicSearchClient()
    target = "https://music.163.com/song?id=42"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "163cn.tv":
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, text="<html></html>")

    try:
        _pin_transport(client._netease, handler)
        _pin_transport(client._qq, handler)
        result = asyncio.run(client.resolve_short_url("https://163cn.tv/abc"))
    finally:
        asyncio.run(client.close())

    assert result == target, f"合法短链应能解析，实际: {result!r}"


def test_resolve_short_url_uses_host_not_substring_for_client() -> None:
    """client 归属按 host 判定：evil.com 的 URL 不因含 y.qq.com 子串而走 QQ client。"""
    client = MusicSearchClient()
    asked: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        return httpx.Response(200, text="<html></html>")

    try:
        _pin_transport(client._netease, handler)
        _pin_transport(client._qq, handler)
        asyncio.run(client.resolve_short_url("https://evil.example.com/?x=y.qq.com"))
    finally:
        asyncio.run(client.close())

    assert asked == [], f"子串伪装的 URL 不该被请求: {asked}"


# ================================================================ 4. NapCat URL 归一化边界

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("127.0.0.1:9999", "http://127.0.0.1:9999"),
        ("  127.0.0.1:9999  ", "http://127.0.0.1:9999"),
        ("127.0.0.1:9999/", "http://127.0.0.1:9999"),
        ("http://127.0.0.1:9999/", "http://127.0.0.1:9999"),
        ("https://napcat.example.com", "https://napcat.example.com"),
        # 大小写不敏感（HTTP:// 是合法写法，旧实现会拼成 http://HTTP://...）
        ("HTTP://127.0.0.1:9999", "HTTP://127.0.0.1:9999"),
        ("HTTPS://napcat.example.com/", "HTTPS://napcat.example.com"),
        # 关键边界：rstrip("/") 会把 "http://" 吃成 "http:"
        # 归一化后没有主机名 → 返回空串（调用方据此禁用直连），不是造个非法 URL
        ("http://", ""),
        ("/", ""),
        ("///", ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_napcat_url(raw: str, expected: str) -> None:
    assert normalize_napcat_url(raw) == expected


def test_napcat_client_disabled_when_no_host() -> None:
    """无主机名的配置（"/"、"http://"）应禁用直连，而不是造出非法 base_url。"""
    for raw in ("http://", "/", "///"):
        client = MusicSearchClient(napcat_url=raw)
        try:
            assert client._napcat is None, f"{raw!r} 不该创建直连客户端"
        finally:
            asyncio.run(client.close())
