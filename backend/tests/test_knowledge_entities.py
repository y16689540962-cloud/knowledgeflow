"""实体归一化：走 alias 表查询 + 回退建表（第二十一节）。"""

from __future__ import annotations

import pytest

from app.knowledge.entities import (
    DEFAULT_ENTITY_TYPE,
    EntityRegistry,
    EntityRegistryError,
)
from app.db.models import ContentEntity, Entity, EntityAlias


# --------------------------------------------------------------------------- #
# 回退建表
# --------------------------------------------------------------------------- #
async def test_resolve_creates_entity(entities: EntityRegistry) -> None:
    resolved = await entities.resolve_or_create("人工智能", "concept")
    assert resolved.created is True
    assert resolved.matched_by == "created"
    assert resolved.canonical_name == "人工智能"
    assert resolved.entity_type == "concept"
    assert resolved.aliases == ()
    assert await entities.count() == 1


async def test_second_resolve_reuses_entity(entities: EntityRegistry) -> None:
    first = await entities.resolve_or_create("人工智能", "concept")
    second = await entities.resolve_or_create("人工智能", "concept")
    assert second.created is False
    assert second.matched_by == "canonical"
    assert second.entity_id == first.entity_id
    assert await entities.count() == 1


async def test_resolve_by_alias_falls_back_to_canonical(entities: EntityRegistry) -> None:
    """**禁止简单字符串替换**的关键证据：入库的是 canonical，不是表面形式。"""
    await entities.resolve_or_create("人工智能", "concept", aliases=["AI", "Artificial Intelligence"])
    resolved = await entities.resolve_or_create("AI")

    assert resolved.canonical_name == "人工智能"
    assert resolved.matched_by == "alias"
    assert resolved.created is False
    assert await entities.count() == 1


async def test_alias_cannot_point_to_two_entities(entities: EntityRegistry) -> None:
    """别名表主键（全局唯一）是硬约束：一个别名只能属于一个 canonical。"""
    canonical = await entities.resolve_or_create("人工智能", "concept", aliases=["AI"])
    other = await entities.resolve_or_create("机器学习", "concept", aliases=["AI"])

    assert other.entity_id != canonical.entity_id
    assert await entities.count() == 2
    assert await entities.all_aliases() == [("人工智能", "AI")]

    # 别名依然指回原来的实体
    resolved = await entities.resolve_or_create("AI")
    assert resolved.canonical_name == "人工智能"
    assert resolved.matched_by == "alias"


async def test_canonical_name_is_not_duplicated_into_alias_table(entities: EntityRegistry) -> None:
    resolved = await entities.resolve_or_create("中国", "place")
    rows = await entities.all_aliases()
    assert rows == []  # alias 表只放「额外的表面形式」
    assert await entities.aliases_for(resolved.entity_id) == ()


async def test_resolve_with_aliases(entities: EntityRegistry) -> None:
    resolved = await entities.resolve_or_create("人工智能", "concept", aliases=["AI", "人工智能"])
    assert resolved.aliases == ("AI",)
    stored = await entities.get_by_canonical_name("人工智能")
    assert stored is not None and stored.canonical_name == "人工智能"


async def test_resolve_many_dedupes(entities: EntityRegistry) -> None:
    resolved = await entities.resolve_many(
        [("中国", "place"), ("中国", "place"), ("人工智能", "concept"), ("", "other")]
    )
    assert [item.canonical_name for item in resolved] == ["中国", "人工智能"]
    assert await entities.count() == 2


async def test_blank_name_rejected(entities: EntityRegistry) -> None:
    with pytest.raises(EntityRegistryError):
        await entities.resolve_or_create("   ")


async def test_default_entity_type(entities: EntityRegistry) -> None:
    resolved = await entities.resolve_or_create("某实体")
    assert resolved.entity_type == DEFAULT_ENTITY_TYPE == "other"


async def test_name_is_normalized(entities: EntityRegistry) -> None:
    resolved = await entities.resolve_or_create("  人工\n智能  ")
    assert resolved.canonical_name == "人工 智能"


async def test_get_by_canonical_name_missing(entities: EntityRegistry) -> None:
    assert await entities.get_by_canonical_name("不存在") is None


async def test_find_by_alias_missing(entities: EntityRegistry) -> None:
    assert await entities.find_by_alias("不存在") == ()


# --------------------------------------------------------------------------- #
# add_alias 与冲突
# --------------------------------------------------------------------------- #
async def test_add_alias_creates(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("人工智能", "concept")
    registration = await entities.add_alias(entity.entity_id, "AI")
    assert registration.created is True
    assert registration.conflict is None
    assert await entities.aliases_for(entity.entity_id) == ("AI",)


async def test_add_same_alias_again_is_noop(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("人工智能", "concept")
    await entities.add_alias(entity.entity_id, "AI")
    again = await entities.add_alias(entity.entity_id, "AI")
    assert again.created is False
    assert again.conflict is None


async def test_alias_conflict_is_reported_not_stolen(entities: EntityRegistry) -> None:
    owner = await entities.resolve_or_create("人工智能", "concept")
    await entities.add_alias(owner.entity_id, "AI")
    other = await entities.resolve_or_create("机器学习", "concept")

    registration = await entities.add_alias(other.entity_id, "AI")
    assert registration.conflict is not None
    assert registration.conflict.existing_entity_id == owner.entity_id
    assert registration.conflict.alias == "AI"
    # 归属没有变
    assert await entities.aliases_for(owner.entity_id) == ("AI",)
    assert await entities.aliases_for(other.entity_id) == ()


async def test_conflicting_alias_during_resolve_is_skipped(entities: EntityRegistry) -> None:
    await entities.resolve_or_create("人工智能", "concept", aliases=["AI"])
    resolved = await entities.resolve_or_create("机器学习", "concept", aliases=["AI"])
    assert resolved.canonical_name == "机器学习"
    assert resolved.aliases == ()
    assert await entities.count() == 2


async def test_blank_alias_rejected(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("中国", "place")
    with pytest.raises(EntityRegistryError):
        await entities.add_alias(entity.entity_id, "  ")


async def test_aliases_are_sorted(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("人工智能", "concept", aliases=["Zeta", "AI", "机器学习"])
    assert await entities.aliases_for(entity.entity_id) == ("AI", "Zeta", "机器学习")


# --------------------------------------------------------------------------- #
# content_entities
# --------------------------------------------------------------------------- #
async def test_link_content(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("中国", "place")
    result = await entities.link_content("c1", [(entity.entity_id, 0.9)])
    assert result.linked == (entity.entity_id,)

    listed = await entities.list_content_entities("c1")
    assert [item.canonical_name for item in listed] == ["中国"]


async def test_link_content_is_idempotent(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("中国", "place")
    await entities.link_content("c1", [(entity.entity_id, 0.5)])
    await entities.link_content("c1", [(entity.entity_id, 0.95)])

    listed = await entities.list_content_entities("c1")
    assert len(listed) == 1


async def test_link_content_updates_confidence(entities: EntityRegistry, db) -> None:
    from sqlalchemy import select

    entity = await entities.resolve_or_create("中国", "place")
    await entities.link_content("c1", [(entity.entity_id, 0.5)])
    await entities.link_content("c1", [(entity.entity_id, 0.95)])

    async with db.session_factory() as session:
        rows = (await session.execute(select(ContentEntity.confidence))).scalars().all()
    assert list(rows) == [0.95]


async def test_link_content_without_confidence(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("中国", "place")
    await entities.link_content("c1", [(entity.entity_id, None)])
    assert await entities.list_content_entities("c1")


async def test_list_content_entities_empty(entities: EntityRegistry) -> None:
    assert await entities.list_content_entities("nope") == ()


async def test_list_content_entities_includes_aliases(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("人工智能", "concept", aliases=["AI"])
    await entities.link_content("c1", [(entity.entity_id, 1.0)])
    listed = await entities.list_content_entities("c1")
    assert listed[0].aliases == ("AI",)


async def test_multiple_entities_per_content(entities: EntityRegistry) -> None:
    china = await entities.resolve_or_create("中国", "place")
    ai = await entities.resolve_or_create("人工智能", "concept")
    await entities.link_content("c1", [(china.entity_id, 0.9), (ai.entity_id, 0.8)])
    listed = await entities.list_content_entities("c1")
    # 按 canonical_name 排序（SQLite 的 UTF-8 字节序：中 U+4E2D < 人 U+4EBA）
    assert [item.canonical_name for item in listed] == ["中国", "人工智能"]


# --------------------------------------------------------------------------- #
# 数据库表本身
# --------------------------------------------------------------------------- #
async def test_unique_alias_index_enforced_by_db(entities: EntityRegistry, db) -> None:
    from sqlalchemy.exc import IntegrityError

    first = await entities.resolve_or_create("人工智能", "concept")
    second = await entities.resolve_or_create("机器学习", "concept")

    from app.utils import new_id

    with pytest.raises(IntegrityError):
        async with db.session_factory() as session:
            async with session.begin():
                session.add(EntityAlias(id=new_id(), entity_id=first.entity_id, alias="冲突"))
                session.add(EntityAlias(id=new_id(), entity_id=second.entity_id, alias="冲突"))
                await session.flush()


async def test_unique_canonical_name_index_enforced_by_db(entities: EntityRegistry, db) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.utils import new_id

    await entities.resolve_or_create("中国", "place")
    with pytest.raises(IntegrityError):
        async with db.session_factory() as session:
            async with session.begin():
                session.add(
                    Entity(id=new_id(), canonical_name="中国", entity_type="place", description=None)
                )
                await session.flush()
