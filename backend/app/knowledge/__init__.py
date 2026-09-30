"""知识层出口：实体 / 主题归一化（定稿文档第四节、第二十一节）。"""

from app.knowledge.aliases import AliasIndex, DatabaseAliasLoader
from app.knowledge.entities import (
    DEFAULT_ENTITY_TYPE,
    AliasConflict,
    AliasRegistration,
    EntityLinkResult,
    EntityRegistry,
    EntityRegistryError,
    EntityMergeError,
    MergePlan,
    MergeResult,
    ResolvedEntity,
)
from app.knowledge.topics import (
    ResolvedTopic,
    TopicLinkResult,
    TopicRegistry,
    TopicRegistryError,
)

__all__ = [
    "EntityRegistry",
    "EntityRegistryError",
    "EntityMergeError",
    "ResolvedEntity",
    "AliasConflict",
    "AliasRegistration",
    "EntityLinkResult",
    "MergePlan",
    "MergeResult",
    "DEFAULT_ENTITY_TYPE",
    "TopicRegistry",
    "TopicRegistryError",
    "ResolvedTopic",
    "TopicLinkResult",
    "DatabaseAliasLoader",
    "AliasIndex",
]
