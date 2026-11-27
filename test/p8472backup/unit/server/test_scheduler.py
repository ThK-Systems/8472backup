import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, time
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

from p8472backup.archive.objects import OBJECT_ID_SIZE, TYPE_SNAPSHOT, Object
from p8472backup.server import trusted_time
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.scheduler.scheduler import DEFAULT_COMPACT_PAUSE_SECONDS, Scheduler
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.trusted_time import TrustedTime
from p8472backup.storage.file.lock import RepositoryLock


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


@pytest.fixture
def maintenance_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = datetime(2026, 1, 1, 3, tzinfo=UTC)
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)

    async def stop_after_tick(delay: float) -> None:
        if delay != DEFAULT_COMPACT_PAUSE_SECONDS:
            raise asyncio.CancelledError

    monkeypatch.setattr("p8472backup.server.scheduler.scheduler.asyncio.sleep", stop_after_tick)


@pytest.mark.usefixtures("maintenance_tick")
@pytest.mark.parametrize("from_environment", (False, True), ids=("explicit-root", "environment-and-default-time"))
def test_scheduled_compaction_analyzes_a_real_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, from_environment: bool) -> None:
    client_id, set_id, key_id = UUID(int=1), UUID(int=2), UUID(int=3)
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    repo = facade.init_repository(client_id, {set_id: key_id})
    root = Object(TYPE_SNAPSHOT, b"r" * OBJECT_ID_SIZE, 1)
    facade.backup(client_id, set_id, iter(((root, BytesIO(b"r")),)), prune="")
    total = (repo.packs_root / str(key_id) / "1.pack").stat().st_size
    caplog.set_level(logging.INFO, logger="p8472backup")
    caplog.clear()

    if from_environment:
        monkeypatch.setenv("P8472BACKUP_STORAGE_ROOT", str(tmp_path))
        scheduler = Scheduler()
    else:
        monkeypatch.setenv("P8472BACKUP_STORAGE_ROOT", str(tmp_path / "unused"))
        scheduler = Scheduler(storage_root=tmp_path, compact_start=time(2, 30))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run())

    assert caplog.messages == [
        "Dispatching daily pull batch",
        "Dispatching scheduled compaction analysis",
        f"Compaction analysis completed client_id={client_id} packs=1 total_bytes={total} live_bytes={total} reclaimable_bytes=0",
    ]


@pytest.mark.usefixtures("maintenance_tick")
def test_scheduled_compaction_continues_after_busy_and_damaged_repositories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    backup_sets = {UUID(int=2): UUID(int=3)}
    locked = facade.init_repository(UUID(int=1), backup_sets)
    damaged = facade.init_repository(UUID(int=2), backup_sets)
    healthy = facade.init_repository(UUID(int=3), backup_sets)
    (damaged.path / "repository.json").write_bytes(b"{")
    unfinished = tmp_path / str(UUID(int=4))
    unfinished.mkdir()
    (tmp_path / str(UUID(int=5))).symlink_to(healthy.path, target_is_directory=True)
    inaccessible = facade.init_repository(UUID(int=6), backup_sets)
    (tmp_path / UUID(int=7).hex).mkdir()
    (tmp_path / "unrelated").mkdir()
    is_file = Path.is_file

    def check_metadata(path: Path) -> bool:
        if path == inaccessible.path / "repository.json":
            raise PermissionError("Repository metadata is inaccessible")
        return is_file(path)

    monkeypatch.setattr(Path, "is_file", check_metadata)
    caplog.set_level(logging.INFO, logger="p8472backup")
    caplog.clear()

    with RepositoryLock(locked.path), pytest.raises(asyncio.CancelledError):
        asyncio.run(Scheduler(storage_root=tmp_path, compact_start=time(2, 30)).run())

    assert caplog.record_tuples == [
        ("p8472backup", logging.INFO, "Dispatching daily pull batch"),
        ("p8472backup", logging.INFO, "Dispatching scheduled compaction analysis"),
        ("p8472backup", logging.WARNING, f"Repository discovery failed client_id={inaccessible.client_id} reason=PermissionError"),
        ("p8472backup", logging.WARNING, f"Operation analyze_compaction failed client_id={locked.client_id} reason=BlockingIOError"),
        ("p8472backup", logging.WARNING, f"Operation analyze_compaction failed client_id={damaged.client_id} reason=JSONDecodeError"),
        ("p8472backup", logging.INFO, f"Compaction analysis completed client_id={healthy.client_id} packs=0 total_bytes=0 live_bytes=0 reclaimable_bytes=0"),
    ]
    assert list(unfinished.iterdir()) == []


@pytest.mark.usefixtures("maintenance_tick")
def test_scheduled_compaction_warns_if_the_storage_root_is_unavailable(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Scheduler(storage_root=tmp_path / "missing", compact_start=time(2, 30)).run())

    assert caplog.record_tuples == [("p8472backup", logging.WARNING, "Compaction analysis could not list repositories reason=FileNotFoundError")]


@pytest.mark.usefixtures("maintenance_tick")
def test_scheduled_compaction_has_no_work_without_a_storage_root(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("P8472BACKUP_STORAGE_ROOT", raising=False)
    caplog.set_level(logging.INFO, logger="p8472backup")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Scheduler(compact_start=time(2, 30)).run())

    assert caplog.messages == ["Dispatching daily pull batch"]


@pytest.mark.usefixtures("maintenance_tick")
def test_scheduled_compaction_stops_admitting_repositories_when_a_push_arrives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    facade.init_repository(UUID(int=1), {UUID(int=2): UUID(int=3)})
    caplog.set_level(logging.INFO, logger="p8472backup")
    caplog.clear()

    async def check() -> None:
        scheduler = Scheduler(storage_root=tmp_path, compact_start=time(2, 30))
        loop = asyncio.get_running_loop()
        list_client_ids = RepositoryService.list_client_ids

        def discover_and_start_push(service: RepositoryService, storage_root: Path) -> tuple[UUID, ...]:
            client_ids = list_client_ids(service, storage_root)
            assert asyncio.run_coroutine_threadsafe(scheduler.try_start_push(), loop).result(timeout=2)
            return client_ids

        monkeypatch.setattr(RepositoryService, "list_client_ids", discover_and_start_push)
        await scheduler.run()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(check())

    assert caplog.messages == ["Dispatching daily pull batch", "Dispatching scheduled compaction analysis"]
