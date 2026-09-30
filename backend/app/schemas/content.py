"""``RawContent`` —— Core Pipeline 的唯一输入形态（定稿文档第十四节）。

**Core Pipeline 只能接收 ``RawContent``，不得直接依赖任何平台 API。**
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.enums import MediaType


class RawContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    source_id: str
    source_url: str
    title: str | None = None
    author: str | None = None
    author_id: str | None = None
    description: str | None = None
    media_type: MediaType
    media_path: str | None = None
    raw_text: str | None = None
    transcript: str | None = None
    ocr_text: str | None = None

    @field_validator("source", mode="after")
    @classmethod
    def _source_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source 不能为空")
        return value.strip()

    @classmethod
    def from_fixture(cls, payload: Mapping[str, Any]) -> "RawContent":
        return cls.model_validate(dict(payload))

    def source_text(self) -> str:
        """``raw_text + transcript + ocr_text`` 的统一拼接（Grounding Check 用）。

        ASR / OCR 为空时按实际可用字段拼接，不插入占位符。
        """
        parts = [self.raw_text, self.transcript, self.ocr_text]
        return "\n".join(p for p in parts if p)

    def hash_inputs(self) -> dict[str, str | None]:
        """``content_hash`` 白名单输入（第七节）。"""
        return {
            "source": self.source,
            "title": self.title,
            "author": self.author,
            "description": self.description,
        }


__all__ = ["RawContent"]
