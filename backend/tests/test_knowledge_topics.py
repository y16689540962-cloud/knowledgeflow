"""主题归一化（第四节 ``topics`` / ``content_topics``）。"""

from __future__ import annotations

import pytest

from app.knowledge.topics import TopicRegistry, TopicRegistryError


async def test_resolve_creates_topic(topics: TopicRegistry) -> None:
    resolved = await topics.resolve_or_create("人口结构")
    assert resolved.created is True
    assert resolved.name == "人口结构"


async def test_second_resolve_reuses(topics: TopicRegistry) -> None:
    first = await topics.resolve_or_create("人口结构")
    second = await topics.resolve_or_create("人口结构")
    assert second.created is False
    assert second.topic_id == first.topic_id


async def test_get_by_name_missing(topics: TopicRegistry) -> None:
    assert await topics.get_by_name("不存在") is None


async def test_blank_name_rejected(topics: TopicRegistry) -> None:
    with pytest.raises(TopicRegistryError):
        await topics.resolve_or_create("   ")


async def test_name_is_normalized(topics: TopicRegistry) -> None:
    assert (await topics.resolve_or_create("  人口\n结构 ")).name == "人口 结构"


async def test_resolve_many_dedupes(topics: TopicRegistry) -> None:
    resolved = await topics.resolve_many(["甲", "乙", "甲", "  "])
    assert [item.name for item in resolved] == ["甲", "乙"]


async def test_topics_are_unique_by_name(topics: TopicRegistry) -> None:
    await topics.resolve_or_create("房地产")
    await topics.resolve_or_create("房地产")
    assert len(await topics.resolve_many(["房地产"])) == 1


# --------------------------------------------------------------------------- #
# content_topics
# --------------------------------------------------------------------------- #
async def test_link_content(topics: TopicRegistry) -> None:
    topic = await topics.resolve_or_create("房地产")
    result = await topics.link_content("c1", [(topic.topic_id, 0.7)])
    assert result.linked == (topic.topic_id,)
    listed = await topics.list_content_topics("c1")
    assert [item.name for item in listed] == ["房地产"]


async def test_link_content_is_idempotent(topics: TopicRegistry) -> None:
    topic = await topics.resolve_or_create("房地产")
    await topics.link_content("c1", [(topic.topic_id, 0.5)])
    await topics.link_content("c1", [(topic.topic_id, 0.9)])
    assert len(await topics.list_content_topics("c1")) == 1


async def test_link_content_updates_confidence(topics: TopicRegistry, db) -> None:
    from sqlalchemy import select

    from app.db.models import ContentTopic

    topic = await topics.resolve_or_create("房地产")
    await topics.link_content("c1", [(topic.topic_id, 0.5)])
    await topics.link_content("c1", [(topic.topic_id, 0.95)])

    async with db.session_factory() as session:
        rows = (await session.execute(select(ContentTopic.confidence))).scalars().all()
    assert list(rows) == [0.95]


async def test_link_multiple_topics(topics: TopicRegistry) -> None:
    resolved = await topics.resolve_many(["甲", "乙"])
    await topics.link_content("c1", [(item.topic_id, 0.6) for item in resolved])
    listed = await topics.list_content_topics("c1")
    assert [item.name for item in listed] == ["乙", "甲"]


async def test_list_content_topics_empty(topics: TopicRegistry) -> None:
    assert await topics.list_content_topics("nope") == ()


async def test_unique_topic_name_index_enforced_by_db(topics: TopicRegistry, db) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.db.models import Topic
    from app.utils import new_id

    await topics.resolve_or_create("房地产")
    with pytest.raises(IntegrityError):
        async with db.session_factory() as session:
            async with session.begin():
                session.add(Topic(id=new_id(), name="房地产"))
                await session.flush()
