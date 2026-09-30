"""实体合并的 HTTP 面（第二十一节 + 第二十八节）。

实体是「知识资产」而不是内容，所以这里不跑 A 线：只用真实 HTTP 往返验证
``GET /api/entities`` 与 ``POST /api/entities/merge`` 的契约与状态码派生。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.db.models import Entity
from app.db.session import Database
from app.knowledge.entities import EntityRegistry
from app.utils import new_id

from tests.test_api_app import running  # noqa: F401  —— 复用「带 lifespan 的真客户端」


def _registry(app: FastAPI) -> EntityRegistry:
    return app.state.container.pipeline.entities


async def _insert_entity(db: Database, canonical_name: str) -> str:
    """直接造实体行：用来构造「源 canonical 正好是别人的别名」这种冲突。"""
    entity_id = new_id()
    async with db.session_factory() as session:
        async with session.begin():
            session.add(
                Entity(
                    id=entity_id,
                    canonical_name=canonical_name,
                    entity_type="concept",
                    description=None,
                )
            )
    return entity_id


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
async def test_list_entities_is_empty_at_first(web_app: FastAPI) -> None:
    async with running(web_app) as client:
        response = await client.get("/api/entities")

    assert response.status_code == 200
    assert response.json() == {"total": 0, "items": []}


async def test_list_entities_returns_aliases(web_app: FastAPI) -> None:
    await _registry(web_app).resolve_or_create("人工智能", "concept", aliases=["AI"])

    async with running(web_app) as client:
        response = await client.get("/api/entities")

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["canonical_name"] == "人工智能"
    assert body["items"][0]["aliases"] == ["AI"]


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
async def test_merge_entities(web_app: FastAPI) -> None:
    registry = _registry(web_app)
    target = await registry.resolve_or_create("深度学习", "concept")
    source = await registry.resolve_or_create("Deep Learning", "concept", aliases=["DL"])

    async with running(web_app) as client:
        response = await client.post(
            "/api/entities/merge",
            json={"source_id": source.entity_id, "target_id": target.entity_id},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["source_deleted"] is True
    assert sorted(body["aliases_moved"]) == ["DL", "Deep Learning"]
    assert body["alias_conflicts"] == []
    assert sorted(await registry.aliases_for(target.entity_id)) == ["DL", "Deep Learning"]


async def test_merge_dry_run_does_not_write(web_app: FastAPI) -> None:
    registry = _registry(web_app)
    target = await registry.resolve_or_create("深度学习", "concept")
    source = await registry.resolve_or_create("Deep Learning", "concept")

    async with running(web_app) as client:
        response = await client.post(
            "/api/entities/merge",
            json={
                "source_id": source.entity_id,
                "target_id": target.entity_id,
                "dry_run": True,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is False
    assert body["source_deleted"] is False
    assert await registry.get_by_id(source.entity_id) is not None
    assert await registry.aliases_for(target.entity_id) == ()


async def test_merge_unknown_entity_is_404(web_app: FastAPI) -> None:
    target = await _registry(web_app).resolve_or_create("深度学习", "concept")

    async with running(web_app) as client:
        response = await client.post(
            "/api/entities/merge",
            json={"source_id": "not-there", "target_id": target.entity_id},
        )

    assert response.status_code == 404
    assert response.json()["error_type"] == "ENTITY_NOT_FOUND"


async def test_merge_to_itself_is_400(web_app: FastAPI) -> None:
    entity = await _registry(web_app).resolve_or_create("深度学习", "concept")

    async with running(web_app) as client:
        response = await client.post(
            "/api/entities/merge",
            json={"source_id": entity.entity_id, "target_id": entity.entity_id},
        )

    assert response.status_code == 400


async def test_merge_conflict_keeps_source(web_app: FastAPI, db: Database) -> None:
    """别名被第三方占着 → 不抢不改，源实体保留，冲突如实回传。"""
    registry = _registry(web_app)
    third = await registry.resolve_or_create("神经网络", "concept", aliases=["DL"])
    target = await registry.resolve_or_create("深度学习", "concept")
    source_id = await _insert_entity(db, "DL")

    async with running(web_app) as client:
        response = await client.post(
            "/api/entities/merge",
            json={"source_id": source_id, "target_id": target.entity_id},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["alias_conflicts"] == [
        {"alias": "DL", "existing_entity_id": third.entity_id}
    ]
    assert body["source_deleted"] is False
    # 别名没被抢走
    assert [
        item.canonical_name for item in await registry.find_by_alias("DL")
    ] == ["神经网络"]


async def test_merge_request_rejects_unknown_field(web_app: FastAPI) -> None:
    """请求体严格（``extra="forbid"``）：拼错字段名不会被静默忽略。"""
    target = await _registry(web_app).resolve_or_create("深度学习", "concept")

    async with running(web_app) as client:
        response = await client.post(
            "/api/entities/merge",
            json={"source_id": "x", "target_id": target.entity_id, "srouce": "typo"},
        )

    assert response.status_code == 422


@pytest.mark.parametrize("missing", ["source_id", "target_id"])
async def test_merge_request_requires_both_ids(web_app: FastAPI, missing: str) -> None:
    target = await _registry(web_app).resolve_or_create("深度学习", "concept")
    payload = {"source_id": "x", "target_id": target.entity_id}
    payload.pop(missing)

    async with running(web_app) as client:
        response = await client.post("/api/entities/merge", json=payload)

    assert response.status_code == 422


async def test_entities_routes_are_behind_api_prefix(web_app: FastAPI) -> None:
    """静态页挂在 ``/``，不能把 ``/api/*`` 吃掉。"""
    async with running(web_app) as client:
        response = await client.get("/entities")

    assert response.status_code == 404
