from datetime import UTC, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from p8472backup.archive.datacontent import COMPRESSION_NONE, DataContent
from p8472backup.archive.metacontent import MetaContent
from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, TYPE_DATA, TYPE_METADATA, TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.archive.snapshotrootcontent import SnapshotRootContent
from p8472backup.server.repository import Repository
from p8472backup.server.repository_admin_service import RepositoryAdminService
from p8472backup.server.snapshot_service import SnapshotService
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import HEADER_SIZE as PACK_HEADER_SIZE, Pack


_CLIENT_ID = UUID(int=2)
_BACKUP_SET_ID = UUID(int=3)
_KEY_DOMAIN_ID = UUID(int=4)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    return RepositoryAdminService().init_repository(tmp_path / "repo", _CLIENT_ID, ((_BACKUP_SET_ID, _KEY_DOMAIN_ID),))


@pytest.fixture
def snapshot_service() -> SnapshotService:
    return SnapshotService()


def test_snapshot_catalogue_resolves_objects_through_saved_index(tmp_path: Path, repository: Repository, snapshot_service: SnapshotService) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(b"Backup report\n")
    chunk = source.read_bytes()

    data_id, metadata_id, root_id = b"d" * 32, b"m" * 32, b"r" * 32
    # Use the serialized payloads as opaque bodies for this server-side test.
    bodies = (
        (TYPE_SNAPSHOT, root_id, SnapshotRootContent(b"backup set configuration", (metadata_id,)).to_plaintext()),
        (TYPE_DATA, data_id, DataContent(COMPRESSION_NONE, len(chunk), chunk).to_plaintext()),
        (TYPE_METADATA, metadata_id, MetaContent(source.name.encode("utf-8"), (data_id,)).to_plaintext()),
    )
    records = ((Object(object_type, object_id, len(body)), BytesIO(body)) for object_type, object_id, body in bodies)
    pack_path = repository.packs_root / str(_KEY_DOMAIN_ID) / "1.pack"
    committed = snapshot_service.create_snapshot(repository, _BACKUP_SET_ID, records, root_id)
    snapshot = snapshot_service.get_snapshot(repository, _BACKUP_SET_ID, committed.snapshot_id)
    assert snapshot == committed
    assert snapshot.root_id == root_id
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


def test_create_snapshot_allocates_past_an_unindexed_pack(repository: Repository, snapshot_service: SnapshotService) -> None:
    directory = repository.packs_root / str(_KEY_DOMAIN_ID)
    previous = Object(TYPE_DATA, b"d" * 32, 1)
    Pack.write_to(directory / "1.pack", iter(((previous, BytesIO(b"d")),)))
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    snapshot = snapshot_service.create_snapshot(repository, _BACKUP_SET_ID, iter(((root, BytesIO(b"r")),)), root.object_id)
    assert snapshot.object_catalog.object_ids == {root.object_id}
    with RepositoryLock(repository.path) as lock:
        index = LocationIndex.load_from(repository.index_directory / str(_KEY_DOMAIN_ID), lock)
        assert index.get(root.object_id) == ObjectLocation(2, PACK_HEADER_SIZE, OBJECT_HEADER_SIZE + 1)
    assert Pack.load_from(directory / "2.pack").object_count == 1


@pytest.mark.parametrize("first", (None, Object(TYPE_DATA, b"r" * 32, 1), Object(TYPE_SNAPSHOT, b"a" * 32, 1)))
def test_creating_snapshot_requires_declared_root_first(repository: Repository, snapshot_service: SnapshotService, first: Object | None) -> None:
    records = iter(()) if first is None else iter(((first, BytesIO(b"a")),))
    with pytest.raises(ValueError, match="first record must be"):
        snapshot_service.create_snapshot(repository, _BACKUP_SET_ID, records, b"r" * 32)
    assert not list((repository.packs_root / str(_KEY_DOMAIN_ID)).iterdir())


def test_failed_index_save_does_not_publish_snapshot(repository: Repository, snapshot_service: SnapshotService, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_save(index: LocationIndex, path: Path, lock: RepositoryLock) -> bool:
        lock.require_held_for(path)
        raise OSError("index sync failed")

    monkeypatch.setattr(LocationIndex, "save_to", fail_save)
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    with pytest.raises(OSError, match="index sync failed"):
        snapshot_service.create_snapshot(repository, _BACKUP_SET_ID, iter(((root, BytesIO(b"r")),)), root.object_id)
    assert not list((repository.snapshots_root / str(_BACKUP_SET_ID)).iterdir())
    with RepositoryLock(repository.path) as lock:
        assert LocationIndex.load_from(repository.index_directory / str(_KEY_DOMAIN_ID), lock).get(root.object_id) is None


@pytest.mark.parametrize("target_size", (1, 1_000))
def test_snapshot_filter_stores_missing_objects_and_indexes_locations(repository: Repository, target_size: int) -> None:
    known = Object(TYPE_DATA, b"a" * 32, 1)
    first = Object(TYPE_SNAPSHOT, b"b" * 32, 1)
    second = Object(TYPE_DATA, b"c" * 32, 1)
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

    records = iter(((first, BytesIO(b"b")), (known, known_body), (first, duplicate_body), (second, BytesIO(b"c"))))
    snapshot = SnapshotService().create_snapshot(repository, _BACKUP_SET_ID, records, first.object_id, target_size=target_size)

    length = OBJECT_HEADER_SIZE + first.length
    first_location = ObjectLocation(2, PACK_HEADER_SIZE, length)
    if target_size == 1:
        second_location = ObjectLocation(3, PACK_HEADER_SIZE, length)
    else:
        second_location = ObjectLocation(2, PACK_HEADER_SIZE + length, length)
    with RepositoryLock(repository.path) as lock:
        index = LocationIndex.load_from(index_path, lock)
        assert index.get(known.object_id) == known_location
        for location, obj, body in ((first_location, first, b"b"), (second_location, second, b"c")):
            assert index.get(obj.object_id) == location
            output = BytesIO()
            path = pack_directory / f"{location.pack_id}.pack"
            assert Pack.read_object(path, location.offset, location.length, output) == obj
            assert output.getvalue() == body

    assert snapshot.object_catalog.object_ids == {known.object_id, first.object_id, second.object_id}
    assert known_body.tell() == duplicate_body.tell() == 0


def test_snapshot_filter_needs_no_pack_when_all_objects_are_known(repository: Repository, snapshot_service: SnapshotService) -> None:
    obj = Object(TYPE_SNAPSHOT, b"a" * 32, 1)
    snapshot_service.create_snapshot(repository, _BACKUP_SET_ID, iter(((obj, BytesIO(b"a")),)), obj.object_id)
    snapshot = snapshot_service.create_snapshot(repository, _BACKUP_SET_ID, iter(((obj, BytesIO(b"a")),)), obj.object_id)
    assert snapshot.object_catalog.object_ids == {obj.object_id}
    assert {path.name for path in (repository.packs_root / str(_KEY_DOMAIN_ID)).iterdir()} == {"1.pack"}


def test_incomplete_pack_adds_no_index_entries(repository: Repository, snapshot_service: SnapshotService) -> None:
    obj = Object(TYPE_SNAPSHOT, b"a" * 32, 2)
    with pytest.raises(ValueError, match="Truncated object body"):
        snapshot_service.create_snapshot(repository, _BACKUP_SET_ID, iter(((obj, BytesIO(b"a")),)), obj.object_id)
    with RepositoryLock(repository.path) as lock:
        index = LocationIndex.load_from(repository.index_directory / str(_KEY_DOMAIN_ID), lock)
        assert index.get(obj.object_id) is None
    assert not list((repository.packs_root / str(_KEY_DOMAIN_ID)).iterdir())
