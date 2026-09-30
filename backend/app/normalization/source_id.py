"""``source_id`` 规范与 fallback（定稿文档第五节，强制）。

铁律：

* ``source_id`` **永不为空**。
* ``source_id`` **永不回退成 URL**（短链每次分享可能不同却指向同一视频，不能用作去重依据）。
* 无法解析平台 ID 时：``source_id = f"hash:{content_hash[:16]}"``，
  ``needs_manual_review = True``，``error_type = "SOURCE_ID_RESOLUTION_FAILED"``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.errors import ErrorType
from app.normalization.text import normalize_text

HASH_FALLBACK_PREFIX: Final[str] = "hash:"
HASH_FALLBACK_LENGTH: Final[int] = 16


@dataclass(frozen=True)
class SourceIdResolution:
    source_id: str
    needs_manual_review: bool
    error_type: str | None

    @property
    def is_fallback(self) -> bool:
        return self.error_type is not None


def looks_like_url(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(("http://", "https://", "www."))


def fallback_source_id(content_hash: str) -> str:
    """``hash:`` + content_hash 前 16 位。"""
    digest = normalize_text(content_hash)
    if len(digest) < HASH_FALLBACK_LENGTH:
        raise ValueError("content_hash 长度不足以生成 fallback source_id")
    return f"{HASH_FALLBACK_PREFIX}{digest[:HASH_FALLBACK_LENGTH]}"


def resolve_source_id(raw_source_id: str | None, *, content_hash: str) -> SourceIdResolution:
    """决定最终的 ``source_id``。

    空值、纯空白、以及「被误塞进来的 URL」都视为解析失败，一律走 ``hash:`` fallback。
    """
    candidate = normalize_text(raw_source_id)

    if not candidate or looks_like_url(candidate):
        return SourceIdResolution(
            source_id=fallback_source_id(content_hash),
            needs_manual_review=True,
            error_type=ErrorType.SOURCE_ID_RESOLUTION_FAILED.value,
        )

    return SourceIdResolution(source_id=candidate, needs_manual_review=False, error_type=None)


__all__ = [
    "HASH_FALLBACK_PREFIX",
    "HASH_FALLBACK_LENGTH",
    "SourceIdResolution",
    "looks_like_url",
    "fallback_source_id",
    "resolve_source_id",
]
