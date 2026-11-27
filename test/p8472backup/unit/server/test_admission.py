from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import BinaryIO, Literal
from unittest.mock import Mock
from uuid import UUID

import pytest

from p8472backup.archive.objects import OBJECT_ID_SIZE, TYPE_SNAPSHOT, Object
from p8472backup.common.error import ServerBusyError
from p8472backup.server import trusted_time
from p8472backup.server.admission import reserve_operation
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.trusted_time import TrustedTime
from p8472backup.storage.file.lock import RepositoryLock


_SET_ID = UUID(int=100)
_KEY_ID = UUID(int=101)
_ROOT = Object(TYPE_SNAPSHOT, b"r" * OBJECT_ID_SIZE, 1)
_FQDN = "backup.example.net"


@pytest.fixture
def facade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ServerFacade:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    return ServerFacade(tmp_path, server_fqdn=_FQDN)


def test_backup_slots_are_shared_across_workers_and_facades(tmp_path: Path, facade: ServerFacade) -> None:
    other = ServerFacade(tmp_path, server_fqdn=_FQDN)
    client_ids = [UUID(int=number) for number in range(1, 5)]
    for client_id in client_ids:
        facade.init_repository(client_id, {_SET_ID: _KEY_ID})
    entered = [Event() for _ in range(3)]
    finish = Event()

    def records(started: Event) -> Iterator[tuple[Object, BinaryIO]]:
        started.set()
        assert finish.wait(timeout=10)
        yield _ROOT, BytesIO(b"r")

    with ThreadPoolExecutor(max_workers=3) as pool:
        backups = [pool.submit(facade.backup, client_id, _SET_ID, records(started), prune="") for client_id, started in zip(client_ids[:3], entered, strict=True)]
        try:
            for started in entered:
                assert started.wait(timeout=10)
            rejected = Event()
            with pytest.raises(ServerBusyError, match="capacity"):
                other.backup(client_ids[3], _SET_ID, records(rejected), prune="")
            assert not rejected.is_set()
            assert not other.list(client_ids[3]).backup_sets[0].snapshots
        finally:
            finish.set()
        for backup in backups:
            assert backup.result(timeout=10).root_id == _ROOT.object_id

    assert other.backup(client_ids[3], _SET_ID, iter(((_ROOT, BytesIO(b"r")),)), prune="").root_id == _ROOT.object_id


def test_mounts_hold_repository_slots_until_the_reader_closes(tmp_path: Path, facade: ServerFacade) -> None:
    other = ServerFacade(tmp_path, server_fqdn=_FQDN)
    snapshots: list[tuple[UUID, UUID]] = []
    for number in range(1, 12):
        client_id = UUID(int=number)
        facade.init_repository(client_id, {_SET_ID: _KEY_ID})
        snapshot = facade.backup(client_id, _SET_ID, iter(((_ROOT, BytesIO(b"r")),)), prune="")
        snapshots.append((client_id, snapshot.snapshot_id))
    last_client, last_snapshot = snapshots[-1]

    with ExitStack() as mounts:
        for client_id, snapshot_id in snapshots[:9]:
            mounts.enter_context(facade.mount(client_id, _SET_ID, snapshot_id))
        tenth_client, tenth_snapshot = snapshots[9]
        with pytest.raises(RuntimeError, match="Mount stopped"), facade.mount(tenth_client, _SET_ID, tenth_snapshot):
            with pytest.raises(ServerBusyError):
                other.list(last_client)
            with pytest.raises(ServerBusyError):
                other.backup(last_client, _SET_ID, iter(((_ROOT, BytesIO(b"r")),)), prune="")
            with pytest.raises(ServerBusyError), other.restore(last_client, _SET_ID, last_snapshot):
                pytest.fail("Restore started while all repository slots were occupied")
            with pytest.raises(ServerBusyError):
                other.init_repository(UUID(int=12), {_SET_ID: _KEY_ID})
            assert not (tmp_path / str(UUID(int=12))).exists()
            assert other.version()
            raise RuntimeError("Mount stopped")

        assert other.list(last_client).backup_sets[0].snapshots[0].snapshot_id == last_snapshot


def test_failed_backup_releases_both_slots_after_repository_lock_contention(facade: ServerFacade) -> None:
    client_id = UUID(int=1)
    repo = facade.init_repository(client_id, {_SET_ID: _KEY_ID})
    with ExitStack() as operations:
        for _ in range(2):
            operations.enter_context(reserve_operation("backup"))
        for _ in range(7):
            operations.enter_context(reserve_operation())
        with RepositoryLock(repo.path), pytest.raises(BlockingIOError) as error:
            facade.backup(client_id, _SET_ID, iter(((_ROOT, BytesIO(b"r")),)), prune="")
        assert not isinstance(error.value, ServerBusyError)

        assert facade.backup(client_id, _SET_ID, iter(((_ROOT, BytesIO(b"r")),)), prune="").root_id == _ROOT.object_id


@pytest.mark.parametrize(("kind", "count"), (("backup", 1), ("repository", 10)))
@pytest.mark.parametrize("detached", (False, True), ids=("retired-repository", "detached-repository"))
def test_maintenance_defers_destructive_work_when_capacity_is_unavailable(tmp_path: Path, facade: ServerFacade, kind: Literal["backup", "repository"], count: int, detached: bool, caplog: pytest.LogCaptureFixture) -> None:
    client_id = UUID(int=1)
    repo = facade.init_repository(client_id, {_SET_ID: _KEY_ID})
    facade.remove_repository(client_id)
    service = RepositoryService()
    path = repo.path
    if detached:
        with service.locked_repository(tmp_path, client_id) as (current, lock):
            assert service.delete(current, lock, protection_duration=timedelta(days=7))
        path = path.with_name(f".deleting-{client_id}")

    def run_maintenance() -> None:
        if detached:
            service.finish_deletion(tmp_path, client_id)
        else:
            service.maintain_repository(tmp_path, client_id, protection_duration=timedelta(days=7), compact_threshold_percent=25)

    with ExitStack() as operations:
        for _ in range(count):
            operations.enter_context(reserve_operation(kind))
        run_maintenance()
        assert path.is_dir()

    assert sum(message.endswith("reason=ServerBusyError") for message in caplog.messages) == 1
    run_maintenance()
    assert not path.exists()
