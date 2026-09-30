"""Obsidian frontmatter（定稿文档第二十节）。

字段与顺序严格按文档；用 PyYAML 序列化 —— 手写 YAML 在中文标题、含 ``:``/``#``、
含换行的值上必然会出错，那种「看起来能跑」的实现不值得省一个依赖。

空字符串会被写成 ``''``（YAML 里与文档示例的 ``""`` 等价）。
"""

from __future__ import annotations

from typing import Any, Final

import yaml

#: frontmatter 的字段顺序（文档第二十节，一字不改）。
FRONTMATTER_KEY_ORDER: Final[tuple[str, ...]] = (
    "title",
    "source",
    "source_id",
    "source_url",
    "author",
    "media_type",
    "analysis_type",
    "topics",
    "tags",
    "created_at",
    "processed_at",
    "model",
    "prompt_version",
    "confidence",
    "needs_verification",
    "needs_manual_review",
    "content_hash_version",
)

DELIMITER: Final[str] = "---"


def render_frontmatter(values: dict[str, Any]) -> str:
    """把字典渲染成 ``---\\n...\\n---`` 块（含首尾换行）。"""
    missing = set(FRONTMATTER_KEY_ORDER) - set(values)
    if missing:
        raise ValueError(f"frontmatter 缺少字段：{sorted(missing)}")

    ordered = {key: values[key] for key in FRONTMATTER_KEY_ORDER}
    body = yaml.safe_dump(
        ordered,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=10_000,
    )
    return f"{DELIMITER}\n{body}{DELIMITER}\n"


def parse_frontmatter(markdown: str) -> dict[str, Any]:
    """从笔记文本里解析 frontmatter；没有或不是映射时返回 ``{}``。

    用于「这个文件是不是我们写的、属于哪个 content」这类判断 ——
    解析失败一律当作「不是我们的文件」，绝不覆盖。
    """
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != DELIMITER:
        return {}
    for index in range(1, len(lines)):
        if lines[index].strip() == DELIMITER:
            block = "\n".join(lines[1:index])
            try:
                loaded = yaml.safe_load(block)
            except yaml.YAMLError:
                return {}
            return loaded if isinstance(loaded, dict) else {}
    return {}


def frontmatter_fields_in_order(markdown: str) -> list[str]:
    """返回 frontmatter 里实际出现的键，**按出现顺序**（用于断言顺序正确）。"""
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != DELIMITER:
        return []
    keys: list[str] = []
    for line in lines[1:]:
        if line.strip() == DELIMITER:
            break
        if line and not line.startswith((" ", "-")) and ":" in line:
            keys.append(line.split(":", 1)[0].strip())
    return keys


__all__ = [
    "FRONTMATTER_KEY_ORDER",
    "DELIMITER",
    "render_frontmatter",
    "parse_frontmatter",
    "frontmatter_fields_in_order",
]
