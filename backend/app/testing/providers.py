"""测试用 LLM Provider。

放进 ``app/testing`` 而不是 ``tests/``：它是**可复用**的测试替身，
Phase 4 串联 Pipeline 时同样要用（模拟「第 1 次脏输出、第 2 次干净」这类脚本）。
"""

from __future__ import annotations

from typing import Any, Sequence

from app.errors import ErrorType, LLMError
from app.providers.base import LLMProvider


class ScriptedProvider(LLMProvider):
    """按脚本顺序逐个返回预设结果。

    ``outputs`` 里的元素可以是：

    * ``str``        —— 当作模型原始输出返回
    * ``Exception``  —— 抛出（用来模拟超时、HTTP 500 等；抛完本次调用即结束）

    脚本耗尽后抛 ``AssertionError`` —— 说明实际调用次数超出了预期，
    这本身就是「禁止无限重试」的断言。
    """

    def __init__(
        self,
        outputs: Sequence[str | Exception],
        *,
        name: str = "scripted",
        model: str = "scripted-llm-v1",
    ) -> None:
        self._outputs: list[str | Exception] = list(outputs)
        self._index = 0
        self._name = name
        self._model = model
        self.calls: list[str] = []
        self.systems: list[str | None] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def call_count(self) -> int:
        return self._index

    @property
    def remaining(self) -> int:
        return len(self._outputs) - self._index

    async def complete(
        self,
        content: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
    ) -> str:
        self.calls.append(content)
        self.systems.append(system)
        if self._index >= len(self._outputs):
            raise AssertionError(
                f"脚本已用尽（已调用 {self._index} 次）——说明发生了预期之外的重试"
            )
        item = self._outputs[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item


class TransportFailureProvider(LLMProvider):
    """每次调用都抛指定 ``LLMError``，用于验证传输层错误类型是否被保留。"""

    def __init__(
        self,
        error: LLMError | None = None,
        *,
        name: str = "failing",
        model: str = "failing-llm-v1",
    ) -> None:
        self._error = error or LLMError(
            "模拟传输失败", error_type=ErrorType.LLM_TIMEOUT
        )
        self._name = name
        self._model = model
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def model_name(self) -> str:
        return self._model

    async def complete(
        self,
        content: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
    ) -> str:
        self.calls += 1
        raise self._error


__all__ = ["ScriptedProvider", "TransportFailureProvider"]
