"""API 进出契约（定稿第二十八节：前端从 OpenAPI 生成 TS 类型）。

**每个进出体都是 Pydantic 模型**。返回裸 dict 会让 ``openapi-typescript``
生成 ``any``，等于白做。

两条纪律：

* **不回显密钥**：``HealthResponse`` 只说「配了没有」，不说配了什么。
  数据库路径 / vault 绝对路径也不回显（本地应用也不该顺手泄露本机结构）。
* **正文照给**：``raw_text`` 这类正文对日志是禁区，但对 API 响应不是 ——
  详情页要显示原文。日志铁律见 :mod:`app.logging_config`。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.pipeline.recovery import RecoveryReport
from app.pipeline.result import ProcessingResult, StepRecord
from app.schemas.content import RawContent
from app.schemas.enums import MediaType

API_VERSION = "0.4.0"


class _Strict(BaseModel):
    """所有 API 模型的基类：请求体**拒绝**未知字段。

    响应体用 ``model_config`` 允许额外字段无意义；这里统一严格，
    避免前端拼错字段名时后端静默忽略。
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# 健康
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = API_VERSION
    llm_provider: str
    llm_model: str
    vault_configured: bool
    recovery: "RecoveryReportResponse | None" = None
    recovery_error: str | None = None


# --------------------------------------------------------------------------- #
# 内容
# --------------------------------------------------------------------------- #
class ContentSummary(BaseModel):
    id: str
    source: str
    source_id: str
    title: str | None = None
    author: str | None = None
    media_type: str | None = None
    status: str
    created_at: str
    processed_at: str | None = None
    error_type: str | None = None
    needs_manual_review: bool = False
    analysis_count: int = 0


class AnalysisSummary(BaseModel):
    id: str
    analysis_type: str | None = None
    summary: str | None = None
    model: str | None = None
    prompt_version: str
    chunk_count: int = 0
    created_at: str


class ContentDetail(ContentSummary):
    source_url: str | None = None
    description: str | None = None
    content_hash: str
    content_hash_version: int
    #: 只在「真有失败」时出现；成功时为 ``None``，不给前端一个永远为空的字段去猜。
    error_message: str | None = None
    current_analysis: AnalysisSummary | None = None
    raw_text: str | None = None
    transcript: str | None = None
    ocr_text: str | None = None


class ContentListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ContentSummary]


# --------------------------------------------------------------------------- #
# 处理结果
# --------------------------------------------------------------------------- #
class StepRecordModel(BaseModel):
    step: str
    status: str
    elapsed_ms: float
    detail: str | None = None
    error_type: str | None = None

    @classmethod
    def from_record(cls, record: StepRecord) -> "StepRecordModel":
        return cls(
            step=record.step,
            status=record.status,
            elapsed_ms=record.elapsed_ms,
            detail=record.detail,
            error_type=record.error_type,
        )


class ProcessingResultResponse(BaseModel):
    """``ProcessingResult`` 的 API 形态：**一步都不省**。

    13 个步骤全部回传，前端才能看出「到底卡在哪」。
    """

    outcome: str
    content_id: str | None = None
    status: str | None = None
    analysis_id: str | None = None
    note_path: str | None = None
    duplicate_of: str | None = None
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
    failed_step: str | None = None
    elapsed_ms: float = 0.0
    steps: list[StepRecordModel] = Field(default_factory=list)

    @classmethod
    def from_result(cls, result: ProcessingResult) -> "ProcessingResultResponse":
        return cls(
            outcome=result.outcome,
            content_id=result.content_id,
            status=result.status,
            analysis_id=result.analysis_id,
            note_path=result.note_path,
            duplicate_of=result.duplicate_of,
            deduplicated_by=result.deduplicated_by,
            prompt_version=result.prompt_version,
            model=result.model,
            chunk_count=result.chunk_count,
            claim_count=result.claim_count,
            unverified_count=result.unverified_count,
            entity_count=result.entity_count,
            topic_count=result.topic_count,
            needs_manual_review=result.needs_manual_review,
            error_type=result.error_type,
            error_message=result.error_message,
            failed_step=result.failed_step,
            elapsed_ms=result.elapsed_ms,
            steps=[StepRecordModel.from_record(record) for record in result.steps],
        )


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
class RawContentResponse(BaseModel):
    """采集产物的 API 形态。

    正文给**长度**不给内容：采集接口的重点是「拿到了什么身份」，
    要读正文走 ``GET /api/contents/{id}``。
    """

    source: str
    source_id: str
    source_url: str
    title: str | None = None
    author: str | None = None
    author_id: str | None = None
    description: str | None = None
    media_type: MediaType
    raw_text_chars: int = 0
    transcript_chars: int = 0
    ocr_text_chars: int = 0

    @classmethod
    def from_raw(cls, raw: RawContent) -> "RawContentResponse":
        return cls(
            source=raw.source,
            source_id=raw.source_id,
            source_url=raw.source_url,
            title=raw.title,
            author=raw.author,
            author_id=raw.author_id,
            description=raw.description,
            media_type=raw.media_type,
            raw_text_chars=len(raw.raw_text or ""),
            transcript_chars=len(raw.transcript or ""),
            ocr_text_chars=len(raw.ocr_text or ""),
        )


class ManualIngestRequest(_Strict):
    """手动粘贴（定稿第二十三条，强制存在的降级入口）。"""

    title: str | None = None
    author: str | None = None
    author_id: str | None = None
    description: str | None = None
    source_url: str = ""
    raw_text: str | None = None
    transcript: str | None = None
    ocr_text: str | None = None
    media_type: MediaType = "text"
    #: 采完立刻跑 A 线。关掉就只做「URL / 文本 → RawContent」。
    process: bool = True


class DouyinIngestRequest(_Strict):
    #: 可以直接是抖音「复制链接」给的那**整段分享文案**（口令 + 文案 + 链接 + 引导语）
    #: —— ``DouyinSource`` 会抠出里面的 ``http(s)`` 链接，再走完整的 URL 校验。
    #: 抠不到链接时报 ``INVALID_URL``（错误信息只给长度，不回显整段文案）。
    url: str
    timeout_seconds: int = Field(default=15, gt=0, le=120)
    process: bool = True
    #: 用户**自己**的登录态（定稿第二十九条允许）。不传就回落到服务端配置的
    #: ``DOUYIN_COOKIE``；都没有才是匿名访问 —— 详情接口返回空响应、页面可能给验证页，
    #: 此时如实报 ``COOKIE_REQUIRED`` / ``REQUEST_BLOCKED``，不做任何绕过。
    #: **敏感串**：不回显、不进日志（第二十四节）。
    cookie: str | None = None


class MediaIngestRequest(_Strict):
    """本地媒体文件采集（Phase 7 能力的服务侧入口）。

    把「一个本地视频 / 音频 / 图片文件」变成一条笔记：文件就地引用（不复制），
    类型靠魔数嗅探（不信扩展名），采集后走 ASR / OCR（装了引擎才跑，没装就降级）。
    """

    #: 绝对路径或 ``~`` 开头的路径。必须是已存在的**文件**。
    file_path: str
    title: str | None = None
    author: str | None = None
    description: str | None = None
    #: 补充文本（例如视频文案）。给了它，即使 ASR 不可用 A 线也能跑通。
    raw_text: str | None = None
    source_url: str = ""
    process: bool = True


class IngestResponse(BaseModel):
    raw: RawContentResponse
    #: ``process=False`` 时为 ``None`` —— 不是「空的成功」，是「没要求跑」。
    result: ProcessingResultResponse | None = None


# --------------------------------------------------------------------------- #
# 实体（第二十一节）
# --------------------------------------------------------------------------- #
class AliasConflictModel(BaseModel):
    alias: str
    existing_entity_id: str


class EntitySummary(BaseModel):
    id: str
    canonical_name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)


class EntityListResponse(BaseModel):
    total: int
    items: list[EntitySummary]


class EntityMergeRequest(_Strict):
    """把 ``source_id`` 合并进 ``target_id``。

    ``dry_run=true`` 只预演（返回会动什么、卡在哪），不写库。
    """

    source_id: str
    target_id: str
    dry_run: bool = False
    #: 关掉就只迁别名与归属、保留源实体（调试 / 分步处理用）
    delete_source: bool = True


class EntityMergeResponse(BaseModel):
    source_id: str
    target_id: str
    source_canonical_name: str
    target_canonical_name: str
    aliases_moved: list[str] = Field(default_factory=list)
    alias_conflicts: list[AliasConflictModel] = Field(default_factory=list)
    links_moved: int = 0
    links_dropped: int = 0
    #: 有别名冲突时为 ``False`` —— 源实体被保留，等人工处理（不抢不改）
    source_deleted: bool = False
    applied: bool = True


# --------------------------------------------------------------------------- #
# 任务恢复
# --------------------------------------------------------------------------- #
class RecoveryReportResponse(BaseModel):
    cutoff: str
    timeout_seconds: int
    reset: list[str] = Field(default_factory=list)
    resumed: list[str] = Field(default_factory=list)
    resume_failures: list[list[str]] = Field(default_factory=list)

    @classmethod
    def from_report(cls, report: RecoveryReport) -> "RecoveryReportResponse":
        return cls(
            cutoff=report.cutoff,
            timeout_seconds=report.timeout_seconds,
            reset=list(report.reset),
            resumed=list(report.resumed),
            resume_failures=[list(pair) for pair in report.resume_failures],
        )


def as_dict(model: BaseModel) -> dict[str, Any]:
    """``model_dump`` 的别名 —— 只为让路由里读起来不啰嗦。"""
    return model.model_dump()


__all__ = [
    "API_VERSION",
    "HealthResponse",
    "ContentSummary",
    "ContentDetail",
    "ContentListResponse",
    "AnalysisSummary",
    "StepRecordModel",
    "ProcessingResultResponse",
    "RawContentResponse",
    "ManualIngestRequest",
    "DouyinIngestRequest",
    "IngestResponse",
    "RecoveryReportResponse",
    "MediaIngestRequest",
    "AliasConflictModel",
    "EntitySummary",
    "EntityListResponse",
    "EntityMergeRequest",
    "EntityMergeResponse",
    "as_dict",
]
