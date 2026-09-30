"""实体匹配与别名解析（R2.1，第二十一节）。"""

from __future__ import annotations

import pytest

from app.grounding.aliases import (
    CompositeAliasResolver,
    IdentityAliasResolver,
    StaticAliasResolver,
)
from app.grounding.entities import (
    describe_hits,
    entities_grounded,
    entity_hits_in_text,
    hit_in_source,
    ungrounded_entities,
)
from app.schemas import Entity

SOURCE = "中国人口正在下降，所以未来房地产一定会大涨。"


def entity(name: str, type_: str = "concept") -> Entity:
    return Entity(name=name, type=type_)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# AliasResolver
# --------------------------------------------------------------------------- #
def test_identity_resolver_returns_own_name() -> None:
    assert IdentityAliasResolver().aliases_for("人工智能") == frozenset({"人工智能"})


def test_identity_resolver_empty_name() -> None:
    assert IdentityAliasResolver().aliases_for("   ") == frozenset()


def test_static_resolver_forward_lookup() -> None:
    resolver = StaticAliasResolver.from_pairs([("人工智能", ["AI", "Artificial Intelligence"])])
    assert resolver.aliases_for("人工智能") == frozenset(
        {"人工智能", "AI", "Artificial Intelligence"}
    )


def test_static_resolver_reverse_lookup() -> None:
    """拿别名去问，也要回到同一组（alias 表查询是双向的）。"""
    resolver = StaticAliasResolver.from_pairs([("人工智能", ["AI", "Artificial Intelligence"])])
    assert resolver.aliases_for("AI") == frozenset({"人工智能", "AI", "Artificial Intelligence"})


def test_static_resolver_reverse_lookup_is_case_insensitive() -> None:
    resolver = StaticAliasResolver.from_pairs([("人工智能", ["AI"])])
    assert "人工智能" in resolver.aliases_for("ai")


def test_static_resolver_unknown_name_falls_back_to_itself() -> None:
    resolver = StaticAliasResolver.from_pairs([("人工智能", ["AI"])])
    assert resolver.aliases_for("房地产") == frozenset({"房地产"})


def test_composite_resolver_unions() -> None:
    first = StaticAliasResolver.from_pairs([("房产", ["楼市"])])
    second = StaticAliasResolver.from_pairs([("房产", ["不动产"])])
    resolver = CompositeAliasResolver(resolvers=(first, second))
    assert resolver.aliases_for("房产") == frozenset({"房产", "楼市", "不动产"})


def test_resolver_protocol_is_runtime_checkable() -> None:
    from app.grounding.aliases import AliasResolver

    assert isinstance(IdentityAliasResolver(), AliasResolver)


# --------------------------------------------------------------------------- #
# 实体命中
# --------------------------------------------------------------------------- #
def test_canonical_name_hit() -> None:
    hits = entity_hits_in_text(SOURCE, [entity("房地产")])
    assert [hit.name for hit in hits] == ["房地产"]
    assert hits[0].matched_alias == "房地产"


def test_entity_absent_from_text() -> None:
    assert entity_hits_in_text(SOURCE, [entity("人工智能")]) == ()


def test_alias_hit() -> None:
    resolver = StaticAliasResolver.from_pairs([("人工智能", ["AI"])])
    hits = entity_hits_in_text("AI 已经改变了这个行业。", [entity("人工智能")], resolver)
    assert len(hits) == 1
    assert hits[0].matched_alias == "AI"
    assert hits[0].name == "人工智能"


def test_alias_hit_is_case_insensitive() -> None:
    resolver = StaticAliasResolver.from_pairs([("人工智能", ["AI"])])
    assert entity_hits_in_text("ai 很有用", [entity("人工智能")], resolver)


def test_alias_hit_is_fullwidth_tolerant() -> None:
    resolver = StaticAliasResolver.from_pairs([("人工智能", ["AI"])])
    assert entity_hits_in_text("ＡＩ 很有用", [entity("人工智能")], resolver)


def test_longest_alias_wins() -> None:
    resolver = StaticAliasResolver.from_pairs([("人工智能", ["AI", "Artificial Intelligence"])])
    hits = entity_hits_in_text("Artificial Intelligence 很热", [entity("人工智能")], resolver)
    assert hits[0].matched_alias == "Artificial Intelligence"


def test_multiple_entities_hit() -> None:
    hits = entity_hits_in_text(SOURCE, [entity("中国", "place"), entity("房地产")])
    assert {hit.name for hit in hits} == {"中国", "房地产"}


def test_empty_text_yields_no_hits() -> None:
    assert entity_hits_in_text("", [entity("中国", "place")]) == ()


def test_no_entities_yields_no_hits() -> None:
    assert entity_hits_in_text(SOURCE, []) == ()


def test_entity_via_group_alias() -> None:
    """分析里给的是「房地产」，别名表记在「房产」组下 —— 也要能查到。"""
    resolver = StaticAliasResolver.from_pairs([("房产", ["房地产", "楼市"])])
    hits = entity_hits_in_text(SOURCE, [entity("房地产")], resolver)
    assert hits and hits[0].name == "房地产"


# --------------------------------------------------------------------------- #
# 地面化判定（any 语义）
# --------------------------------------------------------------------------- #
def test_hit_in_source() -> None:
    hit = entity_hits_in_text(SOURCE, [entity("房地产")])[0]
    assert hit_in_source(hit, SOURCE)
    assert not hit_in_source(hit, "完全无关的一段文字。")


def test_entities_grounded_any_semantics() -> None:
    """规约是 any：至少一个实体命中即通过。"""
    hits = entity_hits_in_text(
        "国际货币基金组织与中国的判断一致。", [entity("国际货币基金组织", "organization"), entity("中国", "place")]
    )
    assert len(hits) == 2
    assert entities_grounded(hits, SOURCE) is True


def test_entities_grounded_all_missing() -> None:
    hits = entity_hits_in_text(
        "世界银行给出了不同结论。", [entity("世界银行", "organization")]
    )
    assert entities_grounded(hits, SOURCE) is False


def test_entities_grounded_empty_hits_passes() -> None:
    assert entities_grounded((), "任何文本") is True


def test_ungrounded_entities_lists_all_missing() -> None:
    hits = entity_hits_in_text(
        "世界银行与国际货币基金组织的结论。",
        [entity("世界银行", "organization"), entity("国际货币基金组织", "organization"), entity("中国", "place")],
    )
    assert set(ungrounded_entities(hits, SOURCE)) == {"世界银行", "国际货币基金组织"}


def test_describe_hits() -> None:
    hits = entity_hits_in_text(SOURCE, [entity("中国", "place"), entity("房地产")])
    assert describe_hits(hits) == "中国, 房地产"


# --------------------------------------------------------------------------- #
# 真实 fixture 场景
# --------------------------------------------------------------------------- #
def test_grounding_fail_fixture_entity_is_fabricated(grounding_fail_payload: dict, mixed_raw_content) -> None:
    from app.schemas import ContentAnalysis

    analysis = ContentAnalysis.model_validate(grounding_fail_payload)
    source = mixed_raw_content.raw_text or ""
    hits = entity_hits_in_text(analysis.claims[3].text, analysis.entities)
    assert [hit.name for hit in hits] == ["世界银行"]
    assert entities_grounded(hits, source) is False


@pytest.mark.parametrize("claim_index", [0, 1, 2])
def test_first_three_claims_have_no_ungrounded_entity(
    grounding_fail_payload: dict, mixed_raw_content, claim_index: int
) -> None:
    """前三条都提到了真实存在的「中国」/「房地产」，any 语义下不该被 R2.1 判失败。"""
    from app.schemas import ContentAnalysis

    analysis = ContentAnalysis.model_validate(grounding_fail_payload)
    source = mixed_raw_content.raw_text or ""
    hits = entity_hits_in_text(analysis.claims[claim_index].text, analysis.entities)
    assert hits
    assert entities_grounded(hits, source) is True


def test_only_last_claim_has_ungrounded_entities(
    grounding_fail_payload: dict, mixed_raw_content
) -> None:
    from app.schemas import ContentAnalysis

    analysis = ContentAnalysis.model_validate(grounding_fail_payload)
    source = mixed_raw_content.raw_text or ""
    fired = [
        index
        for index, claim in enumerate(analysis.claims)
        if not entities_grounded(
            entity_hits_in_text(claim.text, analysis.entities), source
        )
    ]
    assert fired == [3]
