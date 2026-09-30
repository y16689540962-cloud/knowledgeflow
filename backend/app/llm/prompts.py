"""Prompt v1 与响应 JSON Schema（第八节 / 第十一节 / 第十三节）。

单一真源原则：**Pydantic 模型是契约的唯一真源**，``RESPONSE_JSON_SCHEMA``
是它的机器可读镜像。``tests/test_llm_prompts.py`` 用结构化断言把两者钉死，
任何一边漂移都会立刻失败。

Prompt 里必须显式禁止的东西：

* 输出 Markdown
* ``facts`` / ``opinions`` / ``inferences`` / ``predictions`` 四个旧数组
* 顶层 ``needs_verification``
* 直接输出 ``unverified_claims``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Sequence

from app.schemas.analysis import ContentAnalysis
from app.schemas.enums import (
    ANALYSIS_TYPES,
    CLAIM_TYPES,
    ENTITY_TYPES,
    EVIDENCE_TYPES,
)

ANALYSIS_PROMPT_VERSION: Final[str] = "analysis_prompt_v1"
SYNTHESIS_PROMPT_VERSION: Final[str] = "synthesis_prompt_v1"

CONTENT_OPEN: Final[str] = "<content>"
CONTENT_CLOSE: Final[str] = "</content>"

# --------------------------------------------------------------------------- #
# 响应 JSON Schema（Pydantic 模型的镜像）
# --------------------------------------------------------------------------- #
EVIDENCE_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "description"],
    "properties": {
        "type": {"type": "string", "enum": list(EVIDENCE_TYPES)},
        "description": {"type": "string"},
        "source_url": {"type": ["string", "null"]},
        "timestamp": {"type": ["string", "null"]},
    },
}

CLAIM_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "type", "confidence"],
    "properties": {
        "text": {"type": "string"},
        "type": {"type": "string", "enum": list(CLAIM_TYPES)},
        "author_position": {"type": ["string", "null"]},
        "time_horizon": {"type": ["string", "null"]},
        "based_on_claims": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": EVIDENCE_JSON_SCHEMA},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_verification": {"type": "boolean"},
    },
}

ENTITY_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "type"],
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string", "enum": list(ENTITY_TYPES)},
    },
}

RESPONSE_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "analysis_type", "claims", "overall_confidence"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "analysis_type": {"type": "string", "enum": list(ANALYSIS_TYPES)},
        "claims": {"type": "array", "items": CLAIM_JSON_SCHEMA},
        "questions": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": ENTITY_JSON_SCHEMA},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

#: 顶层响应 schema 里**刻意不存在**的字段。
#: 注意 ``needs_verification`` 只禁止出现在**顶层**；``claims[].needs_verification``
#: 是文档第八节 schema 的一部分（模型可以填，但最终由规则层覆盖）。
FORBIDDEN_RESPONSE_KEYS: Final[tuple[str, ...]] = (
    "unverified_claims",
    "needs_verification",
    "facts",
    "opinions",
    "inferences",
    "predictions",
)

#: 在任何层级都**不允许**出现的字段。
FORBIDDEN_ANYWHERE_KEYS: Final[tuple[str, ...]] = (
    "unverified_claims",
    "facts",
    "opinions",
    "inferences",
    "predictions",
)


# --------------------------------------------------------------------------- #
# Prompt 文本
# --------------------------------------------------------------------------- #
_CONTRACT_RULES: Final[str] = """\
输出契约（任何一条不满足都会被程序判为无效输出）：
1. 只输出**一个 JSON 对象**，不要 Markdown、不要代码围栏、不要任何解释文字。
2. 顶层字段只有：title, summary, analysis_type, claims, questions, topics, entities, keywords, overall_confidence。
3. claims 是**唯一**的断言容器。禁止输出 facts / opinions / inferences / predictions 这四个数组。
4. 禁止输出顶层 needs_verification（该字段已废弃）。
5. 禁止输出 unverified_claims —— 它由程序从 claims 派生，不由你判断。
6. claims[].needs_verification 一律填 false，最终验证状态由程序按证据规则决定。
7. 不要编造数据、来源、链接、新闻、论文、统计数字。没有证据就把 evidence 写成 [{"type": "none", "description": "..."}]。
8. claims[].text 必须能被原始内容直接支撑；实体与数字必须真的出现在原始内容里。
"""

_CLAIM_TYPES_DOC: Final[str] = """\
claims[].type 四选一，必须严格区分（这是本产品的核心差异）：
- fact       客观陈述，可由外部来源核实。例：中国人口正在下降。
- opinion    作者的主观判断或立场。例：作者认为房地产未来会上涨。
- inference  作者从前提推出的结论（推理链条本身）。例：作者将人口变化与房地产价格联系起来。
- prediction 对未来的预测，无法用现有数据验证。例：作者预测房地产未来上涨。

反面教材：输入「中国人口正在下降，所以未来房地产一定会大涨。」
→ 「未来房地产一定大涨」**绝不允许**写成 fact / inference，只能写成 prediction（以及作者观点 opinion）。
"""

_ANALYSIS_SYSTEM: Final[str] = """\
你是一名严谨的中文内容分析引擎，负责把互联网碎片内容加工成结构化知识资产。

你的职责是**忠实还原**：把原文中的断言拆成若干 claim，逐条判断它是事实、观点、推论还是预测，
并给出证据。你不做价值判断，不做事实核查，不补充原文没有的信息。

""" + _CONTRACT_RULES + """
""" + _CLAIM_TYPES_DOC

_SYNTHESIS_SYSTEM: Final[str] = """\
你是一名严谨的中文内容分析引擎。现在给你的是**同一份长文档**被切分后各分块的独立分析结果，
请把它们合并成一份**全局**分析结果。

合并要求：
- 去重：同一断言在多个分块重复出现时只保留一条，保留证据最完整的那一条。
- 不丢失：任何分块里出现过的独立断言都必须体现在合并结果中。
- 不新增：禁止加入分块分析里没有的断言、实体、数字、来源。
- claims[].based_on_claims 用于记录合并后仍然成立的依赖关系。
- 数字与实体必须能在原始文档中找到依据；不确定就不要写。

""" + _CONTRACT_RULES + """
""" + _CLAIM_TYPES_DOC


@dataclass(frozen=True)
class Prompt:
    version: str
    system: str
    user: str


def _schema_block() -> str:
    import json

    return json.dumps(RESPONSE_JSON_SCHEMA, ensure_ascii=False, indent=2)


def build_analysis_prompt(
    text: str,
    *,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
    retry_hint: str | None = None,
) -> Prompt:
    """单次（或单分块）分析 prompt。"""
    header = ["请分析下面这份内容，按输出契约返回 JSON。"]
    if chunk_index is not None and chunk_total is not None and chunk_total > 1:
        header.append(
            f"注意：当前是长文档的第 {chunk_index}/{chunk_total} 个分块，"
            "只分析本分块出现的内容，不要推测其他分块。"
        )
    if retry_hint:
        header.append(f"上一次输出未通过校验，原因是：{retry_hint}\n请修正后重新输出完整 JSON。")

    user = "\n".join(
        [
            *header,
            "",
            "响应 JSON Schema：",
            _schema_block(),
            "",
            "待分析内容（原文，勿改写）：",
            CONTENT_OPEN,
            text,
            CONTENT_CLOSE,
        ]
    )
    return Prompt(version=ANALYSIS_PROMPT_VERSION, system=_ANALYSIS_SYSTEM, user=user)


def build_synthesis_prompt(
    chunk_analyses: Sequence[ContentAnalysis],
    *,
    retry_hint: str | None = None,
) -> Prompt:
    """synthesis prompt —— 与单分块走**完全相同**的链路（第十一节）。"""
    import json

    payload = [
        analysis.model_dump(
            mode="json",
            exclude={"unverified_claims"},
        )
        for analysis in chunk_analyses
    ]

    header = [f"请把下面 {len(payload)} 份分块分析结果合并成一份全局分析结果，按输出契约返回 JSON。"]
    if retry_hint:
        header.append(f"上一次输出未通过校验，原因是：{retry_hint}\n请修正后重新输出完整 JSON。")

    user = "\n".join(
        [
            *header,
            "",
            "响应 JSON Schema：",
            _schema_block(),
            "",
            "各分块分析结果：",
            CONTENT_OPEN,
            json.dumps(payload, ensure_ascii=False),
            CONTENT_CLOSE,
        ]
    )
    return Prompt(version=SYNTHESIS_PROMPT_VERSION, system=_SYNTHESIS_SYSTEM, user=user)


def build_retry_hint(reason: str, *, limit: int = 200) -> str:
    """把上一次失败原因压成一句可塞进 prompt 的提示（不含正文）。"""
    cleaned = " ".join((reason or "").split())
    return cleaned[:limit]


__all__ = [
    "ANALYSIS_PROMPT_VERSION",
    "SYNTHESIS_PROMPT_VERSION",
    "RESPONSE_JSON_SCHEMA",
    "CLAIM_JSON_SCHEMA",
    "EVIDENCE_JSON_SCHEMA",
    "ENTITY_JSON_SCHEMA",
    "FORBIDDEN_RESPONSE_KEYS",
    "FORBIDDEN_ANYWHERE_KEYS",
    "Prompt",
    "build_analysis_prompt",
    "build_synthesis_prompt",
    "build_retry_hint",
]
