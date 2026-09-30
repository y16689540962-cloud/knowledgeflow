"""任务状态恢复（定稿文档第十七节，强制）。

**禁止依赖 FastAPI BackgroundTasks 作为可靠队列**；不引入 Redis/Celery，
直接用数据库做最小状态机：

```text
pending → processing → completed
                  └──→ failed
```

服务启动时：扫描 ``processing`` 且 ``created_at`` 早于
``TASK_RESET_TIMEOUT_SECONDS`` 的任务，重置为 ``pending`` 后重新处理。
进程重启不会永久丢失任务。

时间戳统一是同一格式的 UTC ISO 字符串，字典序即时间序，因此可以直接比较。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.db.repository import ContentRepository
from app.logging_config import log_event
from app.utils import utc_iso_before

STAGE = "recovery"


@dataclass(frozen=True)
class StaleTask:
    content_id: str
    created_at: str
    error_type: str | None = None


@dataclass(frozen=True)
class RecoveryReport:
    cutoff: str
    timeout_seconds: int
    reset: tuple[str, ...] = ()
    resumed: tuple[str, ...] = ()
    resume_failures: tuple[tuple[str, str], ...] = ()

    @property
    def reset_count(self) -> int:
        return len(self.reset)

    @property
    def resumed_count(self) -> int:
        return len(self.resumed)

    def to_dict(self) -> dict[str, object]:
        return {
            "cutoff": self.cutoff,
            "timeout_seconds": self.timeout_seconds,
            "reset": list(self.reset),
            "resumed": list(self.resumed),
            "resume_failures": [list(item) for item in self.resume_failures],
        }


class TaskRecovery:
    """把卡在 ``processing`` 的任务捞回来。"""

    def __init__(
        self,
        repository: ContentRepository,
        *,
        timeout_seconds: int,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._timeout_seconds = max(0, int(timeout_seconds))
        self._logger = logger or logging.getLogger("knowledgeflow.recovery")

    @property
    def timeout_seconds(self) -> int:
        return self._timeout_seconds

    def cutoff(self) -> str:
        return utc_iso_before(self._timeout_seconds)

    async def find_stale(self) -> tuple[StaleTask, ...]:
        rows = await self._repository.list_stale_status("processing", self.cutoff())
        return tuple(
            StaleTask(
                content_id=row.id,
                created_at=row.created_at,
                error_type=row.error_type,
            )
            for row in rows
        )

    async def reset_stale(self) -> tuple[str, ...]:
        """把超时的 ``processing`` 任务重置为 ``pending``，返回被重置的 id。"""
        stale = await self.find_stale()
        if not stale:
            return ()
        reset = await self._repository.reset_to_pending([item.content_id for item in stale])
        log_event(
            self._logger,
            logging.WARNING,
            stage=STAGE,
            message="重置超时任务",
            count=len(reset),
            timeout_seconds=self._timeout_seconds,
            cutoff=self.cutoff(),
        )
        return reset


__all__ = ["TaskRecovery", "RecoveryReport", "StaleTask", "STAGE"]
