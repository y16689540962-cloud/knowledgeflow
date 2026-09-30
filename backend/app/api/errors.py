"""``error_type`` → HTTP 状态码。

**为什么不让所有请求都返回 200**：定稿要求「失败必须带明确 ``error_type``」。
如果 HTTP 层一律 200，前端就必须解析 body 才能知道成败 —— 那 ``error_type``
在传输层等于白设计。所以这里把 ``error_type`` 直接映射成状态码，
body 里再带上完整的 ``error_type`` 与 ``message``。

未预期的异常**不吞**：记 ``log_event(exc_info=...)`` 后返回 500
（定稿禁止 ``except: pass`` 式静默）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import ErrorType, KnowledgeFlowError
from app.logging_config import log_event

STAGE = "api"

#: 抛异常时的兜底状态码：请求本身有问题。
FALLBACK_STATUS = 400
#: ``ProcessingResult.outcome == "failed"`` 时的兜底状态码：请求格式没问题，但没处理成。
FALLBACK_RESULT_STATUS = 422

STATUS_BY_ERROR_TYPE: dict[ErrorType, int] = {
    # --- 输入有问题（4xx） ---
    ErrorType.INVALID_URL: 400,
    ErrorType.UNSUPPORTED_URL_SCHEME: 400,
    ErrorType.DOMAIN_NOT_ALLOWED: 400,
    ErrorType.PATH_TRAVERSAL_DETECTED: 400,
    ErrorType.CONFIG_INVALID: 400,
    ErrorType.EMPTY_SOURCE_TEXT: 422,
    ErrorType.SOURCE_ID_RESOLUTION_FAILED: 422,
    ErrorType.CONTENT_NOT_FOUND: 404,
    ErrorType.ENTITY_NOT_FOUND: 404,
    ErrorType.ENTITY_MERGE_INVALID: 400,
    ErrorType.MEDIA_TOO_LARGE: 413,
    ErrorType.MEDIA_TYPE_UNSUPPORTED: 415,
    ErrorType.MEDIA_FILE_NOT_FOUND: 404,
    # --- 上游 / 依赖不可用（5xx） ---
    ErrorType.SHORT_LINK_RESOLUTION_FAILED: 502,
    ErrorType.AWEME_ID_NOT_FOUND: 502,
    ErrorType.REQUEST_BLOCKED: 502,
    ErrorType.COOKIE_REQUIRED: 502,
    ErrorType.PARSER_UNSUPPORTED: 502,
    ErrorType.LLM_API_ERROR: 502,
    ErrorType.LLM_INVALID_OUTPUT: 502,
    ErrorType.MEDIA_DOWNLOAD_FAILED: 502,
    ErrorType.CAPABILITY_FAILED: 502,
    ErrorType.CONFIG_LLM_API_KEY_MISSING: 503,
    ErrorType.CONFIG_OBSIDIAN_VAULT_MISSING: 503,
    ErrorType.CONFIG_OBSIDIAN_VAULT_NOT_FOUND: 503,
    ErrorType.LLM_CONFIG_ERROR: 503,
    ErrorType.CAPABILITY_UNAVAILABLE: 503,
    ErrorType.NETWORK_TIMEOUT: 504,
    ErrorType.LLM_TIMEOUT: 504,
    # --- 我们自己炸了 ---
    ErrorType.DATABASE_ERROR: 500,
    ErrorType.OBSIDIAN_WRITE_FAILED: 500,
    ErrorType.PIPELINE_STEP_FAILED: 500,
    ErrorType.UNKNOWN_ERROR: 500,
}


class ErrorResponse(BaseModel):
    """错误响应体 —— 与 ``KnowledgeFlowError.to_dict()`` **同一形状**。

    前端只需要一套解析逻辑。
    """

    model_config = ConfigDict(extra="forbid")

    error_type: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


def status_for_error_type(error_type: str | None, *, fallback: int = FALLBACK_STATUS) -> int:
    """按 ``error_type`` 取状态码；查不到就用 ``fallback``。

    刻意**不做**字符串前缀匹配：新增 ``error_type`` 忘了登记时，
    宁可落到一个明确的兜底值，也不要靠前缀猜出一个看似合理的码。
    """
    if not error_type:
        return fallback
    try:
        return STATUS_BY_ERROR_TYPE[ErrorType(error_type)]
    except ValueError:
        return fallback


def error_payload(
    *,
    error_type: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error_type": error_type,
        "message": message,
        "context": dict(context or {}),
    }


def install_exception_handlers(app: FastAPI, *, logger: logging.Logger | None = None) -> None:
    """挂上异常处理器。幂等：重复调用只是覆盖同名 handler。"""
    log = logger or logging.getLogger("knowledgeflow.api")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 请求体字段错了是**契约问题**，不是业务失败。
        # 原样用 FastAPI 的标准格式（``detail`` 数组），不套 error_type ——
        # 套了就等于把「字段拼错」伪装成某种业务错误。
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                error_type=ErrorType.UNKNOWN_ERROR.value,
                message=str(exc.detail),
            ),
        )

    @app.exception_handler(KnowledgeFlowError)
    async def _knowledgeflow_error(request: Request, exc: KnowledgeFlowError) -> JSONResponse:
        status = status_for_error_type(exc.error_type_value)
        log_event(
            log,
            logging.WARNING if status < 500 else logging.ERROR,
            stage=STAGE,
            message="业务异常",
            error_type=exc.error_type_value,
            error_summary=exc.message,
            http_status=status,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=status,
            content=error_payload(
                error_type=exc.error_type_value,
                message=exc.message,
                context=dict(exc.context),
            ),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log_event(
            log,
            logging.ERROR,
            stage=STAGE,
            message="未预期异常",
            error_type=ErrorType.UNKNOWN_ERROR.value,
            error_summary=f"{type(exc).__name__}: {exc}",
            exc_info=exc,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                error_type=ErrorType.UNKNOWN_ERROR.value,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )


__all__ = [
    "STAGE",
    "FALLBACK_STATUS",
    "FALLBACK_RESULT_STATUS",
    "STATUS_BY_ERROR_TYPE",
    "ErrorResponse",
    "status_for_error_type",
    "error_payload",
    "install_exception_handlers",
]
