"""audio_cache 的后缀推断、格式校验与清理覆盖测试。

这组用例锚定的是「文件形态」带来的新约束：缓存不再只收 MP3。
语音形态必须仍是 `.mp3`（QQ 语音转码对格式敏感），文件形态则要按真实
后缀落盘——用户下载到的文件名不能撒谎。清理如果只 glob `*.mp3`，
容量闸与过期清理会同时失效，磁盘会静默涨满。
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from audio_cache import (
    AUDIO_SUFFIXES,
    MusicAudioCache,
    AudioCacheError,
    looks_like_audio,
    suffix_from_url,
)


def make_cache(tmp_path: pathlib.Path, napcat_dir: str = "/app/music_cache") -> MusicAudioCache:
    return MusicAudioCache(
        str(tmp_path / "cache"),
        napcat_dir,
        max_size_bytes=1024 * 1024,
        expire_seconds=60,
        max_file_size_bytes=1024 * 1024,
        download_timeout_seconds=5,
    )


# ---------- 后缀推断 ----------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.com/F000abc.flac", ".flac"),
        ("https://a.com/C400abc.m4a?fromtag=1", ".m4a"),
        ("https://a.com/M500abc.mp3", ".mp3"),
        ("https://a.com/song", ".mp3"),
        ("", ".mp3"),
        ("not-a-url", ".mp3"),
    ],
)
def test_suffix_from_url(url: str, expected: str) -> None:
    assert suffix_from_url(url) == expected


def test_suffix_from_url_rejects_unknown_extension() -> None:
    """白名单外的后缀（如 .exe / .html）必须退回默认值，不能原样采纳。"""
    assert suffix_from_url("https://a.com/x.exe") == ".mp3"
    assert suffix_from_url("https://a.com/error.html") == ".mp3"


# ---------- 文件头校验 ----------


def test_looks_like_audio_accepts_flac_and_m4a() -> None:
    assert looks_like_audio(b"fLaC\x00\x00", ".flac") is True
    # m4a 的 ftyp 魔数在偏移 4，不是 0
    assert looks_like_audio(b"\x00\x00\x00\x20ftypM4A ", ".m4a") is True


@pytest.mark.parametrize(
    ("head", "suffix"),
    [
        (b"<html><b", ".flac"),
        (b"fLaC\x00\x00", ".m4a"),
        (b"\x00\x00\x00\x20ftypM4A ", ".flac"),
    ],
)
def test_looks_like_audio_rejects_mismatch(head: bytes, suffix: str) -> None:
    """后缀与实际内容不符必须拒绝，否则文件名会撒谎。"""
    assert looks_like_audio(head, suffix) is False


def test_looks_like_audio_empty_suffix_uses_mp3_rule() -> None:
    assert looks_like_audio(b"ID3\x03", "") is True
    assert looks_like_audio(b"<html>", "") is False


# ---------- 多后缀落盘 ----------



def test_get_or_download_uses_real_suffix(tmp_path: pathlib.Path) -> None:
    """URL 是 .flac 就必须落成 .flac，而不是改名成 .mp3。"""
    cache = make_cache(tmp_path)
    requested: list[str] = []

    class _Resp:
        status_code = 200

        async def aiter_bytes(self, chunk: int):
            yield b"fLaC" + b"\x00" * 116

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    class _Stream:
        def stream(self, method, url):
            requested.append(url)
            return _Ctx()

    cache._client = _Stream()

    async def run():
        try:
            return await cache.get_or_download("163", "42", "https://x.com/F00042.flac")
        finally:
            await cache.close()

    path = asyncio.run(run())
    assert path.suffix == ".flac", f"后缀被改写: {path}"
    assert path.is_file() and path.stat().st_size > 0
    assert requested == ["https://x.com/F00042.flac"], f"请求的直链不对: {requested}"


def test_get_or_download_rejects_html_for_flac(tmp_path: pathlib.Path) -> None:
    """错误页冒充 .flac 必须被拒——否则用户下载到的是一堆 HTML。"""
    cache = make_cache(tmp_path)

    class _Resp:
        status_code = 200

        async def aiter_bytes(self, chunk: int):
            yield b"<!DOCTYPE html><html>copyright limited</html>"

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    class _Stream:
        def stream(self, method, url):
            return _Ctx()

    cache._client = _Stream()

    async def run():
        try:
            await cache.get_or_download("163", "42", "https://x.com/F00042.flac")
        finally:
            await cache.close()

    with pytest.raises(AudioCacheError):
        asyncio.run(run())


def test_tmp_files_do_not_pollute_audio_listing(tmp_path: pathlib.Path) -> None:
    """`.tmp` 残留不属于音频，清理要单独处理，不能靠 `*.mp3` 兜底。"""
    cache = make_cache(tmp_path)
    root = pathlib.Path(cache._storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "163_1.flac").write_bytes(b"fLaC" + b"\x00" * 20)
    orphan = root / "163_2.mp3.tmp"
    orphan.write_bytes(b"\xff\xfb" + b"\x00" * 20)

    audio = [item.name for item in cache._iter_cached_audio()]
    assert "163_1.flac" in audio
    assert "163_2.mp3.tmp" not in audio
    assert list(cache._iter_orphan_temps()) == [orphan]


def test_cleanup_covers_non_mp3_suffixes(tmp_path: pathlib.Path) -> None:
    """过期清理必须覆盖 .flac/.m4a，否则文件形态会让磁盘静默涨满。"""
    cache = make_cache(tmp_path)
    root = pathlib.Path(cache._storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    stale = root / "163_1.flac"
    stale.write_bytes(b"fLaC" + b"\x00" * 20)
    stale.touch()  # 刚创建，不应被清

    (root / "163_2.m4a").write_bytes(b"\x00\x00\x00\x20ftyp" + b"\x00" * 16)
    # 把 mtime 拨到过期线之外
    import os
    import time

    old = time.time() - cache._expire_seconds - 10
    os.utime(root / "163_2.m4a", (old, old))

    asyncio.run(cache.cleanup())

    assert stale.is_file(), "未过期的 FLAC 不应被清理"
    assert not (root / "163_2.m4a").exists(), "过期的 M4A 没被清理（清理只扫 *.mp3）"


# ---------- 路径映射（文件形态同样要走） ----------


def test_map_napcat_path_for_flac(tmp_path: pathlib.Path) -> None:
    cache = make_cache(tmp_path)
    mapped, ok = cache.napcat_path_checked(tmp_path / "cache" / "163_1.flac")
    assert ok is True
    assert mapped == "/app/music_cache/163_1.flac"
