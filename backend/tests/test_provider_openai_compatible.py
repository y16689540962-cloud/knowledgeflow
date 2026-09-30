"""OpenAI-compatible Adapter：请求构造、错误映射、脱敏（第三节 / 第二十四节）。

全部用 ``httpx.MockTransport`` 跑，**不碰网络**。
"""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest

from app.config import Settings, validate_runtime_requirements
from app.errors import ConfigError, ErrorType, LLMError, LLMInvalidOutputError, RuntimeRequirementError
from app.logging_config import setup_logging
from app.providers import DEFAULT_BASE_URLS, DEFAULT_MODELS
from app.providers.openai_compatible import CHAT_COMPLETIONS_PATH, OpenAICompatibleProvider

API_KEY = "sk-unit-test-key-abcdef123456"
SCHEMA: dict = {"type": "object"}


def ok(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]},
    )


def make_provider(handler, **kwargs) -> OpenAICompatibleProvider:
    kwargs.setdefault("client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    kwargs.setdefault("api_key", API_KEY)
    kwargs.setdefault("model", "gpt-test")
    return OpenAICompatibleProvider(**kwargs)


# --------------------------------------------------------------------------- #
# 构造与配置
# --------------------------------------------------------------------------- #
def test_blank_api_key_rejected() -> None:
    with pytest.raises(RuntimeRequirementError) as excinfo:
        OpenAICompatibleProvider(api_key="  ", model="gpt-test")
    assert excinfo.value.error_type is ErrorType.CONFIG_LLM_API_KEY_MISSING


def test_blank_model_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        OpenAICompatibleProvider(api_key=API_KEY, model=" ")
    assert excinfo.value.error_type is ErrorType.LLM_CONFIG_ERROR


def test_unknown_provider_without_base_url_rejected() -> None:
    with pytest.raises(ConfigError):
        OpenAICompatibleProvider(api_key=API_KEY, model="m", provider_name="mystery")


def test_default_base_urls_present() -> None:
    assert DEFAULT_BASE_URLS["openai"].startswith("https://")
    assert DEFAULT_BASE_URLS["deepseek"].startswith("https://")


def test_endpoint_uses_default_base_url() -> None:
    provider = OpenAICompatibleProvider(api_key=API_KEY, model="m", provider_name="deepseek")
    assert provider.endpoint == DEFAULT_BASE_URLS["deepseek"] + CHAT_COMPLETIONS_PATH
    assert provider.name == "deepseek"


def test_custom_base_url_trailing_slash_stripped() -> None:
    provider = OpenAICompatibleProvider(
        api_key=API_KEY, model="m", base_url="https://example.test/v1/"
    )
    assert provider.endpoint == "https://example.test/v1/chat/completions"


def test_meta_properties() -> None:
    provider = OpenAICompatibleProvider(
        api_key=API_KEY, model="gpt-test", timeout_seconds=42
    )
    assert provider.model_name == "gpt-test"
    assert provider.timeout_seconds == 42


# --------------------------------------------------------------------------- #
# from_settings
# --------------------------------------------------------------------------- #
def test_from_settings_requires_api_key() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(RuntimeRequirementError):
        OpenAICompatibleProvider.from_settings(settings)


def test_from_settings_uses_provider_defaults() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, llm_provider="deepseek", deepseek_api_key=API_KEY
    )
    provider = OpenAICompatibleProvider.from_settings(settings)
    assert provider.name == "deepseek"
    assert provider.model_name == DEFAULT_MODELS["deepseek"]
    assert provider.endpoint == DEFAULT_BASE_URLS["deepseek"] + CHAT_COMPLETIONS_PATH


def test_from_settings_uses_explicit_values() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="openai",
        openai_api_key=API_KEY,
        llm_model="gpt-4o",
        openai_base_url="https://gateway.test/v1",
        llm_timeout_seconds=15,
    )
    provider = OpenAICompatibleProvider.from_settings(settings)
    assert provider.model_name == "gpt-4o"
    assert provider.endpoint == "https://gateway.test/v1/chat/completions"
    assert provider.timeout_seconds == 15


def test_validate_runtime_requirements_still_works() -> None:
    settings = Settings(_env_file=None, openai_api_key=API_KEY)  # type: ignore[call-arg]
    validate_runtime_requirements(settings, require_llm=True)


# --------------------------------------------------------------------------- #
# 请求构造
# --------------------------------------------------------------------------- #
async def test_request_shape() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return ok('{"a": 1}')

    provider = make_provider(handler, base_url="https://api.test/v1")
    result = await provider.complete("一段内容", SCHEMA, system="系统提示")

    assert result == '{"a": 1}'
    assert captured["url"] == "https://api.test/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["headers"]["authorization"] == f"Bearer {API_KEY}"
    assert captured["headers"]["content-type"].startswith("application/json")

    body = captured["body"]
    assert body["model"] == "gpt-test"
    assert body["stream"] is False
    assert body["temperature"] == 0.0
    assert body["messages"][0] == {"role": "system", "content": "系统提示"}
    assert body["messages"][1] == {"role": "user", "content": "一段内容"}
    assert body["response_format"] == {"type": "json_object"}


async def test_system_message_omitted_when_absent() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return ok("{}")

    provider = make_provider(handler)
    await provider.complete("内容", SCHEMA)
    assert [m["role"] for m in captured["body"]["messages"]] == ["user"]


async def test_json_mode_can_be_disabled() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return ok("{}")

    provider = make_provider(handler, json_mode=False)
    await provider.complete("内容", SCHEMA)
    assert "response_format" not in captured["body"]


async def test_json_schema_wrapper_is_used() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return ok("{}")

    provider = make_provider(handler)
    strict = {"name": "content_analysis", "schema": {"type": "object", "properties": {}}}
    await provider.complete("内容", strict)
    assert captured["body"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {**strict, "strict": True},
    }


async def test_analyze_parses_dict() -> None:
    provider = make_provider(lambda request: ok('{"title": "t"}'))
    assert await provider.analyze("内容", SCHEMA) == {"title": "t"}


async def test_analyze_raises_on_non_json() -> None:
    provider = make_provider(lambda request: ok("这不是 JSON"))
    with pytest.raises(json.JSONDecodeError):
        await provider.analyze("内容", SCHEMA)


# --------------------------------------------------------------------------- #
# 错误映射
# --------------------------------------------------------------------------- #
async def test_timeout_maps_to_llm_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("超时")

    provider = make_provider(handler)
    with pytest.raises(LLMError) as excinfo:
        await provider.complete("内容", SCHEMA)
    assert excinfo.value.error_type is ErrorType.LLM_TIMEOUT
    assert excinfo.value.context["provider"] == "openai"


async def test_transport_error_maps_to_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连不上")

    provider = make_provider(handler)
    with pytest.raises(LLMError) as excinfo:
        await provider.complete("内容", SCHEMA)
    assert excinfo.value.error_type is ErrorType.LLM_API_ERROR
    assert excinfo.value.context["error_kind"] == "ConnectError"


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
async def test_http_error_maps_to_api_error(status: int) -> None:
    provider = make_provider(lambda request: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(LLMError) as excinfo:
        await provider.complete("内容", SCHEMA)
    assert excinfo.value.error_type is ErrorType.LLM_API_ERROR
    assert excinfo.value.context["status_code"] == status


async def test_non_json_response_body() -> None:
    provider = make_provider(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(LLMError) as excinfo:
        await provider.complete("内容", SCHEMA)
    assert excinfo.value.error_type is ErrorType.LLM_API_ERROR


async def test_missing_choices() -> None:
    provider = make_provider(lambda request: httpx.Response(200, json={"id": "x"}))
    with pytest.raises(LLMInvalidOutputError) as excinfo:
        await provider.complete("内容", SCHEMA)
    assert excinfo.value.error_type is ErrorType.LLM_INVALID_OUTPUT


async def test_empty_choices_list() -> None:
    provider = make_provider(lambda request: httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMInvalidOutputError):
        await provider.complete("内容", SCHEMA)


async def test_empty_content() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})
    )
    with pytest.raises(LLMInvalidOutputError):
        await provider.complete("内容", SCHEMA)


async def test_null_content() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
    )
    with pytest.raises(LLMInvalidOutputError):
        await provider.complete("内容", SCHEMA)


async def test_text_fallback_field() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, json={"choices": [{"text": "备用字段"}]})
    )
    assert await provider.complete("内容", SCHEMA) == "备用字段"


async def test_non_object_body() -> None:
    provider = make_provider(lambda request: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(LLMInvalidOutputError):
        await provider.complete("内容", SCHEMA)


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
async def test_api_key_never_in_exception() -> None:
    provider = make_provider(lambda request: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(LLMError) as excinfo:
        await provider.complete("内容", SCHEMA)
    payload = excinfo.value.to_dict()
    assert API_KEY not in json.dumps(payload, ensure_ascii=False)
    assert API_KEY not in str(excinfo.value)


async def test_api_key_never_in_logs() -> None:
    stream = io.StringIO()
    logger = setup_logging(stream=stream, level="DEBUG")
    provider = make_provider(lambda request: ok("{}"))

    await provider.complete("内容", SCHEMA)
    logger.debug("调用结束")

    assert API_KEY not in stream.getvalue()


async def test_request_body_never_in_exception() -> None:
    secret_content = "这段正文不该出现在异常里"
    provider = make_provider(lambda request: httpx.Response(429, json={"error": "rate"}))
    with pytest.raises(LLMError) as excinfo:
        await provider.complete(secret_content, SCHEMA)
    assert secret_content not in json.dumps(excinfo.value.to_dict(), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 客户端生命周期
# --------------------------------------------------------------------------- #
async def test_injected_client_is_not_closed() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: ok("{}")))
    provider = OpenAICompatibleProvider(api_key=API_KEY, model="m", client=client)
    await provider.aclose()
    assert not client.is_closed
    await client.aclose()


async def test_owned_client_is_closed() -> None:
    provider = OpenAICompatibleProvider(api_key=API_KEY, model="m")
    await provider.aclose()
    assert provider._client.is_closed  # noqa: SLF001 - 就是为了断言内部客户端状态


async def test_async_context_manager() -> None:
    provider = OpenAICompatibleProvider(
        api_key=API_KEY,
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: ok("{}"))),
    )
    async with provider as entered:
        assert entered is provider


def test_logging_module_not_imported_at_warning_by_default() -> None:
    """确保 httpx 的日志级别被压到 WARNING，避免请求体进日志。"""
    setup_logging(stream=io.StringIO(), level="DEBUG")
    assert logging.getLogger("httpx").level == logging.WARNING
