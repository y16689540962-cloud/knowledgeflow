"""``HttpMediaDownloader``（Phase 7 前置：ASR 得先有文件）。

最要紧的一条在 :func:`test_bytes_are_written_exactly_once` —— 曾经因为
``response.aiter_bytes()`` 被调用两次，同一段正文写进文件两遍，
体积翻倍而内容全错。**真实网络流和 Mock 传输的行为还不一样**，所以这条必须测。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from app.errors import MediaDownloadError, MediaTooLargeError, MediaTypeUnsupportedError
from app.media.downloader import (
    HttpMediaDownloader,
    ensure_within_directory,
    sniff_content_type,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
JPEG = b"\xff\xd8\xff\xe0" + b"\x11" * 5000
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x22" * 9000
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x33" * 7000


def client_returning(content: bytes, header_type: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": header_type})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# 魔数嗅探
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (PNG[:64], "image/png"),
        (JPEG[:64], "image/jpeg"),
        (MP4[:64], "video/mp4"),
        (M4A[:64], "audio/mp4"),
        (b"ID3\x04" + b"\x00" * 60, "audio/mpeg"),
        (b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 50, "audio/wav"),
        (b"<!DOCTYPE html><html></html>", None),
    ],
)
def test_sniff_content_type(head: bytes, expected: str | None) -> None:
    assert sniff_content_type(head) == expected


def test_sniff_never_prefers_declared_header() -> None:
    """声明是 png、实际是 html —— 嗅探以**实际字节**为准。"""
    assert sniff_content_type(b"<html>hi</html>") is None


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("payload", "declared", "media_type", "extension"),
    [
        (PNG, "image/png", "image", ".png"),
        (JPEG, "image/jpeg", "image", ".jpg"),
        (MP4, "video/mp4", "video", ".mp4"),
        (M4A, "audio/mp4", "audio", ".m4a"),
    ],
)
async def test_bytes_are_written_exactly_once(
    tmp_path: Path, payload: bytes, declared: str, media_type: str, extension: str
) -> None:
    """写进磁盘的必须是**原样一份** —— 不多不少不少。

    曾经的实现把已被缓冲的字节又报给了一次新的 ``aiter_bytes()``，
    某些传输会从头再喂一遍 → 文件变成两倍长。
    """
    async with client_returning(payload, declared) as client:
        downloader = HttpMediaDownloader(tmp_path, client=client)
        got = await downloader.download("https://cdn.example.com/a/b", media_type=media_type)  # type: ignore[arg-type]

    disk = got.path.read_bytes()
    assert got.byte_size == len(payload)
    assert len(disk) == len(payload)
    assert hashlib.sha256(disk).digest() == hashlib.sha256(payload).digest()
    assert got.path.suffix == extension
    assert got.content_type == declared


async def test_download_is_idempotent(tmp_path: Path) -> None:
    """同一个 URL 重复下载：同一个文件名、同一份内容。"""
    url = "https://cdn.example.com/same.mp4"
    async with client_returning(MP4, "video/mp4") as client:
        downloader = HttpMediaDownloader(tmp_path, client=client)
        first = await downloader.download(url, media_type="video")
        second = await downloader.download(url, media_type="video")

    assert first.path == second.path
    assert list(tmp_path.glob("*.mp4")) == [first.path]


# --------------------------------------------------------------------------- #
# 失败路径 —— 都必须有明确的 error_type，且不留垃圾
# --------------------------------------------------------------------------- #
async def test_size_limit_stops_mid_stream(tmp_path: Path) -> None:
    """超限必须在流式读取中判定，而不是先读满内存再说。"""
    payload = MP4 + b"\x00" * 5000

    async with client_returning(payload, "video/mp4") as client:
        downloader = HttpMediaDownloader(tmp_path, client=client, max_bytes=1024)
        with pytest.raises(MediaTooLargeError) as excinfo:
            await downloader.download("https://cdn.example.com/big.mp4", media_type="video")

    assert excinfo.value.error_type_value == "MEDIA_TOO_LARGE"
    # 失败后不留半截文件，也不留临时文件
    assert list(tmp_path.glob("*.mp4")) == []
    assert list(tmp_path.glob("dl-*")) == []


async def test_no_temp_file_left_after_any_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = HttpMediaDownloader(tmp_path, client=client)
        with pytest.raises(MediaDownloadError) as excinfo:
            await downloader.download("https://cdn.example.com/x.mp4", media_type="video")

    assert excinfo.value.error_type_value == "MEDIA_DOWNLOAD_FAILED"
    assert list(tmp_path.glob("dl-*")) == []


async def test_html_body_is_rejected_not_saved(tmp_path: Path) -> None:
    """抖音不给 Cookie 时返回验证页（HTTP 200 + <html>）—— 不能当媒体存下来。

    这条和 Phase 6 联网实测碰到的情形直接对应。
    """
    body = "<html><body>请完成验证</body></html>".encode("utf-8")
    async with client_returning(body, "text/html; charset=utf-8") as client:
        downloader = HttpMediaDownloader(tmp_path, client=client)
        with pytest.raises(MediaTypeUnsupportedError) as excinfo:
            await downloader.download("https://cdn.example.com/v.mp4", media_type="video")

    assert excinfo.value.error_type_value == "MEDIA_TYPE_UNSUPPORTED"
    assert list(tmp_path.glob("*.mp4")) == []
    assert list(tmp_path.glob("*.bin")) == []


async def test_declared_header_cannot_override_magic_bytes(tmp_path: Path) -> None:
    """Header 声称 image/png，实际是 JPEG —— 按**实际**形态落扩展名。"""
    async with client_returning(JPEG, "image/png") as client:
        downloader = HttpMediaDownloader(tmp_path, client=client)
        got = await downloader.download("https://cdn.example.com/a", media_type="image")

    assert got.content_type == "image/jpeg"
    assert got.path.suffix == ".jpg"


@pytest.mark.parametrize("media_type", ["text", "article", "mixed"])
async def test_non_media_types_are_rejected(tmp_path: Path, media_type: str) -> None:
    async with client_returning(PNG, "image/png") as client:
        downloader = HttpMediaDownloader(tmp_path, client=client)
        with pytest.raises(MediaTypeUnsupportedError):
            await downloader.download("https://cdn.example.com/a", media_type=media_type)  # type: ignore[arg-type]


async def test_timeout_is_reported(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = HttpMediaDownloader(tmp_path, client=client, timeout_seconds=1)
        with pytest.raises(MediaDownloadError) as excinfo:
            await downloader.download("https://cdn.example.com/slow.mp4", media_type="video")

    assert "超时" in excinfo.value.message


async def test_invalid_url_is_rejected(tmp_path: Path) -> None:
    async with client_returning(PNG, "image/png") as client:
        downloader = HttpMediaDownloader(tmp_path, client=client)
        with pytest.raises(MediaDownloadError):
            await downloader.download("not-a-url", media_type="image")


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #
def test_ensure_within_directory_allows_children(tmp_path: Path) -> None:
    assert ensure_within_directory(tmp_path, tmp_path / "a" / "b.png") == (tmp_path / "a" / "b.png").resolve()


def test_ensure_within_directory_blocks_siblings(tmp_path: Path) -> None:
    with pytest.raises(MediaDownloadError):
        ensure_within_directory(tmp_path / "base", tmp_path / "outside.png")


async def test_downloaded_file_stays_inside_download_dir(tmp_path: Path) -> None:
    async with client_returning(PNG, "image/png") as client:
        downloader = HttpMediaDownloader(tmp_path / "assets", client=client)
        got = await downloader.download("https://cdn.example.com/a.png", media_type="image")

    assert got.path.parent.resolve() == (tmp_path / "assets").resolve()
