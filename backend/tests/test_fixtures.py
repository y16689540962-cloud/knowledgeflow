"""fixture 加载器与 8 个 fixture（第十五节，强制）。

Phase 1 验收口径：

* 8 个文件全部存在且能被 loader 读取
* ``valid`` 能通过 Pydantic
* ``invalid`` 能被 Pydantic 正确拒绝
* ``fenced`` / ``preamble`` / ``grounding_fail`` 只需可加载
  （repair / retry / Grounding Check 属于 Phase 2）
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.errors import FixtureNotFoundError
from app.schemas import ContentAnalysis, RawContent
from app.testing import (
    FIXTURES_DIR,
    LLM_OUTPUT_FIXTURES,
    RAW_CONTENT_FIXTURES,
    REQUIRED_FIXTURES,
    fixture_exists,
    fixture_path,
    list_fixtures,
    load_fixture_json,
    load_fixture_text,
    missing_fixtures,
)


def test_exactly_eight_required_fixtures_declared() -> None:
    assert len(REQUIRED_FIXTURES) == 8


def test_fixtures_directory_exists() -> None:
    assert FIXTURES_DIR.is_dir(), FIXTURES_DIR


def test_no_required_fixture_missing() -> None:
    assert missing_fixtures() == []


def test_no_unexpected_fixture_files() -> None:
    assert set(list_fixtures()) == set(REQUIRED_FIXTURES)


def test_required_and_helper_lists_are_consistent() -> None:
    assert set(RAW_CONTENT_FIXTURES) | set(LLM_OUTPUT_FIXTURES) == set(REQUIRED_FIXTURES)
    assert set(RAW_CONTENT_FIXTURES).isdisjoint(LLM_OUTPUT_FIXTURES)


@pytest.mark.parametrize("name", REQUIRED_FIXTURES)
def test_every_fixture_is_loadable_as_text(name: str) -> None:
    assert fixture_exists(name)
    assert load_fixture_text(name).strip() != ""


@pytest.mark.parametrize("name", REQUIRED_FIXTURES)
def test_fixture_name_extension_is_optional(name: str) -> None:
    assert fixture_path(name.removesuffix(".json")) == fixture_path(name)


def test_missing_fixture_raises() -> None:
    with pytest.raises(FixtureNotFoundError):
        load_fixture_text("does_not_exist.json")


# --------------------------------------------------------------------------- #
# RawContent fixtures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", RAW_CONTENT_FIXTURES)
def test_raw_content_fixtures_parse(name: str) -> None:
    raw = RawContent.from_fixture(load_fixture_json(name))
    assert raw.media_type in {"video", "image", "article", "audio", "text", "mixed"}


def test_raw_content_fixture_is_plain_video() -> None:
    raw = RawContent.from_fixture(load_fixture_json("raw_content.json"))
    assert raw.source == "douyin"
    assert raw.media_type == "video"
    assert raw.source_id == "7321567890123456789"


def test_raw_content_long_exceeds_default_budget() -> None:
    raw = RawContent.from_fixture(load_fixture_json("raw_content_long.json"))
    assert len(raw.transcript or "") > 12000


def test_raw_content_mixed_contains_all_four_kinds_of_statement() -> None:
    raw = RawContent.from_fixture(load_fixture_json("raw_content_mixed.json"))
    text = raw.raw_text or ""
    assert "中国人口正在下降" in text
    assert "一定会涨" in text


# --------------------------------------------------------------------------- #
# LLM output fixtures
# --------------------------------------------------------------------------- #
def test_valid_fixture_is_clean_json() -> None:
    payload = load_fixture_json("llm_analysis_valid.json")
    assert isinstance(payload, dict)
    assert "claims" in payload


def test_valid_fixture_passes_schema() -> None:
    analysis = ContentAnalysis.from_llm_payload(load_fixture_json("llm_analysis_valid.json"))
    assert analysis.analysis_type == "mixed"
    assert len(analysis.claims) == 6


def test_invalid_fixture_is_valid_json_but_fails_schema() -> None:
    payload = load_fixture_json("llm_analysis_invalid.json")  # json.loads 必须成功
    assert isinstance(payload, dict)
    with pytest.raises(ValidationError):
        ContentAnalysis.model_validate(payload)


def test_fenced_fixture_is_not_plain_json() -> None:
    """它存在的意义就是「裸输出」—— 直接 json.loads 必须失败。"""
    text = load_fixture_text("llm_analysis_fenced.json")
    assert text.lstrip().startswith("```")
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


def test_fenced_fixture_contains_valid_payload_inside_fence() -> None:
    text = load_fixture_text("llm_analysis_fenced.json")
    inner = text.split("```json", 1)[1].rsplit("```", 1)[0].strip()
    assert ContentAnalysis.model_validate(json.loads(inner)).title


def test_preamble_fixture_is_not_plain_json() -> None:
    text = load_fixture_text("llm_analysis_preamble.json")
    assert not text.lstrip().startswith("{")
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


def test_preamble_fixture_contains_embedded_payload() -> None:
    text = load_fixture_text("llm_analysis_preamble.json")
    start = text.index("{")
    end = text.rindex("}") + 1
    assert ContentAnalysis.model_validate(json.loads(text[start:end])).title


def test_grounding_fail_fixture_passes_schema() -> None:
    """Grounding Check 是 Phase 2 的事；Phase 1 只确认它是「合法但可疑」的输入。"""
    payload = load_fixture_json("llm_analysis_grounding_fail.json")
    analysis = ContentAnalysis.model_validate(payload)
    assert any(entity.name == "国际货币基金组织" for entity in analysis.entities)


def test_grounding_fail_fixture_references_numbers_absent_from_source() -> None:
    payload = load_fixture_json("llm_analysis_grounding_fail.json")
    raw = RawContent.from_fixture(load_fixture_json("raw_content_mixed.json"))
    source_text = raw.source_text()
    claim_text = payload["claims"][0]["text"]
    assert "2035" in claim_text
    assert "2035" not in source_text
    assert "国际货币基金组织" not in source_text


def test_fixtures_do_not_contain_secrets() -> None:
    for name in REQUIRED_FIXTURES:
        text = load_fixture_text(name)
        assert "sk-" not in text, name
        assert "Bearer " not in text, name
