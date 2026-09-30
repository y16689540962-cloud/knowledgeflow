"""应用级依赖容器与启动恢复。

**为什么是「容器 + 显式注入」，而不是模块级单例**：
``create_app()`` 必须在测试里能被换成「临时库 + Mock LLM + 临时 vault」。
一旦 anywhere 出现 ``get_settings()`` 这种带 ``lru_cache`` 的全局单例，
测试就会读到真实 ``.env``，A 线「必须能脱离抖音/网络/ASR/OCR 独立跑通」
这条要求直接失守。

启动恢复（定稿第十七节）：

* 扫描 ``processing`` 且超过 ``TASK_RESET_TIMEOUT_SECONDS`` 的任务 → 重置 → 重跑。
* **恢复失败不得让应用起不来**：异常被记进 ``recovery_error``，
  ``/api/health`` 与 ``/api/tasks/recovery`` 能看见，服务照常提供读写。
* 不用 FastAPI BackgroundTasks —— 第十七节明确禁止把它当可靠队列。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from app.capabilities.service import MediaCapabilityService
from app.config import Settings, get_settings, validate_runtime_requirements
from app.db.repository import ContentRepository
from app.db.session import Database
from app.errors import ErrorType
from app.ingestion.douyin import DouyinSource
from app.logging_config import log_event
from app.pipeline.recovery import RecoveryReport
from app.pipeline.service import ProcessingPipeline
from app.providers.base import LLMProvider
from app.providers.openai_compatible import DEFAULT_MODELS, OpenAICompatibleProvider

STAGE = "startup"

#: ``DouyinSource(timeout_seconds=..., cookie=...)`` 的工厂类型 —— 可注入，测试才能不联网。
#: 第二个参数是用户**自己**的登录态（``None`` = 匿名）。
DouyinSourceFactory = Callable[[int, str | None], DouyinSource]


def _default_douyin_factory(timeout_seconds: int, cookie: str | None = None) -> DouyinSource:
    return DouyinSource(timeout_seconds=timeout_seconds, cookie=cookie)


def make_douyin_factory(settings: Settings) -> DouyinSourceFactory:
    """抖音采集源工厂：请求里没给 Cookie 时，回落到 ``.env`` 的 ``DOUYIN_COOKIE``。

    两个来源都必须是**用户自己的**登录态（第二十九条）；都不给就是匿名访问。
    放 ``.env`` 里的价值在于：用户填一次就长期生效，Cookie 不必每次都经过
    请求体 / 命令行（后者会留在 shell 历史里）。
    """
    default_cookie = (settings.douyin_cookie or "").strip()

    def factory(timeout_seconds: int, cookie: str | None = None) -> DouyinSource:
        resolved = (cookie or "").strip() or default_cookie
        return DouyinSource(timeout_seconds=timeout_seconds, cookie=resolved or None)

    return factory


@dataclass
class AppContainer:
    """一个应用实例的全部依赖。挂在 ``app.state.container`` 上。"""

    settings: Settings
    database: Database
    provider: LLMProvider
    pipeline: ProcessingPipeline
    repository: ContentRepository
    logger: logging.Logger
    #: 抖音采集源工厂（可注入，默认真联网）
    douyin_source_factory: DouyinSourceFactory = _default_douyin_factory
    #: 媒体能力编排（ASR / OCR；默认 Null provider —— 引擎没装就安静降级，
    #: 不让「想处理一条本地视频」变成前置依赖问题）。
    media_capability_service: MediaCapabilityService = field(
        default_factory=MediaCapabilityService
    )
    #: 应用是否**自己创建**了 database（决定 lifespan 是否负责建表 / 关闭）
    owns_database: bool = False
    #: 启动时那次恢复的结果；没跑过就是 ``None``
    recovery_report: RecoveryReport | None = None
    #: 启动时恢复本身失败了（不是「任务失败」，是恢复流程炸了）
    recovery_error: str | None = None
    #: 供 /api/meta 之类展示；不含任何密钥
    version: str = field(default="0.4.0")

    @property
    def vault_configured(self) -> bool:
        """有没有 vault 可写。

        看 **pipeline 实际用的** vault，而不是再读一遍 settings ——
        ``create_app(vault_root=...)`` 可以覆盖 ``OBSIDIAN_VAULT_PATH``，
        只问 settings 会答「没配」，可 pipeline 明明写得进去。
        """
        return self.pipeline.vault_root is not None


def build_default_provider(settings: Settings) -> LLMProvider:
    """按配置构造真实 LLM 提供方。

    缺 API Key 时**立刻**抛 ``CONFIG_LLM_API_KEY_MISSING``：
    与其让服务起来后每个请求都以 503 失败，不如在启动那一刻说清楚。
    测试要绕过它，就显式传 ``provider=``。
    """
    validate_runtime_requirements(settings, require_llm=True)
    model = settings.llm_model.strip() or DEFAULT_MODELS.get(settings.llm_provider, "")
    return OpenAICompatibleProvider(
        api_key=settings.llm_api_key,
        model=model,
        base_url=settings.llm_base_url,
        provider_name=settings.llm_provider,
        timeout_seconds=settings.llm_timeout_seconds,
    )


async def run_startup_recovery(container: AppContainer) -> RecoveryReport:
    """启动恢复。异常**不外抛**（调用方决定怎么处理）。

    单个任务重跑失败不算恢复失败 —— 它已经进了 ``report.resume_failures``。
    这里只捕获「恢复流程本身炸了」（比如数据库不可用）。
    """
    try:
        report = await container.pipeline.recover_and_resume()
    except Exception as exc:  # noqa: BLE001 —— 记录后降级，绝不静默
        container.recovery_error = f"{type(exc).__name__}: {exc}"
        log_event(
            container.logger,
            logging.ERROR,
            stage=STAGE,
            message="启动恢复失败（服务继续启动）",
            error_type=ErrorType.PIPELINE_STEP_FAILED.value,
            error_summary=str(exc),
            exc_info=exc,
        )
        return RecoveryReport(
            cutoff=container.pipeline.recovery.cutoff(),
            timeout_seconds=container.pipeline.recovery.timeout_seconds,
        )

    container.recovery_report = report
    log_event(
        container.logger,
        logging.INFO,
        stage=STAGE,
        message="启动恢复完成",
        reset=report.reset_count,
        resumed=report.resumed_count,
        failed=len(report.resume_failures),
    )
    return report


def build_container(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    provider: LLMProvider | None = None,
    vault_root: object | None = None,
    run_startup_recovery_now: bool = False,
    logger: logging.Logger | None = None,
    douyin_source_factory: DouyinSourceFactory | None = None,
    media_capability_service: MediaCapabilityService | None = None,
) -> AppContainer:
    """把零散依赖拼成一个 :class:`AppContainer`。

    ``database`` 为 ``None`` 时按 settings 建一个（``owns_database=True``）。
    """
    resolved_settings = settings or get_settings()
    owns_database = database is None
    resolved_database = database or Database.from_settings(resolved_settings)
    resolved_logger = logger or logging.getLogger("knowledgeflow.api")
    # 先定 provider 再传给 pipeline：容器与 pipeline 用的是**同一个对象**，
    # 不出现「容器里的 provider 和 pipeline 里的不是一回事」这种第二个真源。
    resolved_provider = provider or build_default_provider(resolved_settings)

    pipeline = ProcessingPipeline.from_database(
        settings=resolved_settings,
        provider=resolved_provider,
        database=resolved_database,
        vault_root=vault_root,
        logger=resolved_logger,
    )

    return AppContainer(
        settings=resolved_settings,
        database=resolved_database,
        provider=resolved_provider,
        pipeline=pipeline,
        repository=ContentRepository(resolved_database.session_factory),
        logger=resolved_logger,
        douyin_source_factory=douyin_source_factory or make_douyin_factory(resolved_settings),
        media_capability_service=media_capability_service or MediaCapabilityService(),
        owns_database=owns_database,
    )


__all__ = [
    "STAGE",
    "DouyinSourceFactory",
    "AppContainer",
    "build_container",
    "build_default_provider",
    "make_douyin_factory",
    "run_startup_recovery",
]
