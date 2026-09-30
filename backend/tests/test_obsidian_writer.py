"""Obsidian Writer（第十八 / 十九 / 二十节）。"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.errors import PathTraversalError
from app.obsidian.layout import SUBDIRS, VAULT_NAMESPACE
from app.obsidian.renderer import NoteContext, render_note
from app.obsidian.writer import ObsidianWriter
from app.obsidian.frontmatter import parse_frontmatter

IDENTITY = ("manual", "hash:0123456789abcdef")


def write(writer: ObsidianWriter, context: NoteContext, **kwargs):
    """按 context 自身的身份写入（消歧测试依赖它随 context 变化）。"""
    note = render_note(context)
    return writer.write_rendered(
        note, identity=(context.source, context.source_id), **kwargs
    )


# --------------------------------------------------------------------------- #
# 基本写入
# --------------------------------------------------------------------------- #
def test_write_creates_note_in_processed(obsidian_writer: ObsidianWriter, vault_root: Path, note_context) -> None:
    result = write(obsidian_writer, note_context)
    assert result.status == "created"
    assert result.relative_path == os.path.join(
        VAULT_NAMESPACE, "Processed", "人口下降之后，房子还会涨吗.md"
    )
    assert result.absolute_path.is_file()
    assert result.absolute_path.read_text(encoding="utf-8").startswith("---\n")


def test_write_creates_layout(obsidian_writer: ObsidianWriter, vault_root: Path, note_context) -> None:
    write(obsidian_writer, note_context)
    for subdir in SUBDIRS:
        assert (vault_root / VAULT_NAMESPACE / subdir).is_dir()


def test_written_body_matches_render(obsidian_writer: ObsidianWriter, note_context) -> None:
    result = write(obsidian_writer, note_context)
    assert result.absolute_path.read_text(encoding="utf-8") == render_note(note_context).markdown


def test_byte_size_is_reported(obsidian_writer: ObsidianWriter, note_context) -> None:
    result = write(obsidian_writer, note_context)
    assert result.byte_size == result.absolute_path.stat().st_size


def test_no_crlf_in_output(obsidian_writer: ObsidianWriter, note_context) -> None:
    result = write(obsidian_writer, note_context)
    raw = result.absolute_path.read_bytes()
    assert b"\r\n" not in raw


def test_no_temp_files_left_behind(obsidian_writer: ObsidianWriter, vault_root: Path, note_context) -> None:
    write(obsidian_writer, note_context)
    leftovers = [p for p in (vault_root / VAULT_NAMESPACE).rglob("*") if p.suffix == ".tmp"]
    assert leftovers == []


def test_unicode_filename_on_disk(obsidian_writer: ObsidianWriter, note_context) -> None:
    result = write(obsidian_writer, note_context)
    assert "人口下降之后，房子还会涨吗" in str(result.absolute_path)


# --------------------------------------------------------------------------- #
# 幂等与 reprocess
# --------------------------------------------------------------------------- #
def test_second_write_updates_same_file(obsidian_writer: ObsidianWriter, vault_root: Path, note_context) -> None:
    first = write(obsidian_writer, note_context)
    second = write(obsidian_writer, note_context)
    assert first.relative_path == second.relative_path
    assert first.status == "created"
    assert second.status == "updated"
    notes = list((vault_root / VAULT_NAMESPACE / "Processed").glob("*.md"))
    assert len(notes) == 1


def test_reprocess_with_new_analysis_title_keeps_same_file(
    obsidian_writer: ObsidianWriter, vault_root: Path, note_context
) -> None:
    """核心不变量：reprocess 换了 AI 标题，也绝不能产生第二个文件。"""
    write(obsidian_writer, note_context)
    changed_analysis = note_context.analysis.model_copy(update={"title": "另一个 AI 标题"})
    write(obsidian_writer, replace(note_context, analysis=changed_analysis))

    notes = list((vault_root / VAULT_NAMESPACE / "Processed").glob("*.md"))
    assert len(notes) == 1
    assert "# 另一个 AI 标题" in notes[0].read_text(encoding="utf-8")


def test_same_title_different_content_is_disambiguated(
    obsidian_writer: ObsidianWriter, vault_root: Path, note_context
) -> None:
    first = write(obsidian_writer, note_context)
    other = replace(note_context, content_id="aabbccddeeff0011", source_id="hash:other")
    second = write(obsidian_writer, other)

    assert second.disambiguated is True
    assert second.relative_path != first.relative_path
    assert second.relative_path.endswith("-aabbcc.md")
    assert first.absolute_path.is_file()
    assert second.absolute_path.is_file()
    # 第一个文件的身份没有被改写
    assert parse_frontmatter(first.absolute_path.read_text(encoding="utf-8"))["source_id"] == IDENTITY[1]


def test_disambiguation_is_stable_across_rewrites(
    obsidian_writer: ObsidianWriter, vault_root: Path, note_context
) -> None:
    write(obsidian_writer, note_context)
    other = replace(note_context, content_id="aabbccddeeff0011", source_id="hash:other")
    first_other = write(obsidian_writer, other)
    second_other = write(obsidian_writer, other)

    assert first_other.relative_path == second_other.relative_path
    assert second_other.status == "updated"
    notes = list((vault_root / VAULT_NAMESPACE / "Processed").glob("*.md"))
    assert len(notes) == 2


def test_foreign_file_is_never_overwritten(
    obsidian_writer: ObsidianWriter, vault_root: Path, note_context
) -> None:
    """目标路径上有一份不是我们写的文件 → 换名字，绝不覆盖。"""
    processed = vault_root / VAULT_NAMESPACE / "Processed"
    processed.mkdir(parents=True)
    foreign = processed / "人口下降之后，房子还会涨吗.md"
    foreign.write_text("# 用户自己的笔记\n很珍贵的内容\n", encoding="utf-8")

    result = write(obsidian_writer, note_context)
    assert result.disambiguated is True
    assert foreign.read_text(encoding="utf-8") == "# 用户自己的笔记\n很珍贵的内容\n"


def test_write_markdown_requires_identity_for_disambiguation(
    obsidian_writer: ObsidianWriter, vault_root: Path
) -> None:
    """没有身份信息时不做消歧（调用方自己负责），但仍然不报错。"""
    obsidian_writer.ensure_layout()
    rel = os.path.join(VAULT_NAMESPACE, "Processed", "x.md")
    first = obsidian_writer.write_markdown(rel, "一")
    second = obsidian_writer.write_markdown(rel, "二")
    assert first.relative_path == second.relative_path
    assert second.status == "updated"


# --------------------------------------------------------------------------- #
# 冲突策略
# --------------------------------------------------------------------------- #
def test_keep_existing_policy_skips(vault_root: Path, note_context) -> None:
    writer = ObsidianWriter(vault_root, conflict_policy="keep_existing")
    first = write(writer, note_context)
    original = first.absolute_path.read_text(encoding="utf-8")

    changed = replace(note_context, analysis=note_context.analysis.model_copy(update={"title": "新标题"}))
    second = write(writer, changed)
    assert second.status == "skipped"
    assert second.absolute_path.read_text(encoding="utf-8") == original


def test_keep_existing_still_creates_first_time(vault_root: Path, note_context) -> None:
    writer = ObsidianWriter(vault_root, conflict_policy="keep_existing")
    assert write(writer, note_context).status == "created"


def test_unknown_conflict_policy_rejected(vault_root: Path) -> None:
    with pytest.raises(ValueError):
        ObsidianWriter(vault_root, conflict_policy="merge")  # type: ignore[arg-type]


def test_policy_property(vault_root: Path) -> None:
    assert ObsidianWriter(vault_root).conflict_policy == "overwrite"
    assert ObsidianWriter(vault_root, conflict_policy="keep_existing").conflict_policy == "keep_existing"


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "relative",
    [
        "../evil.md",
        "KnowledgeFlow/../../evil.md",
        "KnowledgeFlow/Processed/../../../evil.md",
        "/tmp/evil.md",
    ],
)
def test_traversal_is_rejected(obsidian_writer: ObsidianWriter, relative: str) -> None:
    with pytest.raises(PathTraversalError):
        obsidian_writer.write_markdown(relative, "内容")


def test_absolute_path_helper_rejects_escape(obsidian_writer: ObsidianWriter) -> None:
    with pytest.raises(PathTraversalError):
        obsidian_writer.absolute_path("KnowledgeFlow/../../x.md")


def test_writes_stay_inside_namespace(obsidian_writer: ObsidianWriter, vault_root: Path, note_context) -> None:
    result = write(obsidian_writer, note_context)
    assert str(result.absolute_path).startswith(str(vault_root.resolve() / VAULT_NAMESPACE))


# --------------------------------------------------------------------------- #
# 其它位置
# --------------------------------------------------------------------------- #
def test_failed_note_goes_to_failed_dir(obsidian_writer: ObsidianWriter, note_context) -> None:
    from app.obsidian.renderer import render_failure_note

    note = render_failure_note(note_context, error_type="X", error_message="y")
    result = obsidian_writer.write_rendered(note, kind="failed", identity=IDENTITY)
    assert result.relative_path == os.path.join(
        VAULT_NAMESPACE, "Failed", "人口下降之后，房子还会涨吗.md"
    )


def test_inbox_kind(obsidian_writer: ObsidianWriter, note_context) -> None:
    result = write(obsidian_writer, note_context, kind="inbox")
    assert result.relative_path == os.path.join(
        VAULT_NAMESPACE, "Inbox", "人口下降之后，房子还会涨吗.md"
    )


@pytest.mark.parametrize("kind", ["inbox", "processed", "failed"])
def test_all_kinds_writeable(obsidian_writer: ObsidianWriter, kind: str, note_context) -> None:
    assert write(obsidian_writer, note_context, kind=kind).absolute_path.is_file()


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def test_read_missing_returns_none(obsidian_writer: ObsidianWriter) -> None:
    assert obsidian_writer.read_text("KnowledgeFlow/Processed/nope.md") is None
    assert obsidian_writer.read_identity("KnowledgeFlow/Processed/nope.md") is None


def test_read_identity_returns_written_identity(obsidian_writer: ObsidianWriter, note_context) -> None:
    result = write(obsidian_writer, note_context)
    assert obsidian_writer.read_identity(result.relative_path) == IDENTITY


def test_read_identity_on_foreign_file(obsidian_writer: ObsidianWriter, vault_root: Path) -> None:
    obsidian_writer.ensure_layout()
    rel = os.path.join(VAULT_NAMESPACE, "Processed", "foreign.md")
    (vault_root / rel).write_text("没有 frontmatter 的文件\n", encoding="utf-8")
    assert obsidian_writer.read_identity(rel) is None


def test_vault_root_property(vault_root: Path) -> None:
    assert ObsidianWriter(vault_root).vault_root == vault_root


# --------------------------------------------------------------------------- #
# 不自动建实体空白笔记（第二十一节）
# --------------------------------------------------------------------------- #
def test_no_entity_notes_created(obsidian_writer: ObsidianWriter, vault_root: Path, note_context) -> None:
    write(obsidian_writer, note_context)
    namespace = vault_root / VAULT_NAMESPACE
    all_files = sorted(path.name for path in namespace.rglob("*") if path.is_file())
    assert all_files == ["人口下降之后，房子还会涨吗.md"]
    for entity in note_context.entities:
        assert not (namespace / f"{entity.canonical_name}.md").exists()
