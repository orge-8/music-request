"""本地音乐音频缓存（voice 模式 + voice_source=local 时使用）。

为什么要本地缓存：NapCat 发语音、发文件都需要能读到文件。让 MaiBot 先把音频落到
磁盘再给 NapCat 一个路径，比让 NapCat 自己去拉远程 URL 稳定得多
（远程 URL 常带防盗链、有时效）。
代价是 MaiBot 与 NapCat 必须能访问同一份文件，所以有两个目录配置：
`cache_storage_dir`（MaiBot 侧写入）与 `cache_napcat_dir`（NapCat 侧读取）。

三条硬规则：
    1. **按后缀校验文件头**。语音只收 MP3（FLAC/M4A 改名成 `.mp3` 会静默损坏）；
       文件形态允许 FLAC/M4A 等，但要按各自的后缀校验魔数，
       不能把 HTML 错误页当成音频落盘。
    2. **原子落盘**：先写 `.tmp` 再 `os.replace`，避免 NapCat 读到半个文件。
    3. **容量与过期双闸**：超出容量立刻按最久未用淘汰；定时清掉长期未用的。

另配一个**探针**（`write_cache_probe`）：写一个带随机内容的文件并给出它在 NapCat
侧的路径与内容摘要。判定「两个目录是不是同一份文件」只能靠内容比对——
「两边都能 ls 到同名文件」也可能是两份不同的数据。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("plugin.music-request.cache")

# 下载音频时的请求头：多数音乐 CDN 校验 UA/Referer
_DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
}

# 读取大小上限的块大小
_CHUNK_SIZE = 64 * 1024

# 探针文件名前缀（以 . 开头：不会被音频清理扫到）
PROBE_PREFIX = ".probe_"

#: 允许落盘的音频后缀白名单
AUDIO_SUFFIXES = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac")

#: 各后缀的文件头魔数：{后缀: ((偏移, 魔数), ...)}（mp3 另有专门判定）
_AUDIO_MAGIC: dict[str, tuple[tuple[int, bytes], ...]] = {
    ".flac": ((0, b"fLaC"),),
    ".m4a": ((4, b"ftyp"),),
    ".wav": ((0, b"RIFF"),),
    ".ogg": ((0, b"OggS"),),
    ".aac": ((0, b"\xff\xf1"),),
}


class AudioCacheError(RuntimeError):
    """音频下载或格式校验失败。"""


@dataclass
class CacheProbe:
    """缓存目录探针：用于验证 MaiBot 与 NapCat 看到的是同一份文件。"""

    filename: str
    digest: str          # 内容 MD5 的前 8 位，与 `md5sum` / `certutil` 输出直接可比
    storage_path: str    # MaiBot 侧完整路径
    napcat_path: str     # NapCat 侧完整路径
    mapped: bool         # 路径映射是否成功（False 说明两目录前缀对不上）


def map_to_napcat(storage_dir: str, napcat_dir: str, path: Path) -> tuple[str, bool]:
    """把 MaiBot 侧路径映射成 NapCat 侧路径。

    映射方式：取相对 `storage_dir` 的部分，拼到 `napcat_dir` 下。
    `napcat_dir` 留空时视同与 `storage_dir` 相同。

    Args:
        storage_dir: MaiBot 侧缓存根目录。
        napcat_dir: NapCat 侧缓存根目录。
        path: 待映射的路径。

    Returns:
        `(NapCat 侧路径, 是否映射成功)`。`path` 不在 `storage_dir` 下时返回原路径与
        False —— 此时交给 NapCat 的是宿主机路径，多半打不开，调用方应显式告警。
    """
    try:
        relative = Path(path).resolve().relative_to(Path(storage_dir).resolve())
    except (ValueError, OSError):
        return str(path), False

    target_root = (napcat_dir or storage_dir).strip()
    if target_root.startswith("/"):
        # 容器内路径：用 POSIX 拼接，避免在 Windows 宿主上拼出反斜杠混排的怪路径
        return posixpath.join(target_root.rstrip("/"), relative.as_posix()), True
    return str(Path(target_root) / relative), True


def write_cache_probe(storage_dir: str, napcat_dir: str) -> CacheProbe:
    """在缓存目录写入一个带随机内容的探针文件。

    用途：让用户在 NapCat 侧读同一个文件并比对摘要，从而判定两个配置目录是否
    指向「同一份文件」——这是 `voice_source=local` 唯一真正的前提条件。

    Args:
        storage_dir: MaiBot 侧缓存根目录（不存在会被创建）。
        napcat_dir: NapCat 侧缓存根目录。

    Returns:
        CacheProbe。

    Raises:
        OSError: 目录不可创建或不可写。
    """
    root = Path(storage_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    token = os.urandom(4).hex()
    filename = f"{PROBE_PREFIX}{token}.txt"
    content = f"music-request cache probe {token} {int(time.time())}".encode("ascii")
    probe_path = root / filename
    probe_path.write_bytes(content)

    napcat_path, mapped = map_to_napcat(storage_dir, napcat_dir, probe_path)
    return CacheProbe(
        filename=filename,
        digest=hashlib.md5(content).hexdigest()[:8],
        storage_path=str(probe_path),
        napcat_path=napcat_path,
        mapped=mapped,
    )


def looks_like_mp3(head: bytes) -> bool:
    """判断文件头是否是 MP3。

    接受两种形态：
        - 带 ID3v2 标签：文件以 `ID3` 开头
        - 裸 MPEG 音频帧：`0xFF` 后跟高 3 位为 1（0xE0 掩码）

    Args:
        head: 文件前若干字节。

    Returns:
        像 MP3 返回 True。
    """
    if len(head) >= 3 and head[:3] == b"ID3":
        return True
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


def suffix_from_url(url: str, default: str = ".mp3") -> str:
    """从 URL 路径推断音频后缀（只认白名单内的后缀）。

    语音形态请求的是 `mp3_only`，拿到的必然是 `.mp3`；文件形态可能拿到
    `.flac` / `.m4a`，必须按真实后缀落盘，否则用户下载到的文件名会撒谎。

    Args:
        url: 音频直链。
        default: 推断不出来时的兜底后缀。

    Returns:
        白名单内的后缀（含点），如 `.flac`。
    """
    path = (urlparse(str(url or "")).path or "").lower()
    for suffix in AUDIO_SUFFIXES:
        if path.endswith(suffix):
            return suffix
    return default if default in AUDIO_SUFFIXES else ".mp3"


def looks_like_audio(head: bytes, suffix: str) -> bool:
    """按后缀校验文件头，拦住 HTML 错误页与被改名的异构文件。

    只做「像不像」的粗判，不做完整容器解析——目的是防止把错误响应当音频落盘
    再发给 NapCat，而不是做编解码器识别。

    Args:
        head: 文件前若干字节。
        suffix: 期望的后缀（如 `.flac`）。

    Returns:
        文件头与后缀相符返回 True。
    """
    normalized = (suffix or "").lower()
    if normalized in ("", ".mp3"):
        return looks_like_mp3(head)
    for offset, magic in _AUDIO_MAGIC.get(normalized, ()):
        if head[offset:offset + len(magic)] == magic:
            return True
    return False


class MusicAudioCache:
    """带 LRU 淘汰与过期清理的本地音乐缓存。"""

    def __init__(
        self,
        storage_dir: str,
        napcat_dir: str,
        *,
        max_size_bytes: int,
        expire_seconds: int,
        max_file_size_bytes: int,
        download_timeout_seconds: int,
    ) -> None:
        self._storage_dir = Path(storage_dir).expanduser()
        self._napcat_dir = (napcat_dir or storage_dir).strip()
        self._max_size = max(int(max_size_bytes), 1)
        self._expire_seconds = max(int(expire_seconds), 1)
        self._max_file_size = max(int(max_file_size_bytes), 1)
        self._timeout = max(int(download_timeout_seconds), 1)

        self._client: httpx.AsyncClient | None = None
        # 同一首歌并发点播时只下载一次
        self._locks: dict[str, asyncio.Lock] = {}
        # 正在被发送流程引用的文件：淘汰时跳过，避免删掉正要发的文件
        self._in_use: dict[Path, int] = {}

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        """建目录、扫描存量、按容量与过期做一次清理。"""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(
            headers=_DOWNLOAD_HEADERS,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
        )
        await self.cleanup()

    async def close(self) -> None:
        """关闭下载客户端。"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug("关闭音频下载客户端失败", exc_info=True)
            self._client = None

    # ---------- 路径映射 ----------

    @staticmethod
    def _safe_name(platform: str, song_id: str, suffix: str = ".mp3") -> str:
        """把平台+歌曲 ID 转成安全的文件名（挡住路径穿越）。

        后缀参与命名：文件形态可能落 `.flac` / `.m4a`，不能一律写成 `.mp3`。
        """
        raw = f"{platform}_{song_id}"
        safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in raw)
        ext = suffix if suffix in AUDIO_SUFFIXES else ".mp3"
        return (safe or "unknown")[:120] + ext

    def napcat_path_checked(self, path: Path) -> tuple[str, bool]:
        """把 MaiBot 侧路径映射成 NapCat 能看到的路径，并报告是否映射成功。

        两个目录配置成一样时就是原路径；Docker 场景配成容器内路径。

        Returns:
            `(NapCat 侧路径, 是否映射成功)`。返回 False 时交给 NapCat 的是宿主机
            路径，多半打不开——调用方应显式告警，而不是静默发出去。
        """
        return map_to_napcat(str(self._storage_dir), self._napcat_dir, path)

    def napcat_path(self, path: Path) -> str:
        """`napcat_path_checked` 的简化版（丢弃映射状态）。"""
        return self.napcat_path_checked(path)[0]

    # ---------- 取用 ----------

    async def get_or_download(
        self, platform: str, song_id: str, url: str, suffix: str = ""
    ) -> Path:
        """取缓存文件；未命中则下载。

        Args:
            platform: 平台标识。
            song_id: 歌曲 ID。
            url: 音频直链。
            suffix: 期望的后缀；留空则按 URL 推断。
                语音形态走 `mp3_only`，实际总是 `.mp3`；文件形态可能是 `.flac` / `.m4a`。

        Returns:
            缓存文件路径（MaiBot 侧视角）。

        Raises:
            AudioCacheError: 下载失败、超大小上限或文件头与后缀不符。
        """
        if self._client is None:
            raise AudioCacheError("音频缓存尚未初始化")

        ext = suffix if suffix in AUDIO_SUFFIXES else suffix_from_url(url)
        path = self._storage_dir / self._safe_name(platform, song_id, ext)
        lock = self._locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            if path.is_file() and path.stat().st_size > 0:
                self._touch(path)
                return path
            await self._download(path, url, ext)

        return path

    def retain(self, path: Path) -> None:
        """标记文件正在被使用，淘汰时跳过。"""
        self._in_use[path] = self._in_use.get(path, 0) + 1

    def release(self, path: Path) -> None:
        """解除使用标记。"""
        count = self._in_use.get(path, 0)
        if count <= 1:
            self._in_use.pop(path, None)
        else:
            self._in_use[path] = count - 1
        self._touch(path)

    async def _download(self, path: Path, url: str, suffix: str) -> None:
        """流式下载并原子落盘，逐块校验大小上限与文件头。"""
        assert self._client is not None
        tmp = path.with_suffix(path.suffix + ".tmp")
        # 目录可能被用户删掉（或从未初始化过）：这里必须自建，否则下载必炸
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise AudioCacheError(f"下载音频失败: HTTP {resp.status_code}")
                total = 0
                head = b""
                with open(tmp, "wb") as fh:
                    async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                        if not chunk:
                            continue
                        if not head:
                            head = chunk[:16]
                            if not looks_like_audio(head, suffix):
                                raise AudioCacheError(
                                    f"下载内容与后缀 {suffix} 不符（可能是错误页或别的格式）"
                                )
                        total += len(chunk)
                        if total > self._max_file_size:
                            raise AudioCacheError(
                                f"音频超过单文件大小上限 {self._max_file_size // (1024 * 1024)}MB"
                            )
                        fh.write(chunk)

            if total <= 0:
                raise AudioCacheError("下载到空文件")
            os.replace(tmp, path)
            logger.info("音频已缓存: %s (%d KB)", path.name, total // 1024)
            await self._enforce_capacity()
        except AudioCacheError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise AudioCacheError(f"下载音频异常: {type(exc).__name__}: {exc}") from exc

    # ---------- 清理 ----------

    @staticmethod
    def _touch(path: Path) -> None:
        """刷新访问时间（用 mtime 兼作 LRU 依据）。"""
        try:
            os.utime(path, None)
        except OSError:
            pass

    def _iter_cached_audio(self):
        """遍历缓存目录里的音频文件（覆盖白名单内全部后缀）。

        不能只 glob `*.mp3`：文件形态会落 `.flac` / `.m4a`，
        漏掉它们会让容量闸与过期清理同时失效。
        """
        if not self._storage_dir.is_dir():
            return
        for suffix in AUDIO_SUFFIXES:
            yield from self._storage_dir.glob(f"*{suffix}")

    def _iter_orphan_temps(self):
        """遍历残留的 `.tmp`（下载中途进程被杀会留下）。"""
        if not self._storage_dir.is_dir():
            return
        yield from self._storage_dir.glob("*.tmp")

    async def cleanup(self) -> None:
        """清掉过期文件与残留临时文件，再按容量淘汰。"""
        if not self._storage_dir.is_dir():
            return
        now = time.time()
        # 残留 .tmp 没有价值，且永不参与容量统计，直接按过期阈值清掉
        for orphan in self._iter_orphan_temps():
            try:
                if now - orphan.stat().st_mtime > max(self._expire_seconds, 3600):
                    orphan.unlink(missing_ok=True)
            except OSError:
                continue
        for item in self._iter_cached_audio():
            if item in self._in_use:
                continue
            try:
                if now - item.stat().st_mtime > self._expire_seconds:
                    item.unlink(missing_ok=True)
            except OSError:
                continue
        await self._enforce_capacity()

    async def _enforce_capacity(self) -> None:
        """总容量超限时，按最久未访问顺序删到限内。"""
        if not self._storage_dir.is_dir():
            return
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for item in self._iter_cached_audio():
            try:
                stat = item.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, item))
            total += stat.st_size

        if total <= self._max_size:
            return

        entries.sort(key=lambda row: row[0])  # 最久未访问的排前面
        for _mtime, size, item in entries:
            if total <= self._max_size:
                break
            if item in self._in_use:
                continue
            try:
                item.unlink(missing_ok=True)
                total -= size
            except OSError:
                continue
        logger.info("音乐缓存已按容量上限淘汰，当前约 %d MB", total // (1024 * 1024))
