"""枚举 / 字面量类型（定稿文档第四节）。

所有枚举都只定义一次，数据库列直接存字符串值。
"""

from __future__ import annotations

from typing import Literal, get_args

#: 原始内容形态（``contents.media_type``）
MediaType = Literal["video", "image", "article", "audio", "text", "mixed"]

#: AI 对内容的整体判断（``analyses.analysis_type`` / ``ContentAnalysis.analysis_type``）
AnalysisType = Literal["fact", "opinion", "tutorial", "news", "analysis", "prediction", "mixed"]

#: 单条 claim 的类型（``claims[].type``）
ClaimType = Literal["fact", "opinion", "inference", "prediction"]

#: 证据类型（``claims[].evidence[].type``）
EvidenceType = Literal["source", "quote", "data", "reference", "none"]

#: 实体类型
EntityType = Literal["person", "company", "place", "concept", "product", "organization", "other"]

#: 任务状态机
ContentStatus = Literal["pending", "processing", "completed", "failed"]

MEDIA_TYPES: tuple[str, ...] = get_args(MediaType)
ANALYSIS_TYPES: tuple[str, ...] = get_args(AnalysisType)
CLAIM_TYPES: tuple[str, ...] = get_args(ClaimType)
EVIDENCE_TYPES: tuple[str, ...] = get_args(EvidenceType)
ENTITY_TYPES: tuple[str, ...] = get_args(EntityType)
CONTENT_STATUSES: tuple[str, ...] = get_args(ContentStatus)

__all__ = [
    "MediaType",
    "AnalysisType",
    "ClaimType",
    "EvidenceType",
    "EntityType",
    "ContentStatus",
    "MEDIA_TYPES",
    "ANALYSIS_TYPES",
    "CLAIM_TYPES",
    "EVIDENCE_TYPES",
    "ENTITY_TYPES",
    "CONTENT_STATUSES",
]
