"""search_rank 纯逻辑单测（离线，零依赖）。

这组用例锚定的是真机日志暴露的缺陷：查询「晚安糖果罐」在网易云命中的
正确曲目因版权拿不到直链，旧实现便一路往下试候选，最终播出了
《嘘嘘声+羊水声+胎心音+八音盒 星星糖果罐 - 晚安宝贝》。
排序与相对过滤必须能把这个干扰项挡掉。
"""

from __future__ import annotations

import pytest

from search_rank import normalize_text, rank_songs, score_song, title_score


class _Song:
    """只带 name / artists 的鸭子类型候选，验证模块不依赖 SongInfo。"""

    def __init__(self, name: str, artists: str = "") -> None:
        self.name = name
        self.artists = artists

    def __repr__(self) -> str:
        return f"_Song({self.name!r}, {self.artists!r})"


# 真机日志里的实际候选（网易云搜索「晚安糖果罐」返回）
_NOISE = _Song("嘘嘘声+羊水声+胎心音+八音盒 星星糖果罐", "晚安宝贝")
_TARGET = _Song("晚安糖果罐", "洛天依")


# ---------- normalize_text ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("晚安糖果罐", "晚安糖果罐"),
        ("  晚安  糖果罐  ", "晚安糖果罐"),
        ("ＡＢＣ", "abc"),
        ("abc", "abc"),
        ("晚安糖果罐（Live版）", "晚安糖果罐live版"),
        ("星星糖果罐 - 晚安宝贝", "星星糖果罐晚安宝贝"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_text(raw, expected: str) -> None:
    assert normalize_text(raw) == expected


# ---------- title_score ----------


def test_title_score_exact_match_is_full() -> None:
    assert title_score("晚安糖果罐", "晚安糖果罐") == 1.0


def test_title_score_ignores_punctuation_and_width() -> None:
    """全角/半角、空白、标点都不能影响「完全匹配」这个判定。"""
    assert title_score("晚安糖果罐", " 晚安糖果罐 ") == 1.0
    assert title_score("稻香", "稻香") == 1.0


def test_title_score_query_inside_title_prefers_shorter_title() -> None:
    """查询被标题包住时，越接近等长越可信——长标题不该占便宜。"""
    short = title_score("晚安糖果罐", "晚安糖果罐")
    padded = title_score("晚安糖果罐", "晚安糖果罐 完整版")
    assert padded < short, f"长标题得分不应不低于等长标题: {padded} vs {short}"
    assert padded >= 0.85


def test_title_score_detects_irrelevant_long_title() -> None:
    """只共享尾词的干扰项必须显著低于目标曲目。"""
    target = title_score("晚安糖果罐", _TARGET.name)
    noise = title_score("晚安糖果罐", _NOISE.name)
    assert target == 1.0
    assert noise < 0.6, f"无关长标题得分过高: {noise}"
    assert target > noise


def test_title_score_handles_empty() -> None:
    assert title_score("", "任何") == 0.0
    assert title_score("任何", "") == 0.0


# ---------- score_song ----------


def test_score_song_token_split_keeps_title_score() -> None:
    """查询带歌手时不能把标题分稀释掉（「歌名 歌手」是联动的标准形态）。"""
    assert score_song("晚安糖果罐 洛天依", _TARGET) == 1.0


def test_score_song_artist_match_adds_bonus() -> None:
    """歌手命中给加分：标题分没到顶时，它能决定同档候选的先后。

    注意标题分封顶 1.0，所以「歌名完全命中」时歌手加分看不出来——
    这里刻意用「部分命中标题 + 备注歌手」的查询形态来观察加分。
    """
    cover = _Song("晚安糖果罐", "某翻唱歌手")
    plan = _Song("晚安糖果罐", "洛天依")
    query = "糖果罐 洛天依"
    assert score_song(query, plan) > score_song(query, cover)


def test_score_song_artist_bonus_is_capped() -> None:
    """加分不能让分数越过 1.0（契约是 0~1，上层阈值按这个尺度设）。"""
    song = _Song("晚安糖果罐", "洛天依")
    assert score_song("晚安糖果罐 洛天依", song) == 1.0


def test_score_song_ignores_too_short_artist_token() -> None:
    """单字 token 不该当成歌手命中，否则「啊」这类噪声会乱加分。"""
    song = _Song("晴天", "阿杜")
    assert score_song("晴天 阿", song) == score_song("晴天", song)


def test_score_song_ranks_target_above_noise() -> None:
    assert score_song("晚安糖果罐", _TARGET) > score_song("晚安糖果罐", _NOISE)


# ---------- rank_songs ----------


def test_rank_songs_sorts_descending() -> None:
    """排序本身：关掉过滤（floor_ratio=0）后仍必须按分数降序。"""
    ranked = rank_songs("晚安糖果罐", [_NOISE, _TARGET], floor_ratio=0.0)
    assert len(ranked) == 2
    assert ranked[0][0] is _TARGET
    assert ranked[0][1] > ranked[1][1]


def test_rank_songs_drops_out_of_band_candidate() -> None:
    """核心回归：目标曲目存在时，白噪音干扰项必须被过滤掉。"""
    ranked = rank_songs("晚安糖果罐", [_NOISE, _TARGET], floor_ratio=0.6)
    kept = [song for song, _ in ranked]
    assert kept == [_TARGET], f"干扰项未被过滤: {kept}"


def test_rank_songs_keeps_same_song_different_versions() -> None:
    """同曲不同版本得分接近，必须都保留——否则版权失败时无从回退。"""
    live = _Song("晚安糖果罐 (Live)", "洛天依")
    ranked = rank_songs("晚安糖果罐", [_TARGET, live, _NOISE], floor_ratio=0.6)
    kept = [song for song, _ in ranked]
    assert _TARGET in kept and live in kept, f"同曲版本被误杀: {kept}"
    assert _NOISE not in kept


def test_rank_songs_floor_ratio_one_keeps_only_best() -> None:
    live = _Song("晚安糖果罐 (Live)", "洛天依")
    ranked = rank_songs("晚安糖果罐", [_TARGET, live], floor_ratio=1.0)
    assert [song for song, _ in ranked] == [_TARGET]


def test_rank_songs_floor_ratio_zero_disables_filtering() -> None:
    ranked = rank_songs("晚安糖果罐", [_NOISE, _TARGET], floor_ratio=0.0)
    assert len(ranked) == 2, "floor_ratio=0 应关闭过滤"


def test_rank_songs_does_not_filter_when_scores_are_all_zero() -> None:
    """分数全为 0（完全无法判断）时退回原始顺序，不做过滤。

    否则一套不可信的分数会把结果永久挡掉——用户会看到「一首都没找到」。
    """
    unrelated = [_Song("完全无关甲"), _Song("完全无关乙")]
    ranked = rank_songs("zzz", unrelated, floor_ratio=0.9)
    assert len(ranked) == 2


def test_rank_songs_always_keeps_at_least_one() -> None:
    """过滤再严也要留下最佳候选，不能让上层拿到空列表。"""
    ranked = rank_songs("晚安糖果罐", [_NOISE], floor_ratio=1.0)
    assert [song for song, _ in ranked] == [_NOISE]


def test_rank_songs_empty_input() -> None:
    assert rank_songs("晚安糖果罐", []) == []


def test_rank_songs_is_stable_for_equal_scores() -> None:
    """同分时保持平台返回顺序（Python 的 sort 稳定），不引入随机性。"""
    first = _Song("晚安糖果罐")
    second = _Song("晚安糖果罐")
    ranked = rank_songs("晚安糖果罐", [first, second], floor_ratio=0.6)
    assert [song for song, _ in ranked] == [first, second]
