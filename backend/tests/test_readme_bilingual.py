"""两份 README 不许漂移。

仓库是双语的：`README.md`（中文）与 `README.en.md`（英文）。
两份文件最大的风险不是翻译质量，而是**改了一边忘了另一边** ——
数字过期、章节缺失、互相不链。这种错不会让任何人报错，只会让读者看到两份不一致的说明。

所以这里把「必须完全一致」的部分钉成检查：结构（章节数、图片数、代码块数）、
关键数字（用例数、覆盖率）、以及互相链接。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ZH = REPO_ROOT / "README.md"
EN = REPO_ROOT / "README.en.md"


@pytest.fixture(scope="module")
def readmes() -> tuple[str, str]:
    if not (ZH.is_file() and EN.is_file()):
        pytest.skip("不是双语仓库布局")
    return ZH.read_text(encoding="utf-8"), EN.read_text(encoding="utf-8")


def _count(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text, re.MULTILINE))


def test_each_readme_links_to_the_other(readmes: tuple[str, str]) -> None:
    zh, en = readmes
    assert "README.en.md" in zh, "中文 README 顶部要有 English 链接"
    assert re.search(r"\[.*?\]\(README\.md\)", en), "英文 README 顶部要有中文链接"


def test_section_counts_match(readmes: tuple[str, str]) -> None:
    """章节结构必须一致 —— 少一节说明翻译漏了整段。"""
    zh, en = readmes
    for level in ("##", "###"):
        zh_n = _count(zh, rf"^{level} ")
        en_n = _count(en, rf"^{level} ")
        assert zh_n == en_n, f"{level} 级章节数不一致：中文 {zh_n} vs 英文 {en_n}"


def test_image_and_code_block_counts_match(readmes: tuple[str, str]) -> None:
    zh, en = readmes
    assert _count(zh, r"!\[.*?\]\(") == _count(en, r"!\[.*?\]\("), "图片数不一致"
    assert _count(zh, r"<img ") == _count(en, r"<img "), "<img> 标签数不一致"
    assert zh.count("```") == en.count("```"), "代码块数量不一致"


def test_tables_have_the_same_shape(readmes: tuple[str, str]) -> None:
    """表格行数一致：表格最容易只改一边。"""
    zh, en = readmes
    zh_rows = _count(zh, r"^\|")
    en_rows = _count(en, r"^\|")
    assert zh_rows == en_rows, f"表格行数不一致：中文 {zh_rows} vs 英文 {en_rows}"


def test_test_counts_agree_across_all_docs(readmes: tuple[str, str]) -> None:
    """两份 README 里的用例数必须一模一样，且**不写死**在当前值上。

    刻意从文档里正则抽数字再互相比 —— 写死具体数字的话，每加一个测试这个用例自己就过期了
    （这个教训在 `test_docs_consistency.py` 里已经吃过一次）。
    """
    zh, en = readmes
    pattern = r"(\d+) passed / (\d+) skipped"
    zh_hits = set(re.findall(pattern, zh))
    en_hits = set(re.findall(pattern, en))
    assert zh_hits, "中文 README 里找不到「N passed / M skipped」"
    assert zh_hits == en_hits, f"两份 README 的用例数不一致：中文 {zh_hits} vs 英文 {en_hits}"


def test_english_readme_is_actually_english(readmes: tuple[str, str]) -> None:
    """英文版不能是大段中文复制过来的。"""
    _, en = readmes
    cjk = len(re.findall(r"[\u4e00-\u9fff]", en))
    total = len(en)
    assert cjk / total < 0.02, f"英文 README 里中文字符占比过高（{cjk}/{total}）"


def test_no_placeholder_text_left_behind(readmes: tuple[str, str]) -> None:
    for name, text in zip(("README.md", "README.en.md"), readmes):
        for marker in ("TODO", "TBD", "FIXME", "XXX"):
            assert marker not in text, f"{name} 里还留着 {marker}"
