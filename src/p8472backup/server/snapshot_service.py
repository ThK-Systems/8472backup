"""Create and retrieve snapshots for an authorized backup set."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain, count
from pathlib import Path
from struct import Struct
from tempfile import NamedTemporaryFile
from typing import BinaryIO, cast
from uuid import UUID, uuid4

from p8472backup.archive.objects import OBJECT_ID_SIZE, TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.server import trusted_time
from p8472backup.server.repository import BackupSet, Repository
from p8472backup.server.snapshot import SNAPSHOT_VERSION, ObjectCatalogue, Snapshot
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import DEFAULT_TARGET_SIZE, Pack


_SNAPSHOT_HEADER = Struct(f"<H16s16s16s{OBJECT_ID_SIZE}sqqQ")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
DEFAULT_PROTECTION_DURATION = timedelta(days=7)


class SnapshotService:
    """Use one repository, backup set, and key domain for snapshot operations."""

    def create_snapshot(
        self,
        repository: Repository,
        backup_set_id: UUID,
        records: Iterator[tuple[Object, BinaryIO]],
        root_id: ObjectId,
        *,
        lock: RepositoryLock,
        snapshot_id: UUID | None = None,
        protection_duration: timedelta = DEFAULT_PROTECTION_DURATION,
        target_size: int = DEFAULT_TARGET_SIZE,
    ) -> Snapshot:
        """Write a snapshot using the repository and lock from locked_repository()."""
        if protection_duration < timedelta():
            raise ValueError("Snapshot protection duration must be nonnegative")
        if target_size <= 0:
            raise ValueError("Pack target size must be positive")

        backup_set = repository.get_backup_set(backup_set_id)
        if backup_set.deleted:
            raise ValueError(f"Backup set is deleted: {backup_set_id}")
        Object.validate_object_id(root_id)
        if snapshot_id is None:
            snapshot_id = uuid4()

        path = repository.snapshot_file(backup_set_id, snapshot_id)
        directory = lock.require_held_for(path.parent)
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        path = directory / path.name
        if os.path.lexists(path):
            raise FileExistsError(f"Snapshot already exists: {path}")

        index_path = repository.index_directory / str(backup_set.key_id)
        index = LocationIndex.load_from(index_path, lock)
        first = next(records, None)
        if first is None or first[0].object_type != TYPE_SNAPSHOT or first[0].object_id != root_id:
            raise ValueError("The first record must be the declared type 3 snapshot root")

        catalogue = self._store_snapshot_objects(chain((first,), records), index, lock, repository.packs_root / str(backup_set.key_id), target_size)
        index.save_to(index_path, lock)

        committed_at = cast(trusted_time.TrustedTime, trusted_time.server_clock).now()
        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            backup_set_id=backup_set_id,
            root_id=root_id,
            committed_at=committed_at,
            protected_until=committed_at + protection_duration,
            object_catalog=catalogue,
        )
        self._save_snapshot(snapshot, repository, path)
        return snapshot

    def get_snapshot(self, repository: Repository, backup_set_id: UUID, snapshot_id: UUID) -> Snapshot:
        """Load a committed snapshot in this service's client and backup set."""
        repository.get_backup_set(backup_set_id)
        with RepositoryLock(repository.path) as lock:
            path = lock.require_held_for(repository.snapshot_file(backup_set_id, snapshot_id))
            return self._load_snapshot(repository, backup_set_id, snapshot_id, path)

    def list_snapshots(self, repository: Repository, lock: RepositoryLock) -> frozenset[Snapshot]:
        """Load the published development records for all registered sets."""
        snapshots: set[Snapshot] = set()
        for backup_set_id in repository.backup_sets:
            directory = lock.require_held_for(repository.snapshots_root / str(backup_set_id))
            snapshots.update(snapshot for _, snapshot in self._read_set_snapshots(repository, backup_set_id, directory, lock))
        return frozenset(snapshots)

    def remove_unprotected_snapshots(self, repository: Repository, backup_set: BackupSet, lock: RepositoryLock, *, protection_duration: timedelta) -> None:
        """Remove expired snapshot records from a deleted backup set."""
        backup_set_id = backup_set.backup_set_id
        if not backup_set.deleted or repository.backup_sets.get(backup_set_id) != backup_set:
            raise ValueError(f"Backup set is not deleted in this repository: {backup_set_id}")
        directory = lock.require_held_for(repository.snapshots_root / str(backup_set_id))
        now = cast(trusted_time.TrustedTime, trusted_time.server_clock).now()
        expired: list[Path] = []
        for path, snapshot in self._read_set_snapshots(repository, backup_set_id, directory, lock):
            effective_deadline = max(snapshot.protected_until, snapshot.committed_at + protection_duration)
            if now > effective_deadline:
                expired.append(path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for path in expired:
                path.unlink()
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _read_set_snapshots(self, repository: Repository, backup_set_id: UUID, directory: Path, lock: RepositoryLock) -> Iterator[tuple[Path, Snapshot]]:
        for path in directory.iterdir():
            if path.suffix == ".snapshot":
                yield path, self._load_snapshot(repository, backup_set_id, UUID(path.stem), lock.require_held_for(path))

    def _save_snapshot(self, snapshot: Snapshot, repository: Repository, path: Path) -> None:
        """Durably publish the snapshot without replacing an existing record."""
        directory = path.parent
        if snapshot.version != SNAPSHOT_VERSION:
            raise ValueError("Unsupported snapshot format")
        if snapshot.committed_at.utcoffset() is None or snapshot.protected_until.utcoffset() is None:
            raise ValueError("Snapshot timestamps must include a time zone")
        if snapshot.protected_until < snapshot.committed_at:
            raise ValueError("Snapshot protection deadline precedes commit time")
        object_ids = snapshot.object_catalog.object_ids
        if snapshot.root_id not in object_ids:
            raise ValueError("Snapshot root is missing from the object catalogue")

        header = _SNAPSHOT_HEADER.pack(
            snapshot.version,
            snapshot.snapshot_id.bytes,
            repository.client_id.bytes,
            snapshot.backup_set_id.bytes,
            snapshot.root_id,
            (snapshot.committed_at - _EPOCH) // _MICROSECOND,
            (snapshot.protected_until - _EPOCH) // _MICROSECOND,
            len(object_ids),
        )
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            output = NamedTemporaryFile(dir=directory, prefix=".snapshot-", delete=False)
            temporary_path = Path(output.name)
            try:
                with output:
                    output.write(header)
                    for object_id in object_ids:
                        Object.validate_object_id(object_id)
                        output.write(object_id)
                    output.flush()
                    os.fsync(output.fileno())
                os.link(temporary_path, path)
            finally:
                temporary_path.unlink()
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _load_snapshot(repository: Repository, backup_set_id: UUID, snapshot_id: UUID, path: Path) -> Snapshot:
        """Read a committed snapshot record under its repository lock."""
        with path.open("rb") as source:
            header = source.read(_SNAPSHOT_HEADER.size)
            if len(header) != _SNAPSHOT_HEADER.size:
                raise ValueError("Truncated snapshot header")
            version, stored_snapshot_id, client_id, stored_backup_set_id, root_id, committed_at, protected_until, count = _SNAPSHOT_HEADER.unpack(header)
            if version != SNAPSHOT_VERSION:
                raise ValueError("Unsupported snapshot format")
            if (stored_snapshot_id, client_id, stored_backup_set_id) != (snapshot_id.bytes, repository.client_id.bytes, backup_set_id.bytes):
                raise ValueError("Snapshot identity or scope does not match the requested record")
            if protected_until < committed_at:
                raise ValueError("Snapshot protection deadline precedes commit time")
            if os.fstat(source.fileno()).st_size != len(header) + count * OBJECT_ID_SIZE:
                raise ValueError("Snapshot file size does not match its catalogue count")

            object_ids = frozenset(iter(lambda: source.read(OBJECT_ID_SIZE), b""))
            if len(object_ids) != count or root_id not in object_ids or any(len(object_id) != OBJECT_ID_SIZE for object_id in object_ids):
                raise ValueError("Invalid snapshot object catalogue")

        return Snapshot(
            snapshot_id=snapshot_id,
            backup_set_id=backup_set_id,
            root_id=root_id,
            committed_at=_EPOCH + timedelta(microseconds=committed_at),
            protected_until=_EPOCH + timedelta(microseconds=protected_until),
            object_catalog=ObjectCatalogue(object_ids),
            version=version,
        )

    def _store_snapshot_objects(
        self,
        records: Iterator[tuple[Object, BinaryIO]],
        index: LocationIndex,
        lock: RepositoryLock,
        pack_directory: Path,
        target_size: int,
    ) -> ObjectCatalogue:
        """Collect the catalogue and write new objects to packs.

        The input iterator must allow advancing past an unread body.
        """
        object_ids: set[ObjectId] = set()

        def iter_missing_objects() -> Iterator[tuple[Object, BinaryIO]]:
            for obj, body in records:
                if obj.object_id in object_ids:
                    continue
                object_ids.add(obj.object_id)
                if index.get(obj.object_id) is None:
                    yield obj, body

        missing = iter_missing_objects()
        destinations = self._pack_destinations(lock.require_held_for(pack_directory))
        while (first := next(missing, None)) is not None:
            pack_id, path = next(destinations)
            if pack_id >= 2**64:
                raise ValueError(f"Pack ID exceeds the supported range: {pack_id}")
            path = lock.require_held_for(path)

            def record_location(object_id: ObjectId, offset: int, length: int) -> None:
                index.put(object_id, ObjectLocation(pack_id, offset, length))

            Pack.write_to(path, chain((first,), missing), target_size, on_stored=record_location)

        return ObjectCatalogue(frozenset(object_ids))

    @staticmethod
    def _pack_destinations(directory: Path) -> Iterator[tuple[int, Path]]:
        highest_id = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                stem = entry.name.removesuffix(".pack")
                if stem != entry.name and stem.isascii() and stem.isdecimal():
                    highest_id = max(highest_id, int(stem))

        for pack_id in count(highest_id + 1):
            yield pack_id, directory / f"{pack_id}.pack"
