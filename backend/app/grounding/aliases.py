"""实体别名解析（第二十一节）。

别名解析必须走「alias 表查询 + 回退建表」，**禁止用简单字符串替换实现**。
本模块只定义解析协议与一个内存实现；Phase 3 会接上数据库里的
``entities`` / ``entity_aliases`` 两张表（同一个 Protocol，不改调用方）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Protocol, runtime_checkable

from app.grounding.folding import fold_for_matching


@runtime_checkable
class AliasResolver(Protocol):
    """给定 canonical 名称，返回它**全部**的表面形式（含自身）。"""

    def aliases_for(self, name: str) -> frozenset[str]:
        ...


@dataclass(frozen=True)
class IdentityAliasResolver:
    """只有名字本身，没有别名。默认实现，保证 Grounding Check 不依赖数据库。"""

    def aliases_for(self, name: str) -> frozenset[str]:
        cleaned = (name or "").strip()
        return frozenset({cleaned}) if cleaned else frozenset()


@dataclass(frozen=True)
class StaticAliasResolver:
    """内存别名表：``{"人工智能": ["AI", "Artificial Intelligence"]}``。

    也支持反向查询：拿 ``AI`` 去问，能回到 ``人工智能`` 这一组。
    """

    groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def _group_for(self, name: str) -> tuple[str, tuple[str, ...]] | None:
        cleaned = (name or "").strip()
        if not cleaned:
            return None
        if cleaned in self.groups:
            return cleaned, tuple(self.groups[cleaned])
        folded = fold_for_matching(cleaned)
        for canonical, aliases in self.groups.items():
            candidates = (canonical, *aliases)
            if any(fold_for_matching(item) == folded for item in candidates):
                return canonical, tuple(aliases)
        return None

    def aliases_for(self, name: str) -> frozenset[str]:
        cleaned = (name or "").strip()
        if not cleaned:
            return frozenset()
        group = self._group_for(cleaned)
        if group is None:
            return frozenset({cleaned})
        canonical, aliases = group
        return frozenset({canonical, *aliases, cleaned})

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, Iterable[str]]]) -> "StaticAliasResolver":
        return cls(groups={canonical: tuple(aliases) for canonical, aliases in pairs})


@dataclass(frozen=True)
class CompositeAliasResolver:
    """按顺序查询多个 resolver，取并集。"""

    resolvers: tuple[AliasResolver, ...]

    def aliases_for(self, name: str) -> frozenset[str]:
        collected: set[str] = set()
        for resolver in self.resolvers:
            collected.update(resolver.aliases_for(name))
        return frozenset(collected)


__all__ = [
    "AliasResolver",
    "IdentityAliasResolver",
    "StaticAliasResolver",
    "CompositeAliasResolver",
]
