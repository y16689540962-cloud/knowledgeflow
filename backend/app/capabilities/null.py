"""降级 provider：什么也不做，但**如实承认**自己什么也没做。

这是所有 Phase 7 路径的默认实现 —— 「没配 ASR」和「ASR 识别出错」是两件事，
前者不该让内容处理失败（定稿第二条原则）。

注意它返回的是 ``None`` 而不是 ``""``：空字符串会被下游当成「有值」，
``RawContent.source_text()`` 就会多拼一个空行，而
``bool(raw.transcript)`` 的判定也会失真。
"""

from __future__ import annotations

from pathlib import Path

NULL_PROVIDER_NAME = "none"


class NullASRProvider:
    """不转写。存在的意义是让调用方不必到处写 ``if provider is not None``。"""

    @property
    def name(self) -> str:
        return NULL_PROVIDER_NAME

    def is_available(self) -> bool:
        return False

    async def transcribe(self, path: Path) -> None:
        return None


class NullOCRProvider:
    """不识别图片文字。"""

    @property
    def name(self) -> str:
        return NULL_PROVIDER_NAME

    def is_available(self) -> bool:
        return False

    async def extract_text(self, path: Path) -> None:
        return None


__all__ = ["NULL_PROVIDER_NAME", "NullASRProvider", "NullOCRProvider"]
