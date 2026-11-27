"""Create and retrieve snapshots for an authorized backup set."""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import chain, count
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO
from uuid import UUID, uuid4

from p8472backup.archive.objects import OBJECT_ID_SIZE, TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository import BackupSet, Repository
from p8472backup.server.snapshot import SNAPSHOT_VERSION, ObjectCatalogue, Snapshot
from p8472backup.server.snapshot_listing import BackupSetListing, SnapshotListEntry
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.json_file import is_json_object, sync_directory, write_json
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import DEFAULT_TARGET_SIZE, Pack


DEFAULT_PROTECTION_DURATION = timedelta(days=7)
SNAPSHOT_REGISTRY_NAME = "snapshot-registry.json"
SNAPSHOT_REGISTRY_VERSION = 1


@dataclass(frozen=True)
class _SnapshotMetadata:
    root_id: ObjectId
    committed_at: datetime
    protected_until: datetime
    catalogue_checksum: bytes
    comment: str | None


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
        comment: str | None = None,
    ) -> Snapshot:
        """Write a snapshot using the repository and lock from locked_repository()."""
        if protection_duration < timedelta():
            raise ValueError("Snapshot protection duration must be nonnegative")
        if target_size <= 0:
            raise ValueError("Pack target size must be positive")
        self._validate_comment(comment)

        if repository.deleted:
            raise ValueError(f"Repository is deleted: {repository.client_id}")
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
        registry = self._read_registry(repository, lock)
        if snapshot_id in registry or os.path.lexists(path) or os.path.lexists(path.with_suffix(".catalog")):
            raise FileExistsError(f"Snapshot already exists: {path}")

        index_path = repository.index_directory / str(backup_set.key_id)
        index = LocationIndex.load_from(index_path, lock)
        first = next(records, None)
        if first is None or first[0].object_type != TYPE_SNAPSHOT or first[0].object_id != root_id:
            raise ValueError("The first record must be the declared type 3 snapshot root")

        catalogue = self._store_snapshot_objects(chain((first,), records), index, lock, repository.packs_root / str(backup_set.key_id), target_size)
        index.save_to(index_path, lock)

        committed_at = trusted_time.server_clock.now()
        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            backup_set_id=backup_set_id,
            root_id=root_id,
            committed_at=committed_at,
            protected_until=committed_at + protection_duration,
            object_catalog=catalogue,
            comment=comment,
        )
        self._save_snapshot(snapshot, repository, path, lock)
        registry[snapshot_id] = backup_set_id
        self._write_registry(repository, lock, registry)
        return snapshot

    def get_snapshot(self, repository: Repository, backup_set_id: UUID, snapshot_id: UUID) -> Snapshot:
        """Load a committed snapshot in this service's client and backup set."""
        repository.get_backup_set(backup_set_id)
        with RepositoryLock(repository.path) as lock:
            if self._read_registry(repository, lock).get(snapshot_id) != backup_set_id:
                raise FileNotFoundError(f"Snapshot is not published in this set: {snapshot_id}")
            return self._load_snapshot(repository, backup_set_id, snapshot_id, lock)

    def list_snapshots(self, repository: Repository, lock: RepositoryLock) -> frozenset[Snapshot]:
        """Load the published snapshots and their catalogues."""
        registry = self._read_registry(repository, lock)
        return frozenset(self._load_snapshot(repository, set_id, snapshot_id, lock) for snapshot_id, set_id in registry.items())

    def list_snapshot_entries(self, repository: Repository, lock: RepositoryLock, *, backup_set_id: UUID | None, protection_duration: timedelta) -> tuple[BackupSetListing, ...]:
        """Load the published metadata needed for the listing."""
        sets = tuple(repository.backup_sets.values()) if backup_set_id is None else (repository.get_backup_set(backup_set_id),)
        rows_by_set: dict[UUID, list[SnapshotListEntry]] = {backup_set.backup_set_id: [] for backup_set in sets}
        registry = self._read_registry(repository, lock)
        now = trusted_time.server_clock.now()
        for snapshot_id, set_id in registry.items():
            if (rows := rows_by_set.get(set_id)) is not None:
                metadata = self._read_snapshot_metadata(repository, set_id, snapshot_id, lock)
                rows.append(SnapshotListEntry(snapshot_id, metadata.comment, metadata.committed_at, self._is_protected(metadata, now, protection_duration)))

        groups: list[BackupSetListing] = []
        for backup_set in sets:
            set_id = backup_set.backup_set_id
            rows = rows_by_set[set_id]
            rows.sort(key=lambda entry: (entry.committed_at, entry.snapshot_id), reverse=True)
            groups.append(BackupSetListing(set_id, backup_set.name or str(set_id), tuple(rows)))
        return tuple(groups)

    def remove_unprotected_snapshots(self, repository: Repository, backup_set: BackupSet, lock: RepositoryLock, *, protection_duration: timedelta) -> None:
        """Remove expired snapshot records from a deleted backup set."""
        backup_set_id = backup_set.backup_set_id
        if not backup_set.deleted or repository.backup_sets.get(backup_set_id) != backup_set:
            raise ValueError(f"Backup set is not deleted in this repository: {backup_set_id}")
        directory = lock.require_held_for(repository.snapshots_root / str(backup_set_id))
        now = trusted_time.server_clock.now()
        registry = self._read_registry(repository, lock)
        expired: list[UUID] = []
        for snapshot_id, set_id in registry.items():
            if set_id != backup_set_id:
                continue
            metadata = self._read_snapshot_metadata(repository, set_id, snapshot_id, lock)
            self._read_catalogue(repository.snapshot_file(set_id, snapshot_id), lock, metadata)
            if not self._is_protected(metadata, now, protection_duration):
                expired.append(snapshot_id)
        if expired:
            for snapshot_id in expired:
                del registry[snapshot_id]
            self._write_registry(repository, lock, registry)
            for snapshot_id in expired:
                path = directory / repository.snapshot_file(backup_set_id, snapshot_id).name
                path.unlink()
                path.with_suffix(".catalog").unlink()
            sync_directory(directory)

    def _read_registry(self, repository: Repository, lock: RepositoryLock) -> dict[UUID, UUID]:
        path = lock.require_held_for(repository.path / SNAPSHOT_REGISTRY_NAME)
        try:
            with path.open("r", encoding="utf-8") as source:
                registry: object = json.load(source)
        except FileNotFoundError as error:
            raise InconsistentStateError("Snapshot register is missing") from error
        if not is_json_object(registry):
            raise ValueError("Invalid snapshot register")
        version, entries = registry.get("version"), registry.get("snapshots")
        if type(version) is not int or version != SNAPSHOT_REGISTRY_VERSION or not is_json_object(entries):
            raise ValueError("Invalid snapshot register")
        snapshots: dict[UUID, UUID] = {}
        for stored_id, stored_set in entries.items():
            if not isinstance(stored_id, str) or not isinstance(stored_set, str):
                raise ValueError("Invalid snapshot register entry")
            snapshot_id, set_id = UUID(stored_id), UUID(stored_set)
            if snapshot_id in snapshots or set_id not in repository.backup_sets:
                raise ValueError("Invalid snapshot register entry")
            snapshots[snapshot_id] = set_id
        if not snapshots:
            for set_id in repository.backup_sets:
                directory = lock.require_held_for(repository.snapshots_root / str(set_id))
                if any(entry.suffix == ".snapshot" for entry in directory.iterdir()):
                    raise InconsistentStateError("Repository contains development snapshot records that require conversion")
        return snapshots

    def _write_registry(self, repository: Repository, lock: RepositoryLock, registry: dict[UUID, UUID]) -> None:
        snapshots = {str(snapshot_id): str(set_id) for snapshot_id, set_id in registry.items()}
        write_json(repository.path / SNAPSHOT_REGISTRY_NAME, lock, {"version": SNAPSHOT_REGISTRY_VERSION, "snapshots": snapshots})

    def _is_protected(self, metadata: _SnapshotMetadata, now: datetime, protection_duration: timedelta) -> bool:
        return now <= max(metadata.protected_until, metadata.committed_at + protection_duration)

    def _validate_comment(self, comment: str | None) -> None:
        if comment is not None and len(comment) > 40:
            raise ValueError("Snapshot comment must be at most 40 characters")

    def _save_snapshot(self, snapshot: Snapshot, repository: Repository, path: Path, lock: RepositoryLock) -> None:
        """Write the durable snapshot files before publishing their register entry."""
        if snapshot.version != SNAPSHOT_VERSION:
            raise ValueError("Unsupported snapshot format")
        if snapshot.committed_at.utcoffset() is None or snapshot.protected_until.utcoffset() is None:
            raise ValueError("Snapshot timestamps must include a time zone")
        if snapshot.protected_until < snapshot.committed_at:
            raise ValueError("Snapshot protection deadline precedes commit time")
        object_ids = snapshot.object_catalog.object_ids
        if snapshot.root_id not in object_ids:
            raise ValueError("Snapshot root is missing from the object catalogue")

        checksum = sha256()
        with NamedTemporaryFile(dir=path.parent, prefix=".snapshot-", delete_on_close=False) as output:
            for object_id in object_ids:
                Object.validate_object_id(object_id)
                output.write(object_id)
                checksum.update(object_id)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.link(output.name, path.with_suffix(".catalog"))

        metadata: dict[str, object] = {
            "version": snapshot.version,
            "snapshot_id": str(snapshot.snapshot_id),
            "client_id": str(repository.client_id),
            "backup_set_id": str(snapshot.backup_set_id),
            "root_id": snapshot.root_id.hex(),
            "committed_at": snapshot.committed_at.astimezone(UTC).isoformat(),
            "protected_until": snapshot.protected_until.astimezone(UTC).isoformat(),
            "catalogue_sha256": checksum.hexdigest(),
        }
        if snapshot.comment is not None:
            metadata["comment"] = snapshot.comment
        write_json(path, lock, metadata)

    def _read_snapshot_metadata(self, repository: Repository, backup_set_id: UUID, snapshot_id: UUID, lock: RepositoryLock) -> _SnapshotMetadata:
        path = lock.require_held_for(repository.snapshot_file(backup_set_id, snapshot_id))
        with path.open("r", encoding="utf-8") as source:
            metadata: object = json.load(source)
        if not is_json_object(metadata):
            raise ValueError("Invalid snapshot metadata")
        version = metadata.get("version")
        if type(version) is not int or version != SNAPSHOT_VERSION:
            raise ValueError("Unsupported snapshot format")
        if (metadata.get("snapshot_id"), metadata.get("client_id"), metadata.get("backup_set_id")) != (str(snapshot_id), str(repository.client_id), str(backup_set_id)):
            raise ValueError("Snapshot identity or scope does not match the requested record")

        stored_root, stored_checksum = metadata.get("root_id"), metadata.get("catalogue_sha256")
        committed, protected = metadata.get("committed_at"), metadata.get("protected_until")
        comment = metadata.get("comment")
        if not isinstance(stored_root, str) or not isinstance(stored_checksum, str) or not isinstance(committed, str) or not isinstance(protected, str):
            raise ValueError("Invalid snapshot metadata")
        if comment is not None and not isinstance(comment, str):
            raise ValueError("Invalid snapshot comment")
        self._validate_comment(comment)
        root_id = bytes.fromhex(stored_root)
        Object.validate_object_id(root_id)
        checksum = bytes.fromhex(stored_checksum)
        if len(checksum) != sha256().digest_size:
            raise ValueError("Invalid snapshot catalogue checksum")
        committed_at = datetime.fromisoformat(committed)
        protected_until = datetime.fromisoformat(protected)
        if committed_at.utcoffset() is None or protected_until.utcoffset() is None:
            raise ValueError("Snapshot timestamps must include a time zone")
        if protected_until < committed_at:
            raise ValueError("Snapshot protection deadline precedes commit time")
        return _SnapshotMetadata(root_id, committed_at.astimezone(UTC), protected_until.astimezone(UTC), checksum, comment)

    def _read_catalogue(self, path: Path, lock: RepositoryLock, metadata: _SnapshotMetadata) -> ObjectCatalogue:
        checksum = sha256()
        object_ids: set[ObjectId] = set()
        with lock.require_held_for(path.with_suffix(".catalog")).open("rb") as source:
            while object_id := source.read(OBJECT_ID_SIZE):
                if len(object_id) != OBJECT_ID_SIZE or object_id in object_ids:
                    raise ValueError("Invalid snapshot object catalogue")
                checksum.update(object_id)
                object_ids.add(object_id)
        if metadata.root_id not in object_ids or checksum.digest() != metadata.catalogue_checksum:
            raise ValueError("Invalid snapshot object catalogue")
        return ObjectCatalogue(frozenset(object_ids))

    def _load_snapshot(self, repository: Repository, backup_set_id: UUID, snapshot_id: UUID, lock: RepositoryLock) -> Snapshot:
        """Read a published snapshot and verify its catalogue."""
        metadata = self._read_snapshot_metadata(repository, backup_set_id, snapshot_id, lock)
        catalogue = self._read_catalogue(repository.snapshot_file(backup_set_id, snapshot_id), lock, metadata)
        return Snapshot(
            snapshot_id=snapshot_id,
            backup_set_id=backup_set_id,
            root_id=metadata.root_id,
            committed_at=metadata.committed_at,
            protected_until=metadata.protected_until,
            object_catalog=catalogue,
            comment=metadata.comment,
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
