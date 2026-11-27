from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, OBJECT_ID_SIZE, TYPE_DATA, TYPE_SNAPSHOT, Object
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository import Repository
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.snapshot import Snapshot
from p8472backup.server.snapshot_service import SNAPSHOT_REGISTRY_NAME, SnapshotService
from p8472backup.server.trusted_time import TrustedTime
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import HEADER_SIZE as PACK_HEADER_SIZE


_CLIENT_ID = UUID(int=1)
_SET_ID = UUID(int=2)
_KEY_ID = UUID(int=3)
_SHARED_SET_ID = UUID(int=4)
_OTHER_SET_ID = UUID(int=5)
_OTHER_KEY_ID = UUID(int=6)


@pytest.fixture
def rebuild_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ServerFacade, Repository]:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    repository = facade.init_repository(_CLIENT_ID, {_SET_ID: _KEY_ID, _SHARED_SET_ID: _KEY_ID, _OTHER_SET_ID: _OTHER_KEY_ID})
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    return facade, repository


def _backup(facade: ServerFacade, set_id: UUID, value: int) -> Snapshot:
    body = bytes((value,))
    data = Object(TYPE_DATA, b"d" * (OBJECT_ID_SIZE - 1) + body, len(body))
    root = Object(TYPE_SNAPSHOT, b"r" * (OBJECT_ID_SIZE - 1) + body, len(body))
    return facade.backup(_CLIENT_ID, set_id, iter(((data, BytesIO(body)), (root, BytesIO(body)))), prune="")


def test_rebuild_checks_shared_sets_and_writes_only_the_selected_index(rebuild_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = rebuild_repository
    first = _backup(facade, _SET_ID, 1)
    shared = _backup(facade, _SHARED_SET_ID, 2)
    other = _backup(facade, _OTHER_SET_ID, 3)
    facade.remove_backup_set(_CLIENT_ID, _SHARED_SET_ID)
    index_path = repository.index_directory / str(_KEY_ID)
    index_path.unlink()
    before = {path: path.read_bytes() for path in repository.path.rglob("*") if path.is_file()}

    facade.rebuild_index(_CLIENT_ID, _SHARED_SET_ID)

    after = {path: path.read_bytes() for path in repository.path.rglob("*") if path.is_file() and path != index_path}
    assert after == before
    facade.check(_CLIENT_ID)
    for value, snapshot in enumerate((first, shared, other), start=1):
        with facade.restore(_CLIENT_ID, snapshot.backup_set_id, snapshot.snapshot_id) as reader:
            output = BytesIO()
            assert reader.read_object(reader.root_id, output).object_id == snapshot.root_id
            assert output.getvalue() == bytes((value,))


def test_rebuild_saves_an_empty_index_for_a_domain_with_no_finalized_packs(rebuild_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = rebuild_repository
    pack_directory = repository.packs_root / str(_KEY_ID)
    for name in (".backup-interrupted", "notes.pack", "01.pack"):
        (pack_directory / name).write_bytes(b"unrelated file")
    index_path = repository.index_directory / str(_KEY_ID)
    index_path.unlink()

    facade.rebuild_index(_CLIENT_ID, _SET_ID)

    with RepositoryLock(repository.path) as lock:
        assert len(LocationIndex.load_from(index_path, lock)) == 0
    assert {path.name for path in pack_directory.iterdir()} == {".backup-interrupted", "notes.pack", "01.pack"}


@pytest.mark.parametrize(
    ("suffix", "payload"),
    (
        pytest.param(".json", None, id="missing-metadata"),
        pytest.param(".json", b"{", id="invalid-metadata"),
        pytest.param(".catalog", None, id="missing-catalogue"),
        pytest.param(".catalog", b"partial", id="invalid-catalogue"),
    ),
)
def test_rebuild_preserves_the_index_when_a_shared_snapshot_is_invalid(rebuild_repository: tuple[ServerFacade, Repository], suffix: str, payload: bytes | None) -> None:
    facade, repository = rebuild_repository
    _backup(facade, _SET_ID, 1)
    shared = _backup(facade, _SHARED_SET_ID, 2)
    facade.remove_backup_set(_CLIENT_ID, _SHARED_SET_ID)
    path = repository.snapshot_file(_SHARED_SET_ID, shared.snapshot_id).with_suffix(suffix)
    if payload is None:
        path.unlink()
    else:
        path.write_bytes(payload)
    index_path = repository.index_directory / str(_KEY_ID)
    original = index_path.read_bytes(), index_path.stat().st_ino

    with pytest.raises(InconsistentStateError) as failure:
        facade.rebuild_index(_CLIENT_ID, _SET_ID)

    assert str(failure.value).startswith(f"{path.relative_to(repository.path)}:")
    assert (index_path.read_bytes(), index_path.stat().st_ino) == original


@pytest.mark.parametrize("missing", (False, True), ids=("truncated-pack", "missing-pack"))
def test_rebuild_preserves_the_index_when_a_required_pack_is_bad(rebuild_repository: tuple[ServerFacade, Repository], missing: bool) -> None:
    facade, repository = rebuild_repository
    _backup(facade, _SET_ID, 1)
    _backup(facade, _SHARED_SET_ID, 2)
    pack = repository.packs_root / str(_KEY_ID) / "2.pack"
    if missing:
        pack.unlink()
        message = "missing index references: 2"
    else:
        pack.write_bytes(pack.read_bytes()[:-1])
        message = r"2\.pack: Pack file length does not match its header"
    index_path = repository.index_directory / str(_KEY_ID)
    original = index_path.read_bytes()

    with pytest.raises(InconsistentStateError, match=message):
        facade.rebuild_index(_CLIENT_ID, _SET_ID)

    assert index_path.read_bytes() == original


def test_rebuild_rejects_a_packed_root_with_the_wrong_type(rebuild_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = rebuild_repository
    snapshot = _backup(facade, _SET_ID, 1)
    pack = repository.packs_root / str(_KEY_ID) / "1.pack"
    with pack.open("r+b") as output:
        output.seek(PACK_HEADER_SIZE + OBJECT_HEADER_SIZE + 1)
        output.write(Object(TYPE_DATA, snapshot.root_id, 1).to_header())
    index_path = repository.index_directory / str(_KEY_ID)
    original = index_path.read_bytes()

    with pytest.raises(InconsistentStateError, match="Invalid pack record for snapshot root"):
        facade.rebuild_index(_CLIENT_ID, _SET_ID)

    assert index_path.read_bytes() == original


@pytest.mark.parametrize("symlink", (False, True), ids=("directory", "symlink"))
def test_rebuild_rejects_a_finalized_pack_path_that_is_not_a_regular_file(rebuild_repository: tuple[ServerFacade, Repository], symlink: bool) -> None:
    facade, repository = rebuild_repository
    _backup(facade, _SET_ID, 1)
    pack_directory = repository.packs_root / str(_KEY_ID)
    path = pack_directory / "2.pack"
    if symlink:
        path.symlink_to(pack_directory / "1.pack")
    else:
        path.mkdir()
    index_path = repository.index_directory / str(_KEY_ID)
    original = index_path.read_bytes()

    with pytest.raises(InconsistentStateError, match=r"Unexpected pack file: 2\.pack"):
        facade.rebuild_index(_CLIENT_ID, _SET_ID)

    assert index_path.read_bytes() == original


def test_rebuild_cannot_replace_an_index_in_a_different_key_domain(rebuild_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = rebuild_repository
    _backup(facade, _SET_ID, 1)
    _backup(facade, _OTHER_SET_ID, 2)
    index_path = repository.index_directory / str(_KEY_ID)
    other_path = repository.index_directory / str(_OTHER_KEY_ID)
    original = other_path.read_bytes()
    index_path.unlink()
    index_path.symlink_to(other_path)

    with pytest.raises(InconsistentStateError, match="Unexpected location index path"):
        facade.rebuild_index(_CLIENT_ID, _SET_ID)

    assert index_path.is_symlink()
    assert other_path.read_bytes() == original


def test_rebuild_keeps_the_checkpoint_if_replacement_fails(rebuild_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = rebuild_repository
    _backup(facade, _SET_ID, 1)
    before = {path: path.read_bytes() for path in repository.index_directory.iterdir()}

    with patch("p8472backup.storage.file.index.os.replace", side_effect=OSError("replace failed")), pytest.raises(OSError, match="replace failed"):
        facade.rebuild_index(_CLIENT_ID, _SET_ID)

    assert {path: path.read_bytes() for path in repository.index_directory.iterdir()} == before


def test_rebuild_keeps_the_checkpoint_if_the_pack_directory_cannot_be_synced(rebuild_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = rebuild_repository
    _backup(facade, _SET_ID, 1)
    index_path = repository.index_directory / str(_KEY_ID)
    original = index_path.read_bytes(), index_path.stat().st_ino

    with patch("p8472backup.server.snapshot_service.sync_directory", side_effect=OSError("pack sync failed")) as sync, pytest.raises(OSError, match="pack sync failed"):
        facade.rebuild_index(_CLIENT_ID, _SET_ID)

    sync.assert_called_once_with(repository.packs_root / str(_KEY_ID))
    assert (index_path.read_bytes(), index_path.stat().st_ino) == original


def test_rebuild_stops_when_the_repository_is_locked(rebuild_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = rebuild_repository
    with RepositoryLock(repository.path), pytest.raises(BlockingIOError):
        facade.rebuild_index(_CLIENT_ID, _SET_ID)


@pytest.mark.parametrize("payload", (None, b"{"), ids=("missing-register", "invalid-register"))
def test_rebuild_requires_a_valid_snapshot_register(rebuild_repository: tuple[ServerFacade, Repository], payload: bytes | None) -> None:
    facade, repository = rebuild_repository
    _backup(facade, _SET_ID, 1)
    registry = repository.path / SNAPSHOT_REGISTRY_NAME
    if payload is None:
        registry.unlink()
    else:
        registry.write_bytes(payload)
    index_path = repository.index_directory / str(_KEY_ID)
    original = index_path.read_bytes()

    with pytest.raises(InconsistentStateError, match=r"snapshot-registry\.json:"):
        facade.rebuild_index(_CLIENT_ID, _SET_ID)

    assert index_path.read_bytes() == original


def test_rebuild_rejects_a_backup_set_with_the_wrong_key_id(rebuild_repository: tuple[ServerFacade, Repository]) -> None:
    _, repository = rebuild_repository
    backup_set = replace(repository.get_backup_set(_SET_ID), key_id=_OTHER_KEY_ID)

    with RepositoryLock(repository.path) as lock, pytest.raises(ValueError, match="Backup set does not match the repository"):
        SnapshotService().rebuild_index(repository, backup_set, lock)
