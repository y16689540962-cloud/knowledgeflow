"""Vault 目录布局（第二十节）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.errors import PathTraversalError
from app.obsidian.layout import (
    NOTE_EXTENSION,
    SUBDIRS,
    VAULT_NAMESPACE,
    ensure_layout,
    namespace_path,
    note_kinds,
    note_relative_path,
    with_filename_suffix,
)


def test_namespace_and_subdirs_match_spec() -> None:
    assert VAULT_NAMESPACE == "KnowledgeFlow"
    assert SUBDIRS == ("Inbox", "Processed", "Failed", "Assets", "Templates")


def test_note_kinds() -> None:
    assert set(note_kinds()) == {"inbox", "processed", "failed"}


def test_note_extension() -> None:
    assert NOTE_EXTENSION == ".md"


def test_namespace_path() -> None:
    assert namespace_path() == "KnowledgeFlow"
    # 分隔符**固定是 ``/``**，不用 os.path.join —— 这是 vault 内的逻辑路径，
    # 要进数据库 / 界面 / 双链，必须在所有平台上都是同一个字符串。
    assert namespace_path("Processed") == "KnowledgeFlow/Processed"


@pytest.mark.parametrize(
    ("kind", "subdir"),
    [("inbox", "Inbox"), ("processed", "Processed"), ("failed", "Failed")],
)
def test_note_relative_path(kind: str, subdir: str) -> None:
    assert note_relative_path(kind, "标题") == f"KnowledgeFlow/{subdir}/标题.md"


def test_note_relative_path_does_not_double_extension() -> None:
    assert note_relative_path("processed", "标题.md").endswith("标题.md")
    assert not note_relative_path("processed", "标题.md").endswith(".md.md")


def test_note_relative_path_unknown_kind() -> None:
    with pytest.raises(ValueError):
        note_relative_path("archive", "标题")  # type: ignore[arg-type]


def test_with_filename_suffix() -> None:
    assert (
        with_filename_suffix("KnowledgeFlow/Processed/标题.md", "abc123")
        == "KnowledgeFlow/Processed/标题-abc123.md"
    )


def test_with_filename_suffix_normalizes_native_separators() -> None:
    """调用方递本机风格的路径时也要归一成 ``/``，不能产出半半拉拉的混合路径。

    Windows 上这条曾经会返回 ``KnowledgeFlow/Processed\\标题-abc.md``
    —— ``os.path.split`` 认 ``\\`` 而 ``os.path.join`` 又补一个 ``\\``。
    """
    native = os.path.join("KnowledgeFlow", "Processed", "标题.md")
    assert with_filename_suffix(native, "abc") == "KnowledgeFlow/Processed/标题-abc.md"


def test_with_filename_suffix_without_directory() -> None:
    assert with_filename_suffix("标题.md", "abc") == "标题-abc.md"


# --------------------------------------------------------------------------- #
# ensure_layout
# --------------------------------------------------------------------------- #
def test_ensure_layout_creates_namespace_and_subdirs(vault_root: Path) -> None:
    created = ensure_layout(vault_root)
    assert len(created) == 1 + len(SUBDIRS)
    assert (vault_root / VAULT_NAMESPACE).is_dir()
    for subdir in SUBDIRS:
        assert (vault_root / VAULT_NAMESPACE / subdir).is_dir()


def test_ensure_layout_is_idempotent(vault_root: Path) -> None:
    first = ensure_layout(vault_root)
    second = ensure_layout(vault_root)
    assert first == second


def test_ensure_layout_returns_absolute_paths(vault_root: Path) -> None:
    assert all(path.is_absolute() for path in ensure_layout(vault_root))


def test_ensure_layout_does_not_create_extra_files(vault_root: Path) -> None:
    ensure_layout(vault_root)
    files = [path for path in (vault_root / VAULT_NAMESPACE).rglob("*") if path.is_file()]
    assert files == []


def test_namespace_symlink_escape_is_blocked(
    tmp_path: Path, requires_symlinks: None
) -> None:
    """KnowledgeFlow 若是个指向 vault 外部的软链，必须拒绝。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / VAULT_NAMESPACE).symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathTraversalError):
        ensure_layout(vault)


def test_missing_vault_root_is_created(tmp_path: Path) -> None:
    root = tmp_path / "not-yet"
    created = ensure_layout(root)
    assert root.is_dir()
    assert created[0].is_dir()
