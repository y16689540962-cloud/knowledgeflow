"""统一异常模型与 ``error_type`` 枚举。

设计约束（来自定稿文档）：

* 所有异常都有一个可落库的 ``error_type`` 字符串（写入 ``contents.error_type``）。
* 禁止 ``except: pass`` 式静默失败 —— 任何失败都必须带上 ``error_type``。
* Phase 1 只定义模型本身，不实现任何真实网络 / LLM 失败处理。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorType(str, Enum):
    """全系统 ``error_type`` 常量。

    取值即数据库里存的字符串，禁止在别处硬编码裸字符串。
    """

    # --- 配置 / 运行时能力 ---
    CONFIG_INVALID = "CONFIG_INVALID"
    CONFIG_LLM_API_KEY_MISSING = "CONFIG_LLM_API_KEY_MISSING"
    CONFIG_OBSIDIAN_VAULT_MISSING = "CONFIG_OBSIDIAN_VAULT_MISSING"
    CONFIG_OBSIDIAN_VAULT_NOT_FOUND = "CONFIG_OBSIDIAN_VAULT_NOT_FOUND"

    # --- URL / source_id （第五节、第六节） ---
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_URL_SCHEME = "UNSUPPORTED_URL_SCHEME"
    DOMAIN_NOT_ALLOWED = "DOMAIN_NOT_ALLOWED"
    SOURCE_ID_RESOLUTION_FAILED = "SOURCE_ID_RESOLUTION_FAILED"

    # --- B 线 Ingestion（第二十二节） ---
    SHORT_LINK_RESOLUTION_FAILED = "SHORT_LINK_RESOLUTION_FAILED"
    AWEME_ID_NOT_FOUND = "AWEME_ID_NOT_FOUND"
    REQUEST_BLOCKED = "REQUEST_BLOCKED"
    COOKIE_REQUIRED = "COOKIE_REQUIRED"
    PARSER_UNSUPPORTED = "PARSER_UNSUPPORTED"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"

    # --- LLM（第八节、第十二节） ---
    LLM_CONFIG_ERROR = "LLM_CONFIG_ERROR"
    LLM_API_ERROR = "LLM_API_ERROR"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_INVALID_OUTPUT = "LLM_INVALID_OUTPUT"

    # --- 输入不足（Phase 2 追加：不产出「空的成功」） ---
    EMPTY_SOURCE_TEXT = "EMPTY_SOURCE_TEXT"

    # --- 存储 / 安全 ---
    DATABASE_ERROR = "DATABASE_ERROR"
    CONTENT_NOT_FOUND = "CONTENT_NOT_FOUND"
    #: 实体 / 主题不存在（合并、查询时按 id 指不到行）
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    #: 合并请求本身不合法（合并到自己 / id 为空）—— 是请求问题，不是数据库故障
    ENTITY_MERGE_INVALID = "ENTITY_MERGE_INVALID"
    PATH_TRAVERSAL_DETECTED = "PATH_TRAVERSAL_DETECTED"
    OBSIDIAN_WRITE_FAILED = "OBSIDIAN_WRITE_FAILED"

    # --- Pipeline ---
    PIPELINE_STEP_FAILED = "PIPELINE_STEP_FAILED"

    # --- 媒体下载 / 可选能力（Phase 7） ---
    #: 下载失败：网络层或 HTTP 状态码层面的问题
    MEDIA_DOWNLOAD_FAILED = "MEDIA_DOWNLOAD_FAILED"
    #: 超过体积上限 —— 在读完整个响应体之前就停手
    MEDIA_TOO_LARGE = "MEDIA_TOO_LARGE"
    #: Content-Type 或魔数与声明的 media_type 不符
    MEDIA_TYPE_UNSUPPORTED = "MEDIA_TYPE_UNSUPPORTED"
    #: 手动指定的本地媒体文件不存在 / 不是文件（Phase 7 的「本地媒体入口」）
    MEDIA_FILE_NOT_FOUND = "MEDIA_FILE_NOT_FOUND"
    #: 真实引擎没装（不是「识别出错了」，是「根本没这个能力」）
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    #: 引擎装了但这一步失败了
    CAPABILITY_FAILED = "CAPABILITY_FAILED"

    # --- 兜底 ---
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


#: 定稿文档第二十二节明确列出的 B 线 ``error_type``。
INGESTION_ERROR_TYPES: tuple[ErrorType, ...] = (
    ErrorType.SHORT_LINK_RESOLUTION_FAILED,
    ErrorType.AWEME_ID_NOT_FOUND,
    ErrorType.SOURCE_ID_RESOLUTION_FAILED,
    ErrorType.REQUEST_BLOCKED,
    ErrorType.COOKIE_REQUIRED,
    ErrorType.PARSER_UNSUPPORTED,
    ErrorType.NETWORK_TIMEOUT,
)


class KnowledgeFlowError(Exception):
    """所有业务异常的基类。

    ``error_type`` 既可被类级声明（子类默认），也可在构造时显式覆盖。
    """

    error_type: ErrorType = ErrorType.UNKNOWN_ERROR
    default_message: str = "KnowledgeFlow error"

    def __init__(
        self,
        message: str | None = None,
        *,
        error_type: ErrorType | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.error_type = error_type if error_type is not None else type(self).error_type
        self.message = message or type(self).default_message
        self.context: dict[str, Any] = dict(context or {})
        super().__init__(self.message)

    @property
    def error_type_value(self) -> str:
        """可直接落库的字符串形式。"""
        return self.error_type.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type.value,
            "message": self.message,
            "context": dict(self.context),
        }

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"{type(self).__name__}(error_type={self.error_type.value!r}, message={self.message!r})"


class ConfigError(KnowledgeFlowError):
    error_type = ErrorType.CONFIG_INVALID
    default_message = "配置不合法"


class RuntimeRequirementError(KnowledgeFlowError):
    """在真正需要某个外部能力时，才抛出的“能力缺失”错误。"""

    error_type = ErrorType.CONFIG_INVALID
    default_message = "运行时依赖缺失"


class NormalizationError(KnowledgeFlowError):
    error_type = ErrorType.CONFIG_INVALID
    default_message = "内容归一化失败"


class URLValidationError(KnowledgeFlowError):
    error_type = ErrorType.INVALID_URL
    default_message = "URL 校验失败"


class PathTraversalError(KnowledgeFlowError):
    error_type = ErrorType.PATH_TRAVERSAL_DETECTED
    default_message = "检测到路径穿越"


class DatabaseError(KnowledgeFlowError):
    error_type = ErrorType.DATABASE_ERROR
    default_message = "数据库操作失败"


class PipelineStepError(KnowledgeFlowError):
    error_type = ErrorType.PIPELINE_STEP_FAILED
    default_message = "Pipeline 步骤失败"


class MediaDownloadError(KnowledgeFlowError):
    error_type = ErrorType.MEDIA_DOWNLOAD_FAILED
    default_message = "媒体下载失败"


class MediaTooLargeError(MediaDownloadError):
    error_type = ErrorType.MEDIA_TOO_LARGE
    default_message = "媒体文件超过体积上限"


class MediaTypeUnsupportedError(MediaDownloadError):
    error_type = ErrorType.MEDIA_TYPE_UNSUPPORTED
    default_message = "媒体类型不受支持"


class CapabilityUnavailableError(KnowledgeFlowError):
    """外部能力（ASR / OCR）**没装**。

    和 :class:`CapabilityFailedError` 的区别很重要：前者是「机器上没有这个能力」，
    定稿第二条原则要求它**不得阻塞 Core Pipeline**（降级即可）；
    后者是「有能力但这步出错」，必须如实报出来。
    """

    error_type = ErrorType.CAPABILITY_UNAVAILABLE
    default_message = "可选能力不可用（依赖未安装）"


class CapabilityFailedError(KnowledgeFlowError):
    error_type = ErrorType.CAPABILITY_FAILED
    default_message = "可选能力执行失败"


class ContentNotFoundError(KnowledgeFlowError):
    error_type = ErrorType.CONTENT_NOT_FOUND
    default_message = "内容不存在"


class EntityNotFoundError(KnowledgeFlowError):
    error_type = ErrorType.ENTITY_NOT_FOUND
    default_message = "实体不存在"


class LLMError(KnowledgeFlowError):
    error_type = ErrorType.LLM_API_ERROR
    default_message = "LLM 调用失败"


class LLMInvalidOutputError(LLMError):
    error_type = ErrorType.LLM_INVALID_OUTPUT
    default_message = "LLM 输出无法通过校验"


class FixtureNotFoundError(KnowledgeFlowError):
    error_type = ErrorType.UNKNOWN_ERROR
    default_message = "fixture 不存在"


__all__ = [
    "ErrorType",
    "INGESTION_ERROR_TYPES",
    "KnowledgeFlowError",
    "ConfigError",
    "RuntimeRequirementError",
    "NormalizationError",
    "URLValidationError",
    "PathTraversalError",
    "DatabaseError",
    "PipelineStepError",
    "ContentNotFoundError",
    "EntityNotFoundError",
    "LLMError",
    "LLMInvalidOutputError",
    "FixtureNotFoundError",
    "MediaDownloadError",
    "MediaTooLargeError",
    "MediaTypeUnsupportedError",
    "CapabilityUnavailableError",
    "CapabilityFailedError",
]
