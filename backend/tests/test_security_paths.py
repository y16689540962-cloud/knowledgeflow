"""``ensure_within_vault`` 路径安全（第十九节，强制）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.errors import ErrorType, PathTraversalError
from app.security import ensure_within_vault, real_vault_root


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "KnowledgeFlow" / "Processed").mkdir(parents=True)
    return root


def test_normal_relative_path_allowed(vault: Path) -> None:
    target = ensure_within_vault(vault, "KnowledgeFlow/Processed/a.md")
    assert target.is_relative_to(vault.resolve())
    assert target.name == "a.md"


def test_returned_path_is_absolute(vault: Path) -> None:
    assert ensure_within_vault(vault, "x.md").is_absolute()


@pytest.mark.parametrize(
    "relative",
    [
        "../evil.md",
        "../../evil.md",
        "KnowledgeFlow/../../evil.md",
        "KnowledgeFlow/Processed/../../../evil.md",
        "..",
        "./../evil.md",
    ],
)
def test_traversal_blocked(vault: Path, relative: str) -> None:
    with pytest.raises(PathTraversalError) as excinfo:
        ensure_within_vault(vault, relative)
    assert excinfo.value.error_type is ErrorType.PATH_TRAVERSAL_DETECTED


@pytest.mark.parametrize("relative", ["/etc/passwd", "/tmp/evil.md", "~/evil.md"])
def test_absolute_path_blocked(vault: Path, relative: str) -> None:
    """``os.path.join(root, abs)`` 会丢弃 root —— 必须显式拦掉。"""
    with pytest.raises(PathTraversalError):
        ensure_within_vault(vault, relative)


def test_vault_root_itself_is_not_a_valid_target(vault: Path) -> None:
    with pytest.raises(PathTraversalError):
        ensure_within_vault(vault, "")


def test_sibling_directory_with_shared_prefix_blocked(tmp_path: Path) -> None:
    """``/x/vault-evil`` 不能因为字符串前缀相同而被放行。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(PathTraversalError):
        ensure_within_vault(vault, "../vault-evil/a.md")


def test_symlink_escape_blocked(tmp_path: Path, requires_symlinks: None) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathTraversalError):
        ensure_within_vault(vault, "link/evil.md")


def test_symlink_inside_vault_allowed(tmp_path: Path, requires_symlinks: None) -> None:
    vault = tmp_path / "vault"
    (vault / "real").mkdir(parents=True)
    (vault / "alias").symlink_to(vault / "real", target_is_directory=True)

    target = ensure_within_vault(vault, "alias/ok.md")
    assert target.name == "ok.md"
    assert str(target).startswith(real_vault_root(vault) + os.sep)


def test_error_context_contains_paths(vault: Path) -> None:
    with pytest.raises(PathTraversalError) as excinfo:
        ensure_within_vault(vault, "../evil.md")
    assert excinfo.value.context["relative"] == "../evil.md"
    assert "target" in excinfo.value.context


def test_accepts_str_and_path(vault: Path) -> None:
    assert ensure_within_vault(str(vault), Path("a/b.md")).name == "b.md"
