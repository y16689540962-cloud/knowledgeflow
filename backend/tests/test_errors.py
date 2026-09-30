"""统一异常模型与 error_type 枚举。"""

from __future__ import annotations

import pytest

from app.errors import (
    INGESTION_ERROR_TYPES,
    ConfigError,
    DatabaseError,
    ErrorType,
    KnowledgeFlowError,
    LLMInvalidOutputError,
    PathTraversalError,
    URLValidationError,
)


def test_error_type_values_are_stable_strings() -> None:
    for member in ErrorType:
        assert member.value == member.name
        assert isinstance(member, str)


def test_ingestion_error_types_match_spec() -> None:
    """第二十二节列出的 7 个 error_type 必须齐全。"""
    assert {member.value for member in INGESTION_ERROR_TYPES} == {
        "SHORT_LINK_RESOLUTION_FAILED",
        "AWEME_ID_NOT_FOUND",
        "SOURCE_ID_RESOLUTION_FAILED",
        "REQUEST_BLOCKED",
        "COOKIE_REQUIRED",
        "PARSER_UNSUPPORTED",
        "NETWORK_TIMEOUT",
    }


def test_llm_invalid_output_is_reachable() -> None:
    assert ErrorType.LLM_INVALID_OUTPUT.value == "LLM_INVALID_OUTPUT"


def test_subclass_default_error_type() -> None:
    assert ConfigError().error_type is ErrorType.CONFIG_INVALID
    assert DatabaseError().error_type is ErrorType.DATABASE_ERROR
    assert PathTraversalError().error_type is ErrorType.PATH_TRAVERSAL_DETECTED


def test_error_type_override() -> None:
    exc = URLValidationError("坏域名", error_type=ErrorType.DOMAIN_NOT_ALLOWED)
    assert exc.error_type is ErrorType.DOMAIN_NOT_ALLOWED
    assert exc.error_type_value == "DOMAIN_NOT_ALLOWED"


def test_default_message_used_when_no_message() -> None:
    assert LLMInvalidOutputError().message == "LLM 输出无法通过校验"


def test_to_dict_is_serializable() -> None:
    exc = DatabaseError("写入失败", context={"content_id": "abc"})
    payload = exc.to_dict()
    assert payload == {
        "error_type": "DATABASE_ERROR",
        "message": "写入失败",
        "context": {"content_id": "abc"},
    }


def test_context_defaults_to_empty_dict() -> None:
    assert ConfigError().context == {}


def test_base_class_is_exception() -> None:
    assert issubclass(KnowledgeFlowError, Exception)


def test_missing_error_type_in_signature_is_not_silent() -> None:
    """任何异常都必须能给出 error_type，不能只有裸字符串消息。"""
    with pytest.raises(KnowledgeFlowError) as excinfo:
        raise PathTraversalError("越界")
    assert excinfo.value.error_type_value == "PATH_TRAVERSAL_DETECTED"
