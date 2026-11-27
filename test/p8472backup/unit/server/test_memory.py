from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from struct import Struct
from typing import BinaryIO
from unittest.mock import Mock
from uuid import UUID

import pytest

from p8472backup.archive.objects import TYPE_DATA, TYPE_SNAPSHOT, Object
from p8472backup.common.error import MemoryBusyError, MemoryLimitError
from p8472backup.server import memory, trusted_time
from p8472backup.server.admission import reserve_operation
from p8472backup.server.lock import RepositoryLock
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.trusted_time import TrustedTime
from p8472backup.storage.file.index import LocationIndex, ObjectLocation


_SET = UUID(int=2)
_KEY = UUID(int=3)
_ROOT = Object(TYPE_SNAPSHOT, b"r" * 32, 1)


@pytest.fixture
def facade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ServerFacade:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    return ServerFacade(tmp_path, server_fqdn="backup.example.net")


def _records(count: int) -> Iterator[tuple[Object, BinaryIO]]:
    for number in range(count):
        yield Object(TYPE_DATA, sha256(number.to_bytes(4, "big")).digest(), 0), BytesIO()
    yield _ROOT, BytesIO(b"r")


def test_checkpoint_is_refused_from_its_header_before_reading_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "index"
    count = 30_000
    header = Struct("<8sHQ").pack(b"8472IDX\x00", 1, count)
    with path.open("wb") as output:
        output.write(header)
        output.truncate(len(header) + 56 * count + 32)
    monkeypatch.setattr(memory, "DEFAULT_MEMORY_BUDGET", 2 * 1024 * 1024)

    with reserve_operation(), RepositoryLock(tmp_path) as lock, pytest.raises(MemoryLimitError):
        LocationIndex.load_from(path, lock)

    # The sparse body has no valid checksum. Without the budget the loader reaches it.
    with RepositoryLock(tmp_path) as lock, pytest.raises(ValueError, match="checksum"):
        LocationIndex.load_from(path, lock)


def test_bucket_growth_is_refused_before_changing_the_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "index"
    location = ObjectLocation(1, 14, 44)
    with RepositoryLock(tmp_path) as lock, LocationIndex() as index:
        for number in range(4096):
            index.put(sha256(number.to_bytes(4, "big")).digest(), location)
        index.save_to(path, lock)
    monkeypatch.setattr(memory, "DEFAULT_MEMORY_BUDGET", (1024 + 310) * 1024)

    with reserve_operation(), RepositoryLock(tmp_path) as lock, LocationIndex.load_from(path, lock) as index:
        with pytest.raises(MemoryLimitError):
            index.put(b"x" * 32, location)
        assert len(index) == 4096
        assert index.get(b"x" * 32) is None
        assert index.get(sha256(bytes(4)).digest()) == location
        assert not index.save_to(path, lock)


def test_open_mounts_share_memory_and_release_it_on_close(facade: ServerFacade, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = UUID(int=1), UUID(int=4)
    for client_id in (first, second):
        facade.init_repository(client_id, {_SET: _KEY})
    first_snapshot = facade.backup(first, _SET, _records(6144), prune="")
    second_snapshot = facade.backup(second, _SET, _records(6144), prune="")
    monkeypatch.setattr(memory, "DEFAULT_MEMORY_BUDGET", 2_650_000)

    with facade.mount(first, _SET, first_snapshot.snapshot_id) as reader:
        assert reader.contains_object(_ROOT.object_id)
        with pytest.raises(MemoryBusyError), facade.restore(second, _SET, second_snapshot.snapshot_id):
            pytest.fail("Both working sets were loaded under the shared limit")

    with facade.restore(second, _SET, second_snapshot.snapshot_id) as reader:
        assert reader.contains_object(_ROOT.object_id)


def test_catalogue_limit_stops_backup_before_commit_and_releases_its_resources(facade: ServerFacade, monkeypatch: pytest.MonkeyPatch) -> None:
    client = UUID(int=1)
    repo = facade.init_repository(client, {_SET: _KEY})
    path = repo.snapshots_root / str(_SET)
    monkeypatch.setattr(memory, "DEFAULT_MEMORY_BUDGET", 1_450_000)
    encountered = 0

    def records() -> Iterator[tuple[Object, BinaryIO]]:
        nonlocal encountered
        for obj, body in _records(2000):
            encountered += 1
            yield obj, body

    with pytest.raises(MemoryLimitError):
        facade.backup(client, _SET, records(), prune="")

    assert 256 < encountered < 2000
    assert not facade.list(client).backup_sets[0].snapshots
    assert not tuple(path.iterdir())
    assert facade.backup(client, _SET, _records(0), prune="").root_id == _ROOT.object_id
