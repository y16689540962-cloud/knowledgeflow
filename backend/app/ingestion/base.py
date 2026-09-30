"""``ContentSource`` 抽象（定稿文档第一条 B 线、第二十三条）。

B 线的职责只有一个：**把外部输入变成 ``RawContent``**。
它不碰 Core Pipeline 的任何内部结构 —— 拿到的产物必须是 ``RawContent``，
交给 ``ProcessingPipeline.process()`` 就能跑。

未来接入同一接口的还有：Bilibili / YouTube / X / Web / GitHub / PDF / 小红书。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from app.schemas.content import RawContent

PayloadT = TypeVar("PayloadT")


class ContentSource(ABC, Generic[PayloadT]):
    """所有采集入口的统一接口。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """写入 ``RawContent.source`` 的值。"""

    @abstractmethod
    async def fetch(self, payload: PayloadT) -> RawContent:
        """取得 ``RawContent``。

        失败时必须抛 :class:`app.errors.KnowledgeFlowError` 子类并带**明确**
        ``error_type``（定稿第二十二节），不得返回半成品、不得返回假的成功。
        """


__all__ = ["ContentSource", "PayloadT"]
