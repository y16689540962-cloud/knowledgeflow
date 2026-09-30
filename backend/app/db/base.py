"""SQLAlchemy 2.x 声明式基类。"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


__all__ = ["Base"]
