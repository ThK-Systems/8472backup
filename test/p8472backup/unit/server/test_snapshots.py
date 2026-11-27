from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from p8472backup.archive.datacontent import COMPRESSION_NONE, DataContent
from p8472backup.archive.metacontent import MetaContent
from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, TYPE_DATA, TYPE_METADATA, TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.archive.snapshotrootcontent import SnapshotRootContent
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository import Repository
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.snapshot_service import SnapshotService
from p8472backup.server.trusted_time import TrustedTime
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import HEADER_SIZE as PACK_HEADER_SIZE, Pack


_CLIENT_ID = UUID(int=2)
_BACKUP_SET_ID = UUID(int=3)
_KEY_DOMAIN_ID = UUID(int=4)
_COMMITTED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    return RepositoryService().init_repository(tmp_path / str(_CLIENT_ID), _CLIENT_ID, ((_BACKUP_SET_ID, _KEY_DOMAIN_ID),))


@pytest.fixture
def snapshot_service(monkeypatch: pytest.MonkeyPatch) -> SnapshotService:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = _COMMITTED_AT
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    return SnapshotService()


def test_snapshot_catalogue_resolves_objects_through_saved_index(tmp_path: Path, repository: Repository, snapshot_service: SnapshotService) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(b"Backup report\n")
    chunk = source.read_bytes()

    data_id, metadata_id, root_id = b"d" * 32, b"m" * 32, b"r" * 32
    # Use the serialized payloads as opaque bodies for this server-side test.
    bodies = (
        (TYPE_DATA, data_id, DataContent(COMPRESSION_NONE, len(chunk), chunk).to_plaintext()),
        (TYPE_METADATA, metadata_id, MetaContent(source.name.encode("utf-8"), (data_id,)).to_plaintext()),
        (TYPE_SNAPSHOT, root_id, SnapshotRootContent(b"backup set configuration", (metadata_id,)).to_plaintext()),
    )
    records = ((Object(object_type, object_id, len(body)), BytesIO(body)) for object_type, object_id, body in bodies)
    pack_path = repository.packs_root / str(_KEY_DOMAIN_ID) / "1.pack"
    with RepositoryService().locked_repository(tmp_path, _CLIENT_ID) as (current, lock):
        committed = snapshot_service.create_snapshot(current, _BACKUP_SET_ID, records, lock=lock)
    snapshot = snapshot_service.get_snapshot(repository, _BACKUP_SET_ID, committed.snapshot_id)
    assert snapshot == committed
    assert snapshot.root_id == root_id
    assert snapshot.committed_at == _COMMITTED_AT
    assert snapshot.committed_at.tzinfo is UTC
    assert snapshot.protected_until - snapshot.committed_at == timedelta(days=7)
    assert snapshot.object_catalog.object_ids == {root_id, metadata_id, data_id}

    restored: dict[ObjectId, bytes] = {}
    with RepositoryLock(repository.path) as lock:
        saved_index = LocationIndex.load_from(repository.index_directory / str(_KEY_DOMAIN_ID), lock)
        for object_id in snapshot.object_catalog.object_ids:
            location = saved_index.get(object_id)
            assert location is not None
            assert location.pack_id == 1
            output = BytesIO()
            obj = Pack.read_object(pack_path, location.offset, location.length, output)
            assert obj.object_id == object_id
            restored[object_id] = output.getvalue()

    root = SnapshotRootContent.from_plaintext(restored[snapshot.root_id])
    assert root.description == b"backup set configuration"
    (entry_id,) = root.metadata_ids
    metadata = MetaContent.from_plaintext(restored[entry_id])
    assert metadata.metadata.decode("utf-8") == source.name
    restored_file = b"".join(DataContent.from_plaintext(restored[chunk_id]).data for chunk_id in metadata.chunk_ids)
    assert restored_file == chunk

    catalogue_path = repository.snapshot_file(_BACKUP_SET_ID, committed.snapshot_id).with_suffix(".catalog")
    with catalogue_path.open("ab") as catalogue_output:
        catalogue_output.write(b"z" * 32)
    with pytest.raises(ValueError, match="Invalid snapshot object catalogue"):
        snapshot_service.get_snapshot(repository, _BACKUP_SET_ID, committed.snapshot_id)
    with RepositoryService().locked_repository(tmp_path, _CLIENT_ID) as (current, lock):
        (deferred,) = snapshot_service.list_snapshots(current, lock)
        with pytest.raises(ValueError, match="Invalid snapshot object catalogue"):
            _ = deferred.object_catalog


def test_snapshot_catalogue_loads_once_under_the_original_lock(repository: Repository, snapshot_service: SnapshotService) -> None:
    admin = RepositoryService()
    storage_root = repository.path.parent
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    with admin.locked_repository(storage_root, _CLIENT_ID) as (current, lock):
        committed = snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((root, BytesIO(b"r")),)), lock=lock)

    catalogue_path = repository.snapshot_file(_BACKUP_SET_ID, committed.snapshot_id).with_suffix(".catalog")
    contents = catalogue_path.read_bytes()
    catalogue_path.unlink()

    reopened = admin.find_by_client_id(storage_root, _CLIENT_ID)
    (detached,) = reopened.snapshots
    assert reopened.snapshots == {committed}

    with admin.locked_repository(storage_root, _CLIENT_ID) as (current, lock):
        (deferred,) = snapshot_service.list_snapshots(current, lock)
        with pytest.raises(RuntimeError, match="Repository lock is not held"):
            _ = detached.object_catalog
        with pytest.raises(FileNotFoundError):
            _ = deferred.object_catalog

        catalogue_path.write_bytes(contents)
        catalogue = deferred.object_catalog
        assert catalogue.object_ids == {root.object_id}
        catalogue_path.unlink()
        assert deferred.object_catalog is catalogue

    assert deferred.object_catalog is catalogue


def test_create_snapshot_allocates_past_an_unindexed_pack(repository: Repository, snapshot_service: SnapshotService) -> None:
    directory = repository.packs_root / str(_KEY_DOMAIN_ID)
    previous = Object(TYPE_DATA, b"d" * 32, 1)
    Pack.write_to(directory / "1.pack", iter(((previous, BytesIO(b"d")),)))
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    with RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock):
        snapshot = snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((root, BytesIO(b"r")),)), lock=lock)
        index = LocationIndex.load_from(repository.index_directory / str(_KEY_DOMAIN_ID), lock)
        assert index.get(root.object_id) == ObjectLocation(2, PACK_HEADER_SIZE, OBJECT_HEADER_SIZE + 1)
    assert snapshot.object_catalog.object_ids == {root.object_id}
    assert Pack.load_from(directory / "2.pack").object_count == 1


@pytest.mark.parametrize("obj", (None, Object(TYPE_DATA, b"d" * 32, 1), Object(TYPE_METADATA, b"m" * 32, 1)))
def test_creating_snapshot_requires_final_root(repository: Repository, snapshot_service: SnapshotService, obj: Object | None) -> None:
    records = iter(()) if obj is None else iter(((obj, BytesIO(b"a")),))
    with RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock):
        with pytest.raises(ValueError, match="Backup input ended before the final snapshot root"):
            snapshot_service.create_snapshot(current, _BACKUP_SET_ID, records, lock=lock)
        assert snapshot_service.list_snapshots(current, lock) == frozenset()
        assert len(LocationIndex.load_from(repository.index_directory / str(_KEY_DOMAIN_ID), lock)) == 0


def test_failed_index_save_does_not_publish_snapshot(repository: Repository, snapshot_service: SnapshotService, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_save(index: LocationIndex, path: Path, lock: RepositoryLock, *, temporary_prefix: str) -> bool:
        lock.require_held_for(path)
        raise OSError("index sync failed")

    monkeypatch.setattr(LocationIndex, "save_to", fail_save)
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    with RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock), pytest.raises(OSError, match="index sync failed"):
        snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((root, BytesIO(b"r")),)), lock=lock)
    assert not list((repository.snapshots_root / str(_BACKUP_SET_ID)).iterdir())
    with RepositoryLock(repository.path) as lock:
        assert LocationIndex.load_from(repository.index_directory / str(_KEY_DOMAIN_ID), lock).get(root.object_id) is None


def test_failed_register_write_leaves_snapshot_unpublished(repository: Repository, snapshot_service: SnapshotService) -> None:
    snapshot_id = UUID(int=42)
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    with (
        RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock),
        patch.object(SnapshotService, "_write_registry", side_effect=OSError("register write failed")),
        pytest.raises(OSError, match="register write failed"),
    ):
        snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((root, BytesIO(b"r")),)), lock=lock, snapshot_id=snapshot_id)

    path = repository.snapshot_file(_BACKUP_SET_ID, snapshot_id)
    assert path.is_file()
    assert path.with_suffix(".catalog").is_file()
    with pytest.raises(FileNotFoundError, match="Snapshot is not published"):
        snapshot_service.get_snapshot(repository, _BACKUP_SET_ID, snapshot_id)


@pytest.mark.parametrize("target_size", (1, 1_000))
def test_snapshot_filter_stores_missing_objects_and_indexes_locations(repository: Repository, snapshot_service: SnapshotService, target_size: int) -> None:
    known = Object(TYPE_DATA, b"a" * 32, 1)
    first = Object(TYPE_DATA, b"b" * 32, 1)
    root = Object(TYPE_SNAPSHOT, b"c" * 32, 1)
    known_body = BytesIO(b"a")
    duplicate_body = BytesIO(b"b")
    index_path = repository.index_directory / str(_KEY_DOMAIN_ID)
    pack_directory = repository.packs_root / str(_KEY_DOMAIN_ID)
    known_location = ObjectLocation(1, PACK_HEADER_SIZE, OBJECT_HEADER_SIZE + known.length)
    with RepositoryLock(repository.path) as lock:
        Pack.write_to(pack_directory / "1.pack", iter(((known, BytesIO(b"a")),)))
        index = LocationIndex.load_from(index_path, lock)
        index.put(known.object_id, known_location)
        index.save_to(index_path, lock)

    records = iter(((first, BytesIO(b"b")), (known, known_body), (first, duplicate_body), (root, BytesIO(b"c"))))
    with RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock):
        snapshot = snapshot_service.create_snapshot(current, _BACKUP_SET_ID, records, lock=lock, target_size=target_size)

    length = OBJECT_HEADER_SIZE + first.length
    first_location = ObjectLocation(2, PACK_HEADER_SIZE, length)
    if target_size == 1:
        root_location = ObjectLocation(3, PACK_HEADER_SIZE, length)
    else:
        root_location = ObjectLocation(2, PACK_HEADER_SIZE + length, length)
    with RepositoryLock(repository.path) as lock:
        index = LocationIndex.load_from(index_path, lock)
        assert index.get(known.object_id) == known_location
        for location, obj, body in ((first_location, first, b"b"), (root_location, root, b"c")):
            assert index.get(obj.object_id) == location
            output = BytesIO()
            path = pack_directory / f"{location.pack_id}.pack"
            assert Pack.read_object(path, location.offset, location.length, output) == obj
            assert output.getvalue() == body

    assert snapshot.object_catalog.object_ids == {known.object_id, first.object_id, root.object_id}
    assert snapshot.root_id == root.object_id
    assert known_body.tell() == duplicate_body.tell() == 0


def test_snapshot_filter_needs_no_pack_when_all_objects_are_known(repository: Repository, snapshot_service: SnapshotService) -> None:
    obj = Object(TYPE_SNAPSHOT, b"a" * 32, 1)
    with RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock):
        snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((obj, BytesIO(b"a")),)), lock=lock)
        snapshot = snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((obj, BytesIO(b"a")),)), lock=lock)
    assert snapshot.object_catalog.object_ids == {obj.object_id}
    assert {path.name for path in (repository.packs_root / str(_KEY_DOMAIN_ID)).iterdir()} == {"1.pack"}


def test_snapshot_rejects_a_root_indexed_as_a_data_object(repository: Repository, snapshot_service: SnapshotService) -> None:
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    data = Object(TYPE_DATA, b"d" * 32, 1)
    with RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock):
        committed = snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((data, BytesIO(b"d")), (root, BytesIO(b"r")))), lock=lock)
        reused = Object(TYPE_SNAPSHOT, data.object_id, 1)
        with pytest.raises(InconsistentStateError, match="Indexed snapshot root"):
            snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((reused, BytesIO(b"d")),)), lock=lock)
        assert snapshot_service.list_snapshots(current, lock) == {committed}


@pytest.mark.parametrize("target_size", (1, 1_000))
def test_snapshot_rejects_a_root_id_used_by_an_earlier_record(repository: Repository, snapshot_service: SnapshotService, target_size: int) -> None:
    data = Object(TYPE_DATA, b"d" * 32, 1)
    root = Object(TYPE_SNAPSHOT, data.object_id, 1)
    records = iter(((data, BytesIO(b"d")), (root, BytesIO(b"r"))))
    with RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock):
        with pytest.raises(ValueError, match="Snapshot root ID matches an earlier object"):
            snapshot_service.create_snapshot(current, _BACKUP_SET_ID, records, lock=lock, target_size=target_size)
        assert snapshot_service.list_snapshots(current, lock) == frozenset()
        assert len(LocationIndex.load_from(repository.index_directory / str(_KEY_DOMAIN_ID), lock)) == 0


def test_snapshot_rejects_a_missing_indexed_pack_before_writing_new_packs(repository: Repository, snapshot_service: SnapshotService) -> None:
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    data = Object(TYPE_DATA, b"d" * 32, 1)
    with RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock):
        committed = snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((data, BytesIO(b"d")), (root, BytesIO(b"r")))), lock=lock)
        pack = repository.packs_root / str(_KEY_DOMAIN_ID) / "1.pack"
        pack.unlink()

        new_root = Object(TYPE_SNAPSHOT, b"s" * 32, 1)
        records = iter(((data, BytesIO(b"d")), (new_root, BytesIO(b"s"))))
        with pytest.raises(InconsistentStateError, match=r"Indexed pack is missing: 1\.pack"):
            snapshot_service.create_snapshot(current, _BACKUP_SET_ID, records, lock=lock, target_size=1)
        assert snapshot_service.list_snapshots(current, lock) == {committed}
        assert list(pack.parent.iterdir()) == []


def test_incomplete_pack_adds_no_index_entries(repository: Repository, snapshot_service: SnapshotService) -> None:
    obj = Object(TYPE_SNAPSHOT, b"a" * 32, 2)
    with RepositoryService().locked_repository(repository.path.parent, _CLIENT_ID) as (current, lock), pytest.raises(ValueError, match="Truncated object body"):
        snapshot_service.create_snapshot(current, _BACKUP_SET_ID, iter(((obj, BytesIO(b"a")),)), lock=lock)
    with RepositoryLock(repository.path) as lock:
        index = LocationIndex.load_from(repository.index_directory / str(_KEY_DOMAIN_ID), lock)
        assert index.get(obj.object_id) is None
    assert not list((repository.packs_root / str(_KEY_DOMAIN_ID)).iterdir())
