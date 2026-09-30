"""Pipeline 的结果模型（定稿文档第十六节、第三十三节）。

``ProcessingResult`` 是流水线对外的唯一产物：成功要看得到「走到哪一步、产出在哪」，
失败要看到「哪一步、什么 error_type、怎么修」——绝不返回一个含糊的 False。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

#: 流水线的有序步骤名（第十六节的链路 + 重跑时的载入步骤）。
STEP_PREFLIGHT: Final[str] = "preflight"
STEP_LOAD_CONTENT: Final[str] = "load_content"
STEP_NORMALIZE: Final[str] = "normalize"
STEP_DEDUPLICATE: Final[str] = "deduplicate"
STEP_PERSIST_CONTENT: Final[str] = "persist_content"
STEP_PREPARE_TEXT: Final[str] = "prepare_text"
STEP_ANALYZE: Final[str] = "analyze"
STEP_PERSIST_ANALYSIS: Final[str] = "persist_analysis"
STEP_EXTRACT_TOPICS: Final[str] = "extract_topics"
STEP_EXTRACT_ENTITIES: Final[str] = "extract_entities"
STEP_NORMALIZE_ALIASES: Final[str] = "normalize_aliases"
STEP_RENDER_MARKDOWN: Final[str] = "render_markdown"
STEP_WRITE_OBSIDIAN: Final[str] = "write_obsidian"
STEP_COMPLETE: Final[str] = "complete"

#: 从 RawContent 出发的完整步骤顺序（不含仅重跑才有的 ``load_content``）。
PIPELINE_STEPS: Final[tuple[str, ...]] = (
    STEP_PREFLIGHT,
    STEP_NORMALIZE,
    STEP_DEDUPLICATE,
    STEP_PERSIST_CONTENT,
    STEP_PREPARE_TEXT,
    STEP_ANALYZE,
    STEP_PERSIST_ANALYSIS,
    STEP_EXTRACT_TOPICS,
    STEP_EXTRACT_ENTITIES,
    STEP_NORMALIZE_ALIASES,
    STEP_RENDER_MARKDOWN,
    STEP_WRITE_OBSIDIAN,
    STEP_COMPLETE,
)

StepStatus = Literal["ok", "failed"]
Outcome = Literal["completed", "failed", "duplicate"]


@dataclass(frozen=True)
class StepRecord:
    step: str
    status: StepStatus
    elapsed_ms: float
    detail: str | None = None
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class ProcessingResult:
    outcome: Outcome
    content_id: str | None
    steps: tuple[StepRecord, ...] = ()
    status: str | None = None
    analysis_id: str | None = None
    note_path: str | None = None
    duplicate_of: str | None = None
    #: 命中重复时，是靠哪个键认出来的：``source_id``（精确身份）或 ``content_hash``（内容指纹）。
    deduplicated_by: str | None = None
    prompt_version: str | None = None
    model: str | None = None
    chunk_count: int = 0
    claim_count: int = 0
    unverified_count: int = 0
    entity_count: int = 0
    topic_count: int = 0
    needs_manual_review: bool = False
    error_type: str | None = None
    error_message: str | None = None
    elapsed_ms: float = 0.0
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.outcome == "completed"

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"

    @property
    def duplicated(self) -> bool:
        return self.outcome == "duplicate"

    @property
    def failed_step(self) -> str | None:
        for record in reversed(self.steps):
            if record.status == "failed":
                return record.step
        return None

    def step_names(self) -> tuple[str, ...]:
        return tuple(record.step for record in self.steps)

    def to_dict(self) -> dict[str, object]:
        """脱敏后的结构化摘要（可直接进日志 / demo 输出）。"""
        return {
            "outcome": self.outcome,
            "content_id": self.content_id,
            "status": self.status,
            "analysis_id": self.analysis_id,
            "note_path": self.note_path,
            "duplicate_of": self.duplicate_of,
            "deduplicated_by": self.deduplicated_by,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "chunk_count": self.chunk_count,
            "claim_count": self.claim_count,
            "unverified_count": self.unverified_count,
            "entity_count": self.entity_count,
            "topic_count": self.topic_count,
            "needs_manual_review": self.needs_manual_review,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "steps": [
                {
                    "step": record.step,
                    "status": record.status,
                    "elapsed_ms": round(record.elapsed_ms, 1),
                    "detail": record.detail,
                    "error_type": record.error_type,
                }
                for record in self.steps
            ],
        }


__all__ = [
    "StepRecord",
    "ProcessingResult",
    "StepStatus",
    "Outcome",
    "PIPELINE_STEPS",
    "STEP_PREFLIGHT",
    "STEP_LOAD_CONTENT",
    "STEP_NORMALIZE",
    "STEP_DEDUPLICATE",
    "STEP_PERSIST_CONTENT",
    "STEP_PREPARE_TEXT",
    "STEP_ANALYZE",
    "STEP_PERSIST_ANALYSIS",
    "STEP_EXTRACT_TOPICS",
    "STEP_EXTRACT_ENTITIES",
    "STEP_NORMALIZE_ALIASES",
    "STEP_RENDER_MARKDOWN",
    "STEP_WRITE_OBSIDIAN",
    "STEP_COMPLETE",
]
