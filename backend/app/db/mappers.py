"""归一化结果 → 数据库行的映射。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.errors import ErrorType
from app.normalization.service import NormalizedContent
from app.schemas.content import RawContent
from app.utils import new_id, utc_now_iso


@dataclass(frozen=True)
class ContentInsert:
    """``contents`` 表一行的插入描述。"""

    id: str
    source: str
    source_id: str
    content_hash: str
    created_at: str = field(default_factory=utc_now_iso)
    content_hash_version: int = 1
    status: str = "pending"
    source_url: str | None = None
    title: str | None = None
    author: str | None = None
    author_id: str | None = None
    description: str | None = None
    media_type: str | None = None
    raw_text: str | None = None
    transcript: str | None = None
    ocr_text: str | None = None
    needs_manual_review: bool = False
    current_analysis_id: str | None = None
    processed_at: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    @classmethod
    def from_normalized(
        cls,
        normalized: NormalizedContent,
        *,
        content_id: str | None = None,
        status: str = "pending",
    ) -> "ContentInsert":
        raw: RawContent = normalized.raw
        return cls(
            id=content_id or new_id(),
            source=normalized.source,
            source_id=normalized.source_id,
            content_hash=normalized.content_hash,
            content_hash_version=normalized.content_hash_version,
            status=status,
            source_url=raw.source_url or None,
            title=raw.title,
            author=raw.author,
            author_id=raw.author_id,
            description=raw.description,
            media_type=raw.media_type,
            raw_text=raw.raw_text,
            transcript=raw.transcript,
            ocr_text=raw.ocr_text,
            needs_manual_review=normalized.needs_manual_review,
            error_type=normalized.error_type,
        )


@dataclass(frozen=True)
class AnalysisInsert:
    """``analyses`` 表一行的插入描述（永不 UPDATE）。"""

    content_id: str
    structured_json: str
    prompt_version: str
    id: str = field(default_factory=new_id)
    analysis_type: str | None = None
    summary: str | None = None
    model: str | None = None
    chunk_count: int = 1
    created_at: str = field(default_factory=utc_now_iso)


#: 未知错误类型兜底字符串。
UNKNOWN_ERROR_TYPE = ErrorType.UNKNOWN_ERROR.value

__all__ = ["ContentInsert", "AnalysisInsert", "UNKNOWN_ERROR_TYPE"]
