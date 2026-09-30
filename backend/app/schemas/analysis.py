"""AI 结构化输出 Schema —— 单一真源（定稿文档第八节）。

关键强制条款：

* 只保留 ``claims[]``；``facts[] / opinions[] / inferences[] / predictions[]`` 四个数组已删除，
  出现即拒绝（说明 prompt 或模型没遵守契约，必须显式失败而不是静默丢数据）。
* ``unverified_claims`` 由程序从 ``claims[]`` 派生，LLM 输出的一律忽略并覆盖。
* 顶层不得出现 ``needs_verification``（历史上它曾是数组，造成同名不同型）。
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.enums import AnalysisType, ClaimType, EntityType, EvidenceType

#: 已删除的旧数组名，出现即视为违反契约。
LEGACY_CLAIM_ARRAYS: tuple[str, ...] = ("facts", "opinions", "inferences", "predictions")

#: 顶层禁止出现的字段名。
FORBIDDEN_TOP_LEVEL_FIELDS: tuple[str, ...] = ("needs_verification",)

#: 按 ``type`` 分组渲染 Markdown 时使用的固定顺序（第九节）。
CLAIM_TYPE_ORDER: tuple[ClaimType, ...] = ("fact", "opinion", "inference", "prediction")


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EvidenceType
    description: str
    source_url: str | None = None
    timestamp: str | None = None


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    type: ClaimType
    author_position: str | None = None
    time_horizon: str | None = None
    based_on_claims: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_verification: bool = False

    @field_validator("text", mode="after")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("claim.text 不能为空白")
        return value

    def has_evidence(self) -> bool:
        """R1 的判据：有 evidence 且不是全为 ``none``。"""
        return bool(self.evidence) and not all(e.type == "none" for e in self.evidence)


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: EntityType

    @field_validator("name", mode="after")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("entity.name 不能为空白")
        return value.strip()


class ContentAnalysis(BaseModel):
    """一份完整的 AI 分析结果。

    ``extra="ignore"``：容忍模型多给的无关字段；
    但合约级冲突（旧数组 / 顶层 needs_verification）显式拒绝。
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    summary: str
    analysis_type: AnalysisType
    claims: list[Claim]
    unverified_claims: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def _enforce_single_source_of_truth(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data

        payload = dict(data)

        # 1) LLM 输出的 unverified_claims 一律忽略（由规则层从 claims 派生）。
        payload.pop("unverified_claims", None)

        # 2) 顶层 needs_verification 已被移除 —— 出现即拒绝。
        forbidden = [k for k in FORBIDDEN_TOP_LEVEL_FIELDS if k in payload]
        if forbidden:
            raise ValueError(
                "顶层字段已被移除，验证状态只存在于 claims[].needs_verification："
                + ", ".join(sorted(forbidden))
            )

        # 3) 旧的四个数组必须不存在。
        legacy = [k for k in LEGACY_CLAIM_ARRAYS if k in payload]
        if legacy:
            raise ValueError(
                "旧 claims 数组不被允许，claims[] 是唯一真源：" + ", ".join(sorted(legacy))
            )

        return payload

    # ------------------------------------------------------------------ #
    # 便捷方法
    # ------------------------------------------------------------------ #
    @classmethod
    def from_llm_payload(cls, payload: Any) -> "ContentAnalysis":
        """解析 LLM 输出（已 json.loads 的 dict）。"""
        return cls.model_validate(payload)

    def claims_of_type(self, claim_type: ClaimType) -> list[Claim]:
        """第九节：事实 / 观点 / 推论 / 预测 四个章节按 type 分组渲染。"""
        return [claim for claim in self.claims if claim.type == claim_type]

    def with_unverified_claims(self, values: list[str]) -> "ContentAnalysis":
        """由规则层（R3）覆写 ``unverified_claims``。"""
        return self.model_copy(update={"unverified_claims": list(values)})


__all__ = [
    "Evidence",
    "Claim",
    "Entity",
    "ContentAnalysis",
    "LEGACY_CLAIM_ARRAYS",
    "FORBIDDEN_TOP_LEVEL_FIELDS",
    "CLAIM_TYPE_ORDER",
]
