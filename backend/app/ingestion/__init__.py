"""Ingestion 层（B 线）：外部输入 → ``RawContent``。

- ``ManualPasteSource``：零依赖降级入口（定稿第二十三条，强制）
- ``DouyinSource``：抖音 URL → ``aweme_id`` → 元数据（定稿第六 / 二十二节）
- ``LocalMediaSource``：本地媒体文件 → 带 ``media_path`` 的 ``RawContent``
  （补上「Phase 7 的能力只能被 demo 脚本调到」这个缺口）
"""

from app.ingestion.base import ContentSource, PayloadT
from app.ingestion.douyin import DouyinMetadata, DouyinSource
from app.ingestion.manual import ManualPastePayload, ManualPasteSource
from app.ingestion.media_file import (
    LocalMediaPayload,
    LocalMediaSource,
    MediaFileNotFoundError,
    MediaTypeUnsupportedError,
)
from app.ingestion.redirects import (
    AwemeIdNotFoundError,
    CookieRequiredError,
    NetworkTimeoutError,
    ParserUnsupportedError,
    RequestBlockedError,
    ResolvedURL,
    ShortLinkResolutionError,
    extract_aweme_id,
    platform_of,
    resolve_short_link,
)

__all__ = [
    "ContentSource",
    "PayloadT",
    "ManualPasteSource",
    "ManualPastePayload",
    "DouyinSource",
    "DouyinMetadata",
    "LocalMediaSource",
    "LocalMediaPayload",
    "MediaFileNotFoundError",
    "MediaTypeUnsupportedError",
    "resolve_short_link",
    "extract_aweme_id",
    "platform_of",
    "ResolvedURL",
    "ShortLinkResolutionError",
    "AwemeIdNotFoundError",
    "RequestBlockedError",
    "CookieRequiredError",
    "ParserUnsupportedError",
    "NetworkTimeoutError",
]
