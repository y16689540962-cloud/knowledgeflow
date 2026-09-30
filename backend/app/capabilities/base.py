"""ASR / OCR 的可插拔协议。

定稿第二十六节把 Phase 7 描述为「**独立可插拔**」，含义拆开是三条：

1. **接口与实现分离。** Core Pipeline 只认 ``RawContent`` 上的
   ``transcript`` / ``ocr_text``，不认 Whisper、不认 PaddleOCR。
2. **缺依赖不阻塞。** 第二条原则：不因为 ASR/OCR 环境问题阻塞 Core Pipeline。
   所以每个 provider 都要先回答 :meth:`is_available`，没装就降级。
3. **真实引擎懒加载。** 第二十七节明确不想让 GPU / FFmpeg / PaddleOCR
   变成前置依赖，所以 import 一律**发生在使用时**，不在模块加载时。

为什么是 async：转写一张图可能几秒、一段视频可能几分钟。
同步接口会把整个事件循环堵死（将来接了 FastAPI 就是整个服务不可用）。
底层引擎本身是同步的，实现方用 ``asyncio.to_thread`` 包一层即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from typing import Protocol, runtime_checkable

#: 一次能力调用的结果状态。
CapabilityStatus = str  # "ok" | "skipped" | "unavailable" | "failed"


@dataclass(frozen=True)
class CapabilityAttempt:
    """一次能力调用的可追溯记录。

    失败必须留下证据：``error_type`` 落这条 struct log / 报告，
    绝不用空字符串冒充「转写完成但没有内容」。
    """

    capability: str  # "asr" | "ocr"
    provider: str
    status: CapabilityStatus
    text_length: int = 0
    elapsed_ms: float | None = None
    error_type: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "provider": self.provider,
            "status": self.status,
            "text_length": self.text_length,
            "elapsed_ms": self.elapsed_ms,
            "error_type": self.error_type,
            "message": self.message,
        }


@runtime_checkable
class ASRProvider(Protocol):
    """音频 / 视频 → ``transcript``。"""

    @property
    def name(self) -> str: ...

    def is_available(self) -> bool:
        """依赖是否真的装好了。``False`` 时调用方应当**跳过**而不是报错。"""
        ...

    async def transcribe(self, path: Path) -> str | None:
        """返回转写文本；「听不出内容」返回 ``None``（而不是空字符串）。"""
        ...


@runtime_checkable
class OCRProvider(Protocol):
    """图片 → ``ocr_text``。"""

    @property
    def name(self) -> str: ...

    def is_available(self) -> bool:
        """依赖是否真的装好了。"""
        ...

    async def extract_text(self, path: Path) -> str | None:
        """返回识别文本；「认不出内容」返回 ``None``。"""
        ...


__all__ = [
    "ASRProvider",
    "CapabilityAttempt",
    "CapabilityStatus",
    "OCRProvider",
]
