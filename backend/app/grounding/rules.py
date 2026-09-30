"""规则层：R1 / R2.1 / R2.2 / R2.3 / R3（第十节，强制）。

核心立场：**不信模型自报**。

* 模型给的 ``claims[].needs_verification`` 一律**被覆盖** ——
  最终值完全由规则决定（``needs_verification = bool(触发的规则)``）。
* R3 派生 ``unverified_claims``，模型自己输出的那个字段早已在校验层被丢弃。

R2.3 的可执行读法（文档说「不要求 claim 文本逐字命中原文」）：
对既无实体、又无关键数字的摘要／概括／推论，**不比对文本**；
只有在「有非 ``none`` 证据但证据描述为空」时才判为「无法建立来源支撑」。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final

from app.grounding.aliases import AliasResolver, IdentityAliasResolver
from app.grounding.entities import (
    EntityHit,
    describe_hits,
    entities_grounded,
    entity_hits_in_text,
    ungrounded_entities,
)
from app.grounding.numbers import (
    NumberToken,
    describe_numbers,
    extract_key_numbers,
    numbers_grounded,
    ungrounded_numbers,
)
from app.schemas.analysis import Claim, ContentAnalysis, Entity

RULE_R1: Final[str] = "R1"
RULE_R2_1: Final[str] = "R2.1"
RULE_R2_2: Final[str] = "R2.2"
RULE_R2_3: Final[str] = "R2.3"

RULE_DESCRIPTIONS: Final[dict[str, str]] = {
    RULE_R1: "没有任何非 none 证据",
    RULE_R2_1: "claim 中出现的实体在源文本里全部找不到",
    RULE_R2_2: "claim 中的关键数字在源文本里找不到对应值",
    RULE_R2_3: "概括性断言缺少可追溯的来源支撑",
}

#: claim 的比对路径。
PATH_STRUCTURED: Final[str] = "structured"
PATH_SEMANTIC: Final[str] = "semantic"


@dataclass(frozen=True)
class ClaimVerdict:
    index: int
    text: str
    claim_type: str
    needs_verification: bool
    rules: tuple[str, ...]
    path: str
    entities: tuple[str, ...]
    ungrounded_entities: tuple[str, ...]
    numbers: tuple[str, ...]
    ungrounded_numbers: tuple[str, ...]

    @property
    def detail(self) -> str:
        if not self.rules:
            return "证据与来源可追溯"
        reasons = "；".join(RULE_DESCRIPTIONS[rule] for rule in self.rules)
        extras: list[str] = []
        if self.ungrounded_entities:
            extras.append("未命中实体：" + ", ".join(self.ungrounded_entities))
        if self.ungrounded_numbers:
            extras.append("未命中数字：" + ", ".join(self.ungrounded_numbers))
        return reasons + ("（" + "；".join(extras) + "）" if extras else "")


@dataclass(frozen=True)
class GroundingReport:
    verdicts: tuple[ClaimVerdict, ...]
    rules_fired: dict[str, int]
    source_length: int

    @property
    def total_claims(self) -> int:
        return len(self.verdicts)

    @property
    def needs_verification_count(self) -> int:
        return sum(1 for verdict in self.verdicts if verdict.needs_verification)

    @property
    def verified_count(self) -> int:
        return self.total_claims - self.needs_verification_count

    def unverified_texts(self) -> tuple[str, ...]:
        return tuple(v.text for v in self.verdicts if v.needs_verification)


def _evaluate_claim(
    index: int,
    claim: Claim,
    *,
    entities: tuple[Entity, ...],
    source_text: str,
    source_numbers: tuple[NumberToken, ...],
    resolver: AliasResolver,
) -> ClaimVerdict:
    hits: tuple[EntityHit, ...] = entity_hits_in_text(claim.text, entities, resolver)
    claim_numbers = extract_key_numbers(claim.text)

    rules: list[str] = []

    # R1 —— 无证据即需验证
    if not claim.has_evidence():
        rules.append(RULE_R1)

    # R2.1 —— Entity grounding（any 语义）
    if hits and not entities_grounded(hits, source_text):
        rules.append(RULE_R2_1)

    # R2.2 —— Numeric grounding（all 语义）
    missing_numbers: tuple[NumberToken, ...] = ()
    if claim_numbers:
        missing_numbers = ungrounded_numbers(claim_numbers, source_numbers)
        if missing_numbers:
            rules.append(RULE_R2_2)

    # R2.3 —— Semantic/evidence grounding（只对「无实体且无数字」的概括性断言生效）
    is_semantic = not hits and not claim_numbers
    if is_semantic and RULE_R1 not in rules:
        has_support = any(
            evidence.description.strip()
            for evidence in claim.evidence
            if evidence.type != "none"
        )
        if not has_support:
            rules.append(RULE_R2_3)

    return ClaimVerdict(
        index=index,
        text=claim.text,
        claim_type=claim.type,
        needs_verification=bool(rules),
        rules=tuple(rules),
        path=PATH_SEMANTIC if is_semantic else PATH_STRUCTURED,
        entities=tuple(hit.name for hit in hits),
        ungrounded_entities=ungrounded_entities(hits, source_text),
        numbers=tuple(token.raw for token in claim_numbers),
        ungrounded_numbers=tuple(token.raw for token in missing_numbers),
    )


def apply_grounding(
    analysis: ContentAnalysis,
    source_text: str,
    *,
    resolver: AliasResolver | None = None,
) -> tuple[ContentAnalysis, GroundingReport]:
    """执行 R1 / R2.x，并按 R3 派生 ``unverified_claims``。

    返回**新的** ``ContentAnalysis``（不修改入参），其
    ``claims[].needs_verification`` 与 ``unverified_claims`` 完全由规则决定。
    """
    resolved = resolver or IdentityAliasResolver()
    entities: tuple[Entity, ...] = tuple(analysis.entities)
    source_body = source_text or ""
    source_numbers = extract_key_numbers(source_body)

    verdicts = tuple(
        _evaluate_claim(
            index,
            claim,
            entities=entities,
            source_text=source_body,
            source_numbers=source_numbers,
            resolver=resolved,
        )
        for index, claim in enumerate(analysis.claims)
    )

    updated_claims = [
        claim.model_copy(update={"needs_verification": verdict.needs_verification})
        for claim, verdict in zip(analysis.claims, verdicts)
    ]
    unverified_claims = [claim.text for claim in updated_claims if claim.needs_verification]

    updated = analysis.model_copy(
        update={"claims": updated_claims, "unverified_claims": unverified_claims}
    )

    counter: Counter[str] = Counter()
    for verdict in verdicts:
        counter.update(verdict.rules)

    report = GroundingReport(
        verdicts=verdicts,
        rules_fired=dict(sorted(counter.items())),
        source_length=len(source_body),
    )
    return updated, report


__all__ = [
    "RULE_R1",
    "RULE_R2_1",
    "RULE_R2_2",
    "RULE_R2_3",
    "RULE_DESCRIPTIONS",
    "PATH_STRUCTURED",
    "PATH_SEMANTIC",
    "ClaimVerdict",
    "GroundingReport",
    "apply_grounding",
    "describe_hits",
    "describe_numbers",
]
