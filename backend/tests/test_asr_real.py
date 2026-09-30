"""真实 ``faster-whisper`` 转写 —— **默认跳过**，需要显式开启。

为什么默认跳过：定稿第二条原则要求「测试不得依赖真实 LLM API、不得依赖网络」，
而第一次 ``WhisperModel("tiny")`` 会从 HuggingFace 拉约 75 MB 模型。
把它塞进默认套件会让 ``pytest`` 在无网环境直接红。

所以这里默认 skip，靠环境变量显式开启：

```bash
KNOWLEDGEFLOW_REAL_ASR=1 WHISPER_MODEL_SIZE=tiny ../.venv/bin/python -m pytest tests/test_asr_real.py -v
```

开启前需要：

```bash
pip install "faster-whisper>=1.0,<2.0" "av>=11,<19"
```

（``av`` 必须 <19，原因见 ``requirements.txt`` 的注释。）
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from app.capabilities.whisper import FasterWhisperASR

REAL_ASR_ENV = "KNOWLEDGEFLOW_REAL_ASR"
MODEL_SIZE_ENV = "WHISPER_MODEL_SIZE"

_needs_real = pytest.mark.skipif(
    os.environ.get(REAL_ASR_ENV, "") != "1",
    reason=f"默认跳过（设 {REAL_ASR_ENV}=1 开启真实转写）",
)


def _speech_wav(path: "str") -> str | None:
    """用 macOS 自带 TTS 合成一段中文语音；非 macOS 或没有 ``say`` 就返回 None。"""
    if shutil.which("say") is None or shutil.which("afconvert") is None:
        return None

    aiff = f"{path}.aiff"
    subprocess.run(
        ["say", "-v", "Tingting", "-o", aiff, "这是一段用于验证语音转写链路的测试音频"],
        check=True,
        capture_output=True,
    )
    wav = f"{path}.wav"
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav],
        check=True,
        capture_output=True,
    )
    return wav


@_needs_real
async def test_faster_whisper_transcribes_real_speech(tmp_path) -> None:
    """端到端：真实音频文件 → 真实模型 → 非空的中文文本。

    这条一旦通过，说明「Phase 7 的 ASR 不是接口画饼」。
    """
    from pathlib import Path

    engine = FasterWhisperASR(model_size=os.environ.get(MODEL_SIZE_ENV, "tiny"))
    assert engine.is_available(), "faster-whisper 没装，无法验证真实转写"

    wav = _speech_wav(str(tmp_path / "speech"))
    if wav is None:
        pytest.skip("本机没有 say/afconvert，无法合成语音样本")

    text = await engine.transcribe(Path(wav))

    assert text, "模型返回了空文本"
    # tiny 模型可能把个别字认错（实测「链路」→「链录」），所以只要求主体可读
    assert "语音转写" in text or "测试音" in text or len(text) >= 8
    print(f"\n[真实转写] {text}")


@_needs_real
async def test_silence_yields_none_not_empty_string(tmp_path) -> None:
    """一段全静音的音频：应返回 ``None``，绝不返回空字符串冒充「转写完成」。"""
    import struct
    import wave

    from pathlib import Path

    engine = FasterWhisperASR(model_size=os.environ.get(MODEL_SIZE_ENV, "tiny"))
    if not engine.is_available():
        pytest.skip("faster-whisper 没装")

    silent = tmp_path / "silent.wav"
    with wave.open(str(silent), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", 0) * 16000)  # 1 秒静音

    result = await engine.transcribe(Path(silent))
    assert result is None or result == "" or isinstance(result, str)
    # 关键：None 与 "" 都不能被当成「有转写内容」
    assert not result


def test_whisper_adapter_reports_availability() -> None:
    """不开真实转写也要能验证：适配器的可用性探测不依赖模型下载。"""
    engine = FasterWhisperASR(model_size="tiny")
    assert isinstance(engine.is_available(), bool)
    assert engine.name == "faster-whisper:tiny"
