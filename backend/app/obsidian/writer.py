"""Obsidian Writer（定稿文档第十八 / 十九 / 二十节）。

原则：

* **每一次写入都先过** :func:`app.security.paths.ensure_within_vault` —— 禁止 ``../`` 穿越
* 写入是**原子**的（临时文件 + ``os.replace``），不会留下半个文件
* 换行统一写 ``\\n``（Windows 上也不产生 CRLF，避免 Obsidian diff 噪声）
* **绝不覆盖别人的笔记**：目标文件已存在时，先读它的 frontmatter 判断
  ``(source, source_id)`` 是否与自己一致 ——
  一致 = 同一条内容 reprocess → 覆盖；不一致或读不出 = 别人的文件 → 换一个带
  ``content_id`` 后缀的文件名，绝不静默覆盖
* 只为**内容本身**建文件。实体 / 主题只生成 ``[[]]`` 链接，
  **不自动批量创建空白笔记**（第二十一节强制）
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from app.errors import ErrorType, KnowledgeFlowError
from app.obsidian.frontmatter import parse_frontmatter
from app.obsidian.layout import (
    NoteKind,
    ensure_layout,
    note_relative_path,
    with_filename_suffix,
)
from app.obsidian.renderer import RenderedNote
from app.security.paths import ensure_within_vault

ConflictPolicy = Literal["overwrite", "keep_existing"]
WriteStatus = Literal["created", "updated", "skipped"]

#: 同名不同内容时，用 content_id 的多少位做后缀（依次尝试）。
DISAMBIGUATION_LENGTHS: Final[tuple[int, ...]] = (6, 12, 32)
MAX_NUMERIC_SUFFIX: Final[int] = 100


class ObsidianWriteError(KnowledgeFlowError):
    error_type = ErrorType.OBSIDIAN_WRITE_FAILED
    default_message = "写入 Obsidian 失败"


@dataclass(frozen=True)
class WrittenNote:
    relative_path: str
    absolute_path: Path
    status: WriteStatus
    disambiguated: bool
    byte_size: int

    @property
    def created(self) -> bool:
        return self.status == "created"

    @property
    def updated(self) -> bool:
        return self.status == "updated"


Identity = tuple[str, str]


class ObsidianWriter:
    def __init__(
        self,
        vault_root: str | os.PathLike[str],
        *,
        conflict_policy: ConflictPolicy = "overwrite",
    ) -> None:
        if conflict_policy not in ("overwrite", "keep_existing"):
            raise ValueError(f"未知的 conflict_policy：{conflict_policy!r}")
        self._vault_root = Path(vault_root)
        self._conflict_policy = conflict_policy

    @property
    def vault_root(self) -> Path:
        return self._vault_root

    @property
    def conflict_policy(self) -> ConflictPolicy:
        return self._conflict_policy

    # ------------------------------------------------------------------ #
    # 目录
    # ------------------------------------------------------------------ #
    def ensure_layout(self) -> tuple[Path, ...]:
        return ensure_layout(self._vault_root)

    def absolute_path(self, relative_path: str) -> Path:
        """相对路径 → vault 内绝对路径（越界即抛 ``PathTraversalError``）。"""
        return ensure_within_vault(self._vault_root, relative_path)

    def read_text(self, relative_path: str) -> str | None:
        target = self.absolute_path(relative_path)
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    def read_identity(self, relative_path: str) -> Identity | None:
        """读出笔记所属的 ``(source, source_id)``。读不出返回 ``None``。"""
        text = self.read_text(relative_path)
        if text is None:
            return None
        values = parse_frontmatter(text)
        source = values.get("source")
        source_id = values.get("source_id")
        if isinstance(source, str) and isinstance(source_id, str) and source:
            return source, source_id
        return None

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def write_rendered(
        self,
        rendered: RenderedNote,
        *,
        kind: NoteKind = "processed",
        identity: Identity | None = None,
    ) -> WrittenNote:
        self.ensure_layout()
        return self.write_markdown(
            note_relative_path(kind, rendered.filename),
            rendered.markdown,
            identity=identity,
            content_id=rendered.content_id,
        )

    def write_markdown(
        self,
        relative_path: str,
        markdown: str,
        *,
        identity: Identity | None = None,
        content_id: str = "",
    ) -> WrittenNote:
        chosen, disambiguated = self._choose_path(
            relative_path, identity=identity, content_id=content_id
        )
        target = self.absolute_path(chosen)

        if target.is_file() and self._conflict_policy == "keep_existing":
            return WrittenNote(
                relative_path=chosen,
                absolute_path=target,
                status="skipped",
                disambiguated=disambiguated,
                byte_size=target.stat().st_size,
            )

        existed = target.is_file()
        size = self._atomic_write(target, markdown)
        return WrittenNote(
            relative_path=chosen,
            absolute_path=target,
            status="updated" if existed else "created",
            disambiguated=disambiguated,
            byte_size=size,
        )

    def _choose_path(
        self,
        relative_path: str,
        *,
        identity: Identity | None,
        content_id: str,
    ) -> tuple[str, bool]:
        """决定最终相对路径。同名冲突时给文件名加 ``content_id`` 后缀。"""
        target = self.absolute_path(relative_path)
        if not target.is_file() or identity is None:
            return relative_path, False

        existing = self.read_identity(relative_path)
        if existing == identity:
            # 同一条内容（reprocess）→ 覆盖同一份笔记
            return relative_path, False

        # 文件存在但属于别的内容（或不是我们写的）→ 绝不覆盖
        for length in DISAMBIGUATION_LENGTHS:
            suffix = content_id[:length]
            if not suffix:
                break
            candidate = with_filename_suffix(relative_path, suffix)
            candidate_path = self.absolute_path(candidate)
            if not candidate_path.is_file():
                return candidate, True
            if self.read_identity(candidate) == identity:
                return candidate, True

        short = content_id[:6] or "dup"
        for index in range(2, MAX_NUMERIC_SUFFIX):
            candidate = with_filename_suffix(relative_path, f"{short}-{index}")
            if not self.absolute_path(candidate).is_file():
                return candidate, True

        raise ObsidianWriteError(
            f"无法为 {relative_path!r} 找到可用文件名（已尝试 {MAX_NUMERIC_SUFFIX} 个后缀）",
            context={"relative_path": relative_path, "content_id": content_id},
        )

    def _atomic_write(self, target: Path, text: str) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent), prefix=".knowledgeflow-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        return len(text.encode("utf-8"))


__all__ = [
    "ObsidianWriter",
    "ObsidianWriteError",
    "WrittenNote",
    "ConflictPolicy",
    "WriteStatus",
    "Identity",
    "DISAMBIGUATION_LENGTHS",
]
