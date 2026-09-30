"""归一化服务：``RawContent`` → 落库所需的稳定字段。"""

from __future__ import annotations

from dataclasses import dataclass

from app.normalization.hashing import CONTENT_HASH_VERSION, compute_content_hash
from app.normalization.source_id import SourceIdResolution, resolve_source_id
from app.normalization.text import normalize_optional, normalize_text
from app.schemas.content import RawContent


@dataclass(frozen=True)
class NormalizedContent:
    """归一化结果。``raw`` 保留原始对象，其余字段是落库与去重要用的稳定值。"""

    raw: RawContent
    source: str
    source_id: str
    content_hash: str
    content_hash_version: int
    needs_manual_review: bool
    error_type: str | None
    source_id_resolution: SourceIdResolution

    @property
    def source_url(self) -> str | None:
        return normalize_optional(self.raw.source_url)

    @property
    def hash_is_discriminating(self) -> bool:
        """内容指纹是否真的有区分度。

        ``content_hash`` v2 吃 ``source/title/author/description/raw_text``。
        如果除 ``source`` 之外全空（连正文都没有），那么**所有**这类内容的哈希都一模一样
        —— 拿它做「同 source + hash」判重会把互不相关的两条内容误判成同一条。

        所以判重前必须先问一句：这个指纹到底区分了什么？

        v1 的白名单没有 ``raw_text``，那时「只粘正文、不填标题」的手贴内容全都
        无区分度；v2 把它们纳入后，只要有正文就一定有区分度。
        """
        raw = self.raw
        return any(
            normalize_text(value)
            for value in (raw.title, raw.author, raw.description, raw.raw_text)
        )


def normalize_content(raw: RawContent) -> NormalizedContent:
    """计算 content_hash 并确定最终 source_id。

    注意：``content_hash`` 只吃 ``source/title/author/description/raw_text``
    （第七节白名单，v2 起纳入 ``raw_text``），因此传进来的 ``source_id`` /
    ``source_url`` / ``transcript`` / ``ocr_text`` 都不会影响哈希值。
    """
    source = normalize_text(raw.source)
    content_hash = compute_content_hash(
        source=source,
        title=raw.title,
        author=raw.author,
        description=raw.description,
        raw_text=raw.raw_text,
    )
    resolution = resolve_source_id(raw.source_id, content_hash=content_hash)

    return NormalizedContent(
        raw=raw,
        source=source,
        source_id=resolution.source_id,
        content_hash=content_hash,
        content_hash_version=CONTENT_HASH_VERSION,
        needs_manual_review=resolution.needs_manual_review,
        error_type=resolution.error_type,
        source_id_resolution=resolution,
    )


__all__ = ["NormalizedContent", "normalize_content"]
