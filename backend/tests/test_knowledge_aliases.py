"""数据库别名 → 同步 Grounding Check 的接缝（第二十一节 ↔ 第十节）。"""

from __future__ import annotations

from app.grounding.rules import RULE_R2_1, apply_grounding
from app.knowledge.aliases import AliasIndex, DatabaseAliasLoader
from app.knowledge.entities import EntityRegistry
from app.schemas import ContentAnalysis, Entity


async def seed(entities: EntityRegistry, alias_loader: DatabaseAliasLoader) -> None:
    await entities.resolve_or_create(
        "人工智能", "concept", aliases=["AI", "Artificial Intelligence"]
    )
    await entities.resolve_or_create("中国", "place")
    alias_loader.invalidate()


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
async def test_index_groups_and_lookup(entities: EntityRegistry, alias_loader: DatabaseAliasLoader) -> None:
    await seed(entities, alias_loader)
    index = await alias_loader.load_index()

    assert index.groups == {
        "人工智能": ("AI", "Artificial Intelligence"),
        "中国": (),
    }
    assert index.canonical_of("AI") == "人工智能"
    assert index.canonical_of("Artificial Intelligence") == "人工智能"
    assert index.canonical_of("人工智能") == "人工智能"
    assert index.canonical_of("中国") == "中国"
    assert index.canonical_of("不存在") is None


async def test_lookup_is_case_and_width_folded(
    entities: EntityRegistry, alias_loader: DatabaseAliasLoader
) -> None:
    await seed(entities, alias_loader)
    index = await alias_loader.load_index()
    assert index.canonical_of("ai") == "人工智能"
    assert index.canonical_of("ＡＩ") == "人工智能"


async def test_empty_table_gives_empty_index(alias_loader: DatabaseAliasLoader) -> None:
    index = await alias_loader.load_index()
    assert index == AliasIndex(groups={}, lookup={})


async def test_aliases_for(entities: EntityRegistry, alias_loader: DatabaseAliasLoader) -> None:
    await seed(entities, alias_loader)
    assert await alias_loader.aliases_for("人工智能") == frozenset(
        {"人工智能", "AI", "Artificial Intelligence"}
    )
    assert await alias_loader.aliases_for("AI") == frozenset(
        {"人工智能", "AI", "Artificial Intelligence"}
    )
    assert await alias_loader.aliases_for("中国") == frozenset({"中国"})
    assert await alias_loader.aliases_for("未知") == frozenset({"未知"})


async def test_cache_can_be_bypassed(entities: EntityRegistry, alias_loader: DatabaseAliasLoader) -> None:
    await seed(entities, alias_loader)
    first = await alias_loader.load_index()
    second = await alias_loader.load_index()  # 命中缓存
    assert first is second

    third = await alias_loader.load_index(use_cache=False)
    assert third is not first
    assert third.groups == first.groups


async def test_new_entity_is_invisible_until_invalidated(
    entities: EntityRegistry, alias_loader: DatabaseAliasLoader
) -> None:
    await seed(entities, alias_loader)
    await alias_loader.load_index()

    await entities.resolve_or_create("房地产", "concept")
    assert (await alias_loader.load_index()).canonical_of("房地产") is None

    alias_loader.invalidate()
    assert (await alias_loader.load_index()).canonical_of("房地产") == "房地产"


# --------------------------------------------------------------------------- #
# 接进 Grounding Check
# --------------------------------------------------------------------------- #
async def test_snapshot_is_a_sync_resolver(entities: EntityRegistry, alias_loader: DatabaseAliasLoader) -> None:
    await seed(entities, alias_loader)
    resolver = await alias_loader.snapshot()
    assert resolver.aliases_for("AI") == frozenset({"人工智能", "AI", "Artificial Intelligence"})


async def test_grounding_uses_database_aliases(
    entities: EntityRegistry, alias_loader: DatabaseAliasLoader
) -> None:
    """claim 写的是 ``AI``，源文本写的是 ``人工智能`` —— 别名表让 R2.1 通过。"""
    await seed(entities, alias_loader)
    resolver = await alias_loader.snapshot()

    analysis = ContentAnalysis.model_validate(
        {
            "title": "t",
            "summary": "s",
            "analysis_type": "mixed",
            "claims": [
                {
                    "text": "AI 正在改变内容行业。",
                    "type": "fact",
                    "confidence": 0.8,
                    "evidence": [{"type": "source", "description": "原文提到人工智能"}],
                }
            ],
            "entities": [{"name": "人工智能", "type": "concept"}],
            "overall_confidence": 0.6,
        }
    )

    without_alias, report_without = apply_grounding(analysis, "这段原文讲的是人工智能的落地。")
    with_alias, report_with_alias = apply_grounding(
        analysis, "这段原文讲的是人工智能的落地。", resolver=resolver
    )

    # 没有别名表时：「AI」找不到，但因为 claim 里识别出的实体「人工智能」命中原文 → 仍然通过
    assert RULE_R2_1 not in report_without.verdicts[0].rules
    assert RULE_R2_1 not in report_with_alias.verdicts[0].rules
    assert with_alias.claims[0].needs_verification is False


async def test_database_alias_rescues_fabricated_surface_form(
    entities: EntityRegistry, alias_loader: DatabaseAliasLoader
) -> None:
    """claim 里只有别名，源文本里只有 canonical —— 没有别名表就会误报。"""
    await seed(entities, alias_loader)
    resolver = await alias_loader.snapshot()

    analysis = ContentAnalysis.model_validate(
        {
            "title": "t",
            "summary": "s",
            "analysis_type": "mixed",
            "claims": [
                {
                    "text": "AI 值得关注。",
                    "type": "fact",
                    "confidence": 0.9,
                    "evidence": [{"type": "reference", "description": "原文出处"}],
                }
            ],
            "entities": [{"name": "AI", "type": "concept"}],
            "overall_confidence": 0.6,
        }
    )

    source = "人工智能 是这几年的关键词。"
    _, without = apply_grounding(analysis, source)
    assert RULE_R2_1 in without.verdicts[0].rules

    _, with_alias = apply_grounding(analysis, source, resolver=resolver)
    assert RULE_R2_1 not in with_alias.verdicts[0].rules


# --------------------------------------------------------------------------- #
# 渲染用链接
# --------------------------------------------------------------------------- #
async def test_linked_entities_maps_alias_to_canonical(
    entities: EntityRegistry, alias_loader: DatabaseAliasLoader
) -> None:
    await seed(entities, alias_loader)
    linked = await alias_loader.linked_entities([("AI", "concept"), ("中国", "place")])

    assert [(item.canonical_name, item.entity_type) for item in linked] == [
        ("人工智能", "concept"),
        ("中国", "place"),
    ]
    assert linked[0].aliases == ("AI", "Artificial Intelligence")
    assert linked[1].aliases == ()


async def test_linked_entities_dedupes_by_canonical(
    entities: EntityRegistry, alias_loader: DatabaseAliasLoader
) -> None:
    await seed(entities, alias_loader)
    linked = await alias_loader.linked_entities(
        [("AI", "concept"), ("人工智能", "concept"), ("Artificial Intelligence", "concept")]
    )
    assert len(linked) == 1


async def test_linked_entities_keeps_unknown_names(
    entities: EntityRegistry, alias_loader: DatabaseAliasLoader
) -> None:
    await seed(entities, alias_loader)
    linked = await alias_loader.linked_entities([("未登记实体", "other")])
    assert linked[0].canonical_name == "未登记实体"
    assert linked[0].aliases == ()


async def test_linked_entities_skips_blank(entities: EntityRegistry, alias_loader: DatabaseAliasLoader) -> None:
    assert await alias_loader.linked_entities([("  ", "other")]) == ()


async def test_linked_entities_are_renderable(
    entities: EntityRegistry, alias_loader: DatabaseAliasLoader
) -> None:
    from app.obsidian.renderer import render_entity

    await seed(entities, alias_loader)
    linked = await alias_loader.linked_entities([("AI", "concept")])
    assert render_entity(linked[0]) == "- [[人工智能]]（AI、Artificial Intelligence）"


async def test_entity_optional_link_helper(entities: EntityRegistry) -> None:
    resolved = await entities.resolve_or_create("人工智能", "concept", aliases=["AI"])
    linked = resolved.as_linked()
    assert linked.canonical_name == "人工智能"
    assert linked.aliases == ("AI",)


def test_entity_schema_still_used_by_grounding() -> None:
    assert Entity(name="人工智能", type="concept").name == "人工智能"
