"""QA 边界 / 退化用例补充（v1.4.5 上线前全检，qa-lead）。

组织原则：每一条「输入 → 输出」正向断言，都配一条退化 / 边界断言。

本文件**只新增测试，不改任何源码**。已知缺陷一律用
`pytest.mark.xfail(strict=True, reason=...)` 标注：
    - strict 语义：若某天实现被修好，用例转为 XPASS → 整轮变红，
      提醒维护者「缺陷已修复，请移除 xfail 标记」，不会被静默遗忘。
    - 用例正文写的是**期望（正确）行为**，因此当前必然失败。

覆盖今日 5 项改动的高风险面：
    1. 双向形态兜底的**否定句 / 反问句**误判（正则放宽后的新风险面）
    2. 触发文本的全占位符退化（`[voiceurl消息]`）
    3. `send_as` 非法值 / 大小写 / 与正则的优先级
    4. `query` / `send_as` 类型错误
    5. file 形态的降级链路与返回文案
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

import pytest

import plugin as plugin_module
from test_music_request import (
    _FakeCache,
    _ScenarioAPI,
    _TARGET_SONG,
    _prepare_plugin,
)

_UNSET = object()

# ---------------------------------------------------------------- 本地小工具


def _run_tool(
    trigger: Any = _UNSET,
    send_as: Any = _UNSET,
    query: Any = "万能处方",
    stream_id: Any = "s1",
    *,
    upload_ok: bool = True,
    unplayable: Iterable[str] = (),
    bind_target: bool = True,
):
    """跑一次 @Tool，返回 (工具返回文本, API 替身)。

    默认配置：play_mode=voice、tool_default_mode=voice（与真机实锤场景一致）。
    """
    plugin, _host = _prepare_plugin()
    api = _ScenarioAPI([_TARGET_SONG], unplayable=set(unplayable), upload_ok=upload_ok)
    plugin._api = api
    plugin._cache = _FakeCache()
    if bind_target:
        plugin._qq_targets["s1"] = {"user_id": "3816023959"}
    kwargs: dict[str, Any] = {"query": query, "stream_id": stream_id}
    if send_as is not _UNSET:
        kwargs["send_as"] = send_as
    if trigger is not _UNSET:
        kwargs["processed_plain_text"] = trigger
    content = asyncio.run(plugin.search_and_play_music(**kwargs))["content"]
    return content, api


def _matched(text: str) -> bool:
    """是否判定为「用户明确要文件」意图。

    走生产用的 `_has_file_intent`（逐小句 + 否定/反问过滤），
    而不是裸正则 `_RE_FILE_INTENT` —— 后者只回答「这句提到文件关键词了吗」。
    """
    return bool(plugin_module._has_file_intent(text))


# ================================================================ 1. 正向：明确要文件

_FILE_INTENT_POSITIVE = [
    "发一首万能处方的文件",
    "发晚安糖果罐的文件",
    "发个文件",
    "我要无损",
    "要好音质",
    "这歌太长发文件吧",
    "来个无损音质的",
    "来无损",
    "要flac",
    "给我发ape",
    "发一份无损",
    "无损音质",          # 裸「无损音质」
    "发一下无损版本",
]


@pytest.mark.parametrize("text", _FILE_INTENT_POSITIVE)
def test_regex_hits_explicit_file_intent(text: str) -> None:
    """明说要文件 / 无损时必须命中（含中间夹歌名，≤12 字）。"""
    assert _matched(text), f"文件意图失配: {text!r}"


# ================================================================ 1b. 反向：普通点歌不得命中

_PLAIN_ORDER = [
    "放一首万能处方",
    "放一下万能处方",
    "来一首万能处方",
    "放歌",
    "随便放点音乐",
    "我想听万能处方",
    "放个歌吧",
    "给我放首安静的歌",       # 「给我」出现在无 flac 的句子里，不得误命中
]


@pytest.mark.parametrize("text", _PLAIN_ORDER)
def test_regex_misses_plain_order(text: str) -> None:
    """普通点歌话术绝不能命中——误判会让用户收到文件（真机实锤 2026-09-12）。"""
    assert not _matched(text), f"文件意图误匹配: {text!r}"


# ================================================================ 2. 缺陷：否定句 / 反问句误判

_NEGATION_TEXTS = [
    "不要发文件，放语音",
    "别发文件了，直接放语音",
    "放语音就行，不要发文件",
    "为什么要发文件？",
    "发文件干嘛？我要语音",
    "我不要无损，普通音质就行",
    "不用发无损音质",
    "这首歌太长不要发文件",
]


@pytest.mark.parametrize("text", _NEGATION_TEXTS)
def test_regex_should_not_hit_negated_or_rhetorical_intent(text: str) -> None:
    """否定句 / 反问句表达的是「不要文件」，不得判为文件意图。

    v1.4.6 已修（D1）：`_has_file_intent` 逐小句判断，句内含否定/反问词即跳过。
    """
    assert not _matched(text), f"否定/反问被误判为文件意图: {text!r}"


@pytest.mark.parametrize(
    "text", ["不要发文件，放语音", "为什么要发文件？", "我不要无损"]
)
def test_tool_should_not_force_file_on_negated_intent(text: str) -> None:
    """端到端：否定句下必须按默认形态（语音）发出，且不产生任何文件上传。

    v1.4.6 已修（D1）：这是最危险的一条——用户明说「不要发文件」，
    旧实现却反向兜底成 file，等于把用户的否定读成了肯定。
    """
    content, api = _run_tool(trigger=text)
    assert content.startswith("已播放"), f"用户明说不要文件却发了文件: {content!r}"
    assert api.uploads == [], f"不该上传文件，实际 {api.uploads}"


# ================================================================ 3. 正则漏判（false negative）

_MISSED_TEXTS = [
    "给我来首 flac",      # 关键词与动词间有空格
    "来 flac",
    "要一个 flac 版",
    "要FLAC",             # 全大写
    "要Flac",             # 首字母大写
    "发" + ("啊" * 13) + "文件",   # 夹字超原 12 上限
]


@pytest.mark.parametrize("text", _MISSED_TEXTS)
def test_regex_should_tolerate_spacing_case_and_length(text: str) -> None:
    """空格、大小写、长歌名都不应让明确的文件意图失效。

    v1.4.6 已修（D3）：正则加 IGNORECASE、允许空格、夹字上限放宽到 14。
    """
    assert _matched(text), f"文件意图漏判: {text!r}"


def test_newline_separated_intent_is_conservative() -> None:
    """跨行输入（`发\\n文件`）**刻意**不判为文件意图。

    小句切分把换行当句界（D3 的 `发\\n文件` 用例因此不再命中）。
    这是有意的取舍：跨行匹配会让「不要发语音\\n文件也不要」这类多句文本
    更容易被拼成一个肯定意图，而漏判只会退回默认形态，风险远小于误判。
    """
    assert not _matched("发\n文件")


# ================================================================ 4. 形态决策：合法 / 非法 / 优先级

def test_send_as_uppercase_file_is_valid() -> None:
    """`FILE` 大写经 lower() 归一后合法 → 即使无触发文本也应发文件。"""
    content, api = _run_tool(send_as="FILE")
    assert content.startswith("已发送文件"), content
    assert len(api.uploads) == 1, api.uploads


def test_send_as_invalid_value_falls_back_to_default_voice() -> None:
    """非法取值（flac）回落配置默认形态，且不静默当成 file。"""
    content, api = _run_tool(send_as="flac")
    assert content.startswith("已播放"), content
    assert api.uploads == [], api.uploads


def test_send_as_invalid_but_trigger_intent_wins() -> None:
    """send_as 非法 ≠ 用户没要文件：触发文本含意图时仍须判为 file。"""
    content, api = _run_tool(send_as="flac", trigger="发文件")
    assert content.startswith("已发送文件"), content
    assert len(api.uploads) == 1, api.uploads


def test_send_as_file_respected_when_trigger_missing() -> None:
    """拿不到触发文本时**不介入**：尊重 LLM 传入的 file，不回落。"""
    content, api = _run_tool(send_as="file")          # 完全不传 processed_plain_text
    assert content.startswith("已发送文件"), content
    assert len(api.uploads) == 1, api.uploads


def test_blank_trigger_treated_as_missing() -> None:
    """纯空白触发文本等同「拿不到」，同样不得触发回落。"""
    content, api = _run_tool(send_as="file", trigger="   ")
    assert content.startswith("已发送文件"), content
    assert len(api.uploads) == 1, api.uploads


def test_send_as_file_falls_back_when_trigger_has_no_intent() -> None:
    """LLM 自作主张传 file、触发消息无此意图 → 回落 tool_default_mode（voice）。"""
    content, api = _run_tool(send_as="file", trigger="放一首万能处方")
    assert content.startswith("已播放"), content
    assert api.uploads == [], "用户没要文件时不该上传文件"


def test_trigger_file_intent_upgrades_omitted_send_as() -> None:
    """LLM 漏传 file、但用户明说要文件 → 代码层强制 file。"""
    content, api = _run_tool(trigger="发一首万能处方的文件")
    assert content.startswith("已发送文件"), content
    assert len(api.uploads) == 1, api.uploads


def test_placeholder_only_trigger_does_not_force_file() -> None:
    """纯占位符触发文本（无文本段）不得被误判成 file 意图。"""
    content, api = _run_tool(trigger="[music消息]")          # 默认 voice
    assert content.startswith("已播放"), content
    assert api.uploads == [], api.uploads


def test_placeholder_only_trigger_should_respect_send_as() -> None:
    """无文本段的消息应视为「拿不到触发文本」→ 尊重 send_as=file。

    v1.4.6 已修（D2）：纯占位符文本（`[voiceurl消息]` 等）不含用户表达，
    按「拿不到触发文本」处理，从而尊重调用方显式入参。
    """
    content, api = _run_tool(send_as="file", trigger="[voiceurl消息]")
    assert content.startswith("已发送文件"), f"占位符被当成真实文本，误回落: {content!r}"
    assert len(api.uploads) == 1, api.uploads


# ================================================================ 5. 输入健壮性

@pytest.mark.parametrize("query", [None, "", "   ", "\t\n"])
def test_blank_query_is_rejected_gracefully(query: Any) -> None:
    """空 / 纯空白 query → 友好提示，不抛异常、不搜索。"""
    content, api = _run_tool(query=query)
    assert content == "请提供歌曲名或关键词", content
    assert api.uploads == [], api.uploads


def test_missing_stream_id_is_rejected_gracefully() -> None:
    """缺 stream_id → 友好提示，而不是静默失败。"""
    content, _api = _run_tool(stream_id="")
    assert "stream_id" in content, content


def test_send_as_none_is_treated_as_omitted() -> None:
    """send_as=None 等同不传 → 用默认形态。"""
    content, api = _run_tool(send_as=None)
    assert content.startswith("已播放"), content
    assert api.uploads == [], api.uploads


@pytest.mark.parametrize("bad", [123, 3.14, ["万能处方"], {"q": "x"}, True])
def test_non_string_query_should_not_crash(bad: Any) -> None:
    """非字符串 query 应得到友好文案（或安全拒绝），不得抛 AttributeError。

    v1.4.6 已修（D4）：入口做类型归一，非 str 先转成文本再走空值判断。
    """
    content, _api = _run_tool(query=bad)
    assert isinstance(content, str) and content, content


@pytest.mark.parametrize("bad", [123, 3.14, ["file"]])
def test_non_string_send_as_should_not_crash(bad: Any) -> None:
    """非字符串 send_as 应回落默认形态，不得抛 AttributeError。

    v1.4.6 已修（D4 同源）：`_resolve_send_as` 对非 str 入参归一为「未指定」。
    """
    content, _api = _run_tool(send_as=bad)
    assert isinstance(content, str) and content, content


# ================================================================ 6. 降级链路与返回文案

def test_upload_failure_degrades_to_card_with_honest_text() -> None:
    """上传失败 → 降级卡片，返回文案必须如实说明是卡片而非文件。"""
    content, api = _run_tool(trigger="发文件", upload_ok=False)
    assert len(api.uploads) == 1, "应先尝试上传（失败）"
    assert "已改为发送音乐卡片" in content, content
    assert "NapCat 上传失败" in content, content
    assert "已发送文件" not in content, "降级后不得再声称已发文件"


def test_no_napcat_target_degrades_to_card_with_honest_text() -> None:
    """无 NapCat 目标 → 降级卡片，原因写进文案。"""
    content, api = _run_tool(trigger="发文件", bind_target=False)
    assert api.uploads == [], "拿不到目标时不该发起上传"
    assert "已改为发送音乐卡片" in content, content
    assert "NapCat" in content, content


def test_no_audio_available_reports_failure_not_success() -> None:
    """平台拿不到音频 → 如实报失败，不得谎报成功、不得发任何歌曲消息。"""
    content, api = _run_tool(
        trigger="发文件", unplayable={_TARGET_SONG.song_id}
    )
    assert "均未取到可播放音频" in content, content
    assert "已发送文件" not in content and "已播放" not in content, content
    assert api.uploads == [], api.uploads
