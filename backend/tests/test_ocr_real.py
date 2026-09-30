"""真实 Tesseract OCR —— **默认跳过**，需要显式开启。

和 :mod:`tests.test_asr_real` 同理：OCR 依赖**两个**东西
（Python 包 ``pytesseract`` + 系统二进制 ``tesseract``），
塞进默认套件会让没装的人直接红，所以默认 skip。

```bash
KNOWLEDGEFLOW_REAL_OCR=1 ../.venv/bin/python -m pytest tests/test_ocr_real.py -v
```

开启前需要：

```bash
pip install pytesseract pillow
brew install tesseract tesseract-lang   # 中文识别必须装 tesseract-lang
```

``brew install tesseract`` 只给 eng/osd 语言包，中文会直接报错 ——
``is_available()`` 只看二进制在不在，**不判断语言包**，这是个已知的边界。
"""

from __future__ import annotations

import glob
import os

import pytest

from app.capabilities.tesseract import TesseractOCR

REAL_OCR_ENV = "KNOWLEDGEFLOW_REAL_OCR"
TEXT = "知识流动 第七阶段 图文识别"

_needs_real = pytest.mark.skipif(
    os.environ.get(REAL_OCR_ENV, "") != "1",
    reason=f"默认跳过（设 {REAL_OCR_ENV}=1 开启真实 OCR）",
)


def _cjk_font() -> str | None:
    candidates = glob.glob("/System/Library/Fonts/*Hei*") + glob.glob(
        "/System/Library/Fonts/Hiragino*"
    )
    return candidates[0] if candidates else None


@_needs_real
async def test_tesseract_reads_chinese(tmp_path) -> None:
    """端到端：造一张中文图片 → 真实识别 → 正确的文字。"""
    from pathlib import Path

    pytest.importorskip("PIL", reason="没装 Pillow，无法造测试图片")
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    font_path = _cjk_font()
    if font_path is None:
        pytest.skip("本机没有可用的中文字体")

    image = Image.new("RGB", (760, 160), "white")
    ImageDraw.Draw(image).text(
        (20, 50), TEXT, fill="black", font=ImageFont.truetype(font_path, 44)
    )
    target = Path(tmp_path) / "chinese.png"
    image.save(target)

    engine = TesseractOCR(lang="chi_sim")
    assert engine.is_available(), "tesseract 不可用（缺二进制或 pytesseract）"

    text = await engine.extract_text(target)
    assert text, "识别结果为空"
    print(f"\n[真实 OCR] {text}")
    assert "知识" in text or "识别" in text


@_needs_real
async def test_blank_image_yields_none(tmp_path) -> None:
    """一张纯白图片：应返回 ``None``，不是空字符串。"""
    from pathlib import Path

    pytest.importorskip("PIL", reason="没装 Pillow")
    from PIL import Image  # noqa: PLC0415

    blank = Path(tmp_path) / "blank.png"
    Image.new("RGB", (200, 80), "white").save(blank)

    result = await TesseractOCR(lang="eng").extract_text(Path(blank))
    assert not result


def test_tesseract_adapter_reports_availability() -> None:
    """不开真实 OCR 也要能验证：可用性探测不依赖识别。"""
    engine = TesseractOCR()
    assert isinstance(engine.is_available(), bool)
    assert engine.name == "tesseract:chi_sim+eng"


def test_availability_requires_both_package_and_binary() -> None:
    """只装 Python 包而没装 tesseract 二进制时，必须报**不可用**。

    macOS 上这是最常见的坑：``pip install pytesseract`` 之后
    ``import pytesseract`` 是成功的，直到真正调用才崩。
    """
    import importlib.util
    import shutil

    has_package = importlib.util.find_spec("pytesseract") is not None
    has_binary = shutil.which("tesseract") is not None
    assert TesseractOCR().is_available() == (has_package and has_binary)
