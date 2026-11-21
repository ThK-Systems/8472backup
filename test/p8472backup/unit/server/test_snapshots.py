from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from p8472backup.archive.dataobject import COMPRESSION_NONE, DataObject
from p8472backup.archive.metaobject import MetaObject
from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, TYPE_DATA, TYPE_METADATA, TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.archive.snapshotroot import SnapshotRoot
from p8472backup.server.snapshots import Snapshot, store_snapshot_objects
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import HEADER_SIZE as PACK_HEADER_SIZE, Pack


def test_snapshot_catalogue_resolves_objects_through_saved_index(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(b"Backup report\n")
    chunk = source.read_bytes()

    data_id, metadata_id, root_id = b"d" * 32, b"m" * 32, b"r" * 32
    # Use the serialized payloads as opaque bodies for this server-side test.
    bodies = (
        (TYPE_DATA, data_id, DataObject(COMPRESSION_NONE, len(chunk), chunk).to_plaintext()),
        (TYPE_METADATA, metadata_id, MetaObject(source.name.encode("utf-8"), (data_id,)).to_plaintext()),
        (TYPE_SNAPSHOT, root_id, SnapshotRoot(b"backup set configuration", (metadata_id,)).to_plaintext()),
    )
    records = ((Object(object_type, object_id, len(body)), BytesIO(body)) for object_type, object_id, body in bodies)
    pack_path = tmp_path / "pack"
    index_path = tmp_path / "index"
    index = LocationIndex()
    with RepositoryLock(tmp_path) as lock:
        catalogue = store_snapshot_objects(records, index, lock, iter(((1, pack_path),)))
        assert index.save_to(index_path, lock)
        committed_at = datetime(2026, 1, 1, tzinfo=UTC)
        snapshot = Snapshot(
            snapshot_id=UUID(int=1),
            client_id=UUID(int=2),
            backup_set_id=UUID(int=3),
            root_id=root_id,
            committed_at=committed_at,
            protected_until=committed_at + timedelta(days=7),
            object_catalog=catalogue,
        )

    assert snapshot.object_catalog.object_ids == {root_id, metadata_id, data_id}
    restored: dict[ObjectId, bytes] = {}
    with RepositoryLock(tmp_path) as lock:
        saved_index = LocationIndex.load_from(index_path, lock)
        for object_id in snapshot.object_catalog.object_ids:
            location = saved_index.get(object_id)
            assert location is not None
            assert location.pack_id == 1
            output = BytesIO()
            obj = Pack.read_object(pack_path, location.offset, location.length, output)
            assert obj.object_id == object_id
            restored[object_id] = output.getvalue()

    root = SnapshotRoot.from_plaintext(restored[snapshot.root_id])
    assert root.description == b"backup set configuration"
    (entry_id,) = root.metadata_ids
    metadata = MetaObject.from_plaintext(restored[entry_id])
    assert metadata.metadata.decode("utf-8") == source.name
    restored_file = b"".join(DataObject.from_plaintext(restored[chunk_id]).data for chunk_id in metadata.chunk_ids)
    assert restored_file == chunk


@pytest.mark.parametrize("target_size", (1, 1_000))
def test_snapshot_filter_stores_missing_objects_and_indexes_locations(tmp_path: Path, target_size: int) -> None:
    known = Object(TYPE_DATA, b"a" * 32, 1)
    first = Object(TYPE_DATA, b"b" * 32, 1)
    second = Object(TYPE_DATA, b"c" * 32, 1)
    known_body = BytesIO(b"a")
    duplicate_body = BytesIO(b"b")
    index = LocationIndex()
    known_location = ObjectLocation(1, PACK_HEADER_SIZE, OBJECT_HEADER_SIZE + known.length)
    index.put(known.object_id, known_location)
    records = iter(((known, known_body), (first, BytesIO(b"b")), (first, duplicate_body), (second, BytesIO(b"c"))))
    first_path, second_path = tmp_path / "first", tmp_path / "second"
    destinations = iter(((2, first_path), (3, second_path)))
    with RepositoryLock(tmp_path) as lock:
        catalogue = store_snapshot_objects(records, index, lock, destinations, target_size=target_size)

    length = OBJECT_HEADER_SIZE + first.length
    first_location = ObjectLocation(2, PACK_HEADER_SIZE, length)
    if target_size == 1:
        second_location = ObjectLocation(3, PACK_HEADER_SIZE, length)
    else:
        second_path = first_path
        second_location = ObjectLocation(2, PACK_HEADER_SIZE + length, length)
    assert index.get(known.object_id) == known_location
    for path, location, obj, body in ((first_path, first_location, first, b"b"), (second_path, second_location, second, b"c")):
        assert index.get(obj.object_id) == location
        output = BytesIO()
        assert Pack.read_object(path, location.offset, location.length, output) == obj
        assert output.getvalue() == body

    assert catalogue.object_ids == {known.object_id, first.object_id, second.object_id}
    assert known_body.tell() == duplicate_body.tell() == 0


def test_snapshot_filter_needs_no_pack_when_all_objects_are_known(tmp_path: Path) -> None:
    index = LocationIndex()
    obj = Object(TYPE_DATA, b"a" * 32, 1)
    index.put(obj.object_id, ObjectLocation(1, PACK_HEADER_SIZE, OBJECT_HEADER_SIZE + obj.length))
    with RepositoryLock(tmp_path) as lock:
        catalogue = store_snapshot_objects(iter(((obj, BytesIO(b"a")),)), index, lock, iter(()))
    assert catalogue.object_ids == {obj.object_id}


def test_incomplete_pack_adds_no_index_entries(tmp_path: Path) -> None:
    index = LocationIndex()
    obj = Object(TYPE_DATA, b"a" * 32, 2)
    path = tmp_path / "pack"
    with RepositoryLock(tmp_path) as lock, pytest.raises(ValueError, match="Truncated object body"):
        store_snapshot_objects(iter(((obj, BytesIO(b"a")),)), index, lock, iter(((1, path),)))
    assert index.get(obj.object_id) is None
    assert not path.exists()
