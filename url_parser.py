"""音乐链接与分享卡片解析（纯逻辑，零第三方依赖，可离线单测）。

本模块不触碰网络与 SDK：
    - `parse_music_url`  从任意文本里识别音乐链接，抽出 (平台, 歌曲ID)
    - `extract_urls`     从文本里抓出全部 URL（并剥掉尾部中文标点）
    - `parse_music_card_text`  从适配器转成纯文本的分享卡片里认出歌名/歌手

短链（163cn.tv / c6.y.qq.com）单独标记为 `short=True`，由调用方走
`MusicSearchClient.resolve_short_url` 拿到真实链接后再解析一次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 平台标识
PLATFORM_NETEASE = "163"
PLATFORM_QQ = "qq"

#: 短链解析目标的域名白名单（防 SSRF：短链不得把请求导向内网/元数据地址）
ALLOWED_MUSIC_HOSTS = frozenset({
    "music.163.com",
    "y.music.163.com",
    "y.qq.com",
    "i.y.qq.com",
    "c6.y.qq.com",
})


@dataclass
class MusicRef:
    """一条音乐链接的解析结果。"""

    platform: str          # "163" 或 "qq"
    song_id: str           # 网易云为数字 ID；QQ音乐为 songmid；短链为短链 ID
    short: bool = False    # True 表示这是短链，song_id 并非真实歌曲 ID

    @property
    def is_short(self) -> bool:
        return self.short


@dataclass
class MusicCardInfo:
    """从纯文本中识别出的音乐分享卡片。"""

    platform: str = ""                 # "163" / "qq" / ""（无法判定）
    song_name: str = ""
    artist: str = ""
    url: str = ""                      # 分享文本里附带的链接（可能含精确歌曲 ID）

    @property
    def query(self) -> str:
        """用于搜索的关键词：有歌手时带上歌手，提高命中率。"""
        return f"{self.song_name} {self.artist}".strip() if self.artist else self.song_name


# ===== URL 解析 =====

# 网易云：music.163.com/song?id=123、music.163.com/#/song?id=123、
#         music.163.com/m/song?id=123、y.music.163.com/m/song?id=123
_NETEASE_SONG_RE = re.compile(r"(?:y\.)?music\.163\.com/(?:#/)?(?:m/)?song\?id=(\d+)")

# 网易云短链：163cn.tv/xxxx、163cn.tv/a/xxxx（可能带多级路径）
_NETEASE_SHORT_RE = re.compile(r"163cn\.tv/([A-Za-z0-9]+(?:/[A-Za-z0-9]+)*)")

# QQ音乐短链：c6.y.qq.com/base/fcgi-bin/u?__=xxx
_QQ_SHORT_RE = re.compile(r"c\d+\.y\.qq\.com/base/fcgi-bin/u\?__=([A-Za-z0-9+/=_-]+)")

# QQ音乐卡片 jumpUrl：i.y.qq.com/v8/playsong.html?songmid=MID
_QQ_PLAYSONG_RE = re.compile(r"y\.qq\.com/v8/playsong\.html\?[^\s]*?songmid=([A-Za-z0-9]+)")

# QQ音乐详情页：y.qq.com/n/ryqq/songDetail/MID
_QQ_DETAIL_RE = re.compile(r"y\.qq\.com/n/(?:ryqq/)?songDetail/([A-Za-z0-9]+)")

# QQ音乐移动端详情页：y.qq.com/n/m/detail/song/MID
_QQ_DETAIL_M_RE = re.compile(r"y\.qq\.com/n/m/detail/song/([A-Za-z0-9]+)")

# 顺序有讲究：短链必须排在长链之前，否则短链会先被别的规则吃掉
_URL_RULES: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (_NETEASE_SONG_RE, PLATFORM_NETEASE, False),
    (_NETEASE_SHORT_RE, PLATFORM_NETEASE, True),
    (_QQ_SHORT_RE, PLATFORM_QQ, True),
    (_QQ_PLAYSONG_RE, PLATFORM_QQ, False),
    (_QQ_DETAIL_RE, PLATFORM_QQ, False),
    (_QQ_DETAIL_M_RE, PLATFORM_QQ, False),
)


def parse_music_url(text: str) -> MusicRef | None:
    """从文本中解析音乐链接。

    支持网易云（标准页 / 移动页 / 卡片 jumpUrl / 163cn.tv 短链）与
    QQ音乐（详情页 / 移动详情页 / playsong jumpUrl / c6.y.qq.com 短链）。

    Args:
        text: 任意包含链接的文本。

    Returns:
        MusicRef；未识别到音乐链接返回 None。短链的 `short` 为 True，
        调用方需先做重定向解析再二次调用本函数。
    """
    if not text:
        return None
    for pattern, platform, is_short in _URL_RULES:
        match = pattern.search(text)
        if match:
            song_id = match.group(1)
            if not song_id:
                continue
            return MusicRef(platform=platform, song_id=song_id, short=is_short)
    return None


#: 从 URL 尾部逐个剥掉的中英文标点（逐个剥，避免误删合法字符）
_TRAILING_PUNCTUATION = frozenset("。，、！？；：""''））》」』,;!?.")

#: URL 允许的字符集（RFC 3986 的 unreserved + 保留字 + 转义符）。
#: **必须用白名单**：中文等字符在 URL 里非法，但会被 `[^\s]+` 这类排除式正则
#: 一路吃进去，于是 "…?id=1。还有这个" 会整段被当成 URL，短链解析就会请求错地址。
_URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
_URL_RE = re.compile(rf"https?://[{_URL_CHARS}]+|www\.[{_URL_CHARS}]+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """提取文本中全部 URL，并剥掉尾部粘连的标点。

    Args:
        text: 待提取文本。

    Returns:
        URL 列表（保持出现顺序）。
    """
    if not text:
        return []
    cleaned: list[str] = []
    for url in _URL_RE.findall(text):
        while url and url[-1] in _TRAILING_PUNCTUATION:
            url = url[:-1]
        if url:
            cleaned.append(url)
    return cleaned


# ===== 分享卡片文本解析 =====

# [QQ音乐] 歌名 - 歌手  /  [网易云音乐] 歌名 - 歌手  /  [音乐分享] 歌名 - 歌手
# 歌名用贪婪匹配，以最后一个 " - " 作分隔，避免歌名自带横杠时被截断
_CARD_TEXT_RE = re.compile(r"\[(QQ音乐|网易云音乐|音乐分享)\]\s*(.+)\s+-\s+(.+)$")

# [小程序] QQ音乐：歌名 - 歌手
_MINAPP_CARD_RE = re.compile(r"\[小程序\]\s*(QQ音乐|网易云音乐)\s*[：:]\s*(.+)\s+-\s+(.+)$")

# 分享xxx的单曲《歌名》: URL (来自@网易云音乐)
_NETEASE_SHARE_RE = re.compile(r"分享.+?的单曲《(.+?)》\s*[：:]\s*(https?://[^\s()]+)")

# 分享歌曲 《歌名》 URL @QQ音乐
_QQ_SHARE_RE = re.compile(r"分享歌曲\s*《(.+?)》\s*(https?://\S+)?\s*@?(QQ音乐|网易云音乐)?")

_PLATFORM_BY_TAG: dict[str, str] = {
    "QQ音乐": PLATFORM_QQ,
    "网易云音乐": PLATFORM_NETEASE,
}


def parse_music_card_text(text: str) -> MusicCardInfo | None:
    """从适配器转换后的纯文本中识别音乐分享卡片。

    识别形态：
        - 卡片：[QQ音乐] 歌名 - 歌手
        - 卡片：[网易云音乐] 歌名 - 歌手
        - 卡片：[音乐分享] 歌名 - 歌手（平台未知，用默认平台搜索）
        - 小程序：[小程序] QQ音乐：歌名 - 歌手
        - 网易云分享文本：分享xxx的单曲《歌名》: URL (来自@网易云音乐)
        - QQ音乐分享文本：分享歌曲 《歌名》URL @QQ音乐

    Args:
        text: 消息纯文本。

    Returns:
        MusicCardInfo；不像音乐分享时返回 None。
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None

    match = _CARD_TEXT_RE.match(stripped)
    if match:
        tag, song_name, artist = match.group(1), match.group(2).strip(), match.group(3).strip()
        return MusicCardInfo(
            platform=_PLATFORM_BY_TAG.get(tag, ""),
            song_name=song_name,
            artist=artist,
        )

    match = _MINAPP_CARD_RE.match(stripped)
    if match:
        tag, song_name, artist = match.group(1), match.group(2).strip(), match.group(3).strip()
        return MusicCardInfo(
            platform=_PLATFORM_BY_TAG.get(tag, ""),
            song_name=song_name,
            artist=artist,
        )

    match = _NETEASE_SHARE_RE.search(stripped)
    if match:
        return MusicCardInfo(
            platform=PLATFORM_NETEASE,
            song_name=match.group(1).strip(),
            url=match.group(2).strip(),
        )

    match = _QQ_SHARE_RE.search(stripped)
    if match:
        return MusicCardInfo(
            platform=_PLATFORM_BY_TAG.get((match.group(3) or "").strip(), ""),
            song_name=match.group(1).strip(),
            url=(match.group(2) or "").strip(),
        )

    return None


def is_allowed_music_url(url: str) -> bool:
    """URL 的 host 是否在音乐服务白名单内（短链重定向的 SSRF 闸门）。

    Args:
        url: 待校验 URL。

    Returns:
        在白名单内返回 True。
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in ALLOWED_MUSIC_HOSTS
