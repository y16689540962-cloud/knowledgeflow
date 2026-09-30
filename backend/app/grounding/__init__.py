"""Grounding Check 层出口（第十节）。"""

from app.grounding.aliases import (
    AliasResolver,
    CompositeAliasResolver,
    IdentityAliasResolver,
    StaticAliasResolver,
)
from app.grounding.entities import (
    EntityHit,
    describe_hits,
    entities_grounded,
    entity_hits_in_text,
    hit_in_source,
    ungrounded_entities,
)
from app.grounding.folding import fold_for_matching
from app.grounding.numbers import (
    FUZZY_MARKERS,
    NumberToken,
    describe_numbers,
    extract_key_numbers,
    extract_numbers,
    numbers_grounded,
    parse_chinese_number,
    scale_of,
    ungrounded_numbers,
)
from app.grounding.rules import (
    PATH_SEMANTIC,
    PATH_STRUCTURED,
    RULE_DESCRIPTIONS,
    RULE_R1,
    RULE_R2_1,
    RULE_R2_2,
    RULE_R2_3,
    ClaimVerdict,
    GroundingReport,
    apply_grounding,
)

__all__ = [
    "apply_grounding",
    "GroundingReport",
    "ClaimVerdict",
    "RULE_R1",
    "RULE_R2_1",
    "RULE_R2_2",
    "RULE_R2_3",
    "RULE_DESCRIPTIONS",
    "PATH_STRUCTURED",
    "PATH_SEMANTIC",
    "AliasResolver",
    "IdentityAliasResolver",
    "StaticAliasResolver",
    "CompositeAliasResolver",
    "EntityHit",
    "entity_hits_in_text",
    "hit_in_source",
    "entities_grounded",
    "ungrounded_entities",
    "describe_hits",
    "fold_for_matching",
    "NumberToken",
    "extract_numbers",
    "extract_key_numbers",
    "numbers_grounded",
    "ungrounded_numbers",
    "parse_chinese_number",
    "scale_of",
    "describe_numbers",
    "FUZZY_MARKERS",
]
