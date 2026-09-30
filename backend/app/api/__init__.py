"""FastAPI 应用层（Phase 8）。

定稿文档里的位置：

* 第三节技术栈里有 **FastAPI**。
* 第十七节：「**禁止依赖 FastAPI BackgroundTasks 作为可靠队列**」——
  所以本层的写入接口是**同步 await 到出结果**，不丢后台。
* 第二十八节：「未来前端不手写 API 类型，使用 ``openapi-typescript``
  从 FastAPI OpenAPI 自动生成」—— 所以进出契约**全部**是 Pydantic 模型，
  绝不返回裸 dict（那会让生成的 TS 类型退化成 ``any``）。

三件必须做对的事：

1. ``create_app()`` 是工厂，依赖**全部显式注入** —— 测试才能换掉
   LLM Provider / 数据库 / vault，不碰真实环境。
2. 启动时跑 :func:`run_startup_recovery`（第十七节），
   **恢复失败不得让应用起不来**。
3. HTTP 状态码由 ``error_type`` 派生，不是一律 200 ——
   前端靠状态码就能分支，不用解析 body。
"""

from app.api.app import create_app, mount_web_ui, WEB_DIR, API_TITLE, API_VERSION
from app.api.container import AppContainer, build_default_provider, run_startup_recovery
from app.api.routes import API_PREFIX
from app.api.errors import ErrorResponse, install_exception_handlers, status_for_error_type
from app.api.schemas import (
    AnalysisSummary,
    ContentDetail,
    ContentListResponse,
    ContentSummary,
    DouyinIngestRequest,
    HealthResponse,
    IngestResponse,
    ManualIngestRequest,
    ProcessingResultResponse,
    RawContentResponse,
    RecoveryReportResponse,
    StepRecordModel,
)

__all__ = [
    "create_app",
    "mount_web_ui",
    "WEB_DIR",
    "API_TITLE",
    "API_VERSION",
    "API_PREFIX",
    "AppContainer",
    "build_default_provider",
    "run_startup_recovery",
    "install_exception_handlers",
    "status_for_error_type",
    "ErrorResponse",
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
]
