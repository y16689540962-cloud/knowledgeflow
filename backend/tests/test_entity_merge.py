"""实体合并：别名迁移 + 归属重挂（第二十一节的「不抢不改」语义延伸到合并）。

关键取舍：**只要有别名冲突，源实体就不删**。
冲突的那个表面形式没迁成，删掉源它就彻底查不到了 —— 留着现场给人工处理，
比「合并成功但悄悄丢了一个说法」诚实。
"""

from __future__ import annotations

import pytest

from app.db.models import Entity
from app.errors import EntityNotFoundError
from app.knowledge.aliases import DatabaseAliasLoader
from app.knowledge.entities import (
    AliasConflict,
    EntityRegistry,
    EntityRegistryError,
)
from app.utils import new_id


async def _insert_entity(db, canonical_name: str, entity_type: str = "concept") -> str:
    """绕过 ``resolve_or_create`` 的别名回退，直接造一个实体行。

    用来构造「源实体的 canonical 名正好是别人的别名」这种冲突场景 ——
    走 ``resolve_or_create("DL")`` 会直接命中别名回退，根本到不了冲突分支。
    """
    entity_id = new_id()
    async with db.session_factory() as session:
        async with session.begin():
            session.add(
                Entity(
                    id=entity_id,
                    canonical_name=canonical_name,
                    entity_type=entity_type,
                    description=None,
                )
            )
    return entity_id


# --------------------------------------------------------------------------- #
# 预演
# --------------------------------------------------------------------------- #
async def test_plan_merge_does_not_write(entities: EntityRegistry) -> None:
    target = await entities.resolve_or_create("深度学习", "concept")
    source = await entities.resolve_or_create("Deep Learning", "concept", aliases=["DL"])

    plan = await entities.plan_merge(source.entity_id, target.entity_id)

    assert plan.aliases_to_move == ("Deep Learning", "DL")
    assert plan.alias_conflicts == ()
    assert plan.blocked is False
    # 预演不落库：源还在、别名还在源名下
    assert await entities.count() == 2
    assert await entities.aliases_for(source.entity_id) == ("DL",)


async def test_plan_merge_reports_conflict(entities: EntityRegistry, db) -> None:
    third = await entities.resolve_or_create("神经网络", "concept", aliases=["DL"])
    target = await entities.resolve_or_create("深度学习", "concept")
    source_id = await _insert_entity(db, "DL")

    plan = await entities.plan_merge(source_id, target.entity_id)

    assert plan.alias_conflicts == (AliasConflict(alias="DL", existing_entity_id=third.entity_id),)
    assert plan.aliases_to_move == ()
    assert plan.blocked is True


# --------------------------------------------------------------------------- #
# 合并本身
# --------------------------------------------------------------------------- #
async def test_merge_moves_canonical_name_and_deletes_source(entities: EntityRegistry) -> None:
    target = await entities.resolve_or_create("深度学习", "concept")
    source = await entities.resolve_or_create("Deep Learning", "concept")

    result = await entities.merge_entities(source.entity_id, target.entity_id)

    assert result.aliases_moved == ("Deep Learning",)
    assert result.alias_conflicts == ()
    assert result.source_deleted is True
    assert await entities.get_by_id(source.entity_id) is None
    # 源的说法迁到了目标名下，查得到
    assert [item.canonical_name for item in await entities.find_by_alias("Deep Learning")] == [
        "深度学习"
    ]


async def test_merge_moves_source_aliases(entities: EntityRegistry) -> None:
    target = await entities.resolve_or_create("Google DeepMind", "org")
    source = await entities.resolve_or_create("DeepMind", "org", aliases=["DM", "戴密斯"])

    result = await entities.merge_entities(source.entity_id, target.entity_id)

    assert sorted(result.aliases_moved) == ["DM", "DeepMind", "戴密斯"]
    assert sorted(await entities.aliases_for(target.entity_id)) == ["DM", "DeepMind", "戴密斯"]
    assert await entities.get_by_id(source.entity_id) is None


async def test_merge_repoints_content_links(entities: EntityRegistry) -> None:
    target = await entities.resolve_or_create("深度学习", "concept")
    source = await entities.resolve_or_create("Deep Learning", "concept")
    await entities.link_content("c1", [(source.entity_id, 0.8)])
    await entities.link_content("c2", [(source.entity_id, 0.7)])

    result = await entities.merge_entities(source.entity_id, target.entity_id)

    assert result.links_moved == 2
    assert result.links_dropped == 0
    for content_id in ("c1", "c2"):
        linked = await entities.list_content_entities(content_id)
        assert [item.canonical_name for item in linked] == ["深度学习"]


async def test_merge_drops_link_when_target_already_owns_content(entities: EntityRegistry) -> None:
    """同一条内容两边都挂了 → 保留目标那条，删掉源那条，不产生重复行。"""
    target = await entities.resolve_or_create("深度学习", "concept")
    source = await entities.resolve_or_create("Deep Learning", "concept")
    await entities.link_content("c1", [(target.entity_id, 0.9), (source.entity_id, 0.4)])

    result = await entities.merge_entities(source.entity_id, target.entity_id)

    assert (result.links_moved, result.links_dropped) == (0, 1)
    linked = await entities.list_content_entities("c1")
    assert len(linked) == 1
    assert linked[0].canonical_name == "深度学习"
    assert linked[0].entity_id == target.entity_id


async def test_merge_delete_source_false_keeps_source(entities: EntityRegistry) -> None:
    target = await entities.resolve_or_create("深度学习", "concept")
    source = await entities.resolve_or_create("Deep Learning", "concept", aliases=["DL"])

    result = await entities.merge_entities(
        source.entity_id, target.entity_id, delete_source=False
    )

    assert result.source_deleted is False
    assert await entities.get_by_id(source.entity_id) is not None
    # 别名仍然迁走了
    assert sorted(await entities.aliases_for(target.entity_id)) == ["DL", "Deep Learning"]


# --------------------------------------------------------------------------- #
# 冲突：不抢、不改、不删
# --------------------------------------------------------------------------- #
async def test_merge_conflict_keeps_source_and_does_not_steal(entities: EntityRegistry, db) -> None:
    third = await entities.resolve_or_create("神经网络", "concept", aliases=["DL"])
    target = await entities.resolve_or_create("深度学习", "concept")
    source_id = await _insert_entity(db, "DL")

    result = await entities.merge_entities(source_id, target.entity_id)

    assert result.alias_conflicts == (AliasConflict(alias="DL", existing_entity_id=third.entity_id),)
    assert result.aliases_moved == ()
    assert result.source_deleted is False
    # 源保留 —— 不制造「合并成功但说法丢了」的假象
    assert await entities.get_by_id(source_id) is not None
    # 别名没被抢走，还指向第三方
    assert [item.canonical_name for item in await entities.find_by_alias("DL")] == ["神经网络"]


async def test_merge_runtime_conflict_is_reported_not_stolen(
    entities: EntityRegistry, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预演时没看见、执行那一刻才发现被占 —— 同样不抢，只报告。"""
    third = await entities.resolve_or_create("神经网络", "concept", aliases=["DL"])
    target = await entities.resolve_or_create("深度学习", "concept")
    source_id = await _insert_entity(db, "DL")

    async def _blind_owners(names: list[str]) -> dict[str, str]:
        return {}

    monkeypatch.setattr(entities, "_alias_owners", _blind_owners)

    result = await entities.merge_entities(source_id, target.entity_id)

    assert result.alias_conflicts == (AliasConflict(alias="DL", existing_entity_id=third.entity_id),)
    assert result.aliases_moved == ()
    assert result.source_deleted is False
    assert [item.canonical_name for item in await entities.find_by_alias("DL")] == ["神经网络"]


async def test_merge_drops_alias_that_duplicates_target_canonical(
    entities: EntityRegistry, db
) -> None:
    """源的别名正好等于目标 canonical → 冗余项（canonical 不进 alias 表），随源删掉。"""
    target = await entities.resolve_or_create("深度学习", "concept")
    source_id = await _insert_entity(db, "Deep Learning")
    await entities.add_alias(source_id, "深度学习")

    result = await entities.merge_entities(source_id, target.entity_id)

    assert result.aliases_moved == ("Deep Learning",)
    assert result.source_deleted is True
    assert await entities.aliases_for(target.entity_id) == ("Deep Learning",)


# --------------------------------------------------------------------------- #
# dry-run / 校验
# --------------------------------------------------------------------------- #
async def test_dry_run_does_not_write(entities: EntityRegistry) -> None:
    target = await entities.resolve_or_create("深度学习", "concept")
    source = await entities.resolve_or_create("Deep Learning", "concept", aliases=["DL"])
    await entities.link_content("c1", [(source.entity_id, 0.8)])

    result = await entities.merge_entities(
        source.entity_id, target.entity_id, dry_run=True
    )

    assert result.applied is False
    assert result.source_deleted is False
    assert result.aliases_moved == ("Deep Learning", "DL")
    assert result.links_moved == 1
    # 一行都没动
    assert await entities.get_by_id(source.entity_id) is not None
    assert await entities.aliases_for(target.entity_id) == ()
    assert await entities.list_content_entities("c1")


async def test_merge_to_itself_is_rejected(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("深度学习", "concept")
    with pytest.raises(EntityRegistryError):
        await entities.merge_entities(entity.entity_id, entity.entity_id)


async def test_merge_with_blank_id_is_rejected(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("深度学习", "concept")
    with pytest.raises(EntityRegistryError):
        await entities.merge_entities("", entity.entity_id)


async def test_merge_unknown_entity_raises_entity_not_found(entities: EntityRegistry) -> None:
    entity = await entities.resolve_or_create("深度学习", "concept")
    with pytest.raises(EntityNotFoundError) as excinfo:
        await entities.merge_entities("not-there", entity.entity_id)
    assert excinfo.value.error_type_value == "ENTITY_NOT_FOUND"


async def test_merge_by_name(entities: EntityRegistry) -> None:
    target = await entities.resolve_or_create("深度学习", "concept")
    source = await entities.resolve_or_create("Deep Learning", "concept")

    result = await entities.merge_by_name("Deep Learning", "深度学习")

    assert result.target_id == target.entity_id
    assert result.source_deleted is True
    assert await entities.get_by_id(source.entity_id) is None


async def test_merge_by_name_unknown_raises(entities: EntityRegistry) -> None:
    await entities.resolve_or_create("深度学习", "concept")
    with pytest.raises(EntityNotFoundError):
        await entities.merge_by_name("查无此实体", "深度学习")


# --------------------------------------------------------------------------- #
# 与别名索引的接缝
# --------------------------------------------------------------------------- #
async def test_merge_invalidates_alias_index_cache(entities: EntityRegistry, db) -> None:
    """合并后索引必须重建 —— 否则下一次 Grounding 还在用旧索引。"""
    loader = DatabaseAliasLoader(session_factory=db.session_factory)
    registry = EntityRegistry(db.session_factory, alias_loader=loader)
    target = await registry.resolve_or_create("深度学习", "concept")
    source = await registry.resolve_or_create("Deep Learning", "concept")

    before = await loader.load_index()
    assert before.canonical_of("Deep Learning") == "Deep Learning"

    await registry.merge_entities(source.entity_id, target.entity_id)

    after = await loader.load_index()
    assert after.canonical_of("Deep Learning") == "深度学习"


async def test_merge_without_loader_leaves_stale_cache(entities: EntityRegistry, db) -> None:
    """反证：registry 没接 loader 时缓存不会自己失效 —— 所以必须显式 invalidate。"""
    loader = DatabaseAliasLoader(session_factory=db.session_factory)
    registry = EntityRegistry(db.session_factory)  # 故意不接 loader
    target = await registry.resolve_or_create("深度学习", "concept")
    source = await registry.resolve_or_create("Deep Learning", "concept")
    await loader.load_index()

    await registry.merge_entities(source.entity_id, target.entity_id)

    assert (await loader.load_index()).canonical_of("Deep Learning") == "Deep Learning"
    # 强制重建才看得到合并结果
    assert (
        await loader.load_index(use_cache=False)
    ).canonical_of("Deep Learning") == "深度学习"


async def test_list_entities(entities: EntityRegistry) -> None:
    assert await entities.list_entities() == ()
    await entities.resolve_or_create("人工智能", "concept", aliases=["AI"])
    await entities.resolve_or_create("中国", "place")

    listed = await entities.list_entities()

    assert [item.canonical_name for item in listed] == ["中国", "人工智能"]
    assert [item.aliases for item in listed] == [(), ("AI",)]
