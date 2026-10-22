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

#: 「标题可信线」：重搜兜底后仍无候选命中歌手 token 时，最佳匹配的
#: 标题分低于这条线就不播（宁报失败也不放错歌）。
#: 取 `_SCORE_QUERY_IN_TITLE`（查询被标题完整包住）作为下限——
#: 低于它意味着标题连「查询单元的完整包含关系」都够不上，
#: 只靠字符覆盖凑分，正是《我最讨厌摇滚乐》这类干扰项的得分形态。
TITLE_CREDIBILITY_LINE = _SCORE_QUERY_IN_TITLE


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


# ---------- 歌手校验（真机实锤 2026-10-20） ----------
#
# query="但我讨厌摇滚乐 洛天依" 整串丢给网易云搜索，被「洛天依」token
# 带偏返回 5 条无关候选，《我最讨厌摇滚乐 - 显然乐队》靠字符覆盖拿到
# 0.6 分压线胜出被播出——洛天依版根本不在候选里。
# 教训：用户明确点名歌手时，「artists 不匹配」必须硬剔除（扣分挡不住
# 相对地板）；平台搜索被歌手 token 带偏时，需要按歌名重搜兜底。


def query_tokens(query: str) -> list[str]:
    """把查询按「歌名 歌手」形态拆成 token（保留原顺序，已归一化）。

    Args:
        query: 用户查询串。

    Returns:
        归一化后的 token 列表；空查询返回空列表。
    """
    normalized = [normalize_text(tk) for tk in _TOKEN_SPLIT_RE.split(str(query or ""))]
    return [tk for tk in normalized if tk]


def split_by_artist_match(
    query: str, songs: Sequence[SongT]
) -> tuple[list[SongT], list[SongT], list[SongT]]:
    """把候选按「artists 是否命中查询里的歌手 token」分成三组。

    判据：任一 token（归一化后长度 ≥ `_MIN_ARTIST_TOKEN`）是候选
    artists 的子串即算命中（多歌手 `, ` 连接、后缀如「洛天依ACE」
    都能覆盖）。分组而非直接过滤，是因为**上层需要先看到「有命中」
    才能确认歌手意图**——多词英文歌名（如 "Shape of You"）与「歌名
    歌手」形态无法从 token 本身区分，全不命中时硬过滤会误杀。

    Args:
        query: 用户查询串。
        songs: 候选列表（鸭子类型，只需 `artists` 属性）。

    Returns:
        `(artist_matched, no_artist_field, unmatched)` 三组：
        - artist_matched：artists 命中至少一个 token；
        - no_artist_field：artists 为空，无法判定，豁免（宁放过不错杀）；
        - unmatched：artists 非空且不含任何 token。

        query 没有有效歌手 token 时返回 `(list(songs), [], [])`。
    """
    tokens = [tk for tk in query_tokens(query) if len(tk) >= _MIN_ARTIST_TOKEN]
    if not tokens:
        return list(songs), [], []
    matched: list[SongT] = []
    neutral: list[SongT] = []
    unmatched: list[SongT] = []
    for song in songs:
        artists = normalize_text(getattr(song, "artists", ""))
        if not artists:
            neutral.append(song)
        elif any(tk in artists for tk in tokens):
            matched.append(song)
        else:
            unmatched.append(song)
    return matched, neutral, unmatched


def title_retry_token(query: str) -> str:
    """从「歌名 歌手」查询里取用于重搜的歌名 token。

    取**首个**长度达标的 token——按工具约定的「歌名 歌手」顺序，
    首个 token 就是歌名（`但我讨厌摇滚乐 洛天依` → `但我讨厌摇滚乐`）。
    歌手在前的反序（`洛天依 但我讨厌摇滚乐`）会取到歌手名：重搜结果
    仍要过同一套歌手校验与相关度排序兜底，不比不搜更糟。

    单 token 查询返回空串：重搜等于原样重搜，毫无意义，还会把
    「平台本来就有但标题不完全同字」的正常结果拖进可信线守卫。

    Args:
        query: 用户查询串。

    Returns:
        可用于重搜的 token；单 token / 全是单字 token 时返回空串。
    """
    tokens = query_tokens(query)
    if len(tokens) < 2:
        return ""
    for token in tokens:
        if len(token) >= _MIN_ARTIST_TOKEN:
            return token
    return ""
