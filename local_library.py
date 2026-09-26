"""本地歌曲库 —— 扫描磁盘上的音频文件，供点歌插件直接播放。

设计约束（与插件其它辅助模块一致）：
    - **纯逻辑模块，不许出现 `self.ctx`**：路径解析、扫描、搜索都在这里做完，
      发送由 plugin.py 编排（check_plugin.py 只扫 plugin.py 推导能力名）。
    - 本地文件**不走音频缓存**：文件本来就在盘上，只需要把 MaiBot 侧路径
      映射成 NapCat 进程能看到 的路径（复用 audio_cache.map_to_napcat）。
    - 扫描是同步 IO，但歌曲库规模（几百~几千个文件）远小于事件循环的
      卡顿阈值；扫描结果按目录 mtime + 刷新间隔惰性失效，不搞后台线程。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:  # 包式加载优先：模块挂到 <插件名>.* 命名空间
    from .audio_cache import map_to_napcat
except ImportError:  # 平铺兜底：脚本直跑 / 本地测试
    from audio_cache import map_to_napcat

logger = logging.getLogger(__name__)

# 本地库在 SongInfo.platform 里的标识（_send_song 按它路由到 _send_local）
PLATFORM_LOCAL = "local"

# 认作音频文件的扩展名（大小写不敏感；不含视频容器）
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape", ".wma"}

# 搜索分词：按空白与常见文件名分隔符切开
_RE_TOKEN_SPLIT = re.compile(r"[\s_\-·•()\[\]【】（）,，.．]+")

# 目录失效兜底：配置的刷新间隔非法时用这个（分钟）
_DEFAULT_REFRESH_MINUTES = 10


@dataclass
class LocalTrack:
    """本地库里的一首歌：MaiBot 侧路径 + 从文件名拆出的展示信息。"""

    path: Path          # MaiBot 侧绝对路径（SongInfo.song_id 存它的字符串形式）
    name: str           # 文件名去扩展名，作为歌名展示
    suffix: str         # 扩展名（含点），发文件时保留原始格式
    size_bytes: int = 0

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    def display(self) -> str:
        """一行描述（歌名 + 大小），与 SongInfo.display 风格对齐。"""
        return f"{self.name}（{self.size_mb:.1f}MB）"


class LocalSongLibrary:
    """本地歌曲库：扫描、搜索、NapCat 路径映射。

    用法：
        lib = LocalSongLibrary("D:/music", napcat_dir="/music")
        tracks = lib.search("夜曲")          # 惰性扫描，目录变了会自动重扫
        napcat_path, ok = lib.napcat_path_checked(tracks[0].path)
    """

    def __init__(
        self,
        music_dir: str,
        napcat_dir: str = "",
        *,
        refresh_minutes: int = _DEFAULT_REFRESH_MINUTES,
    ) -> None:
        self._music_dir_raw = (music_dir or "").strip()
        self._napcat_dir_raw = (napcat_dir or "").strip()
        try:
            self._refresh_seconds = max(int(refresh_minutes), 0) * 60
        except (TypeError, ValueError):
            self._refresh_seconds = _DEFAULT_REFRESH_MINUTES * 60
        self._tracks: List[LocalTrack] = []
        self._root: Optional[Path] = None
        self._scanned_at: float = 0.0
        self._scan_error: str = ""

    # ---------- 扫描 ----------

    @property
    def root(self) -> Optional[Path]:
        """解析后的歌曲库根目录；未配置/配置非法时为 None。"""
        if not self._music_dir_raw:
            return None
        if self._root is None:
            try:
                self._root = Path(self._music_dir_raw).resolve()
            except OSError:
                logger.warning("本地歌曲库目录无法解析: %r", self._music_dir_raw)
                return None
        return self._root

    @property
    def scan_error(self) -> str:
        """最近一次扫描的失败原因（空串表示正常）。"""
        return self._scan_error

    def _needs_rescan(self) -> bool:
        """是否需要（重）扫描：没扫过 / 超过刷新间隔 / 目录 mtime 变了。"""
        if self._root is None or not self._tracks and not self._scan_error:
            return True
        if self._refresh_seconds <= 0:
            return False
        if time.monotonic() - self._scanned_at < self._refresh_seconds:
            return False
        try:
            return self.root.stat().st_mtime > self._scanned_at
        except OSError:
            return True

    def rescan(self) -> int:
        """强制重扫，返回曲目数。失败时保留旧索引并记录 scan_error。"""
        root = self.root
        if root is None:
            self._scan_error = "未配置歌曲库目录"
            return 0
        if not root.exists():
            # rglob 对不存在的目录只会静默返回空，必须显式检查，
            # 否则目录被暂时卸载/断连时会把索引清成 0
            self._scan_error = "目录不存在或不可访问"
            logger.warning("本地歌曲库目录不存在: %s", root)
            return len(self._tracks)
        found: List[LocalTrack] = []
        try:
            for entry in sorted(root.rglob("*")):
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                try:
                    found.append(
                        LocalTrack(
                            path=entry.resolve(),
                            name=entry.stem,
                            suffix=entry.suffix.lower(),
                            size_bytes=entry.stat().st_size,
                        )
                    )
                except OSError:
                    continue  # 文件刚好被删/被占用，跳过即可
        except OSError as exc:
            self._scan_error = str(exc)
            logger.warning("扫描本地歌曲库失败: %s: %s", root, exc)
            return len(self._tracks)

        self._tracks = found
        self._scanned_at = time.monotonic()
        self._scan_error = ""
        return len(found)

    def ensure_index(self) -> int:
        """按需刷新索引并返回曲目数（惰性入口，搜索/状态命令都走这里）。"""
        if self._needs_rescan():
            self.rescan()
        return len(self._tracks)

    # ---------- 搜索 ----------

    @staticmethod
    def _score(name_lower: str, keyword_lower: str, tokens: List[str]) -> int:
        """给一个文件名打相关度分（越高越相关）。

        - 整串命中 > 全部 token 命中 > 部分 token 命中；
        - token 越长权重越高（「夜曲」比「的」更说明意图）；
        - 同分时文件名更短者优先（少一层「Live/Remix」修饰）。
        """
        score = 0
        if keyword_lower and keyword_lower in name_lower:
            score += 100
        matched = 0
        for token in tokens:
            if len(token) < 1 or token not in name_lower:
                continue
            matched += 1
            score += 10 + min(len(token), 8)
        if tokens and matched == len(tokens):
            score += 30
        return score

    def search(self, keyword: str, limit: Optional[int] = None) -> List[LocalTrack]:
        """按关键词搜本地库，相关度降序。

        Args:
            keyword: 歌名或「歌名 歌手」串（「歌手 歌名」同理，分词不区分顺序）。
            limit: 返回上限；None 时不限（调用方一般传 search_limit）。

        Returns:
            得分 > 0 的曲目，降序；同分按文件名长度升序。
        """
        self.ensure_index()
        text = (keyword or "").strip().lower()
        if not text:
            return []
        tokens = [t for t in _RE_TOKEN_SPLIT.split(text) if t]
        if not tokens:
            tokens = [text]

        scored: List[Tuple[int, LocalTrack]] = []
        for track in self._tracks:
            name_lower = track.name.lower()
            score = self._score(name_lower, text, tokens)
            if score > 0:
                scored.append((score, track))

        scored.sort(key=lambda pair: (-pair[0], len(pair[1].name), pair[1].name.lower()))
        tracks = [track for _score, track in scored]
        if limit is not None and limit > 0:
            tracks = tracks[:limit]
        return tracks

    # ---------- 状态与路径映射 ----------

    def stats(self) -> Tuple[int, float]:
        """`(曲目数, 总大小 MB)`。会触发惰性刷新。"""
        self.ensure_index()
        total = sum(track.size_bytes for track in self._tracks)
        return len(self._tracks), total / (1024 * 1024)

    def napcat_path_checked(self, path: Path) -> Tuple[str, bool]:
        """把 MaiBot 侧路径映射成 NapCat 进程可见路径。

        Returns:
            `(NapCat 侧路径, 是否映射成功)`。失败时交给 NapCat 的是宿主机
            路径，多半打不开——调用方应显式告警。
        """
        root = self.root
        if root is None:
            return str(path), False
        return map_to_napcat(str(root), self._napcat_dir_raw, path)

    def summary_lines(self) -> List[str]:
        """状态命令用的几行摘要。"""
        count, total_mb = self.stats()
        lines = [
            f"本地歌曲库: {'启用' if count or not self._scan_error else '异常'}，{count} 首 / {total_mb:.0f} MB",
            f"  歌曲目录: {self.root}",
        ]
        if self._napcat_dir_raw:
            lines.append(f"  NapCat 目录: {self._napcat_dir_raw}")
        if self._scan_error:
            lines.append(f"  ⚠️ 最近扫描失败: {self._scan_error}")
        return lines


def keyword_tokens(keyword: str) -> List[str]:
    """把搜索关键词切成 token（测试与插件共用同一套分词口径）。"""
    return [t for t in _RE_TOKEN_SPLIT.split((keyword or "").strip().lower()) if t]


def track_to_song_info_fields(track: LocalTrack) -> Dict[str, str]:
    """LocalTrack → SongInfo 构造字段（song_id 存 MaiBot 侧绝对路径）。"""
    return {
        "song_id": str(track.path),
        "name": track.name,
        "artists": "",
        "album": "",
        "platform": PLATFORM_LOCAL,
    }
