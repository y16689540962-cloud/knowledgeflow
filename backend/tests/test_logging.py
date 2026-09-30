"""日志格式与脱敏（第二十四节，强制）。"""

from __future__ import annotations

import io
import logging

import pytest

from app.logging_config import (
    CONTENT_LIMIT,
    ERROR_SUMMARY_LIMIT,
    REDACTED,
    REDACTED_CONTENT,
    KnowledgeFlowFormatter,
    is_content_key,
    is_sensitive_key,
    log_event,
    redact_mapping,
    scrub_text,
    setup_logging,
)


def render(
    message: str,
    *,
    level: int = logging.INFO,
    stage: str = "normalize",
    content_id: str | None = "abc123",
    allow_content: bool = False,
    **fields: object,
) -> str:
    record = logging.LogRecord("t", level, __file__, 1, message, (), None)
    record.stage = stage
    record.content_id = content_id or "-"
    record.fields = fields
    return KnowledgeFlowFormatter(allow_content=allow_content).format(record)


# --------------------------------------------------------------------------- #
# 格式
# --------------------------------------------------------------------------- #
def test_format_contains_required_segments() -> None:
    line = render("归一化完成", stage="normalize", content_id="deadbeef")
    assert line.startswith("[INFO] ")
    assert "stage=normalize" in line
    assert "content_id=deadbeef" in line
    assert "message=归一化完成" in line


def test_missing_stage_and_content_id_fall_back_to_dash() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hi", (), None)
    line = KnowledgeFlowFormatter().format(record)
    assert "stage=-" in line
    assert "content_id=-" in line


def test_fields_are_rendered_sorted() -> None:
    line = render("ok", chunk_count=3, source="douyin")
    assert line.index("chunk_count=3") < line.index("source=douyin")


def test_level_name_is_rendered() -> None:
    assert render("boom", level=logging.ERROR).startswith("[ERROR]")


def test_exception_is_scrubbed() -> None:
    try:
        raise ValueError("token=sk-abcdefgh12345678")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "失败", (), sys.exc_info())
        line = KnowledgeFlowFormatter().format(record)
    assert "sk-abcdefgh12345678" not in line
    assert REDACTED in line


# --------------------------------------------------------------------------- #
# 敏感信息
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key",
    ["api_key", "OPENAI_API_KEY", "authorization", "access_token", "Cookie", "password", "secret"],
)
def test_sensitive_keys_detected(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["content_id", "source", "chunk_count", "content_hash", "elapsed_ms"])
def test_non_sensitive_keys_are_not_flagged(key: str) -> None:
    assert not is_sensitive_key(key)


def test_sensitive_field_is_redacted() -> None:
    line = render("调用 LLM", api_key="sk-1234567890abcdef")
    assert "sk-1234567890abcdef" not in line
    assert "api_key=" + REDACTED in line


def test_scrub_text_removes_openai_style_key() -> None:
    assert "sk-abcdefghijklmnop" not in scrub_text("key=sk-abcdefghijklmnop")


def test_scrub_text_removes_bearer_header() -> None:
    scrubbed = scrub_text("Authorization: Bearer abcdef1234567890")
    assert "abcdef1234567890" not in scrubbed
    assert scrubbed == "Authorization: Bearer " + REDACTED


def test_scrub_text_keeps_bearer_keyword_standalone() -> None:
    assert scrub_text("Bearer abcdef1234567890") == "Bearer " + REDACTED


def test_scrub_text_removes_assignment_style_token() -> None:
    scrubbed = scrub_text("token=abcdefgh1234 api_key: zzzz9999")
    assert "abcdefgh1234" not in scrubbed
    assert "zzzz9999" not in scrubbed


def test_redact_mapping_is_recursive() -> None:
    data = {"a": {"api_key": "sk-secret-value"}, "b": "fine"}
    assert redact_mapping(data) == {"a": {"api_key": REDACTED}, "b": "fine"}


# --------------------------------------------------------------------------- #
# 正文内容
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["raw_text", "transcript", "ocr_text", "prompt", "response"])
def test_content_keys_detected(key: str) -> None:
    assert is_content_key(key)


@pytest.mark.parametrize("key", ["content_id", "content_hash", "source_id", "chunk_count"])
def test_content_key_detection_does_not_over_match(key: str) -> None:
    assert not is_content_key(key)


def test_transcript_is_suppressed_by_default() -> None:
    line = render("分析完成", transcript="这是逐字稿正文")
    assert "这是逐字稿正文" not in line
    assert "transcript=" + REDACTED_CONTENT in line


def test_raw_text_and_ocr_are_suppressed_by_default() -> None:
    line = render("分析完成", raw_text="原文正文", ocr_text="图片文字")
    assert "原文正文" not in line
    assert "图片文字" not in line


def test_content_allowed_only_when_flag_on() -> None:
    line = render("分析完成", allow_content=True, raw_text="短正文")
    assert "raw_text=短正文" in line


def test_content_truncated_when_allowed() -> None:
    payload = "字" * 500
    line = render("分析完成", allow_content=True, raw_text=payload)
    assert f"raw_text={'字' * CONTENT_LIMIT}" in line


def test_content_fields_are_scrubbed_even_when_allowed() -> None:
    line = render("分析完成", allow_content=True, raw_text="key=sk-abcdefghijkl")
    assert "sk-abcdefghijkl" not in line


# --------------------------------------------------------------------------- #
# log_event / setup_logging
# --------------------------------------------------------------------------- #
def test_log_event_writes_structured_line() -> None:
    stream = io.StringIO()
    logger = setup_logging(stream=stream)
    log_event(logger, logging.INFO, stage="dedup", content_id="c1", message="命中重复", hit=True)
    output = stream.getvalue()
    assert "[INFO]" in output
    assert "stage=dedup" in output
    assert "content_id=c1" in output
    assert "hit=True" in output


def test_log_event_truncates_error_summary() -> None:
    stream = io.StringIO()
    logger = setup_logging(stream=stream)
    long_error = "E" * 500
    log_event(logger, logging.ERROR, stage="llm", error_summary=long_error)
    output = stream.getvalue()
    assert "E" * ERROR_SUMMARY_LIMIT in output
    assert "E" * (ERROR_SUMMARY_LIMIT + 1) not in output


def test_setup_logging_is_idempotent() -> None:
    logger = setup_logging(stream=io.StringIO())
    setup_logging(stream=io.StringIO())
    assert len(logger.handlers) == 1


def test_verbose_content_switch_in_setup_logging() -> None:
    stream = io.StringIO()
    logger = setup_logging(stream=stream, level="DEBUG", verbose_content=True)
    log_event(logger, logging.DEBUG, stage="llm", transcript="逐字稿片段")
    assert "逐字稿片段" in stream.getvalue()


def test_debug_content_suppressed_at_info_level() -> None:
    """Settings 的语义：正文要 DEBUG + LOG_VERBOSE_CONTENT=1 同时满足。"""
    stream = io.StringIO()
    logger = setup_logging(stream=stream, level="INFO", verbose_content=True)
    log_event(logger, logging.DEBUG, stage="llm", transcript="逐字稿片段")
    assert stream.getvalue() == ""
