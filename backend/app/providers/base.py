"""LLM 调用层的统一抽象（定稿文档第三节）。

``analyze()`` 是文档规定的对外签名：``async def analyze(self, content: str, schema: dict) -> dict``。

但脏输出（``` 围栏、前后寒暄）**不可能**在这一层被修好 —— 那不是 Adapter 的职责。
所以本层额外提供 ``complete()``：返回**模型原始文本**，交给
``app.llm.service`` 做抽取 / 修复 / 校验 / 重试。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    """所有 LLM 后端的统一适配器。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """提供方标识，例如 ``mock`` / ``openai`` / ``deepseek``。"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """实际请求的模型名，会写入 ``analyses.model``。"""

    @abstractmethod
    async def complete(
        self,
        content: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
    ) -> str:
        """发起一次调用，返回**未经清洗**的模型原始文本。

        失败时抛 :class:`app.errors.LLMError` 子类，并带明确 ``error_type``。
        """

    async def analyze(self, content: str, schema: dict[str, Any]) -> dict[str, Any]:
        """文档规定的便捷接口：把原始文本直接 ``json.loads`` 成 dict。

        原始文本不是合法 JSON 时抛 ``json.JSONDecodeError``
        —— 修复是服务层的事，这里不做任何猜测。
        """
        raw = await self.complete(content, schema)
        return json.loads(raw)


__all__ = ["LLMProvider"]
