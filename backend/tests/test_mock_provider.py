"""极简 MockProvider 桩（Phase 1 第 10 项）。"""

from __future__ import annotations

import json

import pytest

from app.providers import LLMProvider, MockProvider
from app.schemas import ContentAnalysis
from app.testing import fixture_path, load_fixture_json, load_fixture_text

SCHEMA: dict = {"type": "object"}


def test_mock_provider_is_llm_provider() -> None:
    assert isinstance(MockProvider(payload={}), LLMProvider)


def test_requires_payload_or_raw_text() -> None:
    with pytest.raises(ValueError):
        MockProvider()


async def test_returns_payload(valid_analysis_payload: dict) -> None:
    provider = MockProvider(payload=valid_analysis_payload)
    result = await provider.analyze("任意输入", SCHEMA)
    assert result["title"] == valid_analysis_payload["title"]
    assert ContentAnalysis.model_validate(result).claims


async def test_returns_deep_copy() -> None:
    """每次调用都要返回独立对象，避免调用方改坏 fixture。"""
    payload = {"title": "t", "claims": []}
    provider = MockProvider(payload=payload)
    first = await provider.analyze("a", SCHEMA)
    first["title"] = "被改坏了"
    second = await provider.analyze("b", SCHEMA)
    assert second["title"] == "t"
    assert provider.payload["title"] == "t"


async def test_records_calls(valid_analysis_payload: dict) -> None:
    provider = MockProvider(payload=valid_analysis_payload)
    await provider.analyze("第一次", SCHEMA)
    await provider.analyze("第二次", SCHEMA)
    assert provider.calls == ["第一次", "第二次"]


async def test_name_and_model() -> None:
    provider = MockProvider(payload={}, model="mock-llm-v2")
    assert provider.name == "mock"
    assert provider.model_name == "mock-llm-v2"


async def test_from_json_file(valid_analysis_payload: dict) -> None:
    provider = MockProvider.from_json_file(str(fixture_path("llm_analysis_valid.json")))
    result = await provider.analyze("x", SCHEMA)
    assert result["analysis_type"] == "mixed"


async def test_raw_text_mode_does_not_repair_fenced_output() -> None:
    """Phase 1 明确不实现 repair：脏输出必须原样抛错。"""
    text = load_fixture_text("llm_analysis_fenced.json")
    provider = MockProvider(raw_text=text)
    with pytest.raises(json.JSONDecodeError):
        await provider.analyze("x", SCHEMA)


async def test_raw_text_mode_returns_parsed_payload() -> None:
    text = load_fixture_text("llm_analysis_valid.json")
    provider = MockProvider(raw_text=text)
    assert (await provider.analyze("x", SCHEMA))["analysis_type"] == "mixed"


def test_no_network_dependency() -> None:
    """桩里不允许出现任何 HTTP 客户端调用。"""
    from app.providers import mock as mock_module

    source = open(mock_module.__file__, encoding="utf-8").read()
    for banned in ("httpx", "requests", "urllib.request", "aiohttp", "socket"):
        assert banned not in source, banned


def test_valid_analysis_payload_fixture_is_json(valid_analysis_payload: dict) -> None:
    assert load_fixture_json("llm_analysis_valid.json") == valid_analysis_payload
