"""Run the daily pull and maintenance jobs."""

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from p8472backup.common.error import ServerBusyError
from p8472backup.common.log import Log
from p8472backup.server import trusted_time
from p8472backup.server.admission import has_capacity
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.snapshot_service import configured_protection_duration


DEFAULT_PULL_START = time(2)
DEFAULT_PULL_PAUSE_SECONDS = 5 * 60
DEFAULT_COMPACT_PAUSE_SECONDS = 30 * 60
DEFAULT_COMPACT_START: Literal[0] = 0
DEFAULT_COMPACT_THRESHOLD_PERCENT = 25
_CHECK_INTERVAL_SECONDS = 30


class Scheduler:
    """Run the daily jobs in order in the server's event loop."""

    def __init__(
        self,
        *,
        compact_start: Literal[0] | time = DEFAULT_COMPACT_START,
        storage_root: Path | None = None,
        compact_threshold_percent: int = DEFAULT_COMPACT_THRESHOLD_PERCENT,
        protection_duration: timedelta | None = None,
    ) -> None:
        if not 0 <= compact_threshold_percent <= 100:
            raise ValueError("Compaction threshold must be between 0 and 100 percent")
        self._storage_root = storage_root
        self._compact_start = compact_start
        self._compact_threshold_percent = compact_threshold_percent
        self._protection_duration = configured_protection_duration(protection_duration)

    async def run(self) -> None:
        """Run today's batch once it is due, then wait for the next day."""
        last_pull: date | None = None
        last_compact: date | None = None
        compact_due: date | None = None
        while True:
            now = trusted_time.server_clock.now()
            today = now.date()
            pull_start = datetime.combine(today, DEFAULT_PULL_START, UTC)
            pull_is_due = (last_pull is None or today > last_pull) and now >= pull_start
            if pull_is_due:
                last_pull = today
                Log.info("Dispatching daily pull batch")
                await self._run_pull_batch()
                if self._compact_start == 0:
                    await asyncio.sleep(DEFAULT_COMPACT_PAUSE_SECONDS)
                    compact_due = today
                now = trusted_time.server_clock.now()

            if isinstance(self._compact_start, time):
                today = now.date()
                if (compact_due is None or today > compact_due) and now >= datetime.combine(today, self._compact_start, UTC):
                    compact_due = today

            compact_is_due = compact_due is not None and (last_compact is None or compact_due > last_compact)
            if compact_is_due and has_capacity("maintenance"):
                last_compact = compact_due
                await self._compact()

            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

    async def _run_pull_batch(self) -> None:
        for index, (client_id, backup_set_id) in enumerate(self._pull_jobs()):
            if index:
                await asyncio.sleep(DEFAULT_PULL_PAUSE_SECONDS)
            while True:
                trusted_time.server_clock.now()
                if has_capacity("backup"):
                    try:
                        await self._pull(client_id, backup_set_id)
                    except ServerBusyError:
                        # The facade can lose the slot to a concurrent operation.
                        pass
                    else:
                        break
                await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

    def _pull_jobs(self) -> Iterable[tuple[UUID, UUID]]:
        """Return the client and set IDs for registered pull jobs."""
        return ()

    async def _pull(self, _client_id: UUID, _backup_set_id: UUID) -> None:
        """Run a pull job once the backup operation is available."""

    async def _compact(self) -> None:
        """Process due maintenance one repository at a time while idle."""
        storage_root = self._storage_root
        if storage_root is None:
            return

        Log.info("Dispatching scheduled repository maintenance")
        repositories = RepositoryService()
        try:
            deleting = await asyncio.to_thread(repositories.list_client_ids, storage_root, deleting=True)
            client_ids = await asyncio.to_thread(repositories.list_client_ids, storage_root)
        except OSError as error:
            Log.warning("Repository maintenance could not list repositories reason=%s", type(error).__name__)
            return

        for client_id in deleting:
            if not has_capacity("maintenance"):
                return
            await asyncio.to_thread(repositories.finish_deletion, storage_root, client_id)
        for client_id in client_ids:
            if not has_capacity("maintenance"):
                break
            await asyncio.to_thread(
                repositories.maintain_repository,
                storage_root,
                client_id,
                protection_duration=self._protection_duration,
                compact_threshold_percent=self._compact_threshold_percent,
            )
