"""OpenAI-compatible LLM Adapter（定稿文档第三节）。

第一阶段支持 OpenAI 与 DeepSeek —— 两者都是 OpenAI-compatible，共用一份实现，
只有 ``base_url`` / ``model`` / ``api_key`` 不同。

**脱敏铁律**：本模块绝不把 API Key、Authorization 头、请求体或响应体写进日志或异常消息。
异常只带 ``status_code`` / ``provider`` / ``model`` 这类元信息。
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from app.config import Settings, validate_runtime_requirements
from app.errors import ConfigError, ErrorType, LLMError, LLMInvalidOutputError, RuntimeRequirementError
from app.providers.base import LLMProvider

CHAT_COMPLETIONS_PATH = "/chat/completions"

#: 各提供方的默认 API 端点。
DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
}

#: ``LLM_MODEL`` 留空时使用的默认模型（否则无法发起请求）。
DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
}


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "",
        provider_name: str = "openai",
        timeout_seconds: int = 120,
        json_mode: bool = True,
        temperature: float = 0.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not (api_key or "").strip():
            raise RuntimeRequirementError(
                "API Key 为空，无法构造 LLM 提供方",
                error_type=ErrorType.CONFIG_LLM_API_KEY_MISSING,
                context={"provider": provider_name},
            )
        if not (model or "").strip():
            raise ConfigError(
                "模型名为空，无法构造 LLM 提供方",
                error_type=ErrorType.LLM_CONFIG_ERROR,
                context={"provider": provider_name},
            )

        self._provider_name = provider_name
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._base_url = (base_url or DEFAULT_BASE_URLS.get(provider_name, "")).rstrip("/")
        if not self._base_url:
            raise ConfigError(
                f"未配置 BASE_URL，且提供方 {provider_name!r} 没有已知默认端点",
                error_type=ErrorType.LLM_CONFIG_ERROR,
                context={"provider": provider_name},
            )
        self._timeout_seconds = timeout_seconds
        self._json_mode = json_mode
        self._temperature = temperature
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
        )

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}{CHAT_COMPLETIONS_PATH}"

    @property
    def timeout_seconds(self) -> int:
        return self._timeout_seconds

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> "OpenAICompatibleProvider":
        """从配置构造。**这是「真正执行前」的校验点**（第二十五节）。"""
        validate_runtime_requirements(settings, require_llm=True)
        provider_name = settings.llm_provider
        return cls(
            api_key=settings.llm_api_key,
            model=settings.llm_model or DEFAULT_MODELS.get(provider_name, ""),
            base_url=settings.llm_base_url,
            provider_name=provider_name,
            timeout_seconds=settings.llm_timeout_seconds,
            client=client,
        )

    # ------------------------------------------------------------------ #
    # 调用
    # ------------------------------------------------------------------ #
    def _build_response_format(self, schema: dict[str, Any]) -> dict[str, Any] | None:
        """``json_mode=False`` 时不发 ``response_format``（部分兼容端点会 400）。"""
        if not self._json_mode:
            return None
        if isinstance(schema, Mapping) and {"name", "schema"} <= set(schema):
            return {"type": "json_schema", "json_schema": {**dict(schema), "strict": True}}
        return {"type": "json_object"}

    def _build_payload(
        self, content: str, schema: dict[str, Any], system: str | None
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": False,
        }
        response_format = self._build_response_format(schema)
        if response_format is not None:
            payload["response_format"] = response_format
        return payload

    def _redacted_context(self, **extra: Any) -> dict[str, Any]:
        context = {"provider": self._provider_name, "model": self._model}
        context.update(extra)
        return context

    async def complete(
        self,
        content: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
    ) -> str:
        payload = self._build_payload(content, schema, system)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = await self._client.post(self.endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"LLM 请求超时（{self._timeout_seconds}s）",
                error_type=ErrorType.LLM_TIMEOUT,
                context=self._redacted_context(timeout_seconds=self._timeout_seconds),
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"LLM 传输失败：{type(exc).__name__}",
                error_type=ErrorType.LLM_API_ERROR,
                context=self._redacted_context(error_kind=type(exc).__name__),
            ) from exc

        if response.status_code >= 400:
            raise LLMError(
                f"LLM 返回 HTTP {response.status_code}",
                error_type=ErrorType.LLM_API_ERROR,
                context=self._redacted_context(status_code=response.status_code),
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(
                "LLM 响应体不是合法 JSON",
                error_type=ErrorType.LLM_API_ERROR,
                context=self._redacted_context(status_code=response.status_code),
            ) from exc

        return self._extract_content(body)

    def _extract_content(self, body: Any) -> str:
        if not isinstance(body, Mapping):
            raise LLMInvalidOutputError(
                "LLM 响应不是 JSON 对象",
                context=self._redacted_context(),
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMInvalidOutputError(
                "LLM 响应缺少 choices",
                context=self._redacted_context(),
            )
        first = choices[0]
        if not isinstance(first, Mapping):
            raise LLMInvalidOutputError("LLM 响应 choices[0] 结构异常", context=self._redacted_context())

        message = first.get("message")
        content: Any = None
        if isinstance(message, Mapping):
            content = message.get("content")
        if content is None:
            # 少数兼容端点把文本放在 choices[0].text
            content = first.get("text")
        if not isinstance(content, str) or not content.strip():
            raise LLMInvalidOutputError(
                "LLM 响应缺少可用的文本内容",
                context=self._redacted_context(),
            )
        return content

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OpenAICompatibleProvider":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


__all__ = [
    "OpenAICompatibleProvider",
    "CHAT_COMPLETIONS_PATH",
    "DEFAULT_BASE_URLS",
    "DEFAULT_MODELS",
]
