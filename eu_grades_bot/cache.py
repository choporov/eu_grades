from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .grades import CacheNotReadyError, GradesRepository, StudentGrades


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheStatus:
    last_success_at: datetime | None
    age_seconds: float | None
    last_attempt_at: datetime | None
    last_failure_at: datetime | None
    refreshing: bool
    stale: bool


class GradesCache:
    """One shared refresh task per bot, with blocking work confined to a worker."""

    def __init__(self, repository: GradesRepository, stale_after_seconds: int = 1800):
        if stale_after_seconds <= 0:
            raise ValueError("CACHE_STALE_AFTER_SECONDS must be positive.")
        self.repository = repository
        self.stale_after_seconds = stale_after_seconds
        self._refresh_task: asyncio.Task[None] | None = None
        self._last_attempt_at: datetime | None = None
        self._last_failure_at: datetime | None = None

    @property
    def status(self) -> CacheStatus:
        completed_at, age_seconds = self.repository.cache_info()
        return CacheStatus(
            last_success_at=completed_at,
            age_seconds=age_seconds,
            last_attempt_at=self._last_attempt_at,
            last_failure_at=self._last_failure_at,
            refreshing=self._refresh_task is not None and not self._refresh_task.done(),
            stale=age_seconds is None or age_seconds >= self.stale_after_seconds,
        )

    def start_refresh(self) -> asyncio.Task[None]:
        # Called only on the application's event loop. There is no await between
        # checking and assigning, so simultaneous requests join the same task.
        if self._refresh_task is None or self._refresh_task.done():
            self._last_attempt_at = datetime.now(timezone.utc)
            self._refresh_task = asyncio.create_task(self._reload(), name="grades-cache-refresh")
            self._refresh_task.add_done_callback(self._observe_completion)
        return self._refresh_task

    @staticmethod
    def _observe_completion(task: asyncio.Task[None]) -> None:
        # Local TTL refreshes may have no waiter. Failures are already logged by
        # _reload, but still need to be retrieved to avoid an unhandled task error.
        if not task.cancelled():
            task.exception()

    async def refresh(self) -> None:
        # Cancelling one command must not cancel the worker used by other callers.
        await asyncio.shield(self.start_refresh())

    async def _reload(self) -> None:
        try:
            await asyncio.to_thread(self.repository.reload)
        except Exception:
            self._last_failure_at = datetime.now(timezone.utc)
            logger.exception(
                "Grades cache refresh failed; last_success_at=%s, age_seconds=%s",
                self.status.last_success_at,
                self.status.age_seconds,
            )
            raise
        self._last_failure_at = None
        logger.info("Grades cache refreshed successfully at %s", self.status.last_success_at)

    async def get_student_grades(self, email: str) -> StudentGrades:
        _, age_seconds = self.repository.cache_info()
        if age_seconds is None:
            if not self.repository.reload_when_stale:
                # Drive is loaded by the startup/periodic job or explicit /refresh.
                raise CacheNotReadyError("Grades cache has not been loaded yet.")
            try:
                await self.refresh()
            except Exception as exc:
                raise CacheNotReadyError("Could not load grades cache.") from exc
        elif self.repository.reload_when_stale and age_seconds >= self.repository.cache_ttl_seconds:
            self.start_refresh()
        return self.repository.get_cached_student_grades(email)

    async def wait_for_refresh(self) -> None:
        """Let an active worker finish before the application shuts down."""
        if self._refresh_task is not None:
            try:
                await asyncio.shield(self._refresh_task)
            except Exception:
                pass  # The failure was recorded and logged in _reload.
