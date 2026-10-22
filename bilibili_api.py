"""B站（bilibili）视频音频提取客户端。

从 B站视频中提取音频流，作为点歌插件的第三平台。接口约定沿用社区通用实现
（bilibili-API-collect 文档仓库已于 2026-01 因律师函下架，WBI 混淆表与签名
算法以 timerring/bilitool 等多个独立镜像实现交叉核对为准），并按本插件自身
的安全规范重写（不引入 bilibili-api 等重型逆向库）。

技术路线（2026-10-21 方案评审定稿：仅视频音频，不做音频区）：
    1. 搜索视频：`x/web-interface/search/type?search_type=video`，**必须 WBI 签名**
       且要带 buvid3 cookie，否则风控（HTTP 412 / code -412）。
    2. 取 cid：`x/player/pagelist?bvid=`（主通路，免 WBI）；兜底
       `x/web-interface/wbi/view`。**不用** `x/web-interface/view`——该路径对
       家宽客户端稳定返回 412（真机 2026-10-21 实测，见 get_video_cid 注释）。
       `video_id` 同时接受 BV 号与 av 号，两个接口都原生支持 `aid=` 参数。
    3. 取音频流：`x/player/playurl?fnval=16`（DASH，需 WBI 签名），
       `dash.audio[]` 按 bandwidth 取最高——**只取音频轨，不需要 ffmpeg 拼接**，
       m4s 直接按 `.m4a` 落盘（容器就是 ftyp MP4）。

三条硬规则（与 music_api / audio_cache 同源的安全纪律）：
    - **SESSDATA 必须带 domain 限定**（`.bilibili.com`）：直接传 dict 会创建
      「无 domain」cookie，httpx 会把它附加到该 client 的所有请求上。
    - **CDN 白名单 + 内网拦截**：播放直链 host 只允许 bilivideo.com /
      bilivideo.cn / akamaized.net 后缀，且逐项做内网/保留地址字面量判定；
      不在白名单的 URL 一律拒绝（SSRF 闸门，与短链解析同一思想）。
    - **请求头必须带 Referer**：B站 CDN 校验 Referer，缺了 403。

WBI 签名（bilibili-API-collect 标准）：
    img_key/sub_key 取自 nav 接口 `wbi_img.img_url/sub_url` 的文件名（去扩展名）；
    混淆表重排 `img_key + sub_key` 取前 32 位得 mixin_key；
    参数加 `wts` 后按 key 排序 urlencode（value 剔除 `!'()*`），
    `w_rid = md5(query + mixin_key)`。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

try:  # 包式加载（Runner 主路径）：命名空间化，避免跨插件顶层模块撞名
    from .music_api import MusicAPIResponseError, SongInfo
    from .url_parser import PLATFORM_BILI
except ImportError:  # 平铺兜底：脚本直跑 / 测试
    from music_api import MusicAPIResponseError, SongInfo
    from url_parser import PLATFORM_BILI

logger = logging.getLogger("plugin.music-request.bilibili")

REQUEST_TIMEOUT = 10.0

_BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    # B站 API 与 CDN 都校验 Referer，缺了 403
    "Referer": "https://www.bilibili.com/",
}

_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
_SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
#: 取 cid 的主通路：`player/pagelist` 只返回分P列表（cid / duration），
#: 正是我们需要的全部信息，且实测不受 `web-interface/view` 那套风控影响。
#: 真机 2026-10-21 实测（同一 IP、同一 cookie、相隔数秒）：
#:   view            → HTTP 412
#:   player/pagelist → HTTP 200（data[0].cid）
#:   web-interface/wbi/view → HTTP 200
_PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist"
#: 取 cid 的兜底通路：WBI 版 view，与搜索接口同一套签名通路。
_WBI_VIEW_URL = "https://api.bilibili.com/x/web-interface/wbi/view"
_PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"

# WBI 密钥缓存时长（秒）：密钥每日轮换，30 分钟内复用足够
_WBI_TTL_SECONDS = 1800

# 搜索响应里标题的高亮标签（命中关键词被 <em class="keyword"> 包住）
_RE_EM_TAG = re.compile(r"</?em[^>]*>")

# BV 号格式：BV + 10 位字母数字
_RE_BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")

# av 号格式：可带 av 前缀，也允许纯数字（VCPedia 两种写法都有）
_RE_AVID = re.compile(r"^(?:av)?(\d{1,12})$", re.I)

# 搜索结果里允许的时长展示格式：MM:SS / H:MM:SS（元素也可能直接给秒数）
_RE_DURATION_TEXT = re.compile(r"^\d{1,2}(?::\d{1,2}){1,2}$")

# 音频 CDN 白名单后缀（host == 后缀 或 host 以 "." + 后缀 结尾）。
# 覆盖 upos-sz-*.bilivideo.com / upos-sz-mirrorakam.akamaized.net /
# *.bilivideo.cn 等全部已知播放 CDN；后缀匹配挡住 bilivideo.com.evil.com 伪装。
_BILI_CDN_SUFFIXES = ("bilivideo.com", "bilivideo.cn", "akamaized.net")

# 内网 / 保留地址字面量（纵深防御；主防线是 CDN 后缀白名单本身）
_RE_PRIVATE_HOST = re.compile(
    r"^(?:localhost|0\.|10\.|127\.|169\.254\.|192\.168\."
    r"|172\.(?:1[6-9]|2\d|3[01])\.|\[?::1\]?$|\[?fc|\[?fd)",
    re.IGNORECASE,
)

# WBI 混淆表（bilibili-API-collect 固定常量；nav 接口约定的一部分）
_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

# WBI 签名时从参数值里剔除的字符（官方前端行为）
_WBI_STRIP_CHARS = "!'()*"


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """按混淆表重排 `img_key + sub_key` 并取前 32 位。"""
    full = f"{img_key}{sub_key}"
    return "".join(full[i] for i in _MIXIN_KEY_ENC_TAB if i < len(full))[:32]


def sign_wbi(
    params: dict[str, Any],
    img_key: str,
    sub_key: str,
    *,
    wts: int | None = None,
) -> dict[str, str]:
    """给请求参数附加 WBI 签名（wts + w_rid）。

    Args:
        params: 业务参数（会被复制，不改动原 dict）。
        img_key / sub_key: nav 接口下发的两把 WBI 密钥。
        wts: 测试注入用的时间戳；None 取当前时间。

    Returns:
        带 `wts` 与 `w_rid` 的新参数字典（值全为 str）。
    """
    wts_value = int(time.time()) if wts is None else int(wts)
    merged: dict[str, str] = {str(k): str(v) for k, v in params.items()}
    merged["wts"] = str(wts_value)
    # value 剔除特殊字符后按 key 排序 urlencode
    cleaned = sorted(
        (key, "".join(ch for ch in value if ch not in _WBI_STRIP_CHARS))
        for key, value in merged.items()
    )
    from urllib.parse import urlencode

    query = urlencode(cleaned)
    w_rid = hashlib.md5((query + get_mixin_key(img_key, sub_key)).encode()).hexdigest()
    out = dict(merged)
    out["w_rid"] = w_rid
    return out


def body_snippet(resp: httpx.Response, limit: int = 120) -> str:
    """取响应体前若干字符用于诊断（压成单行，B站 412 返回的是整页 HTML）。"""
    try:
        text = resp.text
    except Exception:  # pragma: no cover - 依赖 httpx 内部解码失败
        return ""
    return " ".join(str(text).split())[:limit]


def cid_from_pagelist(payload: Any) -> int | None:
    """从 `player/pagelist` 响应取第一分 P 的 cid。

    结构：`{"code":0,"data":[{"cid":3307690,"page":1,"part":"",...}]}`
    优先取 `page == 1` 的条目；列表里找不到 page 字段时退回第一条。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None
    for entry in data:
        if isinstance(entry, dict) and entry.get("page") == 1:
            cid = _positive_int(entry.get("cid"))
            if cid is not None:
                return cid
    first = data[0]
    return _positive_int(first.get("cid")) if isinstance(first, dict) else None


def duration_from_pagelist(payload: Any) -> int | None:
    """从 `player/pagelist` 响应累加总时长（秒）；无有效数据返回 None。

    多P视频累加所有分P——直连播放只取 P1，但时长闸宁严勿松：
    真机实测「洛天依带你铁道唱歌」系列单P就有 8.3 分钟（约 19MB），
    在 60 秒上传预算下传不完，应提前回退而不是白等一次超时。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None
    total = 0
    for entry in data:
        if isinstance(entry, dict):
            value = entry.get("duration")
            if isinstance(value, (int, float)) and value > 0:
                total += int(value)
    return total or None


def cid_from_view(payload: Any) -> int | None:
    """从 `web-interface/view`（含 wbi 版）响应取 cid：`{"data":{"cid":...}}`。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    return _positive_int(data.get("cid")) if isinstance(data, dict) else None


def _positive_int(value: Any) -> int | None:
    """转正整数；0 / 负数 / 非数字一律视为无效（cid 不可能是 0）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_duration_seconds(raw: Any) -> int:
    """把 B站的时长表示转成秒：`"05:30"` → 330，`"1:02:03"` → 3723，`330` → 330。

    解析不了返回 0（调用方把 0 视为「未知时长，不过滤」）。
    """
    text = str(raw or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    if not _RE_DURATION_TEXT.match(text):
        return 0
    total = 0
    for part in text.split(":"):
        total = total * 60 + int(part)
    return total


def clean_search_title(raw: Any) -> str:
    """去掉搜索结果标题里的关键词高亮标签并压缩空白。"""
    text = _RE_EM_TAG.sub("", str(raw or ""))
    return " ".join(text.split())


def is_bili_audio_url(url: str) -> bool:
    """播放直链 host 是否在 B站 CDN 白名单内且非内网地址（SSRF 闸门）。"""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return False
    if not host or _RE_PRIVATE_HOST.match(host):
        return False
    try:  # 字面量 IP 一律拒绝（CDN 域名不会是 IP 形态）
        import ipaddress

        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return any(
        host == suffix or host.endswith("." + suffix) for suffix in _BILI_CDN_SUFFIXES
    )


def is_valid_bvid(bvid: Any) -> bool:
    """BV 号格式校验（挡住把任意字符串塞进 view/playurl 接口）。"""
    return bool(_RE_BVID.match(str(bvid or "")))


def normalize_video_id(value: Any) -> tuple[str, str]:
    """把 B站视频标识归一化成 `(kind, id)`，kind 为 `"bvid"` / `"aid"` / `""`。

    为什么必须同时支持两种：VCPedia 词条信息框里 **11/13 写的是 av 号**
    （`bb_id = av2075941`），只有少数写 BV。

    Examples:
        `"BV1zs411S7sz"` → `("bvid", "BV1zs411S7sz")`
        `"av2129461"` / `"2129461"` → `("aid", "2129461")`
        其它 → `("", "")`
    """
    text = str(value or "").strip()
    if not text:
        return "", ""
    if _RE_BVID.match(text):
        return "bvid", text
    m = _RE_AVID.match(text)
    if m:
        return "aid", m.group(1)
    return "", ""


#: BV 号编码表（社区公开算法，与 bilibili-API-collect 记载一致）
_BV_TABLE = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
#: BV 号里承载 aid 的 6 个字符位置
_BV_POS = (11, 10, 3, 8, 4, 6)
_BV_XOR = 177451812
_BV_ADD = 8728348608


def av2bv(aid: Any) -> str:
    """av 号 → BV 号（纯计算，零网络）。

    **为什么必须转换**：`player/playurl` 不接受 `aid=` 参数（实测返回
    `code=-400 请求错误`），而 `player/pagelist` 接受。VCPedia 里 11/13 写的
    是 av 号，不转就取不到音频流——这条实测结论由
    `tests/test_bilibili.py::test_aid_converted_to_bvid_for_requests` 守着。

    算法经两重验证：
        1. `av2129461 → BV1zs411S7sz` 与已知对应精确吻合；
        2. 权御天下 / 达拉崩吧 / 万古生香 / 大氿歌 / 九九八十一 五首，
           用 aid 与转换出的 bvid 查 cid 全部一致。

    Raises:
        ValueError: aid 不是整数。
    """
    mixed = (int(aid) ^ _BV_XOR) + _BV_ADD
    chars = list("BV1  4 1 7  ")
    for index, pos in enumerate(_BV_POS):
        chars[pos] = _BV_TABLE[mixed // (58 ** index) % 58]
    return "".join(chars)


def bv2av(bvid: Any) -> int | None:
    """BV 号 → av 号（给测试与诊断用；非法 BV 返回 None）。"""
    text = str(bvid or "")
    if not _RE_BVID.match(text):
        return None
    mixed = 0
    for index, pos in enumerate(_BV_POS):
        char = text[pos]
        if char not in _BV_TABLE:
            return None
        mixed += _BV_TABLE.index(char) * (58 ** index)
    return (mixed - _BV_ADD) ^ _BV_XOR


def resolve_video_bvid(video_id: Any) -> str:
    """把 BV / av / 纯数字标识统一解析成 BV 号；非法返回空串。

    统一成 BV 再发请求，是因为 playurl 只认 bvid（见 `av2bv`）。
    """
    kind, ident = normalize_video_id(video_id)
    if kind == "bvid":
        return ident
    if kind == "aid":
        return av2bv(int(ident))
    return ""


def songs_from_search_payload(
    data: Any,
    *,
    limit: int,
    max_duration_seconds: int = 0,
) -> list[SongInfo]:
    """把 search/type 响应解析成候选列表（纯函数，可离线单测）。

    解析规则：
        - 只取 `data.result` 里 `type == "video"` 的条目；
        - 标题去掉 `<em>` 高亮标签；UP主 写入 artists（相关度排序直接复用）；
        - 时长超过 `max_duration_seconds`（配置上限，>0 时生效）的候选剔除；
        - **先按播放量降序稳定排序再返回**：上层 `rank_songs` 按相关度排序时
          同分候选保持该顺序，等于「相关度并列时播放量高的优先」。

    Raises:
        MusicAPIResponseError: 响应结构完全不符合协议时。
    """
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise MusicAPIResponseError("B站搜索响应不是预期结构", detail=str(data)[:200])
    payload = data["data"]
    result = payload.get("result")
    if result is None:
        return []  # 无结果时 result 缺省，不是协议错误
    if not isinstance(result, list):
        raise MusicAPIResponseError(
            f"B站搜索 result 不是列表: type={type(result).__name__}"
        )

    parsed: list[tuple[int, SongInfo]] = []
    for item in result:
        if not isinstance(item, dict) or str(item.get("type") or "") != "video":
            continue
        bvid = str(item.get("bvid") or "")
        title = clean_search_title(item.get("title"))
        if not is_valid_bvid(bvid) or not title:
            continue
        duration = parse_duration_seconds(item.get("duration"))
        if max_duration_seconds > 0 and duration > max_duration_seconds:
            continue
        author = str(item.get("author") or "")
        try:
            plays = int(item.get("play") or 0)
        except (TypeError, ValueError):
            plays = 0
        parsed.append((plays, SongInfo(
            song_id=bvid, name=title, artists=author, platform=PLATFORM_BILI,
        )))

    parsed.sort(key=lambda pair: pair[0], reverse=True)  # 稳定排序：同播放量保持 API 序
    return [song for _plays, song in parsed[:max(int(limit), 1)]]


def _url_host(url: str) -> str:
    """取 URL 的 netloc（含端口）用于诊断；解析失败返回占位符。"""
    try:
        return urlparse(str(url or "")).netloc or "(无 host)"
    except Exception:
        return "(无法解析)"


def audio_stream_urls(stream: Any) -> list[str]:
    """一个 dash stream 的候选直链：`baseUrl` 在前，`backupUrl[]` 依次在后。

    必须把备份地址纳入候选，原因是 B站会按网络环境差异化下发：
    对家宽 / 命中 PCDN 调度的客户端，`baseUrl` 指向 B站自建 PCDN 节点
    （形如 `*.edge.mountaintoys.cn:4483`、参数带 `os=mcdn`），而同一 stream
    的 `backupUrl` 通常仍指向官方 `*.bilivideo.com`。只读 `baseUrl` 会把
    「有官方备份可用」误判成「没有可用音频轨」——真机 2026-10-21 即如此
    （三条音频轨 30216/30232/30280 全被白名单拒掉）。

    不放松白名单：PCDN 域名不在 `_BILI_CDN_SUFFIXES` 内，这里只是让官方
    备份地址有机会被选中。
    """
    urls: list[str] = []
    if not isinstance(stream, dict):
        return urls
    base = str(stream.get("baseUrl") or stream.get("base_url") or "").strip()
    if base:
        urls.append(base)
    backups = stream.get("backupUrl") or stream.get("backup_url") or []
    if isinstance(backups, list):
        for item in backups:
            url = str(item or "").strip()
            if url and url not in urls:
                urls.append(url)
    return urls


def audio_stream_from_playurl_payload(data: Any) -> str | None:
    """从 playurl 响应里挑最高码率的音频直链（纯函数，可离线单测）。

    `dash.audio[]` 每项带 `id`（30216=64k / 30232=132k / 30280=192k /
    30250 杜比 / 30251 Hi-Res）与 `bandwidth`；按 `(bandwidth, id)` 取最大。
    只取音频轨，不做音视频拼接。

    每条 stream 的候选地址来自 `baseUrl` + `backupUrl[]`（见
    `audio_stream_urls`）；同一 stream 内部 baseUrl 优先，跨 stream 之间
    带宽优先。拿到后必须过 `is_bili_audio_url` 白名单——接口返回的 URL
    也可能被利用。
    """
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        return None
    dash = data["data"].get("dash")
    if not isinstance(dash, dict):
        return None
    audio_list = dash.get("audio")
    if not isinstance(audio_list, list) or not audio_list:
        return None

    best: tuple[int, int, str] | None = None
    rejected_hosts: list[str] = []
    for stream in audio_list:
        if not isinstance(stream, dict):
            continue
        try:
            bandwidth = int(stream.get("bandwidth") or 0)
        except (TypeError, ValueError):
            bandwidth = 0
        try:
            stream_id = int(stream.get("id") or 0)
        except (TypeError, ValueError):
            stream_id = 0
        for url in audio_stream_urls(stream):
            if not is_bili_audio_url(url):
                rejected_hosts.append(_url_host(url))
                continue
            # 同一条 stream 内 baseUrl 在前、backupUrl 在后，命中即停
            if best is None or (bandwidth, stream_id) > (best[0], best[1]):
                best = (bandwidth, stream_id, url)
            break

    if best is None:
        if rejected_hosts:
            # 把 host 打出来：下一轮若 backupUrl 也是 PCDN，凭这行日志就能定案，
            # 不必再让用户重放点歌去猜。
            logger.warning(
                "B站音频直链全部不在 CDN 白名单（含 backupUrl），涉及 host: %s",
                ", ".join(sorted(set(rejected_hosts))),
            )
        return None
    return best[2]


class BilibiliClient:
    """B站搜索与音频提取客户端。

    Args:
        sessdata: SESSDATA Cookie（登录态）。空串 = 匿名（音质上限 64k，
            风控概率更高，但可用）。
        buvid3: buvid3 Cookie。留空时首次请求前经 SPI 接口自动获取。
        transport: 可选的 httpx transport（仅测试注入用，生产传 None）。
    """

    def __init__(
        self,
        sessdata: str = "",
        buvid3: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        cookies = httpx.Cookies()
        # 必须逐条 set domain：直接传 dict 会创建「无 domain」cookie，
        # httpx 会把它附加到该 client 的所有请求上（凭据外发风险，与
        # 网易云 / QQ音乐同一条纪律）
        self._sessdata_configured = bool(sessdata)
        if sessdata:
            cookies.set("SESSDATA", sessdata, domain=".bilibili.com", path="/")
        self._buvid3_configured = bool(buvid3)
        if buvid3:
            cookies.set("buvid3", buvid3, domain=".bilibili.com", path="/")
        self._spi_done = self._buvid3_configured
        self._client = httpx.AsyncClient(
            headers=_BILI_HEADERS, cookies=cookies, timeout=REQUEST_TIMEOUT,
            transport=transport,
        )
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_fetched_at = 0.0
        self._cid_cache: dict[str, int] = {}
        # 视频总时长（秒）：pagelist 顺带返回，直连前用它做时长闸
        self._duration_cache: dict[str, int] = {}
        # nav 接口回显的登录态：None = 还没查过，True/False = 实测结果。
        # 配了 SESSDATA 但 isLogin=false 是 412 的头号真凶（cookie 过期后
        # B站仍返回 HTTP 200 + code=-101，不会报错，只会静默降级成匿名）。
        self._login_state: bool | None = None
        # 最近一次取流失败的原因；由 get_audio_url 写入供上层展示
        self.last_error: str = ""

    @property
    def sessdata_configured(self) -> bool:
        """是否配置了登录态（仅用于状态展示）。"""
        return self._sessdata_configured

    @property
    def login_state(self) -> bool | None:
        """nav 接口实测的登录态：None=未查，True=已登录，False=已失效。"""
        return self._login_state

    def _credential_hint(self) -> str:
        """风控/失败时给出可操作的凭据诊断（不带任何 cookie 值）。"""
        if not self._sessdata_configured:
            return "匿名态（未配置 bilibili.sessdata），风控概率较高"
        if self._login_state is False:
            return "已配置 SESSDATA 但 nav 回显 isLogin=false，登录态已失效，请更新 cookie"
        if self._login_state is None:
            return "已配置 SESSDATA 但尚未验证有效性"
        return "已配置 SESSDATA 且登录态有效，可能是请求过频"

    async def close(self) -> None:
        """关闭 HTTP 连接池。"""
        try:
            await self._client.aclose()
        except Exception:
            logger.debug("关闭 B站客户端失败", exc_info=True)

    # ---------- 内部：凭据与签名 ----------

    async def _ensure_buvid3(self, *, force: bool = False) -> bool:
        """匿名场景先经 SPI 接口拿 buvid3（搜索接口没有它会被风控 412）。

        Args:
            force: 命中 412 后重新领取一次 buvid3/4。B站的软风控认设备指纹，
                刷新 buvid 是官方前端在 412 页面上的实际动作，因此这是
                唯一有实证依据的自救手段。

        Returns:
            True 表示本次确实拿到了新的 buvid3；False 表示跳过或失败。
        """
        if self._spi_done and not force:
            return False
        self._spi_done = True  # 失败也不再重试：每次搜索都打 SPI 反而更招风控
        try:
            resp = await self._client.get(_SPI_URL)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning(
                "获取 buvid3 失败（SPI 接口不可达）: %s%s",
                type(exc).__name__,
                "（412 自救刷新）" if force else "",
            )
            return False
        data = payload.get("data") if isinstance(payload, dict) else None
        b3 = str(data.get("b_3") or "") if isinstance(data, dict) else ""
        b4 = str(data.get("b_4") or "") if isinstance(data, dict) else ""
        if not b3:
            logger.warning("SPI 接口响应缺少 b_3（buvid3 未取得）")
            return False
        self._client.cookies.set("buvid3", b3, domain=".bilibili.com", path="/")
        if b4:
            self._client.cookies.set("buvid4", b4, domain=".bilibili.com", path="/")
        logger.info("已自动获取 buvid3（SPI 接口）%s", "——412 自救刷新" if force else "")
        return True

    async def _get_wbi_keys(self) -> tuple[str, str]:
        """取（带缓存的）WBI 密钥对。"""
        now = time.monotonic()
        if self._wbi_keys is not None and now - self._wbi_fetched_at < _WBI_TTL_SECONDS:
            return self._wbi_keys
        try:
            resp = await self._client.get(_NAV_URL)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise MusicAPIResponseError(f"获取 WBI 密钥失败: {type(exc).__name__}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        # nav 同时是最好的登录态探针：配了 SESSDATA 却 isLogin=false 说明
        # cookie 已过期。B站对过期 cookie 仍返回 HTTP 200（只把 code 置 -101），
        # 静默退化成匿名额度——412 概率骤升却完全不报错，必须显式记下来。
        if isinstance(data, dict) and isinstance(data.get("isLogin"), bool):
            self._login_state = bool(data["isLogin"])
            if self._sessdata_configured and not self._login_state:
                logger.warning(
                    "B站 SESSDATA 已失效（nav.isLogin=false code=%r）：当前等同匿名，"
                    "412 概率升高且音质封顶 64k，请更新 bilibili.sessdata",
                    payload.get("code"),
                )
        elif self._sessdata_configured and payload.get("code") not in (0, None):
            self._login_state = False
            logger.warning(
                "B站 nav 返回异常（code=%r）：SESSDATA 可能已失效", payload.get("code")
            )
        wbi = data.get("wbi_img") if isinstance(data, dict) else None
        if not isinstance(wbi, dict):
            raise MusicAPIResponseError(
                "nav 响应缺少 wbi_img",
                detail=f"code={payload.get('code')!r} msg={payload.get('message')!r}",
            )

        def key_from_url(url: Any) -> str:
            path = str(url or "").split("?")[0]
            filename = path.rsplit("/", 1)[-1]
            return filename.rsplit(".", 1)[0]

        img_key = key_from_url(wbi.get("img_url"))
        sub_key = key_from_url(wbi.get("sub_url"))
        if not img_key or not sub_key:
            raise MusicAPIResponseError("WBI 密钥为空", detail=str(wbi)[:200])
        self._wbi_keys = (img_key, sub_key)
        self._wbi_fetched_at = now
        return self._wbi_keys

    async def _signed_get(
        self,
        url: str,
        params: dict[str, Any],
        what: str,
        *,
        retry_412: bool = True,
    ) -> dict[str, Any]:
        """带 WBI 签名与 buvid3 前置的 GET，返回校验过 code 的 JSON。

        Args:
            url / params: 目标接口与业务参数（签名由本方法附加）。
            what: 日志与错误信息里的操作描述，如 `"搜索 '普通DISCO'"`。
            retry_412: 命中 HTTP 412 时是否刷新 buvid3 后重试一次。
                只重试一次——B站的软风控不会因为反复尝试而放宽，
                连续重试只会把设备指纹打得更死。

        Raises:
            MusicAPIResponseError: 超时、风控、协议不符或业务 code 非 0。
        """
        await self._ensure_buvid3()
        img_key, sub_key = await self._get_wbi_keys()
        signed = sign_wbi(params, img_key, sub_key)
        try:
            resp = await self._client.get(url, params=signed)
        except httpx.TimeoutException as exc:
            raise MusicAPIResponseError(f"B站{what}超时") from exc
        except httpx.RequestError as exc:
            raise MusicAPIResponseError(
                f"B站{what}网络异常 ({type(exc).__name__})"
            ) from exc
        if resp.status_code == 412:
            if retry_412:
                refreshed = await self._ensure_buvid3(force=True)
                logger.warning(
                    "B站%s命中 HTTP 412，已刷新 buvid3=%s 后重试一次（%s）",
                    what, "成功" if refreshed else "失败", self._credential_hint(),
                )
                return await self._signed_get(url, params, what, retry_412=False)
            raise MusicAPIResponseError(
                f"B站{what}触发风控（HTTP 412，已重试 1 次）",
                detail=self._credential_hint(),
            )
        try:
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise MusicAPIResponseError(
                f"B站{what}响应异常: HTTP {resp.status_code}",
                detail=body_snippet(resp),
            ) from exc
        if not isinstance(payload, dict):
            raise MusicAPIResponseError(f"B站{what}响应不是对象", detail=str(payload)[:200])
        code = payload.get("code")
        if code == -412:
            raise MusicAPIResponseError(f"B站{what}触发风控（code=-412），请稍后再试")
        if code not in (0, None):
            raise MusicAPIResponseError(
                f"B站{what}业务失败: code={code!r} message={payload.get('message')!r}"
            )
        return payload

    # ---------- 对外：搜索 / cid / 音频直链 ----------

    async def search_videos(
        self, query: str, limit: int = 5, *, max_duration_seconds: int = 0
    ) -> list[SongInfo]:
        """搜索 B站视频（按相关度返回，播放量降序预排）。

        Args:
            query: 搜索关键词。
            limit: 返回数量上限。
            max_duration_seconds: 时长上限（秒），0 = 不过滤。

        Raises:
            MusicAPIResponseError: 风控、超时或协议不符。
        """
        payload = await self._signed_get(
            _SEARCH_URL,
            {
                "search_type": "video",
                "keyword": query,
                "page": 1,
                # 多取一倍做时长过滤的余量；接口上限 50
                "page_size": min(max(int(limit), 1) * 2, 50),
            },
            f"搜索 {query!r}",
        )
        return songs_from_search_payload(
            payload, limit=limit, max_duration_seconds=max_duration_seconds
        )

    async def get_video_cid(self, video_id: str) -> int:
        """查视频的 cid（第一分 P；带进程内缓存）。

        `video_id` 接受 BV 号或 av 号（含纯数字），内部统一转成 BV 再发请求
        （playurl 只认 bvid，见 `av2bv`）。

        两条通路依次尝试，任一成功即返回：

        1. `player/pagelist`（主）：只需 cid 与时长，响应体极小，且实测不受
           `web-interface/view` 那套风控影响；
        2. `web-interface/wbi/view`（兜底）：WBI 版 view，与搜索同一签名通路。

        为什么不再用 `web-interface/view`：真机 2026-10-21 实测，同一台机器、
        同一份 cookie、相隔数秒，`view` 稳定 412 而 `pagelist` / `wbi/view`
        都是 200。换接口比在风控上硬磕有效——这一点由
        `test_primary_cid_path_is_pagelist` 钉死，防止有人改回去。

        Raises:
            MusicAPIResponseError: 标识非法，或两条通路均失败（错误里带各自原因）。
        """
        bvid = resolve_video_bvid(video_id)
        if not bvid:
            raise MusicAPIResponseError(f"非法的 B站视频标识: {video_id!r}")
        if bvid in self._cid_cache:
            return self._cid_cache[bvid]

        params = {"bvid": bvid}
        attempts = (
            (_PAGELIST_URL, f"查分P列表（{bvid}）", cid_from_pagelist),
            (_WBI_VIEW_URL, f"查视频信息（{bvid}）", cid_from_view),
        )
        failures: list[str] = []
        for url, what, extractor in attempts:
            try:
                payload = await self._signed_get(url, params, what)
            except MusicAPIResponseError as exc:
                failures.append(f"{what}: {exc.diagnostic()}")
                logger.warning("取 cid 通路失败，换下一条: %s", failures[-1])
                continue
            cid = extractor(payload)
            if cid is not None:
                self._cid_cache[bvid] = cid
                # pagelist 自带 duration，顺手存下——省一次请求
                duration = duration_from_pagelist(payload)
                if duration:
                    self._duration_cache[bvid] = duration
                return cid
            failures.append(f"{what}: 响应里没有有效 cid")
            logger.warning("取 cid 通路返回异常结构: bvid=%s url=%s", bvid, url)

        raise MusicAPIResponseError(
            f"B站取 cid 失败（{len(attempts)} 条通路均不可用）: {video_id}",
            detail=" ; ".join(failures)[:300],
        )

    async def get_video_duration(self, video_id: str) -> int | None:
        """取视频总时长（秒）；拿不到返回 None。

        命中缓存直接返回；否则触发一次 `get_video_cid`——pagelist 通路会把
        `duration` 顺带填进缓存，所以**不会多花一次请求**。

        用途：VCPedia 直连前判断这首能不能在 60 秒上传预算内发完。
        曲库的 `duration` 字段实测 100% 为空，只能现场查。
        """
        bvid = resolve_video_bvid(video_id)
        if not bvid:
            return None
        if bvid in self._duration_cache:
            return self._duration_cache[bvid]
        try:
            await self.get_video_cid(video_id)
        except MusicAPIResponseError:
            return None
        return self._duration_cache.get(bvid)

    async def get_audio_url(self, video_id: str) -> str | None:
        """取视频里最高码率的音频直链（DASH 音频轨，容器即 m4a）。

        `video_id` 接受 BV 号或 av 号（含纯数字）。

        Returns:
            通过 CDN 白名单校验的音频直链；拿不到返回 None。

        Note:
            失败原因写入 `self.last_error`。本方法按契约返回 None 而不抛异常，
            但上游此前只看到「未返回可用音频」——412、视频失效、无音频轨
            三种完全不同的故障在用户侧长得一模一样（真机 2026-10-21 正是
            如此）。所以这里必须把原因落在对象上，供展示层读取。
        """
        self.last_error = ""
        try:
            cid = await self.get_video_cid(video_id)
            bvid = resolve_video_bvid(video_id)
            payload = await self._signed_get(
                _PLAYURL_URL,
                {"bvid": bvid, "cid": str(cid), "qn": "64", "fnval": "16", "fnver": "0", "fourk": "1"},
                f"取音频流（{bvid}）",
            )
        except MusicAPIResponseError as exc:
            self.last_error = exc.diagnostic()
            logger.warning(
                "B站取音频流失败: %s %s: %s", video_id, type(exc).__name__, self.last_error
            )
            return None
        url = audio_stream_from_playurl_payload(payload)
        if url is None:
            self.last_error = "playurl 未返回可用音频轨（可能是付费/充电视频或 dash 缺 audio）"
            logger.info("B站未返回可用音频轨: id=%s（%s）", video_id, self.last_error)
        return url
