"""API 路由（Phase 8）。

三条贯穿全部路由的纪律：

1. **写入接口同步 await 到出结果**，不丢后台任务
   （定稿第十七节：禁止依赖 FastAPI BackgroundTasks 作为可靠队列）。
2. **状态码由 ``error_type`` 派生**，不是一律 200。
   ``ProcessingResult.outcome == "failed"`` 也照样映射成 4xx/5xx。
3. **返回的永远是完整的 ``ProcessingResult``**，13 个步骤一个不省 ——
   前端要能看出「到底卡在哪一步」。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.container import AppContainer
from app.api.errors import (
    FALLBACK_RESULT_STATUS,
    ErrorResponse,
    status_for_error_type,
)
from app.api.schemas import (
    AliasConflictModel,
    AnalysisSummary,
    ContentDetail,
    ContentListResponse,
    ContentSummary,
    DouyinIngestRequest,
    EntityListResponse,
    EntityMergeRequest,
    EntityMergeResponse,
    EntitySummary,
    HealthResponse,
    IngestResponse,
    ManualIngestRequest,
    MediaIngestRequest,
    ProcessingResultResponse,
    RawContentResponse,
    RecoveryReportResponse,
)
from app.db.models import Analysis, Content
from app.errors import ContentNotFoundError
from app.ingestion.manual import ManualPastePayload, ManualPasteSource
from app.ingestion.media_file import LocalMediaPayload, LocalMediaSource
from app.pipeline.recovery import RecoveryReport
from app.pipeline.result import ProcessingResult

API_PREFIX = "/api"

#: 列表接口一次最多给多少条。定死上限是为了防止 ``?limit=1000000`` 直接把库读穿。
MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 50

router = APIRouter(prefix=API_PREFIX)


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #
def get_container(request: Request) -> AppContainer:
    """拿应用容器。挂 ``app.state`` 而不是全局单例 —— 每个 app 实例一份。"""
    container: AppContainer = request.app.state.container
    return container


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _apply_result_status(response: Response, result: ProcessingResult | None) -> None:
    """把 ``ProcessingResult`` 的成败反映到 HTTP 状态码上。

    ``duplicate`` 不是失败，保持 200（body 里的 ``duplicate_of`` 说明它去重了）。
    """
    if result is None or not result.failed:
        return
    response.status_code = status_for_error_type(
        result.error_type, fallback=FALLBACK_RESULT_STATUS
    )


def _summary_of(content: Content, analysis_count: int) -> ContentSummary:
    return ContentSummary(
        id=content.id,
        source=content.source,
        source_id=content.source_id,
        title=content.title,
        author=content.author,
        media_type=content.media_type,
        status=content.status,
        created_at=content.created_at,
        processed_at=content.processed_at,
        error_type=content.error_type,
        needs_manual_review=content.needs_manual_review,
        analysis_count=analysis_count,
    )


def _analysis_summary(analysis: Analysis) -> AnalysisSummary:
    return AnalysisSummary(
        id=analysis.id,
        analysis_type=analysis.analysis_type,
        summary=analysis.summary,
        model=analysis.model,
        prompt_version=analysis.prompt_version,
        chunk_count=analysis.chunk_count,
        created_at=analysis.created_at,
    )


# --------------------------------------------------------------------------- #
# 健康 / 元信息
# --------------------------------------------------------------------------- #
@router.get(
    "/health",
    response_model=HealthResponse,
    summary="健康检查",
    description="只回答「配了没有」，**不回显**任何密钥、数据库路径或 vault 绝对路径。",
    tags=["meta"],
)
async def health(container: AppContainer = Depends(get_container)) -> HealthResponse:
    return HealthResponse(
        llm_provider=container.provider.name,
        llm_model=container.provider.model_name,
        vault_configured=container.vault_configured,
        recovery=RecoveryReportResponse.from_report(container.recovery_report)
        if container.recovery_report is not None
        else None,
        recovery_error=container.recovery_error,
    )


# --------------------------------------------------------------------------- #
# 内容：只读
# --------------------------------------------------------------------------- #
@router.get(
    "/contents",
    response_model=ContentListResponse,
    summary="内容列表",
    tags=["contents"],
)
async def list_contents(
    container: AppContainer = Depends(get_container),
    status: str | None = Query(default=None, description="按状态过滤：pending/processing/completed/failed"),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ContentListResponse:
    rows = await container.repository.list_page(status=status, limit=limit, offset=offset)
    total = await container.repository.count_contents(status=status)
    items = [
        _summary_of(row, await container.repository.count_analyses(row.id)) for row in rows
    ]
    return ContentListResponse(total=total, limit=limit, offset=offset, items=items)


@router.get(
    "/contents/{content_id}",
    response_model=ContentDetail,
    summary="内容详情",
    responses={404: {"model": ErrorResponse}},
    tags=["contents"],
)
async def get_content(
    content_id: str,
    container: AppContainer = Depends(get_container),
) -> ContentDetail:
    row = await container.repository.get_content(content_id)
    if row is None:
        raise ContentNotFoundError(f"内容不存在：{content_id}", context={"content_id": content_id})

    analysis = await container.repository.get_current_analysis(content_id)
    return ContentDetail(
        **_summary_of(row, await container.repository.count_analyses(content_id)).model_dump(),
        source_url=row.source_url,
        description=row.description,
        content_hash=row.content_hash,
        content_hash_version=row.content_hash_version,
        error_message=row.error_message,
        current_analysis=_analysis_summary(analysis) if analysis is not None else None,
        raw_text=row.raw_text,
        transcript=row.transcript,
        ocr_text=row.ocr_text,
    )


# --------------------------------------------------------------------------- #
# 内容：写入
# --------------------------------------------------------------------------- #
@router.post(
    "/contents/{content_id}/reprocess",
    response_model=ProcessingResultResponse,
    summary="重跑一条内容",
    description=(
        "按 ``content_id`` 重跑（第十七节）。**新增一行 analyses**，旧历史完整保留；"
        "不会命中「重复输入」短路。同步 await 到出结果，不丢后台。"
    ),
    responses={404: {"model": ErrorResponse}},
    tags=["contents"],
)
async def reprocess_content(
    content_id: str,
    response: Response,
    container: AppContainer = Depends(get_container),
) -> ProcessingResultResponse:
    result = await container.pipeline.reprocess(content_id)
    _apply_result_status(response, result)
    return ProcessingResultResponse.from_result(result)


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
@router.post(
    "/ingest/manual",
    response_model=IngestResponse,
    summary="手动粘贴采集（零网络降级入口）",
    description=(
        "定稿第二十三条**强制**存在的入口：抖音彻底失效时产品依然可用。"
        "``process=false`` 时只做「文本 → RawContent」，不跑 A 线。"
    ),
    tags=["ingest"],
)
async def ingest_manual(
    payload: ManualIngestRequest,
    response: Response,
    container: AppContainer = Depends(get_container),
) -> IngestResponse:
    raw = await ManualPasteSource().fetch(
        ManualPastePayload(
            title=payload.title,
            author=payload.author,
            author_id=payload.author_id,
            description=payload.description,
            source_url=payload.source_url,
            raw_text=payload.raw_text,
            transcript=payload.transcript,
            ocr_text=payload.ocr_text,
            media_type=payload.media_type,
        )
    )
    if not payload.process:
        return IngestResponse(raw=RawContentResponse.from_raw(raw), result=None)

    result = await container.pipeline.process(raw)
    _apply_result_status(response, result)
    return IngestResponse(
        raw=RawContentResponse.from_raw(raw),
        result=ProcessingResultResponse.from_result(result),
    )


@router.post(
    "/ingest/douyin",
    response_model=IngestResponse,
    summary="抖音 URL 采集",
    description=(
        "只做公开数据的 GET，不绕验证码 / 不伪造签名 / 不借用他人登录态"
        "（定稿第二十九条）。被平台挡住时如实返回 ``error_type``，不伪造成功。\n\n"
        "``url`` **可以直接填抖音「复制链接」给的那整段分享文案**"
        "（口令 + 文案 + 链接 + 「复制此链接，打开Dou音…」）—— 系统会抠出里面的 "
        "``http(s)`` 链接，抠出来的链接**照样要过** scheme 与域名白名单校验。\n\n"
        "元数据取自抖音**详情接口**（页面已改成 JS 壳页，服务端不再内嵌数据），"
        "而该接口**需要登录态**：请求里的 ``cookie`` 优先，缺省时回落到服务端配置的 "
        "``DOUYIN_COOKIE``。两者都没有就是匿名访问 —— 接口返回空响应，"
        "页面也可能直接给验证页。Cookie 不回显、不进日志。"
    ),
    responses={400: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    tags=["ingest"],
)
async def ingest_douyin(
    payload: DouyinIngestRequest,
    response: Response,
    container: AppContainer = Depends(get_container),
) -> IngestResponse:
    # 采集源工厂可注入：测试因此不必联网（定稿第二条原则）。
    # 用完必须关掉它自带的 httpx client —— 每次请求泄漏一个连接池，
    # 服务跑久了会攒出一堆半开连接。
    source = container.douyin_source_factory(payload.timeout_seconds, payload.cookie)
    try:
        raw = await source.fetch(payload.url)
    finally:
        await source.aclose()

    if not payload.process:
        return IngestResponse(raw=RawContentResponse.from_raw(raw), result=None)

    result = await container.pipeline.process(raw)
    _apply_result_status(response, result)
    return IngestResponse(
        raw=RawContentResponse.from_raw(raw),
        result=ProcessingResultResponse.from_result(result),
    )


@router.post(
    "/ingest/media",
    response_model=IngestResponse,
    summary="本地媒体文件采集（视频 / 音频 → ASR，图片 → OCR）",
    description=(
        "把本机的一个媒体文件变成一条笔记。文件**就地引用不复制**；"
        "类型按魔数嗅探（不信扩展名）；采集后自动跑 ASR / OCR ——"
        "引擎没装就**安静降级**（补上 ``raw_text`` 仍能跑通 A 线）。"
    ),
    responses={404: {"model": ErrorResponse}, 415: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    tags=["ingest"],
)
async def ingest_media(
    payload: MediaIngestRequest,
    response: Response,
    container: AppContainer = Depends(get_container),
) -> IngestResponse:
    raw = await LocalMediaSource().fetch(
        LocalMediaPayload(
            file_path=payload.file_path,
            title=payload.title,
            author=payload.author,
            description=payload.description,
            raw_text=payload.raw_text,
            source_url=payload.source_url,
        )
    )
    if not payload.process:
        return IngestResponse(raw=RawContentResponse.from_raw(raw), result=None)

    # 能力编排：装了引擎就转写 / 识别；没装或失败都降级为记录，不阻塞 A 线。
    enriched, _attempts = await container.media_capability_service.enrich(raw)

    result = await container.pipeline.process(enriched)
    _apply_result_status(response, result)
    return IngestResponse(
        raw=RawContentResponse.from_raw(enriched),
        result=ProcessingResultResponse.from_result(result),
    )


# --------------------------------------------------------------------------- #
# 实体（第二十一节）
# --------------------------------------------------------------------------- #
@router.get(
    "/entities",
    response_model=EntityListResponse,
    summary="实体列表（含别名）",
    description="归一化后的全部实体。合并接口要拿 ``id``，从这里取。",
    tags=["entities"],
)
async def list_entities(
    container: AppContainer = Depends(get_container),
) -> EntityListResponse:
    rows = await container.pipeline.entities.list_entities()
    items = [
        EntitySummary(
            id=row.entity_id,
            canonical_name=row.canonical_name,
            entity_type=row.entity_type,
            aliases=list(row.aliases),
        )
        for row in rows
    ]
    return EntityListResponse(total=len(items), items=items)


@router.post(
    "/entities/merge",
    response_model=EntityMergeResponse,
    summary="合并两个实体",
    description=(
        "把源实体合并进目标实体：**别名迁移**（源 canonical 名与别名挂到目标）+ "
        "**归属重挂**（``content_entities`` 改挂目标）+ 删除源实体。\n\n"
        "别名被**第三方**占着时**不抢不改**，只报 ``alias_conflicts``；"
        "只要有冲突，源实体就**保留不删**（避免那个说法彻底查不到）。\n\n"
        "``dry_run=true`` 只预演，不写库。"
    ),
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    tags=["entities"],
)
async def merge_entities(
    payload: EntityMergeRequest,
    container: AppContainer = Depends(get_container),
) -> EntityMergeResponse:
    result = await container.pipeline.entities.merge_entities(
        payload.source_id,
        payload.target_id,
        dry_run=payload.dry_run,
        delete_source=payload.delete_source,
    )
    return EntityMergeResponse(
        source_id=result.source_id,
        target_id=result.target_id,
        source_canonical_name=result.source_canonical_name,
        target_canonical_name=result.target_canonical_name,
        aliases_moved=list(result.aliases_moved),
        alias_conflicts=[
            AliasConflictModel(
                alias=conflict.alias, existing_entity_id=conflict.existing_entity_id
            )
            for conflict in result.alias_conflicts
        ],
        links_moved=result.links_moved,
        links_dropped=result.links_dropped,
        source_deleted=result.source_deleted,
        applied=result.applied,
    )


# --------------------------------------------------------------------------- #
# 任务恢复
# --------------------------------------------------------------------------- #
@router.get(
    "/tasks/recovery",
    response_model=RecoveryReportResponse,
    summary="上一次启动恢复的报告",
    description="启动时自动跑过一次（第十七节）。这里回看那次的结果。",
    tags=["tasks"],
)
async def get_recovery(container: AppContainer = Depends(get_container)) -> RecoveryReportResponse:
    report = container.recovery_report or RecoveryReport(
        cutoff=container.pipeline.recovery.cutoff(),
        timeout_seconds=container.pipeline.recovery.timeout_seconds,
    )
    return RecoveryReportResponse.from_report(report)


@router.post(
    "/tasks/recover",
    response_model=RecoveryReportResponse,
    summary="手动触发一次任务恢复",
    description=(
        "重置超时卡住的 ``processing`` 任务并重新处理。同步 await 到全部跑完。"
        "**不用 BackgroundTasks**（第十七节明确禁止把它当可靠队列）。"
    ),
    tags=["tasks"],
)
async def run_recovery(container: AppContainer = Depends(get_container)) -> RecoveryReportResponse:
    report = await container.pipeline.recover_and_resume()
    container.recovery_report = report
    return RecoveryReportResponse.from_report(report)


__all__ = ["router", "API_PREFIX", "get_container", "MAX_PAGE_LIMIT", "DEFAULT_PAGE_LIMIT"]
