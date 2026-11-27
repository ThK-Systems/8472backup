from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, OBJECT_ID_SIZE, TYPE_DATA, TYPE_SNAPSHOT, Object
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository import Repository
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.snapshot import Snapshot
from p8472backup.server.snapshot_service import SNAPSHOT_REGISTRY_NAME, PackUsage, SnapshotService
from p8472backup.server.trusted_time import TrustedTime
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import HEADER_SIZE as PACK_HEADER_SIZE, Pack


_CLIENT_ID = UUID(int=1)
_SET_ID = UUID(int=2)
_KEY_ID = UUID(int=3)
_SHARED_SET_ID = UUID(int=4)
_OTHER_SET_ID = UUID(int=5)
_OTHER_KEY_ID = UUID(int=6)
_START = datetime(2026, 1, 1, tzinfo=UTC)
_DATA = Object(TYPE_DATA, b"d" * OBJECT_ID_SIZE, 3)
_ROOT = Object(TYPE_SNAPSHOT, b"r" * OBJECT_ID_SIZE, 1)


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ServerFacade, Repository, Mock]:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    repo = facade.init_repository(_CLIENT_ID, {_SET_ID: _KEY_ID})
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = _START
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    return facade, repo, clock


def _backup(facade: ServerFacade) -> Snapshot:
    return facade.backup(_CLIENT_ID, _SET_ID, iter(((_DATA, BytesIO(b"abc")), (_ROOT, BytesIO(b"r")))), prune="")


def _usage(repo: Repository) -> tuple[PackUsage, ...]:
    # Reload repository metadata so newly added or retired sets are visible.
    with RepositoryService().locked_repository(repo.path.parent, repo.client_id) as (current, lock):
        return SnapshotService().analyze_compaction(current, lock)


def test_analysis_keeps_shared_objects_after_prune(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, clock = repository
    old_only = Object(TYPE_DATA, b"o" * OBJECT_ID_SIZE, 2)
    old = facade.backup(_CLIENT_ID, _SET_ID, iter(((_DATA, BytesIO(b"abc")), (old_only, BytesIO(b"oo")), (_ROOT, BytesIO(b"r")))), prune="1d")
    clock.now.return_value = _START + timedelta(days=8)
    new_root = Object(TYPE_SNAPSHOT, b"n" * OBJECT_ID_SIZE, 1)
    current = facade.backup(_CLIENT_ID, _SET_ID, iter(((_DATA, BytesIO(b"abc")), (new_root, BytesIO(b"n")))), prune="1d")
    assert not repo.snapshot_file(_SET_ID, old.snapshot_id).exists()
    before = {path: (path.read_bytes(), path.stat().st_ino) for path in repo.path.rglob("*") if path.is_file()}

    first, second = _usage(repo)

    shared_bytes = PACK_HEADER_SIZE + OBJECT_HEADER_SIZE + _DATA.length
    old_bytes = 2 * OBJECT_HEADER_SIZE + old_only.length + _ROOT.length
    new_bytes = PACK_HEADER_SIZE + OBJECT_HEADER_SIZE + new_root.length
    assert first == PackUsage(_KEY_ID, 1, shared_bytes + old_bytes, shared_bytes)
    assert first.reclaimable_bytes == old_bytes
    assert second == PackUsage(_KEY_ID, 2, new_bytes, new_bytes)
    assert second.reclaimable_bytes == 0
    assert {path: (path.read_bytes(), path.stat().st_ino) for path in repo.path.rglob("*") if path.is_file()} == before
    with facade.restore(_CLIENT_ID, _SET_ID, current.snapshot_id) as reader:
        output = BytesIO()
        assert reader.read_object(_DATA.object_id, output) == _DATA
        assert output.getvalue() == b"abc"


def test_analysis_counts_unindexed_packs_and_duplicate_copies(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, _ = repository
    _backup(facade)
    directory = repo.packs_root / str(_KEY_ID)
    orphan = Object(TYPE_DATA, b"x" * OBJECT_ID_SIZE, 0)
    Pack.write_to(directory / "2.pack", iter(((_DATA, BytesIO(b"abc")), (orphan, BytesIO()))))
    (directory / ".backup-interrupted").write_bytes(b"unfinished pack")
    (directory / "01.pack").write_bytes(b"noncanonical name")

    first, second = _usage(repo)

    assert first.live_bytes == (directory / "1.pack").stat().st_size
    assert first.reclaimable_bytes == 0
    assert second.live_bytes == 0
    assert second.reclaimable_bytes == (directory / "2.pack").stat().st_size


def test_analysis_keeps_protected_retired_sets_and_separates_key_domains(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, _ = repository
    _backup(facade)
    facade.add_backup_set(_CLIENT_ID, _SHARED_SET_ID, _KEY_ID)
    facade.add_backup_set(_CLIENT_ID, _OTHER_SET_ID, _OTHER_KEY_ID)
    other_root = Object(TYPE_SNAPSHOT, b"s" * OBJECT_ID_SIZE, 1)
    facade.backup(_CLIENT_ID, _SHARED_SET_ID, iter(((_DATA, BytesIO(b"abc")), (other_root, BytesIO(b"s")))), prune="")
    facade.backup(_CLIENT_ID, _OTHER_SET_ID, iter(((_DATA, BytesIO(b"abc")), (_ROOT, BytesIO(b"r")))), prune="")
    facade.remove_repository(_CLIENT_ID)

    usage = _usage(repo)

    assert {(pack.key_id, pack.pack_id) for pack in usage} == {(_KEY_ID, 1), (_KEY_ID, 2), (_OTHER_KEY_ID, 1)}
    for pack in usage:
        path = repo.packs_root / str(pack.key_id) / f"{pack.pack_id}.pack"
        assert pack.total_bytes == pack.live_bytes == path.stat().st_size


def test_analysis_counts_the_entire_pack_after_the_last_snapshot_expires(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, clock = repository
    snapshot = _backup(facade)
    clock.now.return_value = _START + timedelta(days=8)
    facade.remove_repository(_CLIENT_ID)
    assert not repo.snapshot_file(_SET_ID, snapshot.snapshot_id).exists()

    (pack,) = _usage(repo)

    assert pack.live_bytes == 0
    assert pack.reclaimable_bytes == (repo.packs_root / str(_KEY_ID) / "1.pack").stat().st_size


def test_analysis_accepts_an_empty_repository_and_requires_a_held_lock(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    _, repo, _ = repository
    assert _usage(repo) == ()
    with pytest.raises(RuntimeError, match="lock is not held"):
        SnapshotService().analyze_compaction(repo, RepositoryLock(repo.path))


@pytest.mark.parametrize(
    ("filename", "contents", "error"),
    (
        pytest.param("register", None, "snapshot-registry", id="missing-register"),
        pytest.param("register", b"{", "snapshot-registry", id="invalid-register"),
        pytest.param(".json", None, r"\.json:", id="missing-metadata"),
        pytest.param(".json", b"{", r"\.json:", id="invalid-metadata"),
        pytest.param(".catalog", None, r"\.catalog:", id="missing-catalogue"),
        pytest.param(".catalog", b"truncated", "truncated object ID", id="invalid-catalogue"),
        pytest.param("index", None, "data/index", id="missing-index"),
        pytest.param("index", b"wrong index", "Truncated location index header", id="invalid-index"),
    ),
)
def test_analysis_rejects_missing_or_damaged_metadata(repository: tuple[ServerFacade, Repository, Mock], filename: str, contents: bytes | None, error: str) -> None:
    facade, repo, _ = repository
    snapshot = _backup(facade)
    paths = {
        "register": repo.path / SNAPSHOT_REGISTRY_NAME,
        ".json": repo.snapshot_file(_SET_ID, snapshot.snapshot_id),
        ".catalog": repo.snapshot_file(_SET_ID, snapshot.snapshot_id).with_suffix(".catalog"),
        "index": repo.index_directory / str(_KEY_ID),
    }
    path = paths[filename]
    if contents is None:
        path.unlink()
    else:
        path.write_bytes(contents)
    before = {path: path.read_bytes() for path in repo.path.rglob("*") if path.is_file()}

    with pytest.raises(InconsistentStateError, match=error):
        _usage(repo)

    assert {path: path.read_bytes() for path in repo.path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize(
    ("damage", "error"),
    (
        pytest.param("missing", "Indexed pack is missing", id="missing-pack"),
        pytest.param("truncated", "Pack file length does not match its header", id="truncated-pack"),
        pytest.param("root-type", "has unexpected object type", id="invalid-root-type"),
    ),
)
def test_analysis_rejects_a_damaged_indexed_pack(repository: tuple[ServerFacade, Repository, Mock], damage: str, error: str) -> None:
    facade, repo, _ = repository
    _backup(facade)
    pack = repo.packs_root / str(_KEY_ID) / "1.pack"
    if damage == "missing":
        pack.unlink()
    elif damage == "truncated":
        pack.write_bytes(pack.read_bytes()[:-1])
    else:
        with pack.open("r+b") as output:
            output.seek(PACK_HEADER_SIZE + OBJECT_HEADER_SIZE + _DATA.length)
            output.write(Object(TYPE_DATA, _ROOT.object_id, _ROOT.length).to_header())

    with pytest.raises(InconsistentStateError, match=error):
        _usage(repo)


def test_analysis_rejects_mismatched_index_locations(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, _ = repository
    _backup(facade)
    index = LocationIndex()
    index.put(_DATA.object_id, ObjectLocation(1, PACK_HEADER_SIZE + 1, OBJECT_HEADER_SIZE + _DATA.length))
    index.put(_ROOT.object_id, ObjectLocation(1, PACK_HEADER_SIZE + OBJECT_HEADER_SIZE + _DATA.length, OBJECT_HEADER_SIZE + _ROOT.length))
    with RepositoryLock(repo.path) as lock:
        index.save_to(repo.index_directory / str(_KEY_ID), lock)

    with pytest.raises(InconsistentStateError, match="indexed locations missing from pack headers: 1"):
        _usage(repo)


def test_analysis_requires_every_catalogued_object_in_the_index(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, _ = repository
    _backup(facade)
    with RepositoryLock(repo.path) as lock:
        LocationIndex().save_to(repo.index_directory / str(_KEY_ID), lock)

    with pytest.raises(InconsistentStateError, match="missing index references: 2"):
        _usage(repo)


def test_analysis_rejects_an_index_redirected_to_another_file(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    _, repo, _ = repository
    index = repo.index_directory / str(_KEY_ID)
    target = index.with_name("other-index")
    index.rename(target)
    index.symlink_to(target)

    with pytest.raises(InconsistentStateError, match="Unexpected location index path"):
        _usage(repo)


def test_analysis_reads_pack_headers_without_copying_bodies(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, _ = repository
    _backup(facade)

    with patch.object(Object, "read_body", side_effect=AssertionError("Analysis must only scan headers")):
        (pack,) = _usage(repo)

    assert pack.live_bytes == pack.total_bytes
