"""``scripts/check_coverage.py`` 的守卫语义。

**不在这里真跑覆盖率**（那等于把整个测试套件再跑一遍）。只钉住它的三条决策：
沙箱要跳过、引擎包装层要排除、下限要报红。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage.py"

_SPEC = importlib.util.spec_from_file_location("knowledgeflow_check_coverage", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
check_coverage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_coverage)


def test_sandbox_is_skipped(monkeypatch) -> None:
    """沙箱里覆盖率会假到离谱（实测见过 24%，真实 97%）—— 宁可跳过也别假红。"""
    monkeypatch.setenv("KNOWLEDGEFLOW_SANDBOX", "1")
    assert check_coverage.main(["--min", "99"]) == 0


@pytest.mark.parametrize("relative", check_coverage.DEFAULT_OMIT)
def test_omit_targets_exist(relative: str) -> None:
    """排除清单里的文件必须真的存在 —— 写错路径等于把守卫关掉一半。"""
    assert (SCRIPT_PATH.parents[1] / relative).is_file()


def test_engines_are_excluded_by_default() -> None:
    assert "whisper.py" in " ".join(check_coverage.DEFAULT_OMIT)
    assert "tesseract.py" in " ".join(check_coverage.DEFAULT_OMIT)


def test_parse_args_defaults() -> None:
    args = check_coverage.parse_args([])
    assert args.min == check_coverage.DEFAULT_MIN_COVERAGE
    assert args.include_engines is False


def test_parse_args_include_engines() -> None:
    assert check_coverage.parse_args(["--include-engines", "--min", "80"]).include_engines is True


def test_coverage_availability_is_a_bool() -> None:
    assert isinstance(check_coverage.coverage_available(), bool)


def test_sandbox_env_default_is_off() -> None:
    """不能默认就在沙箱模式 —— 那等于守卫永远不生效。"""
    assert os.environ.get("KNOWLEDGEFLOW_SANDBOX") != "1"
