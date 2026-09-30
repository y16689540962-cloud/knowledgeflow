"""可选能力出口（定稿第二十六节 Phase 7：ASR / OCR，**独立可插拔**）。

用法（默认就是「什么都不装」的降级配置）：

```python
service = MediaCapabilityService()
raw2, attempts = await service.enrich(raw, media_url=video_url)
```

``attempts`` 是这次调用的流水，**每一次失败或跳过都会留下 ``error_type`` 和原因**。
这是「降级」与「假装成功」的分界线 —— 两者都返回能用的 ``RawContent``，
但只有前者说得清自己到底做了什么。

真实引擎：

* ASR → :class:`app.capabilities.whisper.FasterWhisperASR`
* OCR → :class:`app.capabilities.tesseract.TesseractOCR`

两者都是**懒加载**：没装就 ``is_available() == False``，调用自动跳过，
绝不因为环境问题阻塞 Core Pipeline（定稿第二条原则）。
"""

from __future__ import annotations

from app.capabilities.base import (
    ASRProvider,
    CapabilityAttempt,
    CapabilityStatus,
    OCRProvider,
)
from app.capabilities.null import NULL_PROVIDER_NAME, NullASRProvider, NullOCRProvider
from app.capabilities.service import ASR_MEDIA, OCR_MEDIA, MediaCapabilityService

__all__ = [
    "ASR_MEDIA",
    "ASRProvider",
    "CapabilityAttempt",
    "CapabilityStatus",
    "MediaCapabilityService",
    "NULL_PROVIDER_NAME",
    "NullASRProvider",
    "NullOCRProvider",
    "OCR_MEDIA",
    "OCRProvider",
]
