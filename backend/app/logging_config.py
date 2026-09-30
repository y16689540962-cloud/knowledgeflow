"""统一日志与脱敏（定稿文档第二十四节，强制）。

* 格式：``[LEVEL] stage=... content_id=... message=...``
* 绝对禁止写进日志：API Key / Authorization / token / Cookie、
  ``raw_text`` / ``transcript`` / ``ocr_text`` 正文、LLM 完整请求与响应体。
* 正文内容仅在 ``LOG_LEVEL=DEBUG`` 且 ``LOG_VERBOSE_CONTENT=1`` 时输出，并脱敏截断。
"""

from __future__ import annotations

import logging
import re
import sys
from typing import IO, Any, Mapping

REDACTED = "***REDACTED***"
REDACTED_CONTENT = "***REDACTED_CONTENT***"

#: 错误摘要截断长度（第二十四节：错误摘要截断 200 字符）。
ERROR_SUMMARY_LIMIT = 200
#: 正文内容允许输出时的截断长度。
CONTENT_LIMIT = 200

#: 键名包含这些子串 → 一律脱敏（大小写不敏感）。
SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "token",
    "cookie",
    "secret",
    "password",
    "passwd",
    "credential",
)

#: 键名命中这些名字 → 视为正文内容，默认完全抑制。
#: 采用「精确匹配 或 以 ``_<名字>`` 结尾」，避免误伤 ``content_hash`` / ``content_id``。
CONTENT_KEYS: frozenset[str] = frozenset(
    {
        "raw_text",
        "transcript",
        "ocr_text",
        "text",
        "content",
        "prompt",
        "response",
        "llm_request",
        "llm_response",
        "vault_content",
        "body",
    }
)

_SENSITIVE_KEY_RE = re.compile("|".join(re.escape(k) for k in SENSITIVE_KEY_PATTERNS), re.IGNORECASE)

_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"(?i)(bearer)(\s+)([A-Za-z0-9._\-]{8,})"), r"\1\2" + REDACTED),
    (
        re.compile(
            r"(?i)((?:api[-_]?key|access[-_]?token|refresh[-_]?token|token|secret|password|passwd|cookie|authorization|signature)\s*[=:]\s*)"
            r"(?!(?:bearer|basic|token)\b)"
            r"([^\s,;'\"]{4,})"
        ),
        r"\1" + REDACTED,
    ),
)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(key))


def is_content_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in CONTENT_KEYS:
        return True
    return any(normalized.endswith("_" + candidate) for candidate in CONTENT_KEYS)


def scrub_text(text: str) -> str:
    """脱敏任意字符串中的凭据痕迹。"""
    result = text
    for pattern, replacement in _TEXT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def truncate(text: str, limit: int = ERROR_SUMMARY_LIMIT) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"...<truncated {len(text) - limit} chars>"


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return REDACTED
    return REDACTED


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """递归脱敏字典：敏感键 → 定值，正文键 → 调用方决定。"""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and is_sensitive_key(key):
            result[key] = REDACTED
        elif isinstance(value, Mapping):
            result[key] = redact_mapping(value)
        else:
            result[key] = value
    return result


class KnowledgeFlowFormatter(logging.Formatter):
    """``[LEVEL] stage=... content_id=... message=...`` + 结构字段 + 强制脱敏。"""

    def __init__(self, *, allow_content: bool = False) -> None:
        super().__init__()
        self.allow_content = allow_content

    def render_field(self, key: str, value: Any) -> str:
        if is_sensitive_key(key):
            return REDACTED
        if is_content_key(key):
            if not self.allow_content:
                return REDACTED_CONTENT
            return truncate(scrub_text(str(value)), CONTENT_LIMIT)
        if isinstance(value, Mapping):
            return str(redact_mapping(value))
        if isinstance(value, str):
            return scrub_text(value)
        return str(value)

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - logging API
        stage = getattr(record, "stage", "-")
        content_id = getattr(record, "content_id", "-")
        message = scrub_text(record.getMessage())

        parts = [f"[{record.levelname}] stage={stage} content_id={content_id} message={message}"]

        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            for key in sorted(fields):
                parts.append(f"{key}={self.render_field(key, fields[key])}")

        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            parts.append(f"exception={scrub_text(exc_text)}")

        return " ".join(parts)


def setup_logging(
    *,
    level: str = "INFO",
    verbose_content: bool = False,
    stream: IO[str] | None = None,
    logger_name: str = "knowledgeflow",
) -> logging.Logger:
    """安装统一 handler，并返回根 logger。可重复调用（幂等替换 handler）。"""
    formatter = KnowledgeFlowFormatter(allow_content=bool(verbose_content))

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(formatter)

    logger = logging.getLogger(logger_name)
    logger.handlers = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False

    # 第三方库默认闭嘴，避免把请求体写进日志。
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logger


def log_event(
    logger: logging.Logger,
    level: int,
    *,
    stage: str,
    message: str = "",
    content_id: str | None = None,
    error_summary: str | None = None,
    exc_info: object = False,
    **fields: Any,
) -> None:
    """结构化记一条日志。``fields`` 会逐个经过脱敏。

    ``exc_info`` 传真实的异常对象时，格式化器会打印（并脱敏）堆栈 ——
    用于「未预期异常」，避免 ``except: pass`` 式的静默。
    """
    if error_summary is not None:
        fields.setdefault("error_summary", truncate(scrub_text(error_summary), ERROR_SUMMARY_LIMIT))
    if exc_info and isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    logger.log(
        level,
        message,
        exc_info=exc_info,
        extra={"stage": stage, "content_id": content_id or "-", "fields": fields},
    )


__all__ = [
    "REDACTED",
    "REDACTED_CONTENT",
    "ERROR_SUMMARY_LIMIT",
    "CONTENT_LIMIT",
    "SENSITIVE_KEY_PATTERNS",
    "CONTENT_KEYS",
    "KnowledgeFlowFormatter",
    "is_sensitive_key",
    "is_content_key",
    "scrub_text",
    "truncate",
    "redact_mapping",
    "setup_logging",
    "log_event",
]
