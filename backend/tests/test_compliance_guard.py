"""``scripts/check_compliance_guard.py`` 的负向变异测试。

守卫的价值全在「它会不会红」。七条断言里任何一条只要变成**永远绿**，就等于不存在 ——
而它保护的恰恰是「破坏后不会让任何功能测试失败」的那类边界（CORS 放行通配、
扩展申请越权、免责声明被删……）。

所以这里给每条断言喂一个**故意违规的仓库副本**，断言守卫真的报红。
副本用真文件（README / COMPLIANCE.md / 扩展清单）搭出来，只改坏一处，其余不变 ——
这样报红原因才唯一，不会出「因为别的东西缺失所以失败」的假阳性。

实现上直接调守卫的检查函数（``--repo-root`` 的 Python 入口
``configure_repo_root``），不起子进程：七条用例加起来不到一秒，
CI 不会因此变慢。守卫自己的 ``--repo-root`` 选项就是为这件事留的。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "check_compliance_guard.py"

_SPEC = importlib.util.spec_from_file_location("knowledgeflow_check_compliance_guard", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
guard_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard_mod)

#: 从守卫自己的常量里取，避免两边各写一份文件名后悄悄漂移。
INTERNAL_DOC_NAME: str = guard_mod.INTERNAL_DOC_NAMES[0]

Mutable = Callable[[Path], None]


def _make_repo_copy(tmp_path: Path) -> Path:
    """搭一个最小仓库副本：守卫要读的文件全放真内容，其余一概不放。"""
    root = tmp_path / "repo"
    (root / "extension").mkdir(parents=True)
    for name in ("README.md", "README.en.md", "COMPLIANCE.md"):
        shutil.copyfile(REPO_ROOT / name, root / name)
    shutil.copyfile(
        REPO_ROOT / "extension" / "manifest.json", root / "extension" / "manifest.json"
    )
    return root


def _run_file_checks(root: Path) -> Any:
    """只跑「读仓库文件」的三条断言。

    Cookie / 域名 / CORS / 验证码四类查的是**真代码**（``BACKEND_ROOT``），
    变异一个文档副本影响不到它们，所以不在这里重复跑 —— 跑反而让失败原因变模糊。
    """
    guard_mod.configure_repo_root(root)
    try:
        guard = guard_mod.Guard()
        guard_mod.check_extension_permissions(guard)
        guard_mod.check_disclaimer(guard)
        guard_mod.check_no_internal_docs(guard, guard_mod.tracked_files())
        return guard
    finally:
        guard_mod.configure_repo_root(None)


def _drop_all(root: Path, name: str, marker: str) -> None:
    """把某个文件里的措辞**全部**删掉（只删一处可能还剩一处，仍算通过）。"""
    path = root / name
    text = path.read_text(encoding="utf-8")
    assert marker in text, f"前提不成立：{name} 里找不到 {marker!r}（这条用例过期了）"
    path.write_text(text.replace(marker, ""), encoding="utf-8")


def _mutate_manifest(root: Path, mutate: Callable[[dict], None]) -> None:
    path = root / "extension" / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _host_wildcard(root: Path) -> None:
    _mutate_manifest(root, lambda manifest: manifest["host_permissions"].append("https://*/*"))


def _all_urls_permission(root: Path) -> None:
    _mutate_manifest(root, lambda manifest: manifest["permissions"].append("<all_urls>"))


def _drop_signature_commitment(root: Path) -> None:
    _drop_all(root, "README.md", "不伪造签名")


def _drop_captcha_commitment_en(root: Path) -> None:
    _drop_all(root, "README.en.md", "bypass CAPTCHAs")


def _delete_compliance_doc(root: Path) -> None:
    (root / "COMPLIANCE.md").unlink()


def _plant_internal_doc(root: Path) -> None:
    (root / INTERNAL_DOC_NAME).write_text("内部进度文档（不该出现在公开仓库）", encoding="utf-8")


def _reference_internal_doc(root: Path) -> None:
    target = root / "backend" / "scripts" / "injected.py"
    target.parent.mkdir(parents=True)
    target.write_text(f'doc = "{INTERNAL_DOC_NAME}"\n', encoding="utf-8")


MUTATIONS: tuple[tuple[str, Mutable, str], ...] = (
    ("扩展 host_permissions 放行通配", _host_wildcard, "[FAIL] 扩展权限最小化"),
    ("扩展 permissions 申请 <all_urls>", _all_urls_permission, "[FAIL] 扩展权限最小化"),
    ("中文 README 删掉「不伪造签名」", _drop_signature_commitment, "[FAIL] 免责声明仍在"),
    ("英文 README 删掉 bypass CAPTCHAs", _drop_captcha_commitment_en, "[FAIL] 免责声明仍在"),
    ("删掉 COMPLIANCE.md", _delete_compliance_doc, "[FAIL] 免责声明仍在"),
    ("内部进度文档躺在工作区", _plant_internal_doc, "[FAIL] 无内部文档外泄"),
    ("源码把内部文档名当路径字面量引用", _reference_internal_doc, "[FAIL] 无内部文档外泄"),
)


@pytest.mark.parametrize(
    ("description", "mutate", "expected_section"),
    MUTATIONS,
    ids=[mutation[0] for mutation in MUTATIONS],
)
def test_guard_rejects_each_mutation(
    tmp_path: Path,
    description: str,
    mutate: Mutable,
    expected_section: str,
) -> None:
    """每条断言都必须能抓住对应的违规；抓不住就说明那条断言是永远绿的。"""
    root = _make_repo_copy(tmp_path)
    mutate(root)

    guard = _run_file_checks(root)

    assert guard.problems, f"{description}：守卫没报任何问题 —— 这条断言是永远绿的"
    assert any(section.startswith(expected_section) for section in guard.sections), (
        f"{description}：守卫报红的位置不对。\n"
        f"期望以 {expected_section!r} 开头的 section，实际 sections={guard.sections}"
    )
