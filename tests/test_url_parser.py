"""url_parser 纯逻辑单测（离线，不依赖 SDK、不联网）。

断言刻意写成「反向可验证」：把实现改回旧行为（例如去掉短链优先级、
不剥尾部标点、放宽白名单）时，对应用例必须失败。
"""

from __future__ import annotations

import pytest

from url_parser import (
    MusicCardInfo,
    extract_urls,
    has_card_markup,
    is_allowed_music_url,
    parse_music_card_text,
    parse_music_url,
    strip_card_injected_text,
)


@pytest.mark.parametrize(
    ("text", "platform", "song_id"),
    [
        ("https://music.163.com/#/song?id=12345", "163", "12345"),
        ("https://music.163.com/song?id=12345", "163", "12345"),
        ("https://music.163.com/m/song?id=12345", "163", "12345"),
        ("https://y.music.163.com/m/song?id=987", "163", "987"),
    ],
)
def test_netease_long_links(text: str, platform: str, song_id: str) -> None:
    ref = parse_music_url(text)
    assert ref is not None, f"未识别网易云链接: {text}"
    assert (ref.platform, ref.song_id) == (platform, song_id)
    assert ref.short is False


@pytest.mark.parametrize(
    ("text", "song_id"),
    [
        ("https://y.qq.com/n/ryqq/songDetail/001ABCdef", "001ABCdef"),
        ("https://y.qq.com/n/m/detail/song/002XYZ", "002XYZ"),
        ("https://i.y.qq.com/v8/playsong.html?songmid=003QWE&from=card", "003QWE"),
    ],
)
def test_qq_long_links(text: str, song_id: str) -> None:
    ref = parse_music_url(text)
    assert ref is not None, f"未识别QQ音乐链接: {text}"
    assert (ref.platform, ref.song_id, ref.short) == ("qq", song_id, False)


@pytest.mark.parametrize(
    ("text", "platform"),
    [
        ("https://163cn.tv/abcdEFG", "163"),
        ("https://163cn.tv/a/xyz123", "163"),
        ("https://c6.y.qq.com/base/fcgi-bin/u?__=AbC-1_2", "qq"),
    ],
)
def test_short_links_are_flagged(text: str, platform: str) -> None:
    """短链必须被标记为 short，交给调用方做重定向解析。"""
    ref = parse_music_url(text)
    assert ref is not None, f"未识别短链: {text}"
    assert ref.platform == platform
    assert ref.short is True


def test_short_link_wins_over_generic_rule() -> None:
    """短链规则必须排在长链规则之前——否则 c6.y.qq.com 会被当成普通 QQ 链接。"""
    ref = parse_music_url("https://c6.y.qq.com/base/fcgi-bin/u?__=token")
    assert ref is not None and ref.short is True, "短链规则优先级被破坏"


@pytest.mark.parametrize(
    "text",
    ["今天天气不错", "", "https://example.com/song?id=1", "https://y.qq.com/", "点歌 晴天"],
)
def test_non_music_text_returns_none(text: str) -> None:
    assert parse_music_url(text) is None


def test_extract_urls_strips_trailing_punctuation() -> None:
    text = "听听这个 https://music.163.com/song?id=1。还有 https://y.qq.com/n/ryqq/songDetail/2，" 
    urls = extract_urls(text)
    assert urls == [
        "https://music.163.com/song?id=1",
        "https://y.qq.com/n/ryqq/songDetail/2",
    ], f"尾部标点未被剥净: {urls}"


def test_extract_urls_keeps_legitimate_trailing_chars() -> None:
    """剥标点不能把合法字符也剥掉（如 query 里的 = 和合法路径）。"""
    urls = extract_urls("see https://y.qq.com/n/m/detail/song/abc123")
    assert urls == ["https://y.qq.com/n/m/detail/song/abc123"]


def test_extract_urls_empty_input() -> None:
    assert extract_urls("") == []


def test_card_text_music_card() -> None:
    card = parse_music_card_text("[QQ音乐] 小城夏天 - LBI利比")
    assert isinstance(card, MusicCardInfo)
    assert card.platform == "qq"
    assert card.song_name == "小城夏天"
    assert card.artist == "LBI利比"
    assert card.query == "小城夏天 LBI利比"


def test_card_text_netease_tag() -> None:
    card = parse_music_card_text("[网易云音乐] 我的悲伤是水做的 - ChiliChill")
    assert card is not None
    assert card.platform == "163"
    assert card.query == "我的悲伤是水做的 ChiliChill"


def test_card_text_unknown_platform_tag_keeps_empty() -> None:
    """[音乐分享] 判不出平台，应留空由调用方用默认平台，而不是猜一个。"""
    card = parse_music_card_text("[音乐分享] 某首歌 - 某歌手")
    assert card is not None
    assert card.platform == ""
    assert card.query == "某首歌 某歌手"


def test_card_text_miniapp() -> None:
    card = parse_music_card_text("[小程序] QQ音乐：小城夏天 - LBI利比")
    assert card is not None
    assert (card.platform, card.song_name, card.artist) == ("qq", "小城夏天", "LBI利比")


def test_card_text_netease_share_with_url() -> None:
    card = parse_music_card_text("分享某人的单曲《晴天》: https://163cn.tv/abc (来自@网易云音乐)")
    assert card is not None
    assert card.platform == "163"
    assert card.song_name == "晴天"
    assert card.url == "https://163cn.tv/abc"


def test_card_text_qq_share() -> None:
    card = parse_music_card_text("分享歌曲 《稻香》 https://i.y.qq.com/v8/playsong.html?songmid=abc @QQ音乐")
    assert card is not None
    assert card.platform == "qq"
    assert card.song_name == "稻香"
    assert card.url.startswith("https://i.y.qq.com/")


@pytest.mark.parametrize(
    "text",
    ["", "   ", "普通聊天内容", "[图片]", "分享了一篇文章"],
)
def test_card_text_non_music_returns_none(text: str) -> None:
    assert parse_music_card_text(text) is None


# ---------- 适配器卡片正文：链接尾巴与作用域切分 ----------


def test_card_text_link_tail_is_split_from_artist() -> None:
    """统一 QQ 连接器把 jumpUrl 以「链接: 」拼在正文尾部。

    旧实现会把整条链接吃进歌手名（`LBI利比 链接: https://…`），
    既污染按歌名搜索的召回，又拿不到精确歌曲 ID。
    """
    card = parse_music_card_text(
        "[QQ音乐] 小城夏天 - LBI利比 链接: https://i.y.qq.com/v8/playsong.html?songmid=003QWE"
    )
    assert card is not None
    assert card.song_name == "小城夏天"
    assert card.artist == "LBI利比", f"歌手名被链接污染: {card.artist!r}"
    assert card.query == "小城夏天 LBI利比"
    assert card.url == "https://i.y.qq.com/v8/playsong.html?songmid=003QWE"


def test_card_text_link_tail_trailing_punctuation_stripped() -> None:
    """链接尾巴后跟中文标点时要剥掉，否则解析不出歌曲 ID。"""
    card = parse_music_card_text("[网易云音乐] 晴天 - 周杰伦 链接: https://music.163.com/song?id=186016。")
    assert card is not None
    assert card.url == "https://music.163.com/song?id=186016"


def test_card_text_song_name_only() -> None:
    """卡片只有歌名（没有「 - 歌手」）也要认得，不能把链接当成歌手。"""
    card = parse_music_card_text("[音乐分享] 某首歌 链接: https://music.163.com/song?id=9")
    assert card is not None
    assert card.song_name == "某首歌"
    assert card.artist == ""
    assert card.url == "https://music.163.com/song?id=9"


@pytest.mark.parametrize(
    "text",
    [
        # 纯卡片文本：整段都是适配器渲染的，没有用户正文
        "[QQ音乐] 小城夏天 - LBI利比 链接: https://i.y.qq.com/v8/playsong.html?songmid=1",
        "[音乐分享] 某首歌",
        "[小程序] QQ音乐：小城夏天 - LBI利比",
        "[CQ:music,type=custom,url=https://i.y.qq.com/v8/playsong.html?songmid=1,title=x]",
        "",
    ],
)
def test_strip_card_injected_text_removes_card_payload(text: str) -> None:
    assert strip_card_injected_text(text) == ""


@pytest.mark.parametrize(
    "text",
    [
        "这首歌好听 https://music.163.com/song?id=12345",
        # 用户自己写「链接: 」不是卡片痕迹，不能顺手删掉
        "链接: https://music.163.com/song?id=12345",
        "点歌 晴天",
    ],
)
def test_strip_card_injected_text_keeps_user_text(text: str) -> None:
    assert strip_card_injected_text(text) == text.strip()


def test_strip_card_injected_text_keeps_prefix_before_card() -> None:
    """用户正文在卡片之前：保留正文，卡片起点之后整段丢弃。"""
    prefix = "看看这个 https://music.163.com/song?id=1 "
    stripped = strip_card_injected_text(
        prefix + "[QQ音乐] 小城夏天 - LBI利比 链接: https://i.y.qq.com/v8/playsong.html?songmid=1"
    )
    assert stripped == prefix.strip()
    assert parse_music_url(stripped) is not None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[QQ音乐] 小城夏天 - LBI利比", True),
        ("[小程序] QQ音乐：小城夏天", True),
        ("[json:音乐]", True),
        ("[xml]", True),
        ("[CQ:music,type=163,id=1]", True),
        ("https://music.163.com/song?id=1", False),
        ("点歌 晴天", False),
        ("", False),
    ],
)
def test_has_card_markup(text: str, expected: bool) -> None:
    assert has_card_markup(text) is expected


@pytest.mark.parametrize(
    "url",
    [
        "https://music.163.com/song?id=1",
        "https://y.music.163.com/m/song?id=1",
        "https://y.qq.com/n/ryqq/songDetail/1",
        "https://i.y.qq.com/v8/playsong.html?songmid=1",
        "https://c6.y.qq.com/base/fcgi-bin/u?__=1",
    ],
)
def test_allowed_redirect_hosts(url: str) -> None:
    assert is_allowed_music_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8001/admin",
        "http://169.254.169.254/latest/meta-data/",
        "https://evil.example.com/music.163.com",
        "https://music.163.com.evil.com/song?id=1",
        "",
    ],
)
def test_ssrf_guard_rejects_foreign_hosts(url: str) -> None:
    """短链跳转的 SSRF 闸门：白名单外（含内网、元数据 IP、后缀伪装）一律拒绝。"""
    assert is_allowed_music_url(url) is False
