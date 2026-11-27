import asyncio
import logging
from contextlib import ExitStack, suppress
from datetime import UTC, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from typing import Literal
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import OBJECT_ID_SIZE, TYPE_SNAPSHOT, Object
from p8472backup.common.error import ServerBusyError
from p8472backup.common.log import Log
from p8472backup.server import main, trusted_time
from p8472backup.server.admission import reserve_operation
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.scheduler.scheduler import DEFAULT_COMPACT_PAUSE_SECONDS, Scheduler
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.trusted_time import TrustedTime
from p8472backup.storage.file.json_file import sync_directory
from p8472backup.storage.file.lock import RepositoryLock


def test_server_starts_with_scheduler_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P8472BACKUP_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("P8472BACKUP_COMPACT_THRESHOLD_PERCENT", "invalid")
    monkeypatch.setenv("P8472BACKUP_PROTECTION_SECONDS", "invalid")

    clock = Mock(spec=TrustedTime)
    with patch.object(Log, "configure"), patch.object(main, "TrustedTime", return_value=clock), patch.object(main, "Scheduler", autospec=True) as scheduler:
        main.main()

    scheduler.assert_called_once_with()
    scheduler.return_value.run.assert_awaited_once_with()


@pytest.mark.parametrize("value", (-1, 101))
def test_scheduler_rejects_an_invalid_explicit_threshold(value: int) -> None:
    with pytest.raises(ValueError, match="Compaction threshold"):
        Scheduler(compact_threshold_percent=value)


@pytest.mark.parametrize(("kind", "limit"), (("backup", 3), ("repository", 10)))
def test_queued_pull_uses_one_slot_when_capacity_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Literal["backup", "repository"], limit: int) -> None:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = datetime(2026, 1, 1, 2, tzinfo=UTC)
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    client_id, backup_set_id, key_id = UUID(int=1), UUID(int=2), UUID(int=3)
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    facade.init_repository(client_id, {backup_set_id: key_id})
    root = Object(TYPE_SNAPSHOT, b"r" * OBJECT_ID_SIZE, 1)

    async def check() -> None:
        waiting = asyncio.Event()
        retry = asyncio.Event()
        pulled = asyncio.Event()

        async def wait_for_retry(_delay: float) -> None:
            waiting.set()
            await retry.wait()
            retry.clear()

        async def pull(client: UUID, backup_set: UUID) -> None:
            assert (client, backup_set) == (client_id, backup_set_id)
            facade.backup(client, backup_set, iter(((root, BytesIO(b"r")),)), prune="")
            pulled.set()

        scheduler = Scheduler()
        monkeypatch.setattr(scheduler, "_pull_jobs", lambda: ((client_id, backup_set_id),))
        monkeypatch.setattr(scheduler, "_pull", pull)
        monkeypatch.setattr(asyncio, "sleep", wait_for_retry)
        with ExitStack() as operations, ExitStack() as last_operation:
            for _ in range(limit - 1):
                operations.enter_context(reserve_operation(kind))
            last_operation.enter_context(reserve_operation(kind))
            task = asyncio.create_task(scheduler.run())
            try:
                async with asyncio.timeout(2):
                    await waiting.wait()
                    assert not pulled.is_set()
                    last_operation.close()
                    retry.set()
                    await pulled.wait()
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    asyncio.run(check())
    assert len(facade.list(client_id).backup_sets[0].snapshots) == 1


def test_pull_retries_when_capacity_changes_during_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = datetime(2026, 1, 1, 2, tzinfo=UTC)
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    scheduler = Scheduler()
    calls = 0

    async def pull(_client: UUID, _backup_set: UUID) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ServerBusyError("Capacity was taken during dispatch")

    async def retry_once(_delay: float) -> None:
        if calls == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "_pull_jobs", lambda: ((UUID(int=1), UUID(int=2)),))
    monkeypatch.setattr(scheduler, "_pull", pull)
    monkeypatch.setattr(asyncio, "sleep", retry_once)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run())
    assert calls == 2


@pytest.fixture
def maintenance_tick(monkeypatch: pytest.MonkeyPatch) -> Mock:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = datetime(2026, 1, 1, 3, tzinfo=UTC)
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)

    async def stop_after_tick(delay: float) -> None:
        if delay != DEFAULT_COMPACT_PAUSE_SECONDS:
            raise asyncio.CancelledError

    monkeypatch.setattr("p8472backup.server.scheduler.scheduler.asyncio.sleep", stop_after_tick)
    return clock


@pytest.mark.parametrize("compact_start", (time(2, 30), 0), ids=("fixed-time", "after-pull"))
def test_scheduled_compaction_reclaims_a_pruned_pack(tmp_path: Path, caplog: pytest.LogCaptureFixture, maintenance_tick: Mock, compact_start: time | Literal[0]) -> None:
    client_id, set_id, key_id = UUID(int=1), UUID(int=2), UUID(int=3)
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    repo = facade.init_repository(client_id, {set_id: key_id})
    root = Object(TYPE_SNAPSHOT, b"r" * OBJECT_ID_SIZE, 1)
    facade.backup(client_id, set_id, iter(((root, BytesIO(b"r")),)), prune="1d")
    maintenance_tick.now.return_value += timedelta(days=8)
    new_root = Object(TYPE_SNAPSHOT, b"n" * OBJECT_ID_SIZE, 1)
    snapshot = facade.backup(client_id, set_id, iter(((new_root, BytesIO(b"n")),)), prune="1d")
    caplog.set_level(logging.INFO, logger="p8472backup")
    caplog.clear()

    scheduler = Scheduler(storage_root=tmp_path, compact_start=compact_start)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run())

    assert caplog.messages == [
        "Dispatching daily pull batch",
        "Dispatching scheduled repository maintenance",
        f"Compaction completed client_id={client_id} packs_reclaimed=1 threshold_percent=25",
    ]
    assert sorted(path.name for path in (repo.packs_root / str(key_id)).iterdir()) == ["2.pack"]
    with facade.restore(client_id, set_id, snapshot.snapshot_id) as reader:
        output = BytesIO()
        assert reader.read_object(reader.root_id, output) == new_root
        assert output.getvalue() == b"n"


@pytest.mark.usefixtures("maintenance_tick")
def test_scheduled_compaction_continues_after_busy_and_damaged_repositories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    backup_sets = {UUID(int=2): UUID(int=3)}
    locked = facade.init_repository(UUID(int=1), backup_sets)
    damaged = facade.init_repository(UUID(int=2), backup_sets)
    healthy = facade.init_repository(UUID(int=3), backup_sets)
    facade.remove_backup_set(healthy.client_id, next(iter(backup_sets)))
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
        ("p8472backup", logging.INFO, "Dispatching scheduled repository maintenance"),
        ("p8472backup", logging.WARNING, f"Repository discovery failed client_id={inaccessible.client_id} reason=PermissionError"),
        ("p8472backup", logging.WARNING, f"Operation maintenance failed client_id={locked.client_id} reason=BlockingIOError"),
        ("p8472backup", logging.WARNING, f"Operation maintenance failed client_id={damaged.client_id} reason=JSONDecodeError"),
        ("p8472backup", logging.INFO, f"Backup set deleted client_id={healthy.client_id} backup_set_id={next(iter(backup_sets))}"),
    ]
    assert list(unfinished.iterdir()) == []


@pytest.mark.usefixtures("maintenance_tick")
def test_scheduled_compaction_warns_if_the_storage_root_is_unavailable(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Scheduler(storage_root=tmp_path / "missing", compact_start=time(2, 30)).run())

    assert caplog.record_tuples == [("p8472backup", logging.WARNING, "Repository maintenance could not list repositories reason=FileNotFoundError")]


@pytest.mark.usefixtures("maintenance_tick")
def test_scheduled_compaction_has_no_work_without_a_storage_root(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="p8472backup")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Scheduler(compact_start=time(2, 30)).run())

    assert caplog.messages == ["Dispatching daily pull batch"]


@pytest.mark.usefixtures("maintenance_tick")
def test_scheduled_compaction_stops_admitting_repositories_when_a_backup_arrives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    facade.init_repository(UUID(int=1), {UUID(int=2): UUID(int=3)})
    caplog.set_level(logging.INFO, logger="p8472backup")
    caplog.clear()

    list_client_ids = RepositoryService.list_client_ids

    with ExitStack() as operations:

        def discover_and_start_backup(service: RepositoryService, storage_root: Path, *, deleting: bool = False) -> tuple[UUID, ...]:
            client_ids = list_client_ids(service, storage_root, deleting=deleting)
            if not deleting:
                operations.enter_context(reserve_operation("backup"))
            return client_ids

        monkeypatch.setattr(RepositoryService, "list_client_ids", discover_and_start_backup)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(Scheduler(storage_root=tmp_path, compact_start=time(2, 30)).run())

    assert caplog.messages == ["Dispatching daily pull batch", "Dispatching scheduled repository maintenance"]


@pytest.mark.usefixtures("maintenance_tick")
def test_scheduled_deletion_resumes_after_detaching_a_retired_repository(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    client_id = UUID(int=1)
    backup_sets = {UUID(int=2): UUID(int=3)}
    repo = facade.init_repository(client_id, backup_sets)
    facade.remove_repository(client_id)
    detached = tmp_path / f".deleting-{client_id}"
    caplog.set_level(logging.INFO, logger="p8472backup")
    caplog.clear()

    def interrupted_parent_sync(directory: Path) -> None:
        if directory == tmp_path:
            raise OSError("Storage root sync interrupted")
        sync_directory(directory)

    with (
        patch("p8472backup.server.repository_service.sync_directory", interrupted_parent_sync),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(Scheduler(storage_root=tmp_path).run())
    assert not repo.path.exists()
    assert (detached / "repository.json").exists()
    assert f"Operation maintenance failed client_id={client_id} reason=OSError" in caplog.messages

    # Model a process stopping during recursive removal, after deleting its metadata.
    (detached / "repository.json").unlink()
    new = facade.init_repository(client_id, backup_sets)
    alias = tmp_path / f".deleting-{UUID(int=7)}"
    alias.symlink_to(new.path, target_is_directory=True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Scheduler(storage_root=tmp_path).run())

    assert not detached.exists()
    assert (new.path / "repository.json").is_file()
    assert alias.is_symlink()
    assert caplog.messages.count(f"Repository deleted client_id={client_id}") == 1
