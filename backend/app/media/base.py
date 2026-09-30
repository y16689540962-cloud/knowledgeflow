"""``MediaDownloader`` 抽象与下载结果值对象。

为什么单独一层：

* **ASR 得先有文件。** Phase 6 按第二十六节原文只做「URL → metadata」，
  所以 Phase 7 要转写就必须先把媒体取到本地。这一步是 Phase 7 的**前置**，
  但它自己不属于 Core Pipeline。
* 下载和后续的 ASR/OCR 解耦：下载器只负责「把字节安全地落到磁盘」，
  不知道什么 Whisper。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from typing import Protocol

from app.schemas.enums import MediaType


@dataclass(frozen=True)
class DownloadedMedia:
    """一次下载的结果。

    ``path`` 是**绝对路径**，且一定在配置的下载目录之内（防止路径穿越）。
    """

    path: Path
    media_type: MediaType
    byte_size: int
    content_type: str
    source_url: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "content_type": self.content_type,
            "source_url": self.source_url,
        }


class MediaDownloader(Protocol):
    """把远端媒体取到本地目录。

    失败必须抛 :class:`app.errors.KnowledgeFlowError` 子类并带明确 ``error_type``。
    允许降级：调用方可以选择「下载失败就跳过 ASR」，而不是整条内容失败。
    """

    async def download(self, url: str, *, media_type: MediaType) -> DownloadedMedia:
        ...


__all__ = ["DownloadedMedia", "MediaDownloader"]
