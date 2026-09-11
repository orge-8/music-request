"""音乐平台 API 客户端：网易云音乐 + QQ音乐。

对外只暴露三件事：
    - `search(query, platform)`  搜索歌曲
    - `get_song_url(...)`        取可播放音频直链
    - `qq_music_card(song)`      组装 QQ音乐自定义卡片所需的字段

设计要点（都是踩过坑换来的）：
    - **网易云**优先走 eapi 加密接口（对付费/高音质更友好），失败再退标准
      Web 接口，最后退 `song/media/outer/url` 直链重定向。三层都带超时。
    - **QQ音乐**搜索/详情/取直链都打同一个域名 `u.y.qq.com/cgi-bin/musicu.fcg`，
      只是 module 不同——同域可避免旧接口在部分网络环境不可达。
      取直链必须用 `strMediaMid` 而不是 `songmid` 拼 filename，
      否则有版权的歌也拿不到链接（两张 mid 常常不同）。
    - **短链重定向**只允许跳到白名单域名（防 SSRF）。
    - 上游响应落日志前一律脱敏，避免把 cookie / token 写进日志。
    - 同目录模块用「相对导入优先 + 平铺兜底」：Runner 以包方式加载插件目录，
      相对导入会把模块挂成 `<插件名>.url_parser` 的命名空间，避免与同进程其他
      插件的顶层模块重名；平铺兜底则保证脚本直跑与本地测试也能导入。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

try:  # 包式加载（Runner 的主路径）：命名空间化，避免与别的插件顶层模块撞名
    from .url_parser import (
        PLATFORM_NETEASE,
        PLATFORM_QQ,
        is_allowed_music_url,
    )
except ImportError:  # 平铺兜底：脚本直跑 / 测试
    from url_parser import (
        PLATFORM_NETEASE,
        PLATFORM_QQ,
        is_allowed_music_url,
    )

logger = logging.getLogger("plugin.music-request.api")

# 单次上游请求超时（秒）——所有外调都必须有上限
REQUEST_TIMEOUT = 10.0

# 落日志的响应预览长度，避免一个异常响应淹没整个日志
_PREVIEW_LIMIT = 400

# 网易云 eapi 的 AES-128-ECB 密钥（移动端固定常量）
_EAPI_KEY = b"e82ckenh8dichen8"
_EAPI_MAGIC = "36cd479b6b5"

_NETEASE_WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
}
_NETEASE_EAPI_HEADERS = {
    "User-Agent": "NeteaseMusic/9.1.65.240916182646(9001065);Dalvik/2.1.0 (Linux; U; Android 14)",
    "Referer": "/api/song/enhance/player/url",
}
_QQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://y.qq.com/",
}

# QQ音乐卡片模板
_QQ_CARD_PAGE_TEMPLATE = "https://y.qq.com/n/ryqq/songDetail/{song_id}"
_QQ_CARD_COVER_TEMPLATE = "https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg?max_age=2592000"

# 音频后缀白名单：只有这些扩展名才认为是可播放音频
_AUDIO_SUFFIXES = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac")

# 响应里出现这些字段名就脱敏（避免写日志时泄露登录态）
_SENSITIVE_KEY_PARTS = (
    "authst", "authorization", "cookie", "csrf", "credential",
    "music_u", "music_a", "password", "qqmusic_key", "qm_keyst",
    "p_skey", "secret", "session", "signature", "skey", "ticket", "token",
)


def _redact(value: Any) -> Any:
    """递归遮蔽响应中的凭据字段。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _preview(value: Any) -> str:
    """把上游响应转成脱敏、限长的单行文本，供日志使用。"""
    try:
        text = json.dumps(_redact(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > _PREVIEW_LIMIT:
        return text[:_PREVIEW_LIMIT] + f"...(len={len(text)})"
    return text


class MusicAPIResponseError(RuntimeError):
    """上游返回的结构与预期协议不符。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail

    def diagnostic(self) -> str:
        """带脱敏响应体的诊断文本。"""
        return f"{self} | response={self.detail}" if self.detail else str(self)


@dataclass
class SongInfo:
    """一首歌的最小信息集。"""

    song_id: str            # 网易云为数字 ID；QQ音乐为 songmid
    name: str = ""
    artists: str = ""       # 多歌手用 ", " 连接
    album: str = ""
    platform: str = PLATFORM_NETEASE
    media_id: str = ""      # QQ音乐 strMediaMid —— 拼播放 filename 必须用它
    album_mid: str = ""     # QQ音乐专辑 mid —— 拼封面 URL 用

    def display(self) -> str:
        """人类可读的一行描述。"""
        parts = [self.name or self.song_id]
        if self.artists:
            parts.append(f"- {self.artists}")
        if self.album:
            parts.append(f"({self.album})")
        return " ".join(parts)


# ===== 网易云 eapi 加密 =====

def _aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB + PKCS7 填充。

    Raises:
        RuntimeError: 未安装 `cryptography`（eapi 通道会被跳过，不影响其它通道）。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError("未安装 cryptography，无法使用网易云 eapi 通道") from exc

    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len] * pad_len)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def eapi_encrypt(api_path: str, params: dict[str, Any]) -> str:
    """按网易云移动端规则加密 eapi 参数。

    流程：`nobody{path}use{json}md5forencrypt` → MD5 →
          `{path}-36cd479b6b5-{json}-36cd479b6b5-{md5}` → AES-128-ECB → 大写 hex

    Args:
        api_path: 如 `/api/song/enhance/player/url`。
        params: 请求参数字典。

    Returns:
        大写十六进制密文。

    Raises:
        RuntimeError: 缺少 `cryptography`。
    """
    body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.md5(f"nobody{api_path}use{body}md5forencrypt".encode()).hexdigest()
    sign_text = f"{api_path}-{_EAPI_MAGIC}-{body}-{_EAPI_MAGIC}-{digest}"
    return _aes_ecb_encrypt(_EAPI_KEY, sign_text.encode()).hex().upper()


# ===== 客户端 =====

class MusicSearchClient:
    """网易云 / QQ音乐 / NapCat 三合一客户端。

    Args:
        netease_cookie: 形如 `{"MUSIC_U": "...", "__csrf": "..."}`。
        qq_cookie: 形如 `{"uin": "...", "qqmusic_key": "..."}`。
        napcat_url: NapCat HTTP API 根地址；留空则禁用直连通道。
        napcat_token: NapCat 访问令牌；留空表示不鉴权。
    """

    def __init__(
        self,
        netease_cookie: dict[str, str] | None = None,
        qq_cookie: dict[str, str] | None = None,
        napcat_url: str = "",
        napcat_token: str = "",
    ) -> None:
        self._netease_cookie = netease_cookie or {}
        self._qq_cookie = {
            key: str(value).strip() for key, value in (qq_cookie or {}).items() if value
        }
        napcat_url = (napcat_url or "").strip().rstrip("/")

        netease_cookies = httpx.Cookies()
        if self._netease_cookie.get("MUSIC_U"):
            netease_cookies.set("MUSIC_U", self._netease_cookie["MUSIC_U"], domain=".music.163.com", path="/")
        if self._netease_cookie.get("__csrf"):
            netease_cookies.set("__csrf", self._netease_cookie["__csrf"], domain=".music.163.com", path="/")

        qq_cookies = {key: value for key, value in self._qq_cookie.items() if key in ("uin", "qqmusic_key")}

        # 搜索与取播放地址必须共用同一个客户端：网易云首次 eapi 请求
        # 依赖搜索响应下发的 NMTID cookie，拆成两个会话会拿不到音频。
        self._netease = httpx.AsyncClient(
            headers=_NETEASE_WEB_HEADERS, cookies=netease_cookies,
            timeout=REQUEST_TIMEOUT, follow_redirects=True,
        )
        self._qq = httpx.AsyncClient(
            headers=_QQ_HEADERS, cookies=qq_cookies, timeout=REQUEST_TIMEOUT,
        )
        self._napcat: httpx.AsyncClient | None = None
        if napcat_url:
            headers = {"Content-Type": "application/json"}
            if napcat_token:
                headers["Authorization"] = f"Bearer {napcat_token}"
            self._napcat = httpx.AsyncClient(
                base_url=napcat_url, headers=headers, timeout=REQUEST_TIMEOUT,
            )

    # ---------- 生命周期 ----------

    async def close(self) -> None:
        """关闭全部 HTTP 连接池。"""
        for client in (self._netease, self._qq, self._napcat):
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception:
                logger.debug("关闭 HTTP 客户端失败", exc_info=True)

    @property
    def qq_search_enabled(self) -> bool:
        """QQ音乐搜索需要登录态（uin + qqmusic_key 齐备）。"""
        return bool(self._qq_cookie.get("uin") and self._qq_cookie.get("qqmusic_key"))

    # ---------- 统一入口 ----------

    async def search(self, query: str, platform: str, limit: int = 5) -> list[SongInfo]:
        """按平台搜索歌曲。"""
        if platform == PLATFORM_QQ:
            return await self.search_qq(query, limit)
        return await self.search_netease(query, limit)

    async def get_song_url(
        self,
        song_id: str,
        platform: str,
        media_id: str = "",
        *,
        mp3_only: bool = False,
    ) -> str | None:
        """取歌曲的可播放直链。

        Args:
            song_id: 网易云数字 ID 或 QQ音乐 songmid。
            platform: `"163"` / `"qq"`。
            media_id: QQ音乐的 strMediaMid（拼 filename 用）。
            mp3_only: 本地语音缓存场景只接受 MP3，避免把 FLAC 改名成 .mp3。

        Returns:
            音频直链；拿不到返回 None。
        """
        if platform == PLATFORM_QQ:
            return await self._qq_song_url(song_id, media_id, mp3_only=mp3_only)
        return await self._netease_song_url(song_id, mp3_only=mp3_only)

    # ---------- 网易云 ----------

    async def search_netease(self, query: str, limit: int = 5) -> list[SongInfo]:
        """搜索网易云音乐。"""
        try:
            resp = await self._netease.get(
                "https://music.163.com/api/search/get/web",
                params={"s": query, "type": "1", "limit": str(limit), "offset": "0"},
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise MusicAPIResponseError(f"网易云搜索超时: {query!r}") from exc
        except httpx.HTTPStatusError as exc:
            raise MusicAPIResponseError(
                f"网易云搜索失败: {query!r} status={exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise MusicAPIResponseError(f"网易云搜索网络异常: {query!r} ({type(exc).__name__})") from exc

        data = self._json(resp, f"网易云搜索 {query!r}")
        if not isinstance(data, dict):
            raise MusicAPIResponseError(f"网易云搜索响应不是对象: {query!r}", detail=_preview(data))
        code = data.get("code")
        if code != 200:
            raise MusicAPIResponseError(f"网易云搜索业务失败: {query!r} code={code!r}", detail=_preview(data))

        result = data.get("result")
        songs = result.get("songs") if isinstance(result, dict) else None
        if songs is None:
            # 无结果时网易云会省略 result.songs，这不是协议错误
            return []
        if not isinstance(songs, list):
            raise MusicAPIResponseError(
                f"网易云搜索 songs 不是列表: {query!r} type={type(songs).__name__}",
                detail=_preview(data),
            )

        out: list[SongInfo] = []
        for song in songs:
            if not isinstance(song, dict):
                continue
            song_id = str(song.get("id") or "")
            name = str(song.get("name") or "")
            if not song_id or not name:
                continue
            artists_raw = song.get("artists") or []
            artists = ", ".join(
                str(item.get("name") or "")
                for item in artists_raw
                if isinstance(item, dict) and item.get("name")
            )
            album_raw = song.get("album") or {}
            album = str(album_raw.get("name") or "") if isinstance(album_raw, dict) else ""
            out.append(SongInfo(
                song_id=song_id, name=name, artists=artists, album=album,
                platform=PLATFORM_NETEASE,
            ))
        return out

    async def _netease_song_url(self, song_id: str, *, mp3_only: bool = False) -> str | None:
        """三条通道依次尝试：eapi → 标准 Web → 直链重定向，命中即停。

        必须**惰性**创建协程：写成
        `for coro in (self._a(...), self._b(...), self._c(...))` 时，
        三个协程对象会在循环开始前就全部创建；一旦第一条通道命中，
        剩下两个永远不会被 await，于是每个请求都往 stderr 刷
        `RuntimeWarning: coroutine ... was never awaited`（真机日志实锤）。
        """
        bitrate = 320000 if mp3_only else 999000
        channels = (
            lambda: self._netease_eapi_url(song_id, bitrate),
            lambda: self._netease_web_url(song_id, bitrate),
            lambda: self._netease_outer_url(song_id),
        )
        for channel in channels:
            url = await channel()
            if url:
                return url
        return None

    async def _netease_eapi_url(self, song_id: str, bitrate: int) -> str | None:
        """eapi 加密通道（移动端接口，付费/高音质歌曲命中率最高）。"""
        api_path = "/api/song/enhance/player/url"
        params: dict[str, Any] = {
            "ids": f"[{song_id}]",
            "br": bitrate,
            "csrf_token": self._netease_cookie.get("__csrf", ""),
        }
        try:
            encrypted = eapi_encrypt(api_path, params)
        except RuntimeError as exc:
            logger.debug("跳过网易云 eapi 通道: %s", exc)
            return None

        request_url = f"https://interface.music.163.com/eapi{api_path}"
        payload = {"params": encrypted}
        try:
            had_nmtid = any(cookie.name == "NMTID" for cookie in self._netease.cookies.jar)
            resp = await self._netease.post(
                request_url, data=payload, headers=_NETEASE_EAPI_HEADERS, follow_redirects=False,
            )
            # 首次请求会顺带下发 NMTID；带上它再请求一次才能拿到音频地址
            if not had_nmtid and any(cookie.name == "NMTID" for cookie in self._netease.cookies.jar):
                resp = await self._netease.post(
                    request_url, data=payload, headers=_NETEASE_EAPI_HEADERS, follow_redirects=False,
                )
            resp.raise_for_status()
            data = self._json(resp, f"网易云 eapi {song_id}")
        except (httpx.HTTPError, MusicAPIResponseError) as exc:
            logger.debug("网易云 eapi 取直链失败: %s (%s)", song_id, type(exc).__name__)
            return None
        return self._pick_first_url(data, song_id, "eapi")

    async def _netease_web_url(self, song_id: str, bitrate: int) -> str | None:
        """标准 Web 接口。"""
        params = {"ids": f"[{song_id}]", "br": str(bitrate)}
        csrf = self._netease_cookie.get("__csrf", "")
        if csrf:
            params["csrf_token"] = csrf
        try:
            resp = await self._netease.get(
                "https://music.163.com/api/song/enhance/player/url", params=params,
            )
            resp.raise_for_status()
            data = self._json(resp, f"网易云 Web {song_id}")
        except (httpx.HTTPError, MusicAPIResponseError) as exc:
            logger.debug("网易云标准接口取直链失败: %s (%s)", song_id, type(exc).__name__)
            return None
        return self._pick_first_url(data, song_id, "web")

    async def _netease_outer_url(self, song_id: str) -> str | None:
        """兜底：song/media/outer/url 直链重定向。"""
        try:
            resp = await self._netease.get(
                f"https://music.163.com/song/media/outer/url?id={song_id}.mp3",
                follow_redirects=True,
            )
            final_url = str(resp.url)
        except httpx.HTTPError:
            return None
        if final_url and any(final_url.lower().split("?")[0].endswith(ext) for ext in _AUDIO_SUFFIXES):
            return final_url
        return None

    def _pick_first_url(self, data: Any, song_id: str, channel: str) -> str | None:
        """从 `{"data": [{"url": ...}]}` 形态里取第一个非空 URL。"""
        if not isinstance(data, dict):
            logger.debug("网易云 %s 响应不是对象: %s", channel, song_id)
            return None
        items = data.get("data")
        if not isinstance(items, list) or not items:
            logger.debug("网易云 %s 未返回 data 列表: %s code=%r", channel, song_id, data.get("code"))
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        url = str(first.get("url") or "").strip()
        if url:
            return url
        # 有 code 说明是版权/付费限制，值得记一条 info 而非 debug
        logger.info(
            "网易云 %s 返回空直链: song_id=%s song_code=%r",
            channel, song_id, first.get("code"),
        )
        return None

    # ---------- QQ音乐 ----------

    async def _qq_post(self, req_data: dict[str, Any], what: str) -> dict[str, Any]:
        """统一走 musicu.fcg 的 POST 封装。"""
        try:
            resp = await self._qq.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=req_data)
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise MusicAPIResponseError(f"QQ音乐{what}超时") from exc
        except httpx.HTTPStatusError as exc:
            raise MusicAPIResponseError(f"QQ音乐{what}失败: status={exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise MusicAPIResponseError(f"QQ音乐{what}网络异常 ({type(exc).__name__})") from exc

        data = self._json(resp, f"QQ音乐{what}")
        if not isinstance(data, dict):
            raise MusicAPIResponseError(f"QQ音乐{what}响应不是对象", detail=_preview(data))
        return data

    async def search_qq(self, query: str, limit: int = 5) -> list[SongInfo]:
        """搜索 QQ音乐（需要配置 uin + qqmusic_key）。

        Raises:
            MusicAPIResponseError: 缺登录态、登录态失效或协议不符。
        """
        missing = [
            name for name, value in (
                ("qq.uin", self._qq_cookie.get("uin")),
                ("qq.qqmusic_key", self._qq_cookie.get("qqmusic_key")),
            ) if not value
        ]
        if missing:
            raise MusicAPIResponseError(
                "QQ音乐搜索需要登录态，缺少配置: " + ", ".join(missing)
            )

        uin = self._qq_cookie["uin"]
        authst = self._qq_cookie["qqmusic_key"]
        req_data = {
            "req_1": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {
                    "search_type": 0, "query": query, "page_num": 1, "num_per_page": limit,
                },
            },
            "loginUin": uin,
            "comm": {"uin": uin, "format": "json", "ct": 19, "cv": 0, "authst": authst},
        }

        data = await self._qq_post(req_data, f"搜索 {query!r}")
        req_result = data.get("req_1")
        if not isinstance(req_result, dict):
            raise MusicAPIResponseError(f"QQ音乐搜索响应缺少 req_1: {query!r}", detail=_preview(data))

        top_code, module_code = data.get("code"), req_result.get("code")
        if top_code not in (None, 0) or module_code not in (None, 0):
            reason = "登录态失效或被拒绝" if module_code == 2001 else "业务失败"
            raise MusicAPIResponseError(
                f"QQ音乐搜索{reason}: {query!r} code={top_code!r} module_code={module_code!r}",
                detail=_preview(data),
            )

        result_data = req_result.get("data")
        body = result_data.get("body") if isinstance(result_data, dict) else None
        song_data = body.get("song") if isinstance(body, dict) else None
        song_list = song_data.get("list") if isinstance(song_data, dict) else None
        if song_list is None:
            return []
        if not isinstance(song_list, list):
            raise MusicAPIResponseError(
                f"QQ音乐搜索歌曲列表格式错误: {query!r} type={type(song_list).__name__}",
                detail=_preview(data),
            )

        out: list[SongInfo] = []
        for song in song_list:
            info = self._song_from_qq_payload(song)
            if info is not None:
                out.append(info)
        return out

    def _song_from_qq_payload(self, song: Any) -> SongInfo | None:
        """把 QQ音乐搜索/详情里的一条歌曲记录转成 SongInfo。"""
        if not isinstance(song, dict):
            return None
        song_mid = str(song.get("mid") or song.get("songmid") or "")
        name = str(song.get("name") or song.get("songname") or "")
        if not song_mid or not name:
            return None

        singers = song.get("singer") or []
        artists = ", ".join(
            str(item.get("name") or "")
            for item in singers
            if isinstance(item, dict) and item.get("name")
        )
        album_raw = song.get("album") or {}
        album = str(album_raw.get("name") or "") if isinstance(album_raw, dict) else ""
        album_mid = str(album_raw.get("mid") or "") if isinstance(album_raw, dict) else ""
        if not album_mid:
            album_mid = str(song.get("albummid") or "")

        file_raw = song.get("file") or {}
        media_id = ""
        if isinstance(file_raw, dict):
            media_id = str(file_raw.get("media_mid") or "")
        if not media_id:
            media_id = str(song.get("strMediaMid") or song.get("media_mid") or "")

        return SongInfo(
            song_id=song_mid, name=name, artists=artists, album=album,
            platform=PLATFORM_QQ, media_id=media_id, album_mid=album_mid,
        )

    async def get_qq_song_detail(self, song_mid: str) -> SongInfo | None:
        """按 songmid 查 QQ音乐详情（补 strMediaMid / album_mid，匿名可用）。"""
        req_data = {
            "req_0": {
                "module": "music.pf_song_detail_svr",
                "method": "get_song_detail_yqq",
                "param": {"song_mid": song_mid},
            },
            "comm": {"format": "json", "ct": 19, "cv": 0},
        }
        data = await self._qq_post(req_data, f"歌曲详情 {song_mid}")
        req_result = data.get("req_0")
        if not isinstance(req_result, dict):
            raise MusicAPIResponseError(f"QQ音乐详情响应缺少 req_0: {song_mid}", detail=_preview(data))
        if data.get("code") not in (None, 0) or req_result.get("code") not in (None, 0):
            raise MusicAPIResponseError(
                f"QQ音乐详情业务失败: {song_mid} code={data.get('code')!r} "
                f"module_code={req_result.get('code')!r}",
                detail=_preview(data),
            )
        result_data = req_result.get("data")
        track = result_data.get("track_info") if isinstance(result_data, dict) else None
        if not isinstance(track, dict):
            return None
        return self._song_from_qq_payload(track)

    async def _qq_song_url(self, song_mid: str, media_mid: str = "", *, mp3_only: bool = False) -> str | None:
        """取 QQ音乐可播放直链。

        filenames 按音质从高到低排列，一次性请求，返回第一个有 purl 的。
        `media_mid` 为空时退回 songmid —— 但多数歌曲两者不同，
        用错会「有版权也拿不到链接」，所以调用方应尽量先补查详情。
        """
        resource_mid = media_mid or song_mid
        qualities: list[tuple[str, str]] = [("M800", ".mp3"), ("M500", ".mp3")]
        if not mp3_only:
            qualities = [("F000", ".flac"), *qualities, ("C400", ".m4a")]
        filenames = [f"{prefix}{resource_mid}{ext}" for prefix, ext in qualities]
        return await self._qq_vkey_batch(filenames, song_mid)

    async def _qq_vkey_batch(self, filenames: list[str], song_mid: str) -> str | None:
        """批量请求 vkey，按传入顺序返回第一个可用直链。"""
        uin = self._qq_cookie.get("uin", "0") or "0"
        authst = self._qq_cookie.get("qqmusic_key", "")
        guid = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)

        req_data = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "filename": filenames,
                    "guid": guid,
                    "songmid": [song_mid] * len(filenames),
                    "songtype": [0] * len(filenames),
                    "uin": uin,
                    "loginflag": 1,
                    "platform": "20",
                },
            },
            "loginUin": uin,
            "comm": {"uin": uin, "format": "json", "ct": 19, "cv": 0, "authst": authst},
        }

        try:
            data = await self._qq_post(req_data, f"取直链 {song_mid}")
        except MusicAPIResponseError as exc:
            logger.warning("QQ音乐取直链失败: %s (%s)", song_mid, exc)
            return None

        req_result = data.get("req_0")
        if not isinstance(req_result, dict):
            logger.warning("QQ音乐 vkey 响应缺少 req_0: %s code=%r", song_mid, data.get("code"))
            return None
        if data.get("code") not in (None, 0) or req_result.get("code") not in (None, 0):
            logger.warning(
                "QQ音乐 vkey 业务失败: %s code=%r module_code=%r",
                song_mid, data.get("code"), req_result.get("code"),
            )
            return None

        result_data = req_result.get("data")
        if not isinstance(result_data, dict):
            return None
        sip = result_data.get("sip")
        infos = result_data.get("midurlinfo")
        if not isinstance(sip, list) or not isinstance(infos, list):
            logger.warning("QQ音乐 vkey 字段类型异常: %s", song_mid)
            return None

        # 按 filename 建索引再按请求顺序取，避免上游调整返回顺序导致选错音质
        by_filename = {
            str(info.get("filename") or ""): info
            for info in infos
            if isinstance(info, dict) and info.get("filename")
        }
        ordered: list[Any] = [by_filename.get(name) for name in filenames]
        if not by_filename and len(infos) == len(filenames):
            ordered = list(infos)

        fallback_domain = next((s for s in sip if isinstance(s, str) and s.startswith("https://")), "")
        if not fallback_domain:
            fallback_domain = next((s for s in sip if isinstance(s, str) and s), "")

        for info in ordered:
            if not isinstance(info, dict):
                continue
            purl = str(info.get("purl") or "").strip()
            if not purl:
                continue
            if purl.startswith(("http://", "https://")):
                candidate = purl
            else:
                if not fallback_domain:
                    continue
                candidate = urljoin(fallback_domain, purl)
            parsed = urlparse(candidate)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                return candidate
            logger.debug("QQ音乐 vkey 返回无效直链，继续尝试下一音质: %s", song_mid)

        logger.info(
            "QQ音乐未返回可用直链: song_mid=%s 已登录=%s 候选音质=%s 明细=%s",
            song_mid,
            self.qq_search_enabled,
            filenames,
            [
                {
                    "filename": str(info.get("filename") or ""),
                    "result": info.get("result"),
                    "subcode": info.get("subcode"),
                    "has_purl": bool(info.get("purl")),
                }
                for info in infos if isinstance(info, dict)
            ],
        )
        return None

    async def probe_audio(self, url: str, timeout: float = 5.0) -> bool:
        """HEAD 校验直链是否真的可下载（vkey 直链有时效，过期会 403）。"""
        try:
            resp = await self._qq.head(url, timeout=timeout)
        except Exception:
            # 部分 CDN 不支持 HEAD，退回一次极小范围的 GET
            try:
                resp = await self._qq.get(url, timeout=timeout, headers={"Range": "bytes=0-0"})
            except Exception:
                return False
        return 200 <= resp.status_code < 400

    async def qq_music_card(self, song: SongInfo) -> dict[str, str] | None:
        """组装 QQ音乐自定义音乐卡片所需的字段。

        元数据不足时先补查详情；直链优先 m4a 再 mp3 并做 HEAD 校验，
        拿不到直链就返回 `audio=""` 的跳转卡（用户仍可点开）。

        Returns:
            `{"type","url","title","image","content","audio"}`；无法构卡返回 None。
        """
        if not song.song_id:
            return None

        name, artists = song.name, song.artists
        media_id, album_mid = song.media_id, song.album_mid
        if not (name and media_id and album_mid):
            try:
                detail = await self.get_qq_song_detail(song.song_id)
            except MusicAPIResponseError as exc:
                logger.debug("补查 QQ音乐详情失败: %s (%s)", song.song_id, exc)
                detail = None
            if detail is not None:
                name = name or detail.name
                artists = artists or detail.artists
                media_id = media_id or detail.media_id
                album_mid = album_mid or detail.album_mid

        title = (name or "").strip()
        # 没有标题或封面就没法构成有意义的卡片
        if not title or not album_mid:
            return None

        resource_mid = media_id or song.song_id
        audio = ""
        for prefix, ext in (("C400", ".m4a"), ("M500", ".mp3")):
            candidate = await self._qq_vkey_batch([f"{prefix}{resource_mid}{ext}"], song.song_id)
            if candidate and await self.probe_audio(candidate):
                audio = candidate
                break

        return {
            "type": "custom",
            "url": _QQ_CARD_PAGE_TEMPLATE.format(song_id=song.song_id),
            "title": title,
            "image": _QQ_CARD_COVER_TEMPLATE.format(album_mid=album_mid),
            "content": artists,
            "audio": audio,
        }

    # ---------- 短链 ----------

    async def resolve_short_url(self, url: str) -> str | None:
        """解析音乐短链，返回白名单内的最终 URL。

        先只看 3xx 的 Location（不下载页面），不行再跟随重定向并从
        最终 URL / meta refresh / window.location 里提取。任何指向白名单
        外域名的跳转一律拒绝（防 SSRF）。

        Args:
            url: 短链地址。

        Returns:
            最终 URL；失败或目标不在白名单返回 None。
        """
        client = self._qq if "y.qq.com" in url else self._netease

        try:
            resp = await client.get(url, follow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if location and is_allowed_music_url(location):
                    return location
                if location:
                    logger.warning("短链重定向目标不在白名单，已拒绝: %s", location)
        except httpx.HTTPError:
            logger.debug("短链 Location 解析失败: %s", url)

        try:
            resp = await client.get(url, follow_redirects=True)
        except httpx.HTTPError:
            return None

        final_url = str(resp.url)
        if is_allowed_music_url(final_url):
            return final_url

        html = resp.text or ""
        for pattern in (
            r'<meta\s+http-equiv=["\']refresh["\']\s+content=["\']?\d+;\s*url=([^"\'>\s]+)',
            r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
        ):
            match = re.search(pattern, html, re.IGNORECASE)
            if not match:
                continue
            target = match.group(1).strip()
            if is_allowed_music_url(target):
                return target
            logger.warning("短链页面跳转目标不在白名单，已拒绝: %s", target)
            return None

        logger.warning("短链最终目标不在白名单，已拒绝: %s", final_url)
        return None

    # ---------- NapCat 直连 ----------

    async def get_raw_message(self, message_id: int) -> dict[str, Any] | None:
        """调 NapCat `/get_msg` 拿原始消息（用于从 json 段精确解析卡片歌曲 ID）。"""
        if self._napcat is None:
            return None
        try:
            resp = await self._napcat.post("/get_msg", json={"message_id": message_id})
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            logger.debug("NapCat get_msg 调用失败: %s", message_id)
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else None

    async def napcat_send_message(
        self,
        message: str,
        *,
        group_id: str = "",
        user_id: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        """直连 NapCat 发一条 OneBot 消息（CQ 字符串形式）。

        存在的理由：MaiBot 适配器对 music 段只按固定结构处理（platform+id），
        会把带 url/audio/title/image/content 的 QQ音乐自定义卡片降级成文本。
        直连可绕开这层改写。仅在配置了 `napcat.http_url` 时可用。

        Args:
            message: CQ 消息文本。
            group_id: 群号（与 user_id 二选一）。
            user_id: QQ号。

        Returns:
            (是否成功, NapCat 响应)。
        """
        if self._napcat is None or not message:
            return False, {}
        if group_id:
            path, payload = "/send_group_msg", {"group_id": int(group_id), "message": message}
        elif user_id:
            path, payload = "/send_private_msg", {"user_id": int(user_id), "message": message}
        else:
            logger.warning("NapCat 直连发送缺少目标群号/QQ号")
            return False, {}

        try:
            resp = await self._napcat.post(path, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("NapCat 直连发送失败: path=%s error=%s", path, type(exc).__name__)
            return False, {}
        if not isinstance(data, dict):
            logger.warning("NapCat 直连发送响应异常: path=%s", path)
            return False, {}

        ok = data.get("status") == "ok" or data.get("retcode") == 0
        if not ok:
            # 只记业务码，不回显整条消息（里面含 vkey 直链）
            logger.warning(
                "NapCat 直连发送业务失败: path=%s retcode=%r message=%r",
                path, data.get("retcode"), data.get("message"),
            )
        return ok, data

    async def napcat_upload_file(
        self,
        file_path: str,
        *,
        name: str = "",
        group_id: str = "",
        user_id: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        """通过 NapCat 把本地文件当作「群文件 / 私聊文件」发送。

        为什么必须直连：MaiBot 的 send 能力只有 text / image / emoji / forward /
        hybrid / command / custom，**没有「文件」段**；OneBot 的文件上传也不是消息段，
        而是独立动作 `upload_group_file` / `upload_private_file`。

        `file_path` 必须是 **NapCat 进程能读到的路径** —— 与语音本地缓存同一个前提，
        用 `/点歌自检` 核对两个目录是否指向同一份文件。

        Args:
            file_path: NapCat 侧可见的文件路径。
            name: 对方看到的文件名；留空取路径里的文件名。
            group_id: 目标群号（与 `user_id` 二选一）。
            user_id: 目标 QQ 号。

        Returns:
            `(是否成功, NapCat 响应)`。未配置 NapCat、缺目标、请求失败均返回 False。
        """
        if self._napcat is None or not file_path:
            return False, {}

        # 不用 pathlib：这里既要处理 Windows 反斜杠也要处理容器内正斜杠
        display_name = name or file_path.replace("\\", "/").rsplit("/", 1)[-1]
        try:
            if group_id:
                path = "/upload_group_file"
                payload: dict[str, Any] = {
                    "group_id": int(group_id),
                    "file": file_path,
                    "name": display_name,
                    "folder": "",
                }
            elif user_id:
                path = "/upload_private_file"
                payload = {
                    "user_id": int(user_id),
                    "file": file_path,
                    "name": display_name,
                }
            else:
                logger.warning("NapCat 上传文件缺少目标群号/QQ号")
                return False, {}
        except (TypeError, ValueError):
            logger.warning(
                "NapCat 上传文件的目标不是合法数字: group_id=%r user_id=%r", group_id, user_id
            )
            return False, {}

        try:
            resp = await self._napcat.post(path, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("NapCat 上传文件失败: path=%s error=%s", path, type(exc).__name__)
            return False, {}
        if not isinstance(data, dict):
            logger.warning("NapCat 上传文件响应异常: path=%s", path)
            return False, {}

        ok = data.get("status") == "ok" or data.get("retcode") == 0
        if not ok:
            logger.warning(
                "NapCat 上传文件业务失败: path=%s retcode=%r message=%r wording=%r",
                path, data.get("retcode"), data.get("message"), data.get("wording"),
            )
        return ok, data

    # ---------- 内部工具 ----------

    @staticmethod
    def _json(resp: httpx.Response, what: str) -> Any:
        """解析响应 JSON，失败抛带脱敏预览的协议异常。"""
        try:
            return resp.json()
        except Exception as exc:
            raise MusicAPIResponseError(
                f"{what}响应不是有效 JSON", detail=_preview(resp.text or "")
            ) from exc
