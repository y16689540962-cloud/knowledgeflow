"""fixture 加载器（定稿文档第十五节）。

8 个 fixture 中，5 个 ``llm_analysis_*`` 文件保存的是 **LLM 的原始输出文本**，
所以一律按**纯文本**读取（``load_fixture_text``），再由上层决定是否 ``json.loads``。

理由：``llm_analysis_fenced.json`` 的正文本身带 ``` 围栏、``llm_analysis_preamble.json``
带前后寒暄 —— 它们作为「文件」并不是合法 JSON，这正是它们存在的意义。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from app.errors import FixtureNotFoundError

#: ``app/testing/fixture_loader.py`` → parents[0]=testing, [1]=app, [2]=backend
BACKEND_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
FIXTURES_DIR: Final[Path] = BACKEND_ROOT / "tests" / "fixtures"

#: 第十五节规定的 8 个 fixture。
REQUIRED_FIXTURES: Final[tuple[str, ...]] = (
    "raw_content.json",
    "raw_content_long.json",
    "raw_content_mixed.json",
    "llm_analysis_valid.json",
    "llm_analysis_fenced.json",
    "llm_analysis_preamble.json",
    "llm_analysis_invalid.json",
    "llm_analysis_grounding_fail.json",
)

#: 内容为「RawContent 对象」的 fixture。
RAW_CONTENT_FIXTURES: Final[tuple[str, ...]] = (
    "raw_content.json",
    "raw_content_long.json",
    "raw_content_mixed.json",
)

#: 内容为「LLM 原始输出文本」的 fixture。
LLM_OUTPUT_FIXTURES: Final[tuple[str, ...]] = (
    "llm_analysis_valid.json",
    "llm_analysis_fenced.json",
    "llm_analysis_preamble.json",
    "llm_analysis_invalid.json",
    "llm_analysis_grounding_fail.json",
)


def fixtures_dir() -> Path:
    return FIXTURES_DIR


def fixture_path(name: str) -> Path:
    filename = name if name.endswith(".json") else f"{name}.json"
    return FIXTURES_DIR / filename


def fixture_exists(name: str) -> bool:
    return fixture_path(name).is_file()


def list_fixtures() -> list[str]:
    """返回 fixtures 目录下实际存在的 ``*.json`` 文件名（已排序）。"""
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(p.name for p in FIXTURES_DIR.glob("*.json"))


def missing_fixtures() -> list[str]:
    return [name for name in REQUIRED_FIXTURES if not fixture_exists(name)]


def load_fixture_text(name: str) -> str:
    """按纯文本读取 fixture（不解析 JSON）。"""
    path = fixture_path(name)
    if not path.is_file():
        raise FixtureNotFoundError(f"fixture 不存在：{path}", context={"path": str(path)})
    return path.read_text(encoding="utf-8")


def load_fixture_json(name: str) -> Any:
    """读取并解析 fixture。解析失败时抛 ``json.JSONDecodeError``（这是有意义的信号）。"""
    return json.loads(load_fixture_text(name))


__all__ = [
    "BACKEND_ROOT",
    "FIXTURES_DIR",
    "REQUIRED_FIXTURES",
    "RAW_CONTENT_FIXTURES",
    "LLM_OUTPUT_FIXTURES",
    "fixtures_dir",
    "fixture_path",
    "fixture_exists",
    "list_fixtures",
    "missing_fixtures",
    "load_fixture_text",
    "load_fixture_json",
]
