"""``faster-whisper`` ASR provider（真实引擎，**懒加载**）。

为什么要 lazy import：第二十七节明确不想让 Whisper / GPU / FFmpeg 变成
前置依赖 —— 一个只想跑 Core Pipeline 的人不该被迫下载几 GB 模型。
所以这里在模块加载时**什么都不 import**，只在使用时才动态加载。

三条约定：

* ``is_available()`` 用 :func:`importlib.util.find_spec` 探测，不真的 import
  （真 import 一次要几百毫秒，而且会拖进来重依赖）。
* 模型实例在对象上缓存：同一个 provider 实例只用加载一次模型，
  第二条内容转写不必再等一遍模型加载。
* 底层是同步 API，用 :func:`asyncio.to_thread` 包一层，避免堵死事件循环。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path
from typing import Any

from app.errors import CapabilityFailedError

LOGGER = logging.getLogger(__name__)

PACKAGE = "faster_whisper"
DEFAULT_MODEL_SIZE = "small"
DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_DEVICE = "cpu"

#: 常见的中文 / 英文 candidate 语言；留 None 让模型自己判断语种。
DEFAULT_LANGUAGE: str | None = None


class FasterWhisperASR:
    """用 ``faster-whisper`` 把音频 / 视频转成文字。"""

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        *,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
        language: str | None = DEFAULT_LANGUAGE,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model: Any | None = None

    @property
    def name(self) -> str:
        return f"faster-whisper:{self._model_size}"

    def is_available(self) -> bool:
        return importlib.util.find_spec(PACKAGE) is not None

    def _load_model(self) -> Any:
        if self._model is None:
            module = __import__(PACKAGE, fromlist=["WhisperModel"])
            self._model = module.WhisperModel(
                self._model_size, device=self._device, compute_type=self._compute_type
            )
        return self._model

    async def transcribe(self, path: Path) -> str | None:
        return await asyncio.to_thread(self._run, path)

    def _run(self, path: Path) -> str | None:
        try:
            model = self._load_model()
            segments, _info = model.transcribe(
                str(path), language=self._language, vad_filter=True
            )
            text = "\n".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
        except CapabilityFailedError:
            raise
        except Exception as exc:  # 引擎抛什么都别漏出去，统一成明确 error_type
            raise CapabilityFailedError(
                f"faster-whisper 转写失败：{type(exc).__name__}",
                context={
                    "provider": self.name,
                    "path": str(path),
                    # 不记录任何正文内容 —— 第二十四节脱敏要求
                },
            ) from exc
        # 「听不出东西」返回 None，不返回空字符串（空串会被下游当成有值）
        return text or None


__all__ = [
    "DEFAULT_COMPUTE_TYPE",
    "DEFAULT_DEVICE",
    "DEFAULT_LANGUAGE",
    "DEFAULT_MODEL_SIZE",
    "FasterWhisperASR",
    "PACKAGE",
]
