"""任务状态恢复（定稿文档第十七节，强制）。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from app.db.models import Content
from app.db.repository import ContentRepository
from app.db.session import Database
from app.pipeline.recovery import TaskRecovery
from app.pipeline.service import ProcessingPipeline
from app.providers import MockProvider
from app.testing.providers import ScriptedProvider
from app.utils import parse_iso, to_iso, utc_now

from tests.conftest import insert_raw


async def age_content(db: Database, content_id: str, *, seconds: int) -> None:
    """把 ``created_at`` 往前推，模拟「跑了很久还没结束」。"""
    stale = to_iso(utc_now() - timedelta(seconds=seconds))
    async with db.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Content).where(Content.id == content_id).values(created_at=stale)
            )


async def set_status(db: Database, content_id: str, status: str) -> None:
    async with db.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Content).where(Content.id == content_id).values(status=status)
            )


async def get_status(db: Database, content_id: str) -> dict[str, object]:
    async with db.session_factory() as session:
        row = (
            await session.execute(select(Content).where(Content.id == content_id))
        ).scalar_one()
        return {
            "status": row.status,
            "error_type": row.error_type,
            "error_message": row.error_message,
            "processed_at": row.processed_at,
        }


# --------------------------------------------------------------------------- #
# TaskRecovery
# --------------------------------------------------------------------------- #
async def test_no_stale_tasks(db: Database, repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    await set_status(db, content_id, "processing")
    recovery = TaskRecovery(repo, timeout_seconds=900)
    assert await recovery.find_stale() == ()
    assert await recovery.reset_stale() == ()


async def test_stale_processing_task_is_found(db: Database, repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    await set_status(db, content_id, "processing")
    await age_content(db, content_id, seconds=1800)

    recovery = TaskRecovery(repo, timeout_seconds=900)
    stale = await recovery.find_stale()
    assert [item.content_id for item in stale] == [content_id]


async def test_fresh_processing_task_is_not_touched(db: Database, repo, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    await set_status(db, content_id, "processing")
    recovery = TaskRecovery(repo, timeout_seconds=900)
    assert await recovery.find_stale() == ()
    assert await get_status(db, content_id) == {
        "status": "processing",
        "error_type": None,
        "error_message": None,
        "processed_at": None,
    }


async def test_completed_task_is_not_touched(db: Database, repo, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    await set_status(db, content_id, "completed")
    await age_content(db, content_id, seconds=3600)
    recovery = TaskRecovery(repo, timeout_seconds=900)
    assert await recovery.reset_stale() == ()
    assert (await get_status(db, content_id))["status"] == "completed"


async def test_reset_sets_pending_and_clears_errors(db: Database, repo, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    await set_status(db, content_id, "processing")
    async with db.session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Content)
                .where(Content.id == content_id)
                .values(error_type="OLD_ERROR", error_message="上一次失败了")
            )
    await age_content(db, content_id, seconds=1800)

    recovery = TaskRecovery(repo, timeout_seconds=900)
    reset = await recovery.reset_stale()
    assert reset == (content_id,)

    state = await get_status(db, content_id)
    assert state["status"] == "pending"
    assert state["error_type"] is None
    assert state["error_message"] is None
    assert state["processed_at"] is None


async def test_cutoff_uses_timeout(db: Database, repo: ContentRepository) -> None:
    recovery = TaskRecovery(repo, timeout_seconds=900)
    assert parse_iso(recovery.cutoff()) < utc_now()
    assert recovery.timeout_seconds == 900


async def test_zero_timeout_resets_everything_processing(db: Database, repo, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    await set_status(db, content_id, "processing")
    recovery = TaskRecovery(repo, timeout_seconds=0)
    assert await recovery.reset_stale() == (content_id,)


# --------------------------------------------------------------------------- #
# recover_and_resume
# --------------------------------------------------------------------------- #
async def test_recover_and_resume_reprocesses(
    pipeline: ProcessingPipeline, db: Database, repo: ContentRepository, raw_content, vault_root
) -> None:
    content_id = await insert_raw(repo, raw_content)
    await set_status(db, content_id, "processing")
    await age_content(db, content_id, seconds=3600)

    report = await pipeline.recover_and_resume()

    assert report.reset == (content_id,)
    assert report.resumed == (content_id,)
    assert report.resume_failures == ()
    assert report.timeout_seconds == 900

    state = await get_status(db, content_id)
    assert state["status"] == "completed"
    assert await repo.count_analyses(content_id) == 1
    assert (vault_root / "KnowledgeFlow" / "Processed").is_dir()


async def test_recover_and_resume_with_nothing_stale(pipeline: ProcessingPipeline) -> None:
    report = await pipeline.recover_and_resume()
    assert report.reset == ()
    assert report.resumed == ()
    assert report.reset_count == 0


async def test_recover_and_resume_records_failures(
    pipeline_factory, db, repo, raw_content, invalid_analysis_text: str
) -> None:
    content_id = await insert_raw(repo, raw_content)
    await set_status(db, content_id, "processing")
    await age_content(db, content_id, seconds=3600)

    pipeline = pipeline_factory(ScriptedProvider([invalid_analysis_text] * 3))
    report = await pipeline.recover_and_resume()

    assert report.reset == (content_id,)
    assert report.resumed == ()
    assert report.resume_failures == ((content_id, "LLM_INVALID_OUTPUT"),)


async def test_recovery_report_is_serializable(pipeline: ProcessingPipeline) -> None:
    import json

    report = await pipeline.recover_and_resume()
    assert json.loads(json.dumps(report.to_dict()))["timeout_seconds"] == 900


# --------------------------------------------------------------------------- #
# Repository 支持
# --------------------------------------------------------------------------- #
async def test_list_by_status(db: Database, repo: ContentRepository, raw_content, mixed_raw_content) -> None:
    first = await insert_raw(repo, raw_content)
    second = await insert_raw(repo, mixed_raw_content)
    # 同一秒写入 → created_at 相同，顺序不保证，按集合断言
    assert {row.id for row in await repo.list_by_status("pending")} == {first, second}
    assert await repo.list_by_status("completed") == []
    assert [row.id for row in await repo.list_by_status("pending")] == sorted([first, second])


async def test_list_stale_status(db: Database, repo: ContentRepository, raw_content) -> None:
    content_id = await insert_raw(repo, raw_content)
    await age_content(db, content_id, seconds=3600)
    cutoff = to_iso(utc_now() - timedelta(seconds=60))
    assert [row.id for row in await repo.list_stale_status("pending", cutoff)] == [content_id]
    cutoff_recent = to_iso(utc_now() - timedelta(seconds=7200))
    assert await repo.list_stale_status("pending", cutoff_recent) == []


async def test_reset_to_pending_empty(db: Database, repo: ContentRepository) -> None:
    assert await repo.reset_to_pending([]) == ()


async def test_update_payload_keeps_identity(db: Database, repo, raw_content) -> None:
    from app.db.mappers import ContentInsert
    from app.normalization import normalize_content

    content_id = await insert_raw(repo, raw_content)
    before = await repo.get_content(content_id)
    assert before is not None

    changed = raw_content.model_copy(update={"title": "新标题"})
    insert = ContentInsert.from_normalized(normalize_content(changed), content_id=content_id)
    assert await repo.update_payload(content_id, insert) is True

    after = await repo.get_content(content_id)
    assert after is not None
    assert after.title == "新标题"
    assert after.id == before.id
    assert after.created_at == before.created_at
    assert after.status == before.status
    assert after.current_analysis_id == before.current_analysis_id


async def test_update_status_processing_clears_previous_error(
    db: Database, repo: ContentRepository, raw_content
) -> None:
    content_id = await insert_raw(repo, raw_content)
    await repo.update_status(content_id, "failed", error_type="X", error_message="boom")
    await repo.update_status(content_id, "processing")
    state = await get_status(db, content_id)
    assert state == {"status": "processing", "error_type": None, "error_message": None, "processed_at": None}


async def test_pipeline_uses_configured_timeout(db, pipeline_factory, raw_content) -> None:
    from app.config import Settings

    settings = Settings(_env_file=None, task_reset_timeout_seconds=42)  # type: ignore[call-arg]
    pipeline = pipeline_factory(MockProvider(payload={}), settings=settings)
    assert pipeline.recovery.timeout_seconds == 42
