"""Tesseract OCR provider（真实引擎，**懒加载**）。

和 :mod:`app.capabilities.whisper` 一样的两条约定：

* 依赖**用时才 import**（第二十七节：不想让 OCR 依赖变成前置要求）
* ``is_available()`` 要检查**两个**东西：Python 包 ``pytesseract`` **和**
  系统上的 ``tesseract`` 可执行文件 —— 只装包没装二进制是 macOS 上最常见的坑，
  这时报「可用」只会在真正调用时崩。

语言默认是 ``chi_sim+eng``：只装了英文语言包时 tesseract 会直接报错，
调用方要么指定 lang，要么依赖慢路径降级。
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
from pathlib import Path

from app.errors import CapabilityFailedError

PACKAGE = "pytesseract"
EXECUTABLE = "tesseract"
DEFAULT_LANG = "chi_sim+eng"


class TesseractOCR:
    """用 Tesseract 从图片里读文字。"""

    def __init__(self, lang: str = DEFAULT_LANG) -> None:
        self._lang = lang

    @property
    def name(self) -> str:
        return f"tesseract:{self._lang}"

    def is_available(self) -> bool:
        return importlib.util.find_spec(PACKAGE) is not None and shutil.which(EXECUTABLE) is not None

    async def extract_text(self, path: Path) -> str | None:
        return await asyncio.to_thread(self._run, path)

    def _run(self, path: Path) -> str | None:
        try:
            from PIL import Image  # noqa: PLC0415 - 懒加载：不进前置依赖

            pytesseract = __import__(PACKAGE, fromlist=["image_to_string"])

            with Image.open(path) as image:
                text = pytesseract.image_to_string(image, lang=self._lang)
        except CapabilityFailedError:
            raise
        except Exception as exc:
            raise CapabilityFailedError(
                f"Tesseract 识别失败：{type(exc).__name__}",
                context={"provider": self.name, "path": str(path)},
            ) from exc

        stripped = (text or "").strip()
        # 「图里没字」返回 None，不返回可能有空白的字符串
        return stripped or None


__all__ = ["DEFAULT_LANG", "EXECUTABLE", "PACKAGE", "TesseractOCR"]
