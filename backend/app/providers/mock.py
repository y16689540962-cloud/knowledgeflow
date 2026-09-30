"""MockProvider 桩（Phase 1 第 10 项，Phase 2 扩展 ``complete()``）。

职责严格限定：**只把预先给定的内容当作模型输出来返回**。

不包含：JSON repair、围栏/寒暄处理、Grounding Check、真实网络请求。
那些在 ``app.llm.service`` 与 ``app.grounding`` 里，不在 Adapter 里。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from app.providers.base import LLMProvider


class MockProvider(LLMProvider):
    """两种模式：

    * ``payload=...``  —— 干净 JSON 输出（``complete`` 返回其序列化文本）
    * ``raw_text=...`` —— **原样**返回给定文本，用于模拟带 ``` 围栏或前后寒暄的脏输出
    """

    def __init__(
        self,
        payload: Any = None,
        *,
        raw_text: str | None = None,
        model: str = "mock-llm-v1",
    ) -> None:
        if payload is None and raw_text is None:
            raise ValueError("MockProvider 需要 payload 或 raw_text 之一")
        self._payload = payload
        self._raw_text = raw_text
        self._model = model
        #: 每次调用收到的 user 内容，供测试断言「重试时是否带上了上次的错误提示」。
        self.calls: list[str] = []
        #: 每次调用收到的 system 提示。
        self.systems: list[str | None] = []

    @property
    def name(self) -> str:
        return "mock"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def payload(self) -> Any:
        return copy.deepcopy(self._payload)

    @property
    def raw_text(self) -> str | None:
        return self._raw_text

    async def complete(
        self,
        content: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
    ) -> str:
        self.calls.append(content)
        self.systems.append(system)
        if self._raw_text is not None:
            return self._raw_text
        return json.dumps(self._payload, ensure_ascii=False)

    @classmethod
    def from_payload(cls, payload: Any) -> "MockProvider":
        return cls(payload=payload)

    @classmethod
    def from_json_file(cls, path: str) -> "MockProvider":
        with open(path, "r", encoding="utf-8") as handle:
            return cls(payload=json.load(handle))


__all__ = ["MockProvider"]
