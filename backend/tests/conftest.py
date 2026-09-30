"""pytest 公共夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:  # pragma: no cover - 保证 `import app` 可用
    sys.path.insert(0, str(BACKEND_ROOT))

from app.chunking.budget import TextBudget  # noqa: E402
from app.api import create_app  # noqa: E402
from app.config import Settings, env_template_keys  # noqa: E402
from app.db.mappers import ContentInsert  # noqa: E402
from app.db.repository import ContentRepository  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.grounding.aliases import StaticAliasResolver  # noqa: E402
from app.grounding.rules import apply_grounding  # noqa: E402
from app.knowledge.aliases import DatabaseAliasLoader  # noqa: E402
from app.knowledge.entities import EntityRegistry  # noqa: E402
from app.knowledge.topics import TopicRegistry  # noqa: E402
from app.llm.prompts import ANALYSIS_PROMPT_VERSION  # noqa: E402
from app.llm.service import LLMAnalysisService  # noqa: E402
from app.normalization.service import normalize_content  # noqa: E402
from app.obsidian.renderer import LinkedEntity, NoteContext  # noqa: E402
from app.obsidian.writer import ObsidianWriter  # noqa: E402
from app.pipeline.service import ProcessingPipeline  # noqa: E402
from app.providers.base import LLMProvider  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402
from app.schemas import ContentAnalysis, RawContent  # noqa: E402
from app.testing.fixture_loader import load_fixture_json, load_fixture_text  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉真实环境里可能与 .env 同名的变量，保证测试确定性。"""
    for key in env_template_keys():
        monkeypatch.delenv(key, raising=False)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    database = Database.create(f"sqlite:///{tmp_path / 'knowledgeflow-test.db'}")
    await database.create_all()
    try:
        yield database
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def repo(db: Database) -> ContentRepository:
    return ContentRepository(db.session_factory)


@pytest.fixture
def raw_payload() -> dict:
    return load_fixture_json("raw_content.json")


@pytest.fixture
def raw_content(raw_payload: dict) -> RawContent:
    return RawContent.from_fixture(raw_payload)


@pytest.fixture
def mixed_payload() -> dict:
    return load_fixture_json("raw_content_mixed.json")


@pytest.fixture
def mixed_raw_content(mixed_payload: dict) -> RawContent:
    return RawContent.from_fixture(mixed_payload)


@pytest.fixture
def long_payload() -> dict:
    return load_fixture_json("raw_content_long.json")


@pytest.fixture
def long_raw_content(long_payload: dict) -> RawContent:
    return RawContent.from_fixture(long_payload)


@pytest.fixture
def valid_analysis_payload() -> dict:
    return load_fixture_json("llm_analysis_valid.json")


@pytest.fixture
def valid_analysis_text() -> str:
    return load_fixture_text("llm_analysis_valid.json")


@pytest.fixture
def invalid_analysis_text() -> str:
    return load_fixture_text("llm_analysis_invalid.json")


@pytest.fixture
def fenced_analysis_text() -> str:
    return load_fixture_text("llm_analysis_fenced.json")


@pytest.fixture
def preamble_analysis_text() -> str:
    return load_fixture_text("llm_analysis_preamble.json")


@pytest.fixture
def grounding_fail_payload() -> dict:
    return load_fixture_json("llm_analysis_grounding_fail.json")


# --------------------------------------------------------------------------- #
# Phase 2：LLM 分析
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings_defaults() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def default_budget(settings_defaults: Settings) -> TextBudget:
    return TextBudget.from_settings(settings_defaults)


@pytest.fixture
def alias_resolver() -> StaticAliasResolver:
    return StaticAliasResolver.from_pairs(
        [
            ("人工智能", ["AI", "Artificial Intelligence"]),
            ("房产", ["房地产", "楼市"]),
        ]
    )


def make_service(
    provider: LLMProvider,
    *,
    budget: TextBudget | None = None,
    max_attempts: int = 3,
    alias_resolver=None,
    prompt_reserve_chars: int = 4000,
    logger=None,
    overlap_sentences: int = 1,
) -> LLMAnalysisService:
    """构造一个只依赖显式参数的 LLM 分析服务（不读 .env）。"""
    return LLMAnalysisService(
        provider,
        budget=budget or TextBudget(12000, 6000, 24000),
        max_attempts=max_attempts,
        alias_resolver=alias_resolver,
        prompt_reserve_chars=prompt_reserve_chars,
        logger=logger,
        overlap_sentences=overlap_sentences,
    )


def sentence_series(count: int, *, sentence_chars: int = 12) -> str:
    """造一段由**等长但各不相同**的句子组成的文本。

    句子必须可区分，否则「overlap 是否生效」的断言会因为句子内容相同而失效。
    """
    if sentence_chars < 4:
        raise ValueError("sentence_chars 至少为 4")
    parts: list[str] = []
    for index in range(1, count + 1):
        marker = f"{index:03d}"
        filler = "字" * (sentence_chars - 1 - len(marker))
        parts.append(f"{marker}{filler}。")
    return "".join(parts)


async def insert_raw(
    repo: ContentRepository, raw: RawContent, *, content_id: str | None = None
) -> str:
    """把一条 RawContent 走 normalize → insert 落库，返回 content_id。"""
    normalized = normalize_content(raw)
    insert = ContentInsert.from_normalized(normalized, content_id=content_id)
    return await repo.insert_content(insert)


# --------------------------------------------------------------------------- #
# Phase 3：Obsidian + 知识层
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def entities(db: Database) -> EntityRegistry:
    return EntityRegistry(db.session_factory)


@pytest_asyncio.fixture
async def topics(db: Database) -> TopicRegistry:
    return TopicRegistry(db.session_factory)


@pytest_asyncio.fixture
async def alias_loader(db: Database) -> DatabaseAliasLoader:
    return DatabaseAliasLoader(session_factory=db.session_factory)


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


@pytest.fixture
def obsidian_writer(vault_root: Path) -> ObsidianWriter:
    return ObsidianWriter(vault_root)


@pytest.fixture
def grounded_analysis(mixed_raw_content: RawContent, valid_analysis_payload: dict) -> ContentAnalysis:
    """跑过规则层的分析结果（unverified_claims 由 R3 派生）。"""
    analysis = ContentAnalysis.model_validate(valid_analysis_payload)
    grounded, _ = apply_grounding(analysis, mixed_raw_content.source_text())
    return grounded


@pytest.fixture
def note_context(grounded_analysis: ContentAnalysis, mixed_raw_content: RawContent) -> NoteContext:
    return NoteContext(
        content_id="0f1e2d3c4b5a6978",
        source="manual",
        source_id="hash:0123456789abcdef",
        analysis=grounded_analysis,
        source_url=mixed_raw_content.source_url,
        title=mixed_raw_content.title,
        author=mixed_raw_content.author,
        media_type="video",
        created_at="2026-09-30T00:10:50Z",
        processed_at="2026-09-30T00:12:00Z",
        needs_manual_review=True,
        content_hash_version=1,
        prompt_version=ANALYSIS_PROMPT_VERSION,
        model="mock-llm-v1",
        entities=(
            LinkedEntity(canonical_name="中国", entity_type="place"),
            LinkedEntity(
                canonical_name="人工智能",
                aliases=("AI", "Artificial Intelligence"),
                entity_type="concept",
            ),
        ),
        topics=("人口结构", "房地产"),
        raw_text=mixed_raw_content.raw_text,
        transcript=None,
        ocr_text=None,
    )


# --------------------------------------------------------------------------- #
# Phase 4：Pipeline
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def pipeline_factory(db: Database, settings_defaults: Settings, vault_root: Path):
    """``pipeline_factory(provider, **kwargs)`` → 一个接好测试库与临时 vault 的 Pipeline。"""

    def _factory(provider: LLMProvider | None = None, **kwargs):
        return ProcessingPipeline.from_database(
            settings=kwargs.pop("settings", settings_defaults),
            provider=provider if provider is not None else MockProvider(payload={}),
            database=db,
            vault_root=kwargs.pop("vault_root", vault_root),
            **kwargs,
        )

    return _factory


# --------------------------------------------------------------------------- #
# Phase 9：Web UI
# --------------------------------------------------------------------------- #
@pytest.fixture
def web_app(
    db: Database, settings_defaults: Settings, vault_root: Path, valid_analysis_payload: dict
) -> FastAPI:
    """挂载了静态页的完整应用（`tests/test_web_ui*.py` 共用）。"""
    return create_app(
        settings=Settings(
            _env_file=None,  # type: ignore[call-arg]
            obsidian_vault_path=str(vault_root),
        ),
        database=db,
        provider=MockProvider(payload=valid_analysis_payload),
        vault_root=vault_root,
        configure_logging=False,
        run_startup_recovery=False,
        mount_web=True,
    )


@pytest_asyncio.fixture
async def pipeline(
    pipeline_factory, valid_analysis_payload: dict
) -> ProcessingPipeline:
    return pipeline_factory(MockProvider(payload=valid_analysis_payload))
