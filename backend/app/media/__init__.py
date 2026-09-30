"""媒体下载 / 落地：把远端媒体取到本地目录。

这是 Phase 7（ASR / OCR）的**前置** —— ASR 得先有文件才能转写。
它不是 Core Pipeline 的一部分：下载失败可以选择降级（跳过 ASR），
不影响内容本身进 Pipeline。
"""

from __future__ import annotations

from app.media.base import DownloadedMedia, MediaDownloader
from app.media.downloader import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    HttpMediaDownloader,
    ensure_within_directory,
    sniff_content_type,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DownloadedMedia",
    "HttpMediaDownloader",
    "MediaDownloader",
    "ensure_within_directory",
    "sniff_content_type",
]
