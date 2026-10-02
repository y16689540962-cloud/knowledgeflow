"""``LocalMediaSource`` 与 ``/api/ingest/media``：本地媒体文件 → 笔记。

这是「Phase 7 能力只存在于 demo 脚本」这个缺口的**服务侧补口**：
视频 / 音频 → ASR，图片 → OCR，装了引擎才跑、没装安静降级。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.errors import ErrorType
from app.ingestion.media_file import (
    LocalMediaPayload,
    LocalMediaSource,
    MediaTypeUnsupportedError,
    sniff_media_type,
)


def make_file(tmp_path: Path, name: str, head: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(head)
    return path


# --------------------------------------------------------------------------- #
# 类型嗅探（魔数，不信扩展名）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image"),  # PNG
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image"),  # JPEG
        (b"ID3" + b"\x00" * 8, "audio"),  # MP3 (ID3)
        (b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 8, "audio"),  # WAV
        (b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8, "video"),  # MP4
    ],
)
def test_sniff_media_type(tmp_path: Path, head: bytes, expected: str) -> None:
    path = make_file(tmp_path, "media.bin", head)  # 扩展名故意骗人
    assert sniff_media_type(path) == expected


def test_sniff_rejects_html_disguised_as_media(tmp_path: Path) -> None:
    """`.mp4` 后缀的验证页 HTML 是真实存在过的坑 —— 必须拒。"""
    path = make_file(tmp_path, "fake.mp4", b"<!DOCTYPE html><html><body>")
    assert sniff_media_type(path) is None


def test_sniff_empty_file(tmp_path: Path) -> None:
    path = make_file(tmp_path, "empty.mp3", b"")
    assert sniff_media_type(path) is None


# --------------------------------------------------------------------------- #
# LocalMediaSource
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_builds_raw_content_with_media_path(tmp_path: Path) -> None:
    path = make_file(tmp_path, "photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    raw = await LocalMediaSource().fetch(
        LocalMediaPayload(
            file_path=str(path),
            title="一张截图",
            author="我",
            raw_text="配文说明。",
        )
    )
    assert raw.source == "media"
    assert raw.source_id == ""  # normalize 阶段填 hash: fallback（第五节）
    assert raw.media_type == "image"
    assert raw.media_path == str(path)
    assert raw.title == "一张截图"
    assert raw.raw_text == "配文说明。"


@pytest.mark.asyncio
async def test_fetch_expands_home_dir(tmp_path: Path, monkeypatch) -> None:
    """``~`` 开头的路径要展开 —— 这是用户最自然的写法。"""
    (tmp_path / "clip.mp4").write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8)
    # expanduser 认的环境变量**按平台不同**：POSIX 看 HOME，Windows 看 USERPROFILE。
    # 只设 HOME 的话，Windows 上会展开到真实用户目录 → 找不到 clip.mp4 → 假失败。
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path))

    raw = await LocalMediaSource().fetch(LocalMediaPayload(file_path="~/clip.mp4"))
    assert raw.media_type == "video"
    assert raw.media_path == str(tmp_path / "clip.mp4")


@pytest.mark.asyncio
async def test_missing_file_is_media_file_not_found(tmp_path: Path) -> None:
    from app.ingestion.media_file import MediaFileNotFoundError

    with pytest.raises(MediaFileNotFoundError) as excinfo:
        await LocalMediaSource().fetch(
            LocalMediaPayload(file_path=str(tmp_path / "nope.mp4"))
        )
    assert excinfo.value.error_type_value == ErrorType.MEDIA_FILE_NOT_FOUND.value


@pytest.mark.asyncio
async def test_directory_is_rejected(tmp_path: Path) -> None:
    from app.ingestion.media_file import MediaFileNotFoundError

    with pytest.raises(MediaFileNotFoundError):
        await LocalMediaSource().fetch(LocalMediaPayload(file_path=str(tmp_path)))


@pytest.mark.asyncio
async def test_unknown_format_is_media_type_unsupported(tmp_path: Path) -> None:
    path = make_file(tmp_path, "mystery.mp4", b"RANDOM BYTES HERE")
    with pytest.raises(MediaTypeUnsupportedError) as excinfo:
        await LocalMediaSource().fetch(LocalMediaPayload(file_path=str(path)))
    assert excinfo.value.error_type_value == ErrorType.MEDIA_TYPE_UNSUPPORTED.value
