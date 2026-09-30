"""``ManualPasteSource``：手动粘贴入口（定稿文档第二十三条，**强制**）。

意义：**即使抖音采集彻底失效，产品依然可用。** 这是双轨架构的真正落地。

- 不需要网络、不需要登录态、不需要解析
- ``source = "manual"``，``source_id`` 走 ``hash:`` fallback
- 用户提供的原始链接存入 ``source_url``，但**不参与去重**（``content_hash`` 白名单里没有它）
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from app.ingestion.base import ContentSource
from app.schemas.content import RawContent
from app.schemas.enums import MediaType

SOURCE_NAME = "manual"


class ManualPastePayload(BaseModel):
    """用户粘进来的东西。全部字段可选 —— 粘多少算多少。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    author: str | None = None
    author_id: str | None = None
    description: str | None = None
    source_url: str = ""
    raw_text: str | None = None
    transcript: str | None = None
    ocr_text: str | None = None
    media_type: MediaType = "text"

    @field_validator("source_url", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        """用户粘贴时给个 ``None`` 很常见 —— 规范化成空串。"""
        return "" if value is None else value

    def is_empty(self) -> bool:
        """一个字都没有 —— 这种输入交给 Pipeline 报 ``EMPTY_SOURCE_TEXT``。"""
        return not any(
            (self.raw_text, self.transcript, self.ocr_text, self.title, self.description)
        )


class ManualPasteSource(ContentSource[ManualPastePayload]):
    """与 ``DouyinSource`` 平级的正式 ``ContentSource`` 实现。"""

    @property
    def name(self) -> str:
        return SOURCE_NAME

    async def fetch(self, payload: ManualPastePayload) -> RawContent:
        return RawContent(
            source=SOURCE_NAME,
            # 留空 → normalize 阶段填 ``hash:<content_hash[:16]>``（第五节 fallback）
            source_id="",
            source_url=payload.source_url or "",
            title=payload.title,
            author=payload.author,
            author_id=payload.author_id,
            description=payload.description,
            media_type=payload.media_type,
            media_path=None,
            raw_text=payload.raw_text,
            transcript=payload.transcript,
            ocr_text=payload.ocr_text,
        )


__all__ = ["ManualPastePayload", "ManualPasteSource", "SOURCE_NAME"]
