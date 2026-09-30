#!/usr/bin/env python
"""覆盖率下限守卫：`python scripts/check_coverage.py`

行覆盖率低于下限就退出非 0，用来防「新增代码没人测」的静默退化。

两个刻意的设计：

1. **沙箱环境自动跳过**（退出码 0）。受限沙箱里一部分 tmp / symlink 相关用例
   起不来，覆盖率会掉到离谱的数字（实测见过「pipeline 24%」，真实是 97%）。
   与其报一个假红，不如明确跳过并说明原因。
2. **默认排除真实引擎包装层**（``app/capabilities/whisper.py`` / ``tesseract.py``）。
   它们默认 skip（首次加载模型要联网，定稿第二条原则禁止测试依赖网络），
   算进分母会让下限失去意义。想连它们一起测就先开 ``KNOWLEDGEFLOW_REAL_*``。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MIN_COVERAGE = 90.0

#: 真实引擎包装层（默认 skip，见上）。
DEFAULT_OMIT = ("app/capabilities/whisper.py", "app/capabilities/tesseract.py")

TOTAL_LINE_PREFIX = "TOTAL"


def coverage_available() -> bool:
    try:
        import coverage  # noqa: F401
    except ImportError:
        return False
    return True


def run_coverage(omit: tuple[str, ...]) -> float | None:
    """跑一遍带覆盖率的测试，返回 TOTAL 行的百分比。失败返回 ``None``。"""
    omit_args: list[str] = []
    for pattern in omit:
        omit_args += ["--omit", pattern]

    measured = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q", "--tb=no"],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT)},
    )
    if measured.returncode != 0:
        print("[FAIL] 测试没跑全，覆盖率没有意义：")
        print(measured.stdout[-2000:])
        return None

    report = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--include=app/*", *omit_args],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    for line in report.stdout.splitlines():
        if line.startswith(TOTAL_LINE_PREFIX):
            # 形如：TOTAL   4247    148    97%
            return float(line.rstrip("%").split()[-1])
    print("[FAIL] 解析不出覆盖率 TOTAL 行：")
    print(report.stdout[-2000:])
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="行覆盖率下限守卫")
    parser.add_argument(
        "--min", type=float, default=DEFAULT_MIN_COVERAGE, help=f"下限百分比（默认 {DEFAULT_MIN_COVERAGE}）"
    )
    parser.add_argument(
        "--include-engines",
        action="store_true",
        help="把 ASR/OCR 真实引擎包装层也算进来（通常不该）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not coverage_available():
        print("[SKIP] 没装 coverage（`pip install coverage`），守卫跳过。")
        return 0
    if os.environ.get("KNOWLEDGEFLOW_SANDBOX") == "1":
        print("[SKIP] 沙箱环境：部分用例起不来，覆盖率不可信，守卫跳过。")
        return 0

    omit: tuple[str, ...] = () if args.include_engines else DEFAULT_OMIT
    percent = run_coverage(omit)
    if percent is None:
        return 1

    if percent < args.min:
        print(f"[FAIL] 行覆盖率 {percent}% < 下限 {args.min}%")
        return 1
    print(f"[OK] 行覆盖率 {percent}% >= 下限 {args.min}%")
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
