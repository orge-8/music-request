"""点歌插件 —— 搜索点歌、解析音乐链接与分享卡片，发送语音音频或音乐卡片。

支持网易云音乐（163）与 QQ音乐（qq）双平台，两种播放形态：
    - `card`  音乐卡片（网易云走平台型 music 段；QQ音乐由插件自解析直链与元数据构卡）
    - `voice` 语音音频（可让 MaiBot 下载到本地共享缓存后交给 NapCat 发送）

三个入口：
    - `@Command`   `/点歌 [163|qq] <歌曲名>`、`/选歌 <序号>`、`/点歌状态`
    - `@Tool`      `search_and_play_music`（LLM 自然语言点歌）
    - `@HookHandler` 入站解析音乐链接 / 分享卡片，命中即发送并拦下这条消息

注意事项（都是实测结论，改动前先读）：
    - 本文件**不能**写 `from __future__ import annotations`：Runner 用
      `spec_from_file_location` 加载且不注册 `sys.modules`，注解被字符串化后
      pydantic 会解析不了配置模型。
    - 同目录模块用「相对导入优先 + 平铺兜底」：Runner 以包方式加载插件目录，
      相对导入会把辅助模块挂成 `<插件名>.*` 命名空间，避免与同进程其他插件的
      顶层模块重名；平铺兜底（配合下面这段 sys.path 补丁）保证脚本直跑与
      本地测试也能正常导入。
    - 命令的第三个返回值是**拦截级别**（0 不拦 / 1 拦但对 replyer 可见 / 2 拦且隐藏），
      不是优先级。没发出去就别拦，否则用户会觉得"命令石沉大海"。
"""

import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

# 平铺导入兜底的路径保障：包式加载下用不到，脚本直跑/本地测试时靠它找到辅助模块
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import (
    CONFIG_RELOAD_SCOPE_SELF,
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

try:  # 包式加载优先（Runner 主路径）：模块挂到 <插件名>.* 命名空间，避免跨插件重名
    from .audio_cache import AudioCacheError, MusicAudioCache, write_cache_probe
    from .music_api import MusicAPIResponseError, MusicSearchClient, SongInfo
    from .search_rank import rank_songs
    from .url_parser import (
        PLATFORM_NETEASE,
        PLATFORM_QQ,
        MusicCardInfo,
        MusicRef,
        extract_urls,
        parse_music_card_text,
        parse_music_url,
    )
except ImportError:  # 平铺兜底：脚本直跑 / 本地测试 / 旧式加载
    from audio_cache import AudioCacheError, MusicAudioCache, write_cache_probe
    from music_api import MusicAPIResponseError, MusicSearchClient, SongInfo
    from search_rank import rank_songs
    from url_parser import (
        PLATFORM_NETEASE,
        PLATFORM_QQ,
        MusicCardInfo,
        MusicRef,
        extract_urls,
        parse_music_card_text,
        parse_music_url,
    )

__plugin_id__ = "github.cateye.music-request"

logger = logging.getLogger(__name__)

SUPPORTED_CONFIG_VERSION = "1.4.6"

# 待选列表的默认有效期（秒）
_DEFAULT_SELECT_TTL = 300

# 命令里允许的前缀字符归一化（全角 → 半角），便于与配置值比较
_PREFIX_NORMALIZE = {
    "／": "/", "＃": "#", "！": "!", "。": ".", "：": ":",
    "￥": "$", "％": "%", "－": "-", "＋": "+", "＝": "=",
}

# OneBot CQ 码里展示文本需要转义的字符
_CQ_TEXT_ESCAPES = {"&": "&amp;", ",": "&#44;", "[": "&#91;", "]": "&#93;"}

# 触发消息里的「发文件」意图关键词：LLM 点歌时若能取到触发消息文本，
# 命中关键词则无视 send_as 缺省强制用 file 形态。
# 真机实锤（2026-09-11）：用户明说「发晚安糖果罐的文件」，LongCat 只传了
# query 没传 send_as=file，结果按默认 voice 发了语音——靠工具描述里的提示
# 让模型自觉传参不可靠，代码层兜底才是硬约束。
#
# 注意：本正则只回答「这一句里提到文件关键词了吗」，**不判断是否定/反问**。
# 否定语境（「不要发文件」「为什么要发文件」）由 _has_file_intent 逐句过滤，
# 不能只看正则——真机/QA 实锤（2026-09-12）：纯正则会把否定句当正向意图，
# 反向兜底随即把形态从 voice 强制成 file，用户明说不要文件反而收到文件。
_RE_FILE_INTENT = re.compile(
    # 不在句内跨越句读符号，避免把相邻小句的词拼成一个意图
    r"发[^，。！？；、\n]{0,14}(文件|无损)|音频文件|好音质"
    r"|(发|要|给|来)(我)?(个|首|份|一下)?[^，。！？；、\n]{0,12}?(flac|ape)"
    r"|(要|来|来个|来份|给我)无损|无损.{0,4}(音质|版本|文件)",
    re.IGNORECASE,
)

# 否定 / 反问词：与文件关键词同现于同一小句时，视为「不要文件」
_RE_FILE_INTENT_NEG = re.compile(r"不|别|勿|无需|免了|没必要|干嘛|干什么|为什么|为啥|咋")

# 小句切分：按中英文标点与换行拆分，逐句独立判断意图
_RE_CLAUSE_SPLIT = re.compile(r"[，。！？；、,.!?;\n]+")

# 纯占位文本（如 [voiceurl消息] / [music消息]）：Host 生成的载荷占位符，
# 不含用户表达，不应参与意图判断
_RE_PLACEHOLDER_ONLY = re.compile(r"^\[[^\[\]]{1,24}\]$")


def _has_file_intent(text: str) -> bool:
    """判断触发消息是否**明确要求**「文件 / 无损 / 好音质」形态。

    逐小句判断：某句命中文件关键词、且该句不含否定/反问词时才算数。
    这样「不要发文件，放语音」「为什么要发文件？」「这首歌太长不要发文件」
    都不会被误判为要文件，而「发一首万能处方的文件」照常命中。
    """
    if not text:
        return False
    for clause in _RE_CLAUSE_SPLIT.split(text):
        clause = clause.strip()
        if not clause or not _RE_FILE_INTENT.search(clause):
            continue
        if _RE_FILE_INTENT_NEG.search(clause):
            continue  # 否定 / 反问语境，跳过
        return True
    return False

# 命令通用的前缀捕获组（任意单个非空白/非单词字符，取值由处理器校验）
_PFX = r"(?P<pfx>[^\w\s])"


def _cq_escape_text(value: str) -> str:
    """按 OneBot 规范转义自定义音乐卡片的展示文本。"""
    return "".join(_CQ_TEXT_ESCAPES.get(char, char) for char in value)


def _build_qq_card_cq(payload: Dict[str, str]) -> str:
    """把 QQ音乐卡片字段拼成 NapCat 可识别的自定义音乐卡片 CQ 码。

    `audio` 为空时省略该参数，生成一张可点击跳转的卡片。

    注意（待确认项）：URL / audio / image 字段**刻意不转义**——它们转义后
    需要 NapCat 正确解码 `&amp;` / `&#44;` 才能还原，真机曾按「转了就打不开」
    处理（见 tests/test_music_request.py::test_build_qq_card_cq_escapes_display_text_only）。
    代价是 URL 里的 `,` / `]` 理论上能破坏 CQ 参数结构，属低危（仅能影响本次
    卡片参数字段，不涉及凭据）。改动前需先在真机确认 NapCat 的解码行为。
    """
    parts = ["[CQ:music,type=custom", "url=" + payload["url"]]
    if payload.get("audio"):
        parts.append("audio=" + payload["audio"])
    parts.append("title=" + _cq_escape_text(payload["title"]))
    parts.append("image=" + payload["image"])
    parts.append("content=" + _cq_escape_text(payload.get("content", "")))
    return ",".join(parts) + "]"


# ===== 配置模型 =====


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步，勿手改）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class MusicConfig(PluginConfigBase):
    """点歌行为配置。"""

    __ui_label__ = "点歌"
    __ui_icon__ = "music"
    __ui_order__ = 1

    default_platform: Literal["163", "qq"] = Field(
        default="163", description="默认平台：163(网易云音乐) 或 qq(QQ音乐)"
    )
    command_prefix: str = Field(default="/", description="命令前缀符号，如 / 或 #")
    search_limit: int = Field(default=5, ge=1, le=20, description="搜索结果数量上限")
    relevance_floor: float = Field(
        default=0.6, ge=0.0, le=1.0,
        description=(
            "候选相关度地板（相对最佳匹配的比例）。播放失败时只回退到"
            "「与最佳匹配同一档」的候选；1.0 = 只尝试最佳匹配，0 = 不做过滤。"
            "防止最佳匹配因版权不可播时，静默改播一首完全无关的歌"
        ),
    )
    auto_select_first: bool = Field(
        default=False, description="多首结果时跳过选歌环节，直接发送第一首"
    )
    select_timeout_seconds: int = Field(
        default=_DEFAULT_SELECT_TTL, ge=30, description="待选列表有效期（秒）"
    )
    auto_parse_url: bool = Field(default=True, description="自动解析消息中的音乐链接")
    auto_parse_card: bool = Field(default=True, description="自动解析音乐分享卡片")
    play_mode: Literal["card", "voice", "file"] = Field(
        default="card",
        description=(
            "默认发送形态：card(音乐卡片) / voice(语音音频) / file(音频文件)。"
            "file 需要 NapCat HTTP API，且需配置音频缓存目录"
        ),
    )
    tool_default_mode: Literal["voice", "file"] = Field(
        default="voice",
        description=(
            "LLM 调用点歌工具但没指定发送形态时用哪种：voice(语音，默认) 或 file(音频文件)。"
            "命令与链接解析仍按 play_mode"
        ),
    )
    voice_source: Literal["local", "remote"] = Field(
        default="local",
        description="voice 模式音频来源：local(下载到本地缓存) 或 remote(直接用远程URL)",
    )


class NeteaseConfig(PluginConfigBase):
    """网易云音乐登录态（可选，配了才能点 VIP / 高音质）。"""

    __ui_label__ = "网易云音乐"
    __ui_icon__ = "cloud"
    __ui_order__ = 2

    music_u: str = Field(default="", description="MUSIC_U Cookie（登录凭证）")
    csrf_token: str = Field(default="", description="__csrf Cookie（与 MUSIC_U 配对）")


class QQMusicConfig(PluginConfigBase):
    """QQ音乐登录态（搜索必需：uin + qqmusic_key）。"""

    __ui_label__ = "QQ音乐"
    __ui_icon__ = "headphones"
    __ui_order__ = 3

    uin: str = Field(default="", description="uin——QQ音乐登录账号，搜索必需")
    qqmusic_key: str = Field(
        default="", description="qqmusic_key——登录凭证，搜索必需，账号权益决定可播放范围"
    )


class NapCatConfig(PluginConfigBase):
    """NapCat HTTP API（可选，用于解析卡片原始消息、直连发送 QQ音乐卡片）。"""

    __ui_label__ = "NapCat"
    __ui_icon__ = "server"
    __ui_order__ = 4

    http_url: str = Field(
        default="", description="NapCat HTTP API 地址，如 http://127.0.0.1:9999；留空则禁用直连"
    )
    http_token: str = Field(default="", description="NapCat 访问令牌，留空表示不鉴权")


class AudioCacheConfig(PluginConfigBase):
    """voice 模式的本地音频缓存（MaiBot 与 NapCat 必须能看到同一份文件）。"""

    __ui_label__ = "音频缓存"
    __ui_icon__ = "hard-drive"
    __ui_order__ = 5

    storage_dir: str = Field(
        default="", description="MaiBot 写入缓存的目录；留空则用插件数据目录下的 audio_cache"
    )
    napcat_dir: str = Field(
        default="", description="同一目录在 NapCat 进程内的可见路径；留空则与上面相同"
    )
    max_size_mb: int = Field(default=1024, gt=0, description="缓存总容量上限（MB）")
    expire_hours: int = Field(default=24, gt=0, description="超过这么久未使用的缓存会被清理")
    cleanup_interval_hours: int = Field(default=24, gt=0, description="过期缓存清理任务间隔（小时）")
    max_file_size_mb: int = Field(default=50, gt=0, description="单个音频文件大小上限（MB）")
    download_timeout_seconds: int = Field(default=30, gt=0, description="下载音频的超时时间（秒）")


class MusicRequestConfig(PluginConfigBase):
    """点歌插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)
    netease: NeteaseConfig = Field(default_factory=NeteaseConfig)
    qq: QQMusicConfig = Field(default_factory=QQMusicConfig)
    napcat: NapCatConfig = Field(default_factory=NapCatConfig)
    cache: AudioCacheConfig = Field(default_factory=AudioCacheConfig)


# ===== 插件主类 =====


class MusicRequestPlugin(MaiBotPlugin):
    """点歌插件。

    辅助方法一律写在 `@Command` / `@Tool` / `@HookHandler` **之前**：
    装饰器绑定的是紧随其后的函数，中间插方法会导致静默注册错人。
    """

    config_model = MusicRequestConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)  # 必须第一行，否则 Runner 整体起不来
        self._api: Optional[MusicSearchClient] = None
        self._cache: Optional[MusicAudioCache] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        # 语音发送期间持有该锁；配置热重载重建缓存时靠它等待在途请求结束
        self._cache_guard: Optional[asyncio.Lock] = None
        self._pending: Dict[str, Tuple[List[SongInfo], str, float]] = {}
        # stream_id -> {"group_id": ...} / {"user_id": ...}，供 NapCat 直连发送使用
        self._qq_targets: Dict[str, Dict[str, str]] = {}
        # 路径映射失败只提醒一次，避免每条语音都刷屏
        self._map_warned = False

    # ---------- 基础设施 ----------

    def _run_lock(self, name: str) -> asyncio.Lock:
        """惰性获取 asyncio 锁。

        锁必须在事件循环里创建：`__init__` 阶段还没有运行中的 loop，
        提前创建可能绑到错误的 loop。
        """
        current = getattr(self, name, None)
        if current is None:
            current = asyncio.Lock()
            setattr(self, name, current)
        return current

    def _get_api(self) -> MusicSearchClient:
        """获取（惰性创建）音乐 API 客户端。"""
        if self._api is None:
            netease_cookie: Dict[str, str] = {}
            if self.config.netease.music_u:
                netease_cookie["MUSIC_U"] = self.config.netease.music_u
            if self.config.netease.csrf_token:
                netease_cookie["__csrf"] = self.config.netease.csrf_token

            qq_cookie: Dict[str, str] = {}
            if self.config.qq.uin:
                qq_cookie["uin"] = self.config.qq.uin
            if self.config.qq.qqmusic_key:
                qq_cookie["qqmusic_key"] = self.config.qq.qqmusic_key

            self._api = MusicSearchClient(
                netease_cookie=netease_cookie,
                qq_cookie=qq_cookie,
                napcat_url=self.config.napcat.http_url,
                napcat_token=self.config.napcat.http_token,
            )
        return self._api

    def _cache_dirs(self) -> Tuple[str, str]:
        """解析缓存的两个目录：MaiBot 侧写入路径与 NapCat 侧读取路径。"""
        storage = (self.config.cache.storage_dir or "").strip()
        if not storage:
            storage = str(self.ctx.paths.data_dir / "audio_cache")
        napcat = (self.config.cache.napcat_dir or "").strip() or storage
        return storage, napcat

    async def _warn_map_once(self, stream_id: str = "", *, silent: bool = True) -> None:
        """缓存路径映射失败时告警一次。

        映射失败说明 NapCat 目录与写入目录前缀对不上，交给 NapCat 的是宿主机
        路径——不说破就会被当成「语音/文件莫名发不出去」。只提示一次避免刷屏。
        """
        if self._map_warned:
            return
        self._map_warned = True
        storage, napcat = self._cache_dirs()
        self.ctx.logger.warning(
            "缓存路径映射失败，NapCat 可能读不到文件: 写入目录=%s NapCat目录=%s "
            "（用 /点歌自检 核对两目录是否同一份文件）",
            storage,
            napcat,
        )
        if stream_id and not silent:
            await self.ctx.send.text(
                "提示：缓存目录到 NapCat 的路径映射失败，音频可能发不出去。"
                "请执行 /点歌自检 按提示核对两个目录。",
                stream_id,
            )

    def _needs_local_cache(self) -> bool:
        """是否需要本地音频缓存。

        - `file` 形态：OneBot 的 upload_group_file / upload_private_file 吃的是
          **本地路径**而不是 URL，必须先把文件落盘
        - `voice` + `voice_source=local`：把 MP3 落盘交给 NapCat 发语音
        - LLM 默认要发文件时（tool_default_mode=file），缓存也得先就绪
        """
        music = self.config.music
        if music.play_mode == "file" or music.tool_default_mode == "file":
            return True
        return music.play_mode == "voice" and music.voice_source == "local"

    async def _start_cache(self) -> None:
        """按当前配置启动本地音频缓存（用不到就跳过）。"""
        if not self._needs_local_cache():
            return
        storage, napcat = self._cache_dirs()
        cache = MusicAudioCache(
            storage,
            napcat,
            max_size_bytes=self.config.cache.max_size_mb * 1024 * 1024,
            expire_seconds=self.config.cache.expire_hours * 3600,
            max_file_size_bytes=self.config.cache.max_file_size_mb * 1024 * 1024,
            download_timeout_seconds=self.config.cache.download_timeout_seconds,
        )
        try:
            await cache.initialize()
        except BaseException:
            # 初始化失败也要把连接池收干净，否则反复重载会积累句柄
            await cache.close()
            raise
        self._cache = cache
        interval = self.config.cache.cleanup_interval_hours * 3600
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(interval), name="music-request-cache-cleanup"
        )

    async def _stop_cache(self) -> None:
        """停止清理任务并关闭缓存（等待在途语音发送结束）。"""
        guard = self._run_lock("_cache_guard")
        async with guard:
            if self._cleanup_task is not None:
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    pass
                self._cleanup_task = None
            if self._cache is not None:
                await self._cache.close()
                self._cache = None

    async def _cleanup_loop(self, interval_seconds: int) -> None:
        """周期性清理过期缓存。"""
        while True:
            await asyncio.sleep(max(interval_seconds, 60))
            cache = self._cache
            if cache is None:
                continue
            try:
                await cache.cleanup()
            except Exception:
                self.ctx.logger.exception("清理音乐缓存失败")

    # ---------- 小工具 ----------

    @staticmethod
    def _extract_stream_id(kwargs: Dict[str, Any]) -> str:
        """从命令/工具载荷里取 stream_id（字段名随版本浮动，多路兜底）。"""
        for key in ("stream_id", "chat_id", "session_id", "stream"):
            value = kwargs.get(key)
            if value:
                return str(value)
        message = kwargs.get("message")
        if isinstance(message, dict):
            for key in ("session_id", "stream_id"):
                if message.get(key):
                    return str(message[key])
        return ""

    @staticmethod
    def _extract_text(kwargs: Dict[str, Any]) -> str:
        """取消息纯文本（raw_message 可能是字符串或段列表）。"""
        for key in ("processed_plain_text", "text", "raw_message"):
            value = kwargs.get(key)
            if isinstance(value, str) and value.strip():
                return value
        message = kwargs.get("message")
        if isinstance(message, dict):
            for key in ("processed_plain_text", "raw_message"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    return value
                if isinstance(value, list):
                    joined = " ".join(
                        str(seg.get("data", ""))
                        for seg in value
                        if isinstance(seg, dict) and seg.get("type") == "text"
                    ).strip()
                    if joined:
                        return joined
        return ""

    def _prefix_ok(self, prefix: str) -> bool:
        """命令前缀是否与配置一致（全角会归一化后比较）。"""
        if not prefix:
            return False
        normalized = _PREFIX_NORMALIZE.get(prefix, prefix)
        configured = _PREFIX_NORMALIZE.get(
            str(self.config.music.command_prefix), str(self.config.music.command_prefix)
        )
        return normalized == configured

    def _resolve_platform(self, platform: str = "") -> str:
        """把各种平台写法归一成 `163` / `qq`。"""
        value = (platform or "").strip().lower()
        if value in (PLATFORM_NETEASE, "网易", "网易云", "网易云音乐", "netease"):
            return PLATFORM_NETEASE
        if value in (PLATFORM_QQ, "qq音乐", "qqmusic"):
            return PLATFORM_QQ
        default = str(self.config.music.default_platform).strip().lower()
        return default if default in (PLATFORM_NETEASE, PLATFORM_QQ) else PLATFORM_NETEASE

    @staticmethod
    def _platform_name(platform: str) -> str:
        """平台标识转展示名。"""
        return "QQ音乐" if platform == PLATFORM_QQ else "网易云音乐"

    @staticmethod
    def _same_dir_hint(storage: str, napcat: str) -> bool:
        """两个目录字符串是否字面相同（仅用于状态提示，不是权威判据）。

        权威判据是 `/点歌自检` 的内容摘要比对——两目录可以字面不同却指向同一份
        文件（Docker 挂载），也可以字面相同却因容器隔离而各有一份拷贝。
        """
        normalize = lambda value: str(value).replace("\\", "/").rstrip("/")
        return normalize(storage) == normalize(napcat)

    def _rank_results(self, query: str, results: List[SongInfo]) -> List[SongInfo]:
        """按相关度排序并剔除明显不相关的候选。

        排序只影响「先试哪首」，过滤才是关键：最佳匹配若因版权拿不到音频，
        旧实现会继续往下试，最终把一首完全无关的歌播出去（真机实锤）。
        这里用相对地板把不同档的候选剔掉，宁可报「没找到可播放的」也不要放错。

        Args:
            query: 用户查询串（通常是「歌名」或「歌名 歌手」）。
            results: 平台返回的候选列表。

        Returns:
            过滤后的候选，按相关度降序。
        """
        if len(results) <= 1:
            return list(results)
        ranked = rank_songs(
            query, results, floor_ratio=float(self.config.music.relevance_floor)
        )
        kept = [song for song, _score in ranked]
        if len(kept) < len(results):
            dropped = [song.display() for song in results if song not in kept]
            self.ctx.logger.info(
                "按相关度过滤掉 %d 个候选（query=%r 保留 %d/%d）: %s",
                len(dropped), query, len(kept), len(results), "; ".join(dropped[:3]),
            )
        return kept

    def _format_results(self, results: List[SongInfo]) -> str:
        """把搜索结果格式化成供用户选歌的列表。"""
        prefix = self.config.music.command_prefix
        lines = ["🎵 搜索结果："]
        for index, song in enumerate(results, 1):
            artist = f" - {song.artists}" if song.artists else ""
            album = f" 《{song.album}》" if song.album else ""
            lines.append(f"  {index}. {song.name}{artist}{album}")
        lines.append(f"回复 {prefix}选歌 <序号> 选择，如 {prefix}选歌 1")
        return "\n".join(lines)

    # ---------- 待选状态 ----------

    def _remember_pending(self, stream_id: str, results: List[SongInfo], platform: str) -> None:
        """记录待选列表并顺手清掉过期项。"""
        ttl = int(self.config.music.select_timeout_seconds or _DEFAULT_SELECT_TTL)
        now = time.monotonic()
        for key in [k for k, (_r, _p, ts) in self._pending.items() if now - ts > ttl]:
            self._pending.pop(key, None)
        self._pending[stream_id] = (results, platform, now)

    def _pop_pending(self, stream_id: str) -> Optional[Tuple[List[SongInfo], str, float]]:
        """取出待选列表（过期即视为不存在）。"""
        item = self._pending.get(stream_id)
        if item is None:
            return None
        ttl = int(self.config.music.select_timeout_seconds or _DEFAULT_SELECT_TTL)
        if time.monotonic() - item[2] > ttl:
            self._pending.pop(stream_id, None)
            return None
        return self._pending.pop(stream_id)

    # ---------- QQ 直连目标 ----------

    def _remember_qq_target(self, stream_id: str, message: Any) -> None:
        """从消息载荷里记下 QQ 直连目标（群号或 QQ 号）。"""
        if not stream_id or not isinstance(message, dict):
            return
        if str(message.get("platform") or "qq") != "qq":
            return
        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return
        group_info = message_info.get("group_info")
        user_info = message_info.get("user_info")
        group_id = str(group_info.get("group_id") or "") if isinstance(group_info, dict) else ""
        user_id = str(user_info.get("user_id") or "") if isinstance(user_info, dict) else ""
        if group_id:
            self._qq_targets[stream_id] = {"group_id": group_id}
        elif user_id:
            self._qq_targets[stream_id] = {"user_id": user_id}

    async def _resolve_qq_target(self, stream_id: str) -> Optional[Dict[str, str]]:
        """解析当前会话的 QQ 直连目标。

        优先用消息链路记下的缓存；Tool 等拿不到 message 的场景再按
        stream_id 反查聊天流（需要 `chat.get_all_streams` 能力）。
        """
        if not stream_id:
            return None
        cached = self._qq_targets.get(stream_id)
        if cached:
            return cached
        try:
            streams = await self.ctx.chat.get_all_streams(platform="qq")
        except Exception:
            self.ctx.logger.debug("反查 QQ 聊天流失败，跳过直连通道", exc_info=True)
            return None
        if not isinstance(streams, list):
            return None
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if str(stream.get("stream_id") or stream.get("session_id") or "") != stream_id:
                continue
            group_id = str(stream.get("group_id") or "")
            user_id = str(stream.get("user_id") or "")
            if group_id:
                target = {"group_id": group_id}
            elif user_id:
                target = {"user_id": user_id}
            else:
                continue
            self._qq_targets[stream_id] = target
            return target
        return None

    # ---------- 发送 ----------

    async def _send_card(self, song: SongInfo, stream_id: str, *, silent: bool = False) -> bool:
        """按音乐卡片形态发送歌曲，失败时回退纯文本。"""
        try:
            if song.platform == PLATFORM_QQ:
                return await self._send_qq_card(song, stream_id, silent=silent)
            sent = await self.ctx.send.custom(
                "music", {"type": PLATFORM_NETEASE, "id": song.song_id}, stream_id
            )
        except Exception:
            self.ctx.logger.exception("发送音乐卡片异常: %s %s", song.platform, song.song_id)
            if not silent:
                await self.ctx.send.text(song.display(), stream_id)
            return False

        if sent:
            return True
        self.ctx.logger.warning("音乐卡片发送失败: %s %s", song.platform, song.song_id)
        if not silent:
            await self.ctx.send.text(song.display(), stream_id)
        return False

    async def _send_qq_card(self, song: SongInfo, stream_id: str, *, silent: bool = False) -> bool:
        """发送 QQ音乐卡片：优先 NapCat 直连，其次走适配器 music 段。"""
        api = self._get_api()
        try:
            payload = await api.qq_music_card(song)
        except MusicAPIResponseError as exc:
            self.ctx.logger.warning("解析 QQ音乐卡片失败: %s %s: %s", song.platform, song.song_id, exc)
            payload = None
        except Exception:
            self.ctx.logger.exception("解析 QQ音乐卡片异常: %s %s", song.platform, song.song_id)
            payload = None

        if payload is None:
            if not silent:
                await self.ctx.send.text(f"「{song.display()}」的 QQ音乐卡片生成失败", stream_id)
            return False

        jump_only = not payload.get("audio")
        target = await self._resolve_qq_target(stream_id)
        if target is not None:
            try:
                ok, _ = await api.napcat_send_message(_build_qq_card_cq(payload), **target)
            except Exception:
                self.ctx.logger.exception("NapCat 直连发送异常: %s", song.song_id)
                ok = False
            if ok:
                self.ctx.logger.info("QQ音乐卡片已通过 NapCat 直连发送: %s", song.song_id)
                if jump_only and not silent:
                    await self.ctx.send.text(
                        "该歌曲受版权/登录限制无法直接播放，已发送可点击跳转的卡片", stream_id
                    )
                return True
            self.ctx.logger.warning("NapCat 直连失败，回退适配器 music 段: %s", song.song_id)

        data = {key: value for key, value in payload.items() if value}
        try:
            sent = await self.ctx.send.custom("music", data, stream_id)
        except Exception:
            self.ctx.logger.exception("发送 QQ音乐卡片异常: %s", song.song_id)
            sent = False
        if not sent:
            self.ctx.logger.warning("QQ音乐卡片发送失败: %s", song.song_id)
            if not silent:
                await self.ctx.send.text(f"「{song.display()}」的 QQ音乐卡片发送失败", stream_id)
            return False

        if jump_only and not silent:
            await self.ctx.send.text(
                "该歌曲受版权/登录限制无法直接播放，已发送可点击跳转的卡片", stream_id
            )
        return True

    async def _send_voice(self, song: SongInfo, stream_id: str, *, silent: bool = False) -> bool:
        """获取音频并以语音消息发送。"""
        api = self._get_api()
        local = self.config.music.voice_source == "local"
        guard = self._run_lock("_cache_guard")

        async with guard:
            # 在锁内重新读缓存：配置热重载可能已经把旧缓存关掉了
            cache = self._cache
            if local and cache is None:
                self.ctx.logger.warning("voice_source=local 但音频缓存未就绪，本次改用远程 URL")

            media_id = song.media_id
            if song.platform == PLATFORM_QQ and not media_id:
                try:
                    detail = await api.get_qq_song_detail(song.song_id)
                except MusicAPIResponseError:
                    detail = None
                if detail is not None:
                    media_id = detail.media_id

            try:
                audio_url = await api.get_song_url(
                    song.song_id, song.platform, media_id, mp3_only=local and cache is not None
                )
            except MusicAPIResponseError as exc:
                self.ctx.logger.warning("获取音频直链失败: %s %s: %s", song.platform, song.song_id, exc)
                audio_url = None

            if not audio_url:
                if not silent:
                    await self.ctx.send.text(
                        f"找到「{song.display()}」但音乐平台未返回可用音频", stream_id
                    )
                return False

            reference = audio_url
            cache_path = None
            if local and cache is not None:
                try:
                    cache_path = await cache.get_or_download(song.platform, song.song_id, audio_url)
                    cache.retain(cache_path)
                    reference, mapped = cache.napcat_path_checked(cache_path)
                    if not mapped:
                        await self._warn_map_once(stream_id, silent=silent)
                except AudioCacheError as exc:
                    self.ctx.logger.warning("缓存音频失败，回退远程 URL: %s", exc)
                    cache_path = None
                    reference = audio_url

            try:
                sent = await self.ctx.send.custom("voiceurl", {"url": reference}, stream_id)
            except Exception:
                self.ctx.logger.exception("发送语音异常: %s %s", song.platform, song.song_id)
                sent = False
            finally:
                if cache_path is not None and cache is not None:
                    cache.release(cache_path)

        if sent:
            return True
        self.ctx.logger.warning("发送语音失败: %s %s", song.platform, song.song_id)
        if not silent:
            await self.ctx.send.text(song.display(), stream_id)
        return False

    def _resolve_send_as(self, requested: str) -> str:
        """把发送形态归一成 `card` / `voice` / `file`。

        工具参数留空或给不出有效值时回落配置里的 `tool_default_mode`（默认语音）。
        注意：`card` 虽未在工具参数说明里宣传，但传了就照做——
        把不认识的取值静默替换成默认，属于「用户要 A 却给了 B」，正是要避免的毛病。
        """
        # 类型归一：非 str 入参（QA 实锤：send_as=123）直接 .strip() 会抛 AttributeError
        value = requested.strip().lower() if isinstance(requested, str) else ""
        if value in ("card", "voice", "file"):
            return value
        if value:
            self.ctx.logger.info("send_as=%r 不是有效形态，改用默认形态", requested)
        default = str(self.config.music.tool_default_mode).strip().lower()
        return default if default in ("voice", "file") else "voice"

    async def _degrade_to_card(
        self, song: SongInfo, stream_id: str, *, silent: bool, reason: str,
        outcome: Dict[str, str] | None = None,
    ) -> bool:
        """文件形态不可用时降级为音乐卡片。

        刻意**不降级为语音**：用户选文件图的就是音质，而语音会把音质打到
        SILK 级别。卡片由官方播放器播放，是仅次于文件的形态。
        降级必须留痕（日志 + 可选的用户提示 + 工具返回文本），否则就变成静默换形态。
        """
        self.ctx.logger.warning("文件形态不可用，降级为音乐卡片: %s（%s）", song.display(), reason)
        if outcome is not None:
            outcome["kind"] = "card_degraded"
            outcome["reason"] = reason
        if not silent:
            await self.ctx.send.text(f"暂时发不了文件（{reason}），已改为发送音乐卡片", stream_id)
        return await self._send_card(song, stream_id, silent=True)

    async def _send_file(
        self, song: SongInfo, stream_id: str, *, silent: bool = False,
        outcome: Dict[str, str] | None = None,
    ) -> bool:
        """把歌曲作为「群文件 / 私聊文件」发送，完整保留原始音质。

        这是唯一能绕开 QQ 语音转码（SILK ≈ 6–12 kbps + 60 秒上限）的形态：
        文件原样上传，用户下载后用播放器听。代价是必须有 NapCat HTTP API，
        且 NapCat 要能读到缓存文件（与语音本地缓存同一个前提，用 `/点歌自检` 核对）。
        """
        api = self._get_api()
        target = await self._resolve_qq_target(stream_id)
        if target is None:
            return await self._degrade_to_card(
                song, stream_id, silent=silent,
                reason="未配置 NapCat HTTP API 或拿不到群号/QQ号", outcome=outcome,
            )

        guard = self._run_lock("_cache_guard")
        async with guard:
            cache = self._cache
            if cache is None:
                return await self._degrade_to_card(
                    song, stream_id, silent=silent, reason="音频缓存未启用", outcome=outcome,
                )

            media_id = song.media_id
            if song.platform == PLATFORM_QQ and not media_id:
                try:
                    detail = await api.get_qq_song_detail(song.song_id)
                except MusicAPIResponseError:
                    detail = None
                if detail is not None:
                    media_id = detail.media_id

            try:
                # 文件形态不限制 MP3：优先无损/高码率，这才是它存在的意义
                audio_url = await api.get_song_url(
                    song.song_id, song.platform, media_id, mp3_only=False
                )
            except MusicAPIResponseError as exc:
                self.ctx.logger.warning(
                    "获取音频直链失败: %s %s: %s", song.platform, song.song_id, exc
                )
                audio_url = None
            if not audio_url:
                if outcome is not None:
                    outcome["kind"] = "unavailable"
                if not silent:
                    await self.ctx.send.text(
                        f"找到「{song.display()}」但音乐平台未返回可用音频", stream_id
                    )
                return False

            try:
                path = await cache.get_or_download(song.platform, song.song_id, audio_url)
                cache.retain(path)
            except AudioCacheError as exc:
                return await self._degrade_to_card(
                    song, stream_id, silent=silent,
                    reason=f"音频下载失败（{exc}）", outcome=outcome,
                )

            try:
                napcat_path, mapped = cache.napcat_path_checked(path)
                if not mapped:
                    await self._warn_map_once(stream_id, silent=silent)
                display = song.display().strip() or path.stem
                file_name = f"{display}{path.suffix}"
                ok, _data = await api.napcat_upload_file(
                    napcat_path, name=file_name, **target
                )
            finally:
                cache.release(path)

        if ok:
            self.ctx.logger.info("已作为文件发送: %s（%s）", file_name, napcat_path)
            if outcome is not None:
                outcome["kind"] = "file"
                outcome["file_name"] = file_name
            if not silent:
                await self.ctx.send.text(f"已发送文件：{file_name}", stream_id)
            return True
        return await self._degrade_to_card(
            song, stream_id, silent=silent, reason="NapCat 上传失败", outcome=outcome,
        )

    async def _send_song(
        self, song: SongInfo, stream_id: str, *, silent: bool = False, mode: str = "",
        outcome: Dict[str, str] | None = None,
    ) -> bool:
        """按发送形态分发。`mode` 留空则用配置里的 `play_mode`。

        `outcome` 为可选出参：真实发送形态与降级原因会写回其中，
        供 @Tool 生成**与实际一致**的工具返回文本（避免 reply 模型
        看到「已发送文件」而用户实际收到卡片）。
        """
        resolved = (mode or str(self.config.music.play_mode)).strip().lower()
        if resolved == "file":
            return await self._send_file(song, stream_id, silent=silent, outcome=outcome)
        if outcome is not None:
            outcome["kind"] = "voice" if resolved == "voice" else "card"
        if resolved == "voice":
            return await self._send_voice(song, stream_id, silent=silent)
        return await self._send_card(song, stream_id, silent=silent)

    # ---------- 搜索 ----------

    async def _search(self, query: str, platform: str) -> List[SongInfo]:
        """搜索歌曲（异常口径交给调用方处理）。"""
        api = self._get_api()
        return await api.search(query, platform, limit=int(self.config.music.search_limit))

    async def _search_and_send(self, query: str, platform: str, stream_id: str) -> Tuple[bool, str]:
        """搜索并按「单首直发 / 多首列候选」策略发送（命令与卡片解析共用）。"""
        resolved = self._resolve_platform(platform)
        try:
            results = await self._search(query, resolved)
        except MusicAPIResponseError as exc:
            # 只有 QQ音乐缺登录态时给出可操作的下一步提示
            if resolved == PLATFORM_QQ and "缺少配置" in str(exc):
                self.ctx.logger.warning("QQ音乐搜索不可用: %s", exc)
                message = "QQ音乐搜索需要先配置 qq.uin 与 qqmusic_key，或改用 /点歌 163 <歌曲名>"
            else:
                self.ctx.logger.error("音乐搜索失败: %s", exc.diagnostic())
                message = "搜索歌曲时出错，请稍后再试"
            await self.ctx.send.text(message, stream_id)
            return False, message
        except Exception:
            self.ctx.logger.exception("音乐搜索异常: platform=%s query=%r", resolved, query)
            message = "搜索歌曲时出错，请稍后再试"
            await self.ctx.send.text(message, stream_id)
            return False, message

        # 先按相关度排序再决定「直发还是列候选」：列表顺序直接影响用户选到哪首
        results = self._rank_results(query, results)

        if not results:
            message = f"在{self._platform_name(resolved)}上没找到「{query}」"
            await self.ctx.send.text(message, stream_id)
            return False, message

        if len(results) == 1 or self.config.music.auto_select_first:
            song = results[0]
            sent = await self._send_song(song, stream_id)
            return sent, f"已发送: {song.display()}" if sent else f"发送失败: {song.display()}"

        self._remember_pending(stream_id, results, resolved)
        await self.ctx.send.text(self._format_results(results), stream_id)
        return True, f"找到 {len(results)} 首，已列出候选"

    # ---------- 按歌曲 ID 发送（链接 / 卡片解析用） ----------

    async def _send_by_ref(
        self, ref: MusicRef, stream_id: str, *, card: Optional[MusicCardInfo] = None
    ) -> bool:
        """按链接解析结果发送歌曲（解析场景静默处理，不刷屏）。"""
        song = SongInfo(
            song_id=ref.song_id,
            name=card.song_name if card else "",
            artists=card.artist if card else "",
            platform=ref.platform,
        )
        return await self._send_song(song, stream_id, silent=True)

    async def _ref_from_message_raw(self, message_id: str) -> Optional[MusicRef]:
        """调 NapCat `/get_msg` 从原始消息的 json 段精确解析歌曲 ID。

        适配器把音乐卡片转成纯文本后会丢掉歌曲 ID，只有回溯原始消息才能拿到
        jumpUrl。未配置 NapCat HTTP API 时直接返回 None（走文本兜底）。
        """
        try:
            numeric_id = int(message_id)
        except (TypeError, ValueError):
            return None
        data = await self._get_api().get_raw_message(numeric_id)
        if not data:
            return None
        segments = data.get("message")
        if not isinstance(segments, list):
            return None

        for segment in segments:
            if not isinstance(segment, dict) or segment.get("type") != "json":
                continue
            raw = segment.get("data")
            raw_json = str(raw.get("data") or "") if isinstance(raw, dict) else ""
            if not raw_json:
                continue
            try:
                parsed = json.loads(raw_json)
            except (ValueError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue

            app = str(parsed.get("app") or "")
            meta = parsed.get("meta")
            if not isinstance(meta, dict):
                continue

            # 音乐卡片：app 为 com.tencent.music.lua / com.tencent.structmsg
            if app in ("com.tencent.music.lua", "com.tencent.structmsg"):
                music = meta.get("music") or meta.get("news") or {}
                if isinstance(music, dict):
                    ref = await self._ref_from_url(str(music.get("jumpUrl") or ""))
                    if ref is not None:
                        return ref

            # 音乐小程序：com.tencent.miniapp_01
            if app == "com.tencent.miniapp_01":
                detail = meta.get("detail_1")
                if isinstance(detail, dict) and str(detail.get("title") or "") in (
                    "QQ音乐", "网易云音乐"
                ):
                    ref = await self._ref_from_url(str(detail.get("qqdocurl") or ""))
                    if ref is not None:
                        return ref
        return None

    async def _ref_from_url(self, url: str) -> Optional[MusicRef]:
        """从 URL 解析 MusicRef；短链先做重定向解析。"""
        if not url:
            return None
        ref = parse_music_url(url)
        if ref is None:
            return None
        if not ref.short:
            return ref
        resolved = await self._get_api().resolve_short_url(url)
        if not resolved:
            return None
        inner = parse_music_url(resolved)
        if inner is None or inner.short:
            return None
        return inner

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        """插件加载：初始化并发原语与音频缓存。"""
        self._run_lock("_cache_guard")
        try:
            await self._start_cache()
        except Exception:
            self.ctx.logger.exception("初始化音频缓存失败（voice 本地缓存不可用）")
        # 行为自检：把生效配置打进启动日志，真机排障时一眼可见
        self.ctx.logger.info(
            "点歌插件已加载 version=%s enabled=%s platform=%s mode=%s voice_source=%s "
            "prefix=%s 网易云登录=%s QQ音乐登录=%s NapCat直连=%s",
            SUPPORTED_CONFIG_VERSION,
            self.config.plugin.enabled,
            self.config.music.default_platform,
            self.config.music.play_mode,
            self.config.music.voice_source,
            self.config.music.command_prefix,
            bool(self.config.netease.music_u),
            bool(self.config.qq.uin and self.config.qq.qqmusic_key),
            bool(self.config.napcat.http_url),
        )

    async def on_unload(self) -> None:
        """插件卸载：取消后台任务、关闭连接与缓存。"""
        await self._stop_cache()
        if self._api is not None:
            await self._api.close()
            self._api = None
        self._pending.clear()
        self._qq_targets.clear()
        self.ctx.logger.info("点歌插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热重载：重建 API 客户端与音频缓存。"""
        del config_data, version
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        await self._stop_cache()
        if self._api is not None:
            await self._api.close()
            self._api = None
        try:
            await self._start_cache()
        except Exception:
            self.ctx.logger.exception("重建音频缓存失败")
        self.ctx.logger.info("点歌插件配置已更新，API 客户端与音频缓存已重置")
        # 映射告警去重标记随配置重置：改完缓存/NapCat 路径后应重新提示一次，
        # 否则用户修完配置也看不到核对提示
        self._map_warned = False

    # ---------- @Command ----------

    @Command(
        "点歌",
        description="搜索点歌，可用 /点歌 [163|qq] <歌曲名> 指定平台",
        pattern=(
            _PFX + r"\s*点歌"
            r"(?:\s+(?P<platform>163|qq|网易云音乐|网易云|网易|netease|qq音乐|qqmusic))?"
            r"\s+(?P<query>.+?)\s*$"
        ),
    )
    async def cmd_点歌(
        self, matched_groups: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """处理 `/点歌` 命令。"""
        groups = matched_groups if isinstance(matched_groups, dict) else {}
        if not self._prefix_ok(str(groups.get("pfx") or "")):
            return False, "", 0

        stream_id = self._extract_stream_id(kwargs)
        self._remember_qq_target(stream_id, kwargs.get("message"))

        query = str(groups.get("query") or "").strip()
        if not query:
            # 分组缺失时退回自行解析原始文本，避免因 Host 版本差异静默失效
            match = re.search(
                re.escape(str(self.config.music.command_prefix)) + r"\s*点歌\s*(?P<query>.+)$",
                self._extract_text(kwargs),
            )
            query = (match.group("query") or "").strip() if match else ""

        if not query:
            await self.ctx.send.text(
                f"用法：{self.config.music.command_prefix}点歌 [163|qq] <歌曲名>", stream_id
            )
            return False, "缺少歌曲名", 0

        ok, message = await self._search_and_send(query, str(groups.get("platform") or ""), stream_id)
        return ok, message, 2

    @Command(
        "选歌",
        description="从候选列表里选一首歌播放，用法 /选歌 <序号>",
        pattern=_PFX + r"\s*选歌\s*(?P<index>\d+)\s*$",
    )
    async def cmd_选歌(
        self, matched_groups: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """处理 `/选歌` 命令。"""
        groups = matched_groups if isinstance(matched_groups, dict) else {}
        if not self._prefix_ok(str(groups.get("pfx") or "")):
            return False, "", 0

        stream_id = self._extract_stream_id(kwargs)
        self._remember_qq_target(stream_id, kwargs.get("message"))

        raw_index = str(groups.get("index") or "").strip()
        if not raw_index:
            match = re.search(
                re.escape(str(self.config.music.command_prefix)) + r"\s*选歌\s*(\d+)$",
                self._extract_text(kwargs),
            )
            raw_index = match.group(1) if match else ""

        prefix = self.config.music.command_prefix
        if not raw_index:
            await self.ctx.send.text(f"用法：{prefix}选歌 <序号>", stream_id)
            return False, "缺少序号", 0

        pending = self._pop_pending(stream_id)
        if pending is None:
            await self.ctx.send.text(f"没有待选的歌曲，先用 {prefix}点歌 <歌曲名> 搜索", stream_id)
            return False, "无待选歌曲", 0

        results, _platform, _ts = pending
        index = int(raw_index)
        if index < 1 or index > len(results):
            # 序号非法就把候选放回去，用户还能重选
            self._pending[stream_id] = pending
            await self.ctx.send.text(f"序号超出范围，请输入 1~{len(results)}", stream_id)
            return False, "序号超出范围", 0

        song = results[index - 1]
        sent = await self._send_song(song, stream_id)
        return sent, f"已选择: {song.display()}", 2

    @Command(
        "点歌状态",
        description="查看点歌插件运行状态与自检信息",
        pattern=_PFX + r"\s*点歌状态\s*$",
    )
    async def cmd_点歌状态(
        self, matched_groups: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """状态命令：一眼看清配置是否生效、哪些平台可用。"""
        groups = matched_groups if isinstance(matched_groups, dict) else {}
        if not self._prefix_ok(str(groups.get("pfx") or "")):
            return False, "", 0

        stream_id = self._extract_stream_id(kwargs)
        lines = [
            f"🎧 点歌插件 v{SUPPORTED_CONFIG_VERSION}",
            f"启用: {self.config.plugin.enabled}",
            f"默认平台: {self._platform_name(self._resolve_platform(''))}",
            f"默认发送形态: {self.config.music.play_mode}（voice 来源: {self.config.music.voice_source}）",
            f"LLM 工具默认形态: {self.config.music.tool_default_mode}",
            f"命令前缀: {self.config.music.command_prefix}",
            f"网易云登录: {'已配置' if self.config.netease.music_u else '未配置（仅免费/低音质）'}",
            "QQ音乐登录: "
            + ("已配置" if self.config.qq.uin and self.config.qq.qqmusic_key else "未配置（搜索不可用）"),
            f"NapCat 直连: {'已配置' if self.config.napcat.http_url else '未配置'}",
            f"解析链接/卡片: {self.config.music.auto_parse_url}/{self.config.music.auto_parse_card}",
        ]
        # 缓存使用量（仅 voice 本地缓存有意义）
        cache = self._cache
        if cache is not None:
            storage, napcat = self._cache_dirs()
            count = total = 0
            try:
                for item in Path(storage).glob("*.mp3"):
                    count += 1
                    total += item.stat().st_size
            except OSError:
                pass
            lines.append(f"音频缓存: {count} 个 / {total // (1024 * 1024)} MB")
            lines.append(f"  写入目录: {storage}")
            lines.append(f"  NapCat 目录: {napcat}")
            lines.append("  映射检查: " + (
                "两目录相同 ✅"
                if self._same_dir_hint(storage, napcat)
                else "两目录不同，请用 /点歌自检 核对是否同一份文件"
            ))
        else:
            lines.append("音频缓存: 未启用")
        lines.append(f"待选列表: {len(self._pending)} 个会话")

        text = "\n".join(lines)
        sent = bool(await self.ctx.send.text(text, stream_id))
        return sent, text, 2 if sent else 0

    @Command(
        "点歌自检",
        description="写缓存探针文件，用于核对 NapCat 与 MaiBot 是否看到同一份文件",
        pattern=_PFX + r"\s*点歌自检\s*$",
    )
    async def cmd_点歌自检(
        self, matched_groups: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """缓存目录自检：写探针并给出 NapCat 侧的核对命令。

        `voice_source=local` 唯一真正的前提是「两个目录指向同一份文件」。
        光看 `ls` 能列出同名文件不够——那也可能是两份数据，所以这里给出
        内容摘要，让用户在 NapCat 侧读同一文件后比对。
        """
        groups = matched_groups if isinstance(matched_groups, dict) else {}
        if not self._prefix_ok(str(groups.get("pfx") or "")):
            return False, "", 0

        stream_id = self._extract_stream_id(kwargs)
        storage, napcat = self._cache_dirs()
        try:
            probe = write_cache_probe(storage, napcat)
        except OSError as exc:
            self.ctx.logger.error("写缓存探针失败: %s", exc)
            text = f"❌ 自检失败：无法在 {storage} 写入探针文件（{exc}）"
            sent = bool(await self.ctx.send.text(text, stream_id))
            return sent, text, 2 if sent else 0

        local_mode = (
            self.config.music.play_mode == "voice"
            and self.config.music.voice_source == "local"
        )
        lines = [
            "🔍 点歌缓存目录自检",
            "",
            f"写入目录: {storage}",
            f"NapCat 目录: {napcat}",
            f"探针文件: {probe.filename}",
            f"内容摘要: {probe.digest}（MD5 前 8 位）",
            "路径映射: " + ("✅ 成功" if probe.mapped else "⚠️ 失败（两目录前缀对不上）"),
        ]
        if not local_mode:
            lines.append(
                f"当前播放模式: {self.config.music.play_mode}/"
                f"{self.config.music.voice_source} —— 未使用本地缓存"
            )

        # 按 NapCat 侧路径形态给对应平台的核对命令，避免给出跑不通的写法
        if probe.napcat_path.startswith("/"):
            hints = [
                f"  docker exec <NapCat容器名> md5sum {probe.napcat_path}",
                f"  md5sum {probe.napcat_path}    # NapCat 非 Docker（Linux）",
            ]
        else:
            hints = [
                f'  certutil -hashfile "{probe.napcat_path}" MD5',
                f'  Get-FileHash "{probe.napcat_path}" -Algorithm MD5    # PowerShell',
            ]
        lines += [
            "",
            "在 NapCat 侧执行其一，比对摘要前 8 位：",
            *hints,
            "",
            "一致 → 同一份文件，voice_source=local 可用",
            "不一致或文件不存在 → 两份数据，请改 remote 或修正挂载/路径",
            f"核对完可删除探针: {probe.storage_path}",
        ]

        text = "\n".join(lines)
        sent = bool(await self.ctx.send.text(text, stream_id))
        return sent, text, 2 if sent else 0

    # ---------- @Tool ----------

    @staticmethod
    def _describe_outcome(song: SongInfo, mode: str, outcome: Dict[str, str]) -> str:
        """按**真实发送形态**生成工具返回文本。

        真机实锤（2026-09-11）：文件上传失败降级为卡片后，工具仍返回「已发送文件」，
        reply 模型据此对用户说「文件版《哑巴》你看看收到没」，而用户实际收到卡片。
        这里以 _send_file 写回的 outcome 为准，如实说明已降级，并提示可照实转述。
        """
        display = song.display()
        kind = outcome.get("kind") or ("file" if mode == "file" else "voice")
        if kind == "file":
            return f"已发送文件: {display}"
        if kind == "card_degraded":
            reason = outcome.get("reason") or "文件发送不可用"
            return (
                f"歌曲「{display}」原本要发文件，但发文件失败（{reason}），"
                "已改为发送音乐卡片。请如实告诉用户收到的是音乐卡片而不是文件，"
                "不要再说「文件已发送」。"
            )
        if kind == "unavailable":
            return f"找到「{display}」但音乐平台未返回可用音频"
        if kind == "card":
            return f"已以音乐卡片发送: {display}"
        return f"已播放: {display}"

    @Tool(
        "search_and_play_music",
        description=(
            "搜索歌曲并把歌发到当前聊天。"
            "当用户想听歌、点歌、放首歌、找某首歌时使用；"
            "用户贴了歌词、正在聊某首歌并表示想听（如「放一下」「来一段」「我想听」）时也用这个工具。"
            "**默认不要传 send_as**，插件会按配置的默认形态发送；"
            "只有用户本条消息里明确要求「发文件 / 无损 / 要好音质」时才传 send_as=file。"
            "不要指定平台，插件会自动选可用平台并逐首尝试候选。"
            "本工具已内置换源与重试；若返回播放失败，不要重复调用，直接告诉用户即可。"
        ),
        detailed_description=(
            "参数 query 为歌曲名，或「歌名 歌手」关键词。"
            "若上下文里已有其它插件给出的「可点播查询」串，直接把整串填进 query；"
            "不要把一句歌词当 query，也不要自己另猜歌名。\n"
            "参数 send_as：**绝大多数情况留空不传**，留空时按插件配置的默认形态发送。\n"
            "- 留空（推荐）：用配置默认形态，用户没提要求时一律这样；\n"
            "- voice：语音消息，点开即播；\n"
            "- file：音频文件，保留原始音质、不受 QQ 语音转码与 60 秒限制。"
            "**仅当用户本条消息明确说「发文件 / 要无损 / 要好音质」时**才传，"
            "不要因为「音质更好」就自作主张传 file。\n"
            "调用后会直接把最佳匹配发到当前会话，不会列出候选项让用户选，"
            "因此不要用它做「只是搜一下看看」。"
            "一次只发一首。返回文本以「已播放」/「已发送文件」/「已以音乐卡片发送」开头表示成功；\n"
            "**重要**：若返回文本说「原本要发文件…已改为发送音乐卡片」，说明文件形态失败了，"
            "实际送达的是音乐卡片。此时必须如实告诉用户收到的是卡片，不要谎称已发文件。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="歌曲名或「歌名 歌手」关键词",
                required=True,
            ),
            ToolParameterInfo(
                name="send_as",
                param_type=ToolParamType.STRING,
                description=(
                    "一般留空。留空=按插件配置的默认形态发送；"
                    "仅当用户明确要求「发文件/无损/好音质」时才填 file"
                ),
                required=False,
            ),
        ],
    )
    async def search_and_play_music(
        self, query: str = "", send_as: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """LLM 点歌：直发最佳匹配，失败自动换平台。"""
        stream_id = self._extract_stream_id(kwargs)
        self._remember_qq_target(stream_id, kwargs.get("message"))

        # 类型归一：@Tool 参数由 Host 注入，理论上都是 str，但非 str 会让
        # (query or "").strip() 直接抛 AttributeError（QA 实锤：query=123）。
        keyword = query.strip() if isinstance(query, str) else ("" if query is None else str(query).strip())
        if not keyword:
            return {"content": "请提供歌曲名或关键词"}
        if not stream_id:
            return {"content": "当前会话缺少 stream_id，无法发送歌曲"}

        mode = self._resolve_send_as(send_as)
        trigger_text = self._extract_text(kwargs)
        # 纯占位文本（[voiceurl消息] / [music消息]）是 Host 生成的载荷占位符，
        # 不含用户表达，按「拿不到触发文本」处理，从而尊重调用方入参
        if trigger_text and _RE_PLACEHOLDER_ONLY.match(trigger_text.strip()):
            trigger_text = ""
        # 代码层兜底（双向）：
        # 1) 触发消息明确要「文件/无损/好音质」→ 强制 file，
        #    不依赖 LLM 自觉传 send_as（LongCat 实测会漏传）；
        # 2) 反过来，LLM 自作主张传了 file、但用户根本没提 → 回落配置默认形态。
        #    真机实锤（2026-09-12）：用户配置 tool_default_mode=voice，
        #    只说「放一首万能处方」却收到了文件——模型被工具描述里的「音频文件」诱导了。
        # 否定/反问语境的过滤在 _has_file_intent 内（逐小句判断）。
        has_file_intent = _has_file_intent(trigger_text)
        if mode != "file" and has_file_intent:
            mode = "file"
            self.ctx.logger.info(
                "触发消息含「发文件」意图，强制 file 形态: %.60s", trigger_text
            )
        elif mode == "file" and trigger_text and not has_file_intent:
            fallback = str(self.config.music.tool_default_mode).strip().lower()
            mode = fallback if fallback in ("voice", "file") else "voice"
            self.ctx.logger.info(
                "LLM 传了 send_as=file 但触发消息无此意图，回落默认形态 %s: %.60s",
                mode, trigger_text,
            )
        # 记录形态决策，便于排查「用户没要求却发了文件」这类问题
        self.ctx.logger.info(
            "工具形态决策: send_as=%r -> mode=%s（tool_default_mode=%s 意图=%s）",
            send_as, mode, self.config.music.tool_default_mode, has_file_intent,
        )
        default = self._resolve_platform("")
        platforms = [default, PLATFORM_QQ if default == PLATFORM_NETEASE else PLATFORM_NETEASE]
        failures: List[str] = []

        for platform in platforms:
            try:
                results = await self._search(keyword, platform)
            except MusicAPIResponseError as exc:
                failures.append(f"{self._platform_name(platform)}: {exc}")
                self.ctx.logger.warning("工具搜索失败(%s): %s", platform, exc)
                continue
            except Exception:
                self.ctx.logger.exception("工具搜索异常(%s): %s", platform, keyword)
                failures.append(f"{self._platform_name(platform)}: 异常")
                continue

            # 相关度过滤：只尝试与最佳匹配同一档的候选。
            # 真机实锤：最佳匹配因版权拿不到音频时，旧实现会一路往下试，
            # 最终把一首完全无关的歌播出去——宁可报失败也不要放错歌。
            ranked = self._rank_results(keyword, results)

            for song in ranked:
                outcome: Dict[str, str] = {}
                if await self._send_song(song, stream_id, silent=True, mode=mode, outcome=outcome):
                    return {"content": self._describe_outcome(song, mode, outcome)}

        self.ctx.logger.warning("工具点歌全部失败: query=%r 明细=%s", keyword, failures)
        return {
            "content": (
                f"已在网易云音乐和QQ音乐搜索「{keyword}」，均未取到可播放音频。"
                "请不要重复调用本工具，直接告知用户当前无法播放即可。"
            )
        }

    # ---------- @HookHandler ----------

    @HookHandler(
        "chat.receive.after_process",
        name="parse_music_link",
        description="解析入站消息里的音乐链接与分享卡片并发送",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=20000,
        # 解析类钩子出错不应阻断聊天主流程
        error_policy=ErrorPolicy.SKIP,
    )
    async def parse_music_link(self, message: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """入站解析：命中音乐链接/卡片则发送并拦下这条消息。"""
        target = message if isinstance(message, dict) else kwargs.get("message")
        if not isinstance(target, dict):
            return {"action": "continue"}

        stream_id = str(target.get("session_id") or target.get("stream_id") or "")
        text = self._extract_text({"message": target})
        if not stream_id or not text:
            return {"action": "continue"}

        self._remember_qq_target(stream_id, target)

        # ── 1. 分享卡片 ──
        if self.config.music.auto_parse_card:
            card = parse_music_card_text(text)
            if card is not None and card.query:
                ref = await self._ref_from_message_raw(str(target.get("message_id") or ""))
                if ref is None and card.url:
                    ref = await self._ref_from_url(card.url)

                if ref is not None:
                    sent = await self._send_by_ref(ref, stream_id, card=card)
                    self.ctx.logger.info(
                        "已解析音乐卡片: %s → %s %s", card.query, ref.platform, ref.song_id
                    )
                    return {"action": "abort" if sent else "continue"}

                # 精确解析失败：正文里有音乐链接就交给第 2 步，否则按歌名搜索
                has_link = any(parse_music_url(url) is not None for url in extract_urls(text))
                if not (has_link and self.config.music.auto_parse_url):
                    platform = card.platform or self._resolve_platform("")
                    try:
                        results = await self._search(card.query, platform)
                    except MusicAPIResponseError as exc:
                        self.ctx.logger.warning("卡片搜索失败: %s", exc)
                        results = []
                    except Exception:
                        self.ctx.logger.exception("卡片搜索异常: %r", card.query)
                        results = []
                    # 卡片解析失败后退化成「按歌名搜索」，同样要过相关度过滤，
                    # 否则会把不相关的搜索首条当成用户分享的那首
                    ranked = self._rank_results(card.query, results)
                    if ranked:
                        sent = await self._send_song(ranked[0], stream_id, silent=True)
                        self.ctx.logger.info("已按卡片歌名搜索发送: %s", ranked[0].display())
                        return {"action": "abort" if sent else "continue"}
                    self.ctx.logger.info("卡片歌名搜索无结果: %r", card.query)
                    return {"action": "continue"}

        # ── 2. 纯链接 ──
        if not self.config.music.auto_parse_url:
            return {"action": "continue"}

        for url in extract_urls(text):
            ref = await self._ref_from_url(url)
            if ref is None:
                continue
            sent = await self._send_by_ref(ref, stream_id)
            self.ctx.logger.info("已解析音乐链接: %s %s", ref.platform, ref.song_id)
            # 只处理第一个命中的音乐链接
            return {"action": "abort" if sent else "continue"}

        return {"action": "continue"}


def create_plugin() -> MusicRequestPlugin:
    """Runner 加载入口。"""
    return MusicRequestPlugin()
