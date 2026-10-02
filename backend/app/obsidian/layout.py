"""Obsidian Vault 目录布局（定稿文档第二十节）。

```text
<vault>/KnowledgeFlow/
├── Inbox/
├── Processed/
├── Failed/
├── Assets/
└── Templates/
```
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Final, Literal

from app.security.paths import ensure_within_vault

#: vault 内的命名空间目录 —— 所有写入都必须落在这里面。
VAULT_NAMESPACE: Final[str] = "KnowledgeFlow"

#: 第二十节规定的五个子目录。
SUBDIRS: Final[tuple[str, ...]] = ("Inbox", "Processed", "Failed", "Assets", "Templates")

NoteKind = Literal["inbox", "processed", "failed"]

_KIND_TO_SUBDIR: Final[dict[str, str]] = {
    "inbox": "Inbox",
    "processed": "Processed",
    "failed": "Failed",
}

NOTE_EXTENSION: Final[str] = ".md"


def note_kinds() -> tuple[str, ...]:
    return tuple(_KIND_TO_SUBDIR)


def namespace_path(*parts: str) -> str:
    """拼出 ``KnowledgeFlow/<...>`` 形式的**相对**路径（永远不含绝对路径）。

    分隔符**一律用 ``/``**，不用 ``os.sep``：这是 vault 内的**逻辑路径**，
    不是本机文件系统路径 —— 它会进数据库、进界面、进 Obsidian 双链。
    用 ``os.sep`` 会让同一条笔记在 macOS 上存成 ``KnowledgeFlow/Processed/x.md``、
    在 Windows 上存成 ``KnowledgeFlow\\Processed\\x.md``，同一个内容两种身份。
    （写入时照旧经 ``ensure_within_vault`` 转成本机绝对路径，两种分隔符都认。）
    """
    cleaned = [part for part in parts if part]
    return posixpath.join(VAULT_NAMESPACE, *cleaned) if cleaned else VAULT_NAMESPACE


def note_relative_path(kind: NoteKind, filename: str) -> str:
    """``kind`` → ``KnowledgeFlow/Processed/<filename>.md``。"""
    if kind not in _KIND_TO_SUBDIR:
        raise ValueError(f"未知的 note kind：{kind!r}，可选 {sorted(_KIND_TO_SUBDIR)}")
    stem = filename[:-3] if filename.endswith(NOTE_EXTENSION) else filename
    return namespace_path(_KIND_TO_SUBDIR[kind], f"{stem}{NOTE_EXTENSION}")


def with_filename_suffix(relative_path: str, suffix: str) -> str:
    """给文件名加后缀（用于同名不同内容的消歧）。

    入参可能是任一种分隔符（``note_relative_path`` 给的是 ``/``，但调用方
    也可能递进本机风格路径），先归一到 ``/`` 再拆；**输出永远是 ``/``** ——
    理由同 :func:`namespace_path`。不归一的话 Windows 上会产出
    ``KnowledgeFlow/Processed\\标题-abc.md`` 这种半半拉拉的路径。
    """
    normalized = relative_path.replace(os.sep, "/") if os.sep != "/" else relative_path
    directory, name = posixpath.split(normalized)
    stem = name[:-3] if name.endswith(NOTE_EXTENSION) else name
    new_name = f"{stem}-{suffix}{NOTE_EXTENSION}"
    return posixpath.join(directory, new_name) if directory else new_name


def ensure_layout(vault_root: str | os.PathLike[str]) -> tuple[Path, ...]:
    """创建 ``KnowledgeFlow/`` 及其五个子目录，返回它们的绝对路径。

    幂等；每一步都先过 :func:`ensure_within_vault`。
    """
    created: list[Path] = []
    namespace = ensure_within_vault(vault_root, VAULT_NAMESPACE)
    namespace.mkdir(parents=True, exist_ok=True)
    created.append(namespace)
    for subdir in SUBDIRS:
        target = ensure_within_vault(vault_root, namespace_path(subdir))
        target.mkdir(parents=True, exist_ok=True)
        created.append(target)
    return tuple(created)


__all__ = [
    "VAULT_NAMESPACE",
    "SUBDIRS",
    "NOTE_EXTENSION",
    "NoteKind",
    "note_kinds",
    "namespace_path",
    "note_relative_path",
    "with_filename_suffix",
    "ensure_layout",
]
