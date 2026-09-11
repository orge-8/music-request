"""搜索结果相关度排序与过滤（纯逻辑，零第三方依赖，可离线单测）。

为什么需要它（真机实锤，2026-09-11）：

    用户说「放晚安糖果罐」，网易云搜索第一条正是《晚安糖果罐 - 洛天依》，
    但该曲目在网易云被版权限制（`song_code=-110`）拿不到直链。
    旧实现是「逐条候选依次尝试直到成功」，于是它**静默降级**去试第二条，
    把《嘘嘘声+羊水声+胎心音+八音盒 星星糖果罐 - 晚安宝贝》当成了结果播出去。
    播放失败本身是平台问题，**改播一首完全无关的歌是逻辑问题**。

修法：先按相关度排序，再相对过滤——只尝试「和最佳匹配同一档」的候选。
用**相对地板**（`best × floor_ratio`）而不是绝对阈值：
查询本身很模糊时所有分数都低，绝对阈值会把结果全砍光；相对地板只在
「存在明显更优候选」时才剔除，两侧都不会误伤。

本模块只按 `name` / `artists` 两个属性取数据（鸭子类型），
因此不依赖 `music_api.SongInfo`，也就没有导入顺序问题。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Sequence, TypeVar

SongT = TypeVar("SongT")

#: 归一化时要去掉的标点/空白（NFKC 之后剩下的都是半角形式）
_NOISE_RE = re.compile(r"""[\s\-_·・,，、.。:：;；!！?？()（）\[\]【】<>《》"'“”‘’/\\|+~*#]+""")

#: 用于把「歌名 歌手」拆成 token 的分隔符
_TOKEN_SPLIT_RE = re.compile(r"[\s,，、/]+")

#: 标题完全相等（或完全被包含）时给的基础分
_SCORE_EXACT = 1.0
_SCORE_QUERY_IN_TITLE = 0.85
_SCORE_TITLE_IN_QUERY = 0.7
_SCORE_CHAR_COVERAGE = 0.6

#: 查询里出现歌手名时的加分（用户指明了演唱者，说明这个候选更可信）
_ARTIST_BONUS = 0.25

#: 歌手名至少这么长才拿去做命中判断，避免「A」「呀」这类单字误命中
_MIN_ARTIST_TOKEN = 2


def normalize_text(text: Any) -> str:
    """归一化文本用于比较：全角→半角、去标点空白、转小写。

    Args:
        text: 任意待归一化文本。

    Returns:
        归一化结果，空输入返回空串。
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).lower()
    return _NOISE_RE.sub("", folded)


def title_score(query: str, title: Any) -> float:
    """单个查询串与标题的相关度（0~1）。

    判据优先级：完全相等 > 查询是标题的子串 > 标题是查询的子串 > 字符覆盖率。
    中文没有分词，「字符覆盖率」用「查询里有几个字出现在标题中」近似——
    对「晚安糖果罐」vs「嘘嘘声…星星糖果罐」这种只共享尾词的干扰项足够有效。

    Args:
        query: 查询串（可以是整句，也可以是单个 token）。
        title: 候选标题。

    Returns:
        0~1 的分数。
    """
    q = normalize_text(query)
    t = normalize_text(title)
    if not q or not t:
        return 0.0
    if q == t:
        return _SCORE_EXACT
    if q in t:
        # 标题完整包住了查询：越接近等长越可信，避免长标题（如「… 星星糖果罐」）占便宜
        return _SCORE_QUERY_IN_TITLE + (1.0 - _SCORE_QUERY_IN_TITLE) * (len(q) / len(t))
    if t in q:
        return _SCORE_TITLE_IN_QUERY
    q_chars = set(q)
    covered = sum(1 for ch in q_chars if ch in t)
    return _SCORE_CHAR_COVERAGE * (covered / len(q_chars))


def score_song(query: str, song: Any) -> float:
    """给一首候选歌打相关度分。

    做法：把查询按空白/顿号拆成 token（「歌名 歌手」的常见形态），
    取「整串」与「各 token」中的最高标题分，再叠加歌手命中加分。
    这样 `晚安糖果罐 洛天依` 不会因为多带了歌手名而把标题分拉低。

    Args:
        query: 用户查询串。
        song: 任意带 `name` / `artists` 属性的对象。

    Returns:
        0~1 的分数。
    """
    query = str(query or "").strip()
    if not query:
        return 0.0
    tokens = [tk for tk in _TOKEN_SPLIT_RE.split(query) if tk]
    name = getattr(song, "name", "")
    score = max(
        (title_score(candidate, name) for candidate in [query, *tokens]),
        default=0.0,
    )

    artists = normalize_text(getattr(song, "artists", ""))
    if artists:
        for token in tokens:
            normalized = normalize_text(token)
            if len(normalized) >= _MIN_ARTIST_TOKEN and normalized in artists:
                score = min(1.0, score + _ARTIST_BONUS)
                break
    return min(1.0, score)


def rank_songs(
    query: str,
    songs: Sequence[SongT],
    *,
    floor_ratio: float = 0.6,
) -> list[tuple[SongT, float]]:
    """按相关度降序排序，并剔除明显不在同一档的候选。

    过滤用的是**相对地板**：`最佳分 × floor_ratio`。
    - 最佳分很高（有明确匹配）时，地板随之抬高 → 低分干扰项被剔除；
    - 所有候选都很低（查询本身就模糊）时，地板也很低 → 不会把结果砍光。

    最佳分为 0（完全无法判断相关度）时不做过滤，退回原始顺序，
    避免用一套不可信的分数把结果永久挡掉。

    Args:
        query: 用户查询串。
        songs: 候选列表。
        floor_ratio: 地板比例（0~1）。1.0 表示只保留与最佳同分的候选。

    Returns:
        `(song, score)` 列表，按分数降序；输入为空时返回空列表。
    """
    scored = [(song, score_song(query, song)) for song in songs]
    if not scored:
        return []
    scored.sort(key=lambda pair: pair[1], reverse=True)

    best = scored[0][1]
    if best <= 0.0:
        return scored

    ratio = min(max(float(floor_ratio), 0.0), 1.0)
    cutoff = best * ratio
    kept = [pair for pair in scored if pair[1] >= cutoff]
    return kept or scored[:1]
