"""实体地面化（R2.1）。

规约原文：

```text
if extracted_entities(claim.text):
    if not any(entity_alias_hit(entity, source_text) for entity in extracted_entities(claim.text)):
        claim.needs_verification = True
```

注意这是 ``any`` 语义（**至少一个**实体或其别名命中即通过），刻意宽松：
它拦的是「claim 里全是原文没有的实体」这种整体编造，不追求逐字核对。
数字侧的 R2.2 是 ``all`` 语义，两者互补。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.grounding.aliases import AliasResolver, IdentityAliasResolver
from app.grounding.folding import fold_for_matching
from app.schemas.analysis import Entity


@dataclass(frozen=True)
class EntityHit:
    """claim 文本里出现的一个实体。"""

    name: str
    matched_alias: str
    aliases: frozenset[str]

    @property
    def matched_alias_folded(self) -> str:
        return fold_for_matching(self.matched_alias)


def entity_hits_in_text(
    text: str,
    entities: tuple[Entity, ...] | list[Entity],
    resolver: AliasResolver | None = None,
) -> tuple[EntityHit, ...]:
    """找出 ``text`` 中出现的实体（按 alias 表命中，长别名优先）。"""
    resolved = resolver or IdentityAliasResolver()
    folded = fold_for_matching(text)
    if not folded:
        return ()

    hits: list[EntityHit] = []
    for entity in entities:
        aliases = resolved.aliases_for(entity.name)
        usable = sorted((alias for alias in aliases if alias.strip()), key=len, reverse=True)
        for alias in usable:
            if fold_for_matching(alias) in folded:
                hits.append(EntityHit(name=entity.name, matched_alias=alias, aliases=aliases))
                break
    return tuple(hits)


def hit_in_source(hit: EntityHit, folded_source: str) -> bool:
    return any(fold_for_matching(alias) in folded_source for alias in hit.aliases if alias.strip())


def entities_grounded(hits: tuple[EntityHit, ...], source_text: str) -> bool:
    """``any`` 语义：至少一个实体（或其别名）能在源文本里找到。"""
    if not hits:
        return True
    folded_source = fold_for_matching(source_text)
    return any(hit_in_source(hit, folded_source) for hit in hits)


def ungrounded_entities(hits: tuple[EntityHit, ...], source_text: str) -> tuple[str, ...]:
    """返回源文本里**完全找不到**的实体名（用于报告，不参与判定）。"""
    folded_source = fold_for_matching(source_text)
    return tuple(hit.name for hit in hits if not hit_in_source(hit, folded_source))


def describe_hits(hits: tuple[EntityHit, ...]) -> str:
    return ", ".join(hit.name for hit in hits)


__all__ = [
    "EntityHit",
    "entity_hits_in_text",
    "hit_in_source",
    "entities_grounded",
    "ungrounded_entities",
    "describe_hits",
]
