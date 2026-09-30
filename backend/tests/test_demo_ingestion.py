"""`python scripts/demo_ingestion.py` 必须真的能跑（B 线验收入口）。

只覆盖**零网络**的路径（手动粘贴 / 参数校验 / 域名白名单拒绝），
所有会真的联网的分支都留给 `test_phase6_acceptance.py` 里的 MockTransport。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_ROOT / "scripts" / "demo_ingestion.py"

TIMEOUT_SECONDS = 120


def run_demo(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def test_script_exists() -> None:
    assert SCRIPT.is_file()


def test_no_arguments_is_a_usage_error() -> None:
    """`--url` 与 `--manual-text` 必须二选一（互斥且必填）。"""
    result = run_demo()
    assert result.returncode != 0
    assert "--url" in result.stderr
    assert "--manual-text" in result.stderr


def test_url_and_manual_text_are_mutually_exclusive() -> None:
    result = run_demo("--url", "https://v.douyin.com/x/", "--manual-text", "hi")
    assert result.returncode != 0
    assert "not allowed with argument" in result.stderr


@pytest.mark.parametrize(
    ("title", "text"),
    [
        ("测试标题", "这段是手贴的正文内容。"),
        ("", "只有正文没有标题也不能崩。"),
    ],
)
def test_manual_paste_prints_normalized_identity(title: str, text: str) -> None:
    """手动粘贴是定稿第二十三节的强制降级入口，零网络可用。"""
    args = ["--manual-text", text]
    if title:
        args += ["--manual-title", title]
    result = run_demo(*args)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "=== 手动粘贴 ===" in result.stdout
    assert "source        = manual" in result.stdout
    # source_id 由 normalize 阶段兜底成 hash: 前缀（永不回退成 URL）
    assert "source_id     = （空，交给 normalize 填）" in result.stdout
    assert "归一化 source_id = hash:" in result.stdout
    assert "media_type    = text" in result.stdout


def test_manual_paste_can_run_the_full_a_track(tmp_path: Path) -> None:
    """加 ``--process`` 必须真的把内容跑完并落到指定 vault。"""
    vault = tmp_path / "vault"
    result = run_demo(
        "--manual-title", "端到端手贴",
        "--manual-text", "这段内容会被 Mock LLM 分析并写成笔记。",
        "--process",
        "--vault", str(vault),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "A 线结果：" in result.stdout
    assert re.search(r"outcome\s+= completed", result.stdout), result.stdout
    notes = sorted((vault / "KnowledgeFlow" / "Processed").glob("*.md"))
    assert len(notes) == 1, f"应产出恰好一份笔记，实际 {len(notes)}"


def test_manual_paste_flags_missing_source_id_for_review() -> None:
    """拿不到 source_id 必须记 `SOURCE_ID_RESOLUTION_FAILED` + 需人工复核。"""
    result = run_demo("--manual-text", "没有天然 id 的内容。")
    assert result.returncode == 0
    assert "需人工复核    = True（SOURCE_ID_RESOLUTION_FAILED）" in result.stdout


@pytest.mark.parametrize("blank", ["   ", ""])
def test_blank_input_is_not_silently_successful(blank: str, tmp_path: Path) -> None:
    """空白输入**不在采集层拒绝**，但要一路走到 Pipeline 报 ``EMPTY_SOURCE_TEXT``。

    这是故意的降级分工（`ManualPastePayload.is_empty()` 的契约）：
    采集层不替 Pipeline 做业务判断，只保证「不产出空的成功」。
    """
    result = run_demo("--manual-text", blank)
    assert result.returncode == 0, result.stdout + result.stderr

    vault = tmp_path / "vault"
    processed = run_demo("--manual-text", blank, "--process", "--vault", str(vault))
    assert processed.returncode == 2, processed.stdout + processed.stderr
    assert "EMPTY_SOURCE_TEXT" in processed.stdout
    # 失败内容一个字都不许留在 Processed/ 里
    assert not (vault / "KnowledgeFlow" / "Processed").exists() or not list(
        (vault / "KnowledgeFlow" / "Processed").glob("*.md")
    )


def test_non_whitelisted_host_is_rejected_without_network(tmp_path: Path) -> None:
    """非白名单域名必须**在发请求之前**就被拒绝（`bailout`，不通外网）。"""
    result = run_demo("--url", "https://evil.example.com/video/123", "--timeout", "1")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "error_type=" in result.stdout
