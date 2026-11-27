import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import UUID

import pytest

from p8472backup.server import trusted_time
from p8472backup.server.scheduler.scheduler import Scheduler
from p8472backup.server.trusted_time import TrustedTime


def test_pull_waits_until_one_of_three_push_jobs_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = datetime(2026, 1, 1, 2, tzinfo=UTC)
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    client_id, backup_set_id = UUID(int=1), UUID(int=2)

    async def check() -> None:
        jobs_loaded = asyncio.Event()
        pull_started = asyncio.Event()
        finish_pull = asyncio.Event()

        def pull_jobs() -> tuple[tuple[UUID, UUID], ...]:
            jobs_loaded.set()
            return ((client_id, backup_set_id),)

        async def pull(client: UUID, backup_set: UUID) -> None:
            assert (client, backup_set) == (client_id, backup_set_id)
            pull_started.set()
            await finish_pull.wait()

        scheduler = Scheduler()
        monkeypatch.setattr(scheduler, "_pull_jobs", pull_jobs)
        monkeypatch.setattr(scheduler, "_pull", pull)
        for _ in range(3):
            assert await scheduler.try_start_push()
        assert not await scheduler.try_start_push()

        task = asyncio.create_task(scheduler.run())
        try:
            async with asyncio.timeout(2):
                await jobs_loaded.wait()
                assert not pull_started.is_set()
                scheduler.finish_push()
                await pull_started.wait()
                assert not await scheduler.try_start_push()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(check())
