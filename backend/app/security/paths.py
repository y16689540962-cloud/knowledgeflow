"""路径安全（定稿文档第十九节，强制）。

禁止 ``../`` 路径穿越：所有写入 Obsidian 的路径都必须落在 vault 内。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.errors import ErrorType, PathTraversalError


def real_vault_root(vault_root: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.fspath(vault_root))


def ensure_within_vault(vault_root: str | os.PathLike[str], rel_path: str | os.PathLike[str]) -> Path:
    """返回 vault 内的绝对路径；越界一律抛 ``PathTraversalError``。

    同时拦截：``../`` 穿越、绝对路径（``os.path.join`` 会丢弃 root）、
    以及通过符号链接指向 vault 外部的路径（``realpath`` 已解引用）。
    """
    root = real_vault_root(vault_root)
    relative = os.fspath(rel_path)

    if os.path.isabs(relative):
        raise PathTraversalError(
            f"禁止使用绝对路径写入 vault：{relative}",
            context={"vault_root": root, "relative": relative},
        )

    # ``~`` 不走 ``join`` 的绝对路径分支，但一旦下游做了 expanduser 就会越界 —— 直接拦掉。
    if relative.startswith("~"):
        raise PathTraversalError(
            f"禁止使用 home 展开路径写入 vault：{relative}",
            context={"vault_root": root, "relative": relative},
        )

    target = os.path.realpath(os.path.join(root, relative))
    if not target.startswith(root + os.sep):
        raise PathTraversalError(
            f"路径越出 vault：{relative}",
            error_type=ErrorType.PATH_TRAVERSAL_DETECTED,
            context={"vault_root": root, "target": target, "relative": relative},
        )
    return Path(target)


__all__ = ["real_vault_root", "ensure_within_vault"]
