"""数据库引擎与会话。

用 ``sqlite+aiosqlite`` 异步驱动，与后续 FastAPI / 异步 Pipeline 保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.base import Base
from app.db import models as _models  # noqa: F401  —— 导入即注册全部表
from app.errors import ConfigError

_ASYNC_PREFIX = "sqlite+aiosqlite://"


def to_async_database_url(database_url: str) -> str:
    """把 ``sqlite:///...`` 转成 ``sqlite+aiosqlite:///...``。"""
    url = database_url.strip()
    if url.startswith(_ASYNC_PREFIX):
        return url
    if url.startswith("sqlite+pysqlite://"):
        return _ASYNC_PREFIX + url[len("sqlite+pysqlite://") :]
    if url.startswith("sqlite://"):
        return _ASYNC_PREFIX + url[len("sqlite://") :]
    raise ConfigError(f"不支持的 DATABASE_URL：{url}")


def sqlite_file_path(database_url: str) -> Path | None:
    """从 URL 里取出 SQLite 文件路径；内存库返回 ``None``。"""
    url = to_async_database_url(database_url)
    tail = url[len(_ASYNC_PREFIX) :]
    if tail.startswith("/"):
        tail = tail[1:]
    if not tail or tail in (":memory:", "memory:"):
        return None
    return Path(tail)


@dataclass
class Database:
    """引擎 + 会话工厂的轻量容器。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @classmethod
    def create(cls, database_url: str) -> "Database":
        url = to_async_database_url(database_url)

        file_path = sqlite_file_path(database_url)
        if file_path is not None:
            file_path.parent.mkdir(parents=True, exist_ok=True)

        engine = create_async_engine(url, future=True)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        return cls(engine=engine, session_factory=factory)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Database":
        return cls.create(settings.database_url)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


__all__ = ["Database", "to_async_database_url", "sqlite_file_path"]
