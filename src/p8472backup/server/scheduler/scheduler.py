"""Run the daily pull and maintenance jobs."""

import asyncio
import os
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Literal
from uuid import UUID

from p8472backup.common.error import InconsistentStateError
from p8472backup.common.log import Log
from p8472backup.server import trusted_time
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.snapshot_service import SnapshotService


DEFAULT_PULL_START = time(2)
DEFAULT_PULL_PAUSE_SECONDS = 5 * 60
DEFAULT_COMPACT_PAUSE_SECONDS = 30 * 60
DEFAULT_MAX_BACKUP_JOBS = 3
DEFAULT_COMPACT_START: Literal[0] = 0
_CHECK_INTERVAL_SECONDS = 30


class Scheduler:
    """Run the daily jobs in order in the server's event loop."""

    def __init__(self, *, compact_start: Literal[0] | time = DEFAULT_COMPACT_START, max_backup_jobs: int = DEFAULT_MAX_BACKUP_JOBS, storage_root: Path | None = None) -> None:
        if max_backup_jobs < 1:
            raise ValueError("The backup job limit must be positive")
        if storage_root is None and (configured_root := os.getenv("P8472BACKUP_STORAGE_ROOT")):
            storage_root = Path(configured_root)
        self._storage_root = storage_root
        self._compact_start = compact_start
        self._backup_slots = asyncio.BoundedSemaphore(max_backup_jobs)
        self._active_push_jobs = 0

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
            if compact_is_due and self._active_push_jobs == 0:
                last_compact = compact_due
                await self._compact()

            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

    async def try_start_push(self) -> bool:
        """Reserve a push slot if available, without waiting for one."""
        if self._backup_slots.locked():
            Log.warning("Push deferred because all backup slots are in use")
            return False
        trusted_time.server_clock.now()
        await self._backup_slots.acquire()
        self._active_push_jobs += 1
        return True

    def finish_push(self) -> None:
        """Release the slot held by a completed or failed push job."""
        if self._active_push_jobs == 0:
            raise RuntimeError("No push job holds a backup slot")
        self._active_push_jobs -= 1
        self._backup_slots.release()

    async def _run_pull_batch(self) -> None:
        for index, (client_id, backup_set_id) in enumerate(self._pull_jobs()):
            if index:
                await asyncio.sleep(DEFAULT_PULL_PAUSE_SECONDS)
            async with self._backup_slots:
                trusted_time.server_clock.now()
                await self._pull(client_id, backup_set_id)

    def _pull_jobs(self) -> Iterable[tuple[UUID, UUID]]:
        """Return the client and set IDs for registered pull jobs."""
        return ()

    async def _pull(self, _client_id: UUID, _backup_set_id: UUID) -> None:
        """Run a pull job once the backup operation is available."""

    async def _compact(self) -> None:
        """Analyze finalized pack usage one repository at a time."""
        storage_root = self._storage_root
        if storage_root is None:
            return

        Log.info("Dispatching scheduled compaction analysis")
        try:
            client_ids = await asyncio.to_thread(RepositoryService().list_client_ids, storage_root)
        except OSError as error:
            Log.warning("Compaction analysis could not list repositories reason=%s", type(error).__name__)
            return

        for client_id in client_ids:
            if self._active_push_jobs:
                break
            await asyncio.to_thread(self._analyze_repository, storage_root, client_id)

    def _analyze_repository(self, storage_root: Path, client_id: UUID) -> None:
        try:
            with RepositoryService().locked_repository(storage_root, client_id, operation="analyze_compaction") as (repository, lock):
                usage = SnapshotService().analyze_compaction(repository, lock)
        except (OSError, ValueError, InconsistentStateError):
            # The repository context logs the failure once.
            return

        total_bytes = sum(pack.total_bytes for pack in usage)
        live_bytes = sum(pack.live_bytes for pack in usage)
        Log.info(
            "Compaction analysis completed client_id=%s packs=%d total_bytes=%d live_bytes=%d reclaimable_bytes=%d",
            client_id,
            len(usage),
            total_bytes,
            live_bytes,
            total_bytes - live_bytes,
        )
