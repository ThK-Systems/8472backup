"""Run and commit snapshots on the server's local filesystem."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from struct import Struct
from tempfile import NamedTemporaryFile
from typing import BinaryIO
from uuid import UUID

from p8472backup.archive.objects import OBJECT_ID_SIZE, TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.server.snapshots import ObjectCatalogue, Snapshot, store_snapshot_objects
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import DEFAULT_TARGET_SIZE


_SNAPSHOT_VERSION = 1
_SNAPSHOT_HEADER = Struct(f"<H16s16s16s{OBJECT_ID_SIZE}sqqQ")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def run_snapshot(
    repository: Path,
    records: Iterator[tuple[Object, BinaryIO]],
    *,
    snapshot_id: UUID,
    client_id: UUID,
    backup_set_id: UUID,
    root_id: ObjectId,
    index_path: Path,
    snapshot_directory: Path,
    pack_destinations: Iterator[tuple[int, Path]],
    protection_duration: timedelta = timedelta(days=7),
    target_size: int = DEFAULT_TARGET_SIZE,
) -> Snapshot:
    """Commit one snapshot and return it after durable publication.

    The caller supplies the authorized scope, initialized repository paths, and
    unused pack destinations in that scope. The repository lock covers the
    entire run, including publication.
    """
    if protection_duration < timedelta():
        raise ValueError("Snapshot protection duration must be nonnegative")

    with RepositoryLock(repository) as lock:
        directory = lock.require_held_for(snapshot_directory)
        path = directory / f"{snapshot_id}.snapshot"
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            if os.path.lexists(path):
                raise FileExistsError(f"Snapshot already exists: {path}")

            catalogue = _prepare_snapshot(records, root_id, index_path, lock, pack_destinations, target_size)
            committed_at = datetime.now(UTC)
            snapshot = Snapshot(
                snapshot_id=snapshot_id,
                client_id=client_id,
                backup_set_id=backup_set_id,
                root_id=root_id,
                committed_at=committed_at,
                protected_until=committed_at + protection_duration,
                object_catalog=catalogue,
                version=_SNAPSHOT_VERSION,
            )
            header = _SNAPSHOT_HEADER.pack(
                snapshot.version,
                snapshot.snapshot_id.bytes,
                snapshot.client_id.bytes,
                snapshot.backup_set_id.bytes,
                snapshot.root_id,
                (snapshot.committed_at - _EPOCH) // _MICROSECOND,
                (snapshot.protected_until - _EPOCH) // _MICROSECOND,
                len(catalogue.object_ids),
            )
            output = NamedTemporaryFile(dir=directory, prefix=".snapshot-", delete=False)
            temporary_path = Path(output.name)
            try:
                with output:
                    output.write(header)
                    for object_id in catalogue.object_ids:
                        output.write(object_id)
                    output.flush()
                    os.fsync(output.fileno())
                os.link(temporary_path, path)
            finally:
                temporary_path.unlink()
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return snapshot


def _prepare_snapshot(
    records: Iterator[tuple[Object, BinaryIO]],
    root_id: ObjectId,
    index_path: Path,
    lock: RepositoryLock,
    pack_destinations: Iterator[tuple[int, Path]],
    target_size: int = DEFAULT_TARGET_SIZE,
) -> ObjectCatalogue:
    """Persist the packs and index for a snapshot under a held repository lock.

    The repository must already have an index checkpoint. The caller keeps the
    lock through snapshot publication or abort.
    """
    Object.validate_object_id(root_id)
    index = LocationIndex.load_from(index_path, lock)
    first = next(records, None)
    if first is None or first[0].object_type != TYPE_SNAPSHOT or first[0].object_id != root_id:
        raise ValueError("The first record must be the declared type 3 snapshot root")
    catalogue = store_snapshot_objects(chain((first,), records), index, lock, pack_destinations, target_size)
    index.save_to(index_path, lock)
    return catalogue


def load_snapshot(path: Path, lock: RepositoryLock) -> Snapshot:
    """Read a committed snapshot record under its repository lock."""
    path = lock.require_held_for(path)
    with path.open("rb") as source:
        header = source.read(_SNAPSHOT_HEADER.size)
        if len(header) != _SNAPSHOT_HEADER.size:
            raise ValueError("Truncated snapshot header")
        version, snapshot_id, client_id, backup_set_id, root_id, committed_at, protected_until, count = _SNAPSHOT_HEADER.unpack(header)
        if version != _SNAPSHOT_VERSION:
            raise ValueError("Unsupported snapshot format")
        if protected_until < committed_at:
            raise ValueError("Snapshot protection deadline precedes commit time")
        if os.fstat(source.fileno()).st_size != len(header) + count * OBJECT_ID_SIZE:
            raise ValueError("Snapshot file size does not match its catalogue count")

        object_ids = frozenset(iter(lambda: source.read(OBJECT_ID_SIZE), b""))
        if len(object_ids) != count or root_id not in object_ids or any(len(object_id) != OBJECT_ID_SIZE for object_id in object_ids):
            raise ValueError("Invalid snapshot object catalogue")

    return Snapshot(
        snapshot_id=UUID(bytes=snapshot_id),
        client_id=UUID(bytes=client_id),
        backup_set_id=UUID(bytes=backup_set_id),
        root_id=root_id,
        committed_at=_EPOCH + timedelta(microseconds=committed_at),
        protected_until=_EPOCH + timedelta(microseconds=protected_until),
        object_catalog=ObjectCatalogue(object_ids),
        version=version,
    )
