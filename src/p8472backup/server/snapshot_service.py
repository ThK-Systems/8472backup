"""Create and retrieve snapshots for an authorized backup set."""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from itertools import chain, count
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO
from uuid import UUID, uuid4

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, OBJECT_ID_SIZE, TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository import BackupSet, Repository
from p8472backup.server.retention.policy import parse_prune_rule, retained_snapshots
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


@dataclass(frozen=True)
class _SnapshotRegistry:
    snapshots: dict[UUID, UUID]
    pending_deletions: dict[UUID, UUID]


class SnapshotService:
    """Use one repository, backup set, and key domain for snapshot operations."""

    def validate_snapshot_input(self, root_id: ObjectId, comment: str | None) -> None:
        """Validate snapshot arguments available before reading the object stream."""
        self._validate_comment(comment)
        Object.validate_object_id(root_id)

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
        self.validate_snapshot_input(root_id, comment)

        backup_set = repository.get_active_backup_set(backup_set_id)
        if snapshot_id is None:
            snapshot_id = uuid4()

        path = repository.snapshot_file(backup_set_id, snapshot_id)
        directory = lock.require_held_for(path.parent)
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        path = directory / path.name
        registry = self._read_registry(repository, lock)
        if snapshot_id in registry.snapshots or snapshot_id in registry.pending_deletions or os.path.lexists(path) or os.path.lexists(path.with_suffix(".catalog")):
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
            _catalogue=catalogue,
            comment=comment,
        )
        self._save_snapshot(snapshot, repository, path, lock)
        registry.snapshots[snapshot_id] = backup_set_id
        self._write_registry(repository, lock, registry)
        return snapshot

    def get_snapshot(self, repository: Repository, backup_set_id: UUID, snapshot_id: UUID) -> Snapshot:
        """Load a committed snapshot in this service's client and backup set."""
        repository.get_backup_set(backup_set_id)
        with RepositoryLock(repository.path) as lock:
            if self._read_registry(repository, lock).snapshots.get(snapshot_id) != backup_set_id:
                raise FileNotFoundError(f"Snapshot is not published in this set: {snapshot_id}")
            return self._load_snapshot(repository, backup_set_id, snapshot_id, lock)

    def list_snapshots(self, repository: Repository, lock: RepositoryLock) -> frozenset[Snapshot]:
        """Load published metadata and defer catalogue reads under the given lock."""
        registry = self._read_registry(repository, lock)
        return frozenset(self._load_snapshot(repository, set_id, snapshot_id, lock, defer_catalogue=True) for snapshot_id, set_id in registry.snapshots.items())

    def list_snapshot_entries(self, repository: Repository, lock: RepositoryLock, *, backup_set_id: UUID | None, protection_duration: timedelta) -> tuple[BackupSetListing, ...]:
        """Load the published metadata needed for the listing."""
        sets = tuple(repository.backup_sets.values()) if backup_set_id is None else (repository.get_backup_set(backup_set_id),)
        rows_by_set: dict[UUID, list[SnapshotListEntry]] = {backup_set.backup_set_id: [] for backup_set in sets}
        registry = self._read_registry(repository, lock)
        now = trusted_time.server_clock.now()
        for snapshot_id, set_id in registry.snapshots.items():
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

    def check(self, repository: Repository, lock: RepositoryLock) -> None:
        """Verify published snapshots and indexed packs without modifying repository data."""
        try:
            registry = self._read_registry(repository, lock)
        except (OSError, ValueError, InconsistentStateError) as error:
            raise InconsistentStateError(self._check_file_error(repository, repository.path / SNAPSHOT_REGISTRY_NAME, error)) from error

        domains: dict[UUID, list[tuple[UUID, UUID]]] = {backup_set.key_id: [] for backup_set in repository.backup_sets.values()}
        for snapshot_id, set_id in registry.snapshots.items():
            domains[repository.get_backup_set(set_id).key_id].append((set_id, snapshot_id))

        findings: list[str] = []
        for key_id, snapshots in domains.items():
            self._check_key_domain(repository, lock, key_id, snapshots, findings)
        if findings:
            raise InconsistentStateError("\n".join(findings))

    def _check_key_domain(self, repository: Repository, lock: RepositoryLock, key_id: UUID, snapshots: list[tuple[UUID, UUID]], findings: list[str]) -> None:
        index_path = repository.index_directory / str(key_id)
        try:
            index: LocationIndex | None = LocationIndex.load_from(index_path, lock)
        except (OSError, ValueError) as error:
            findings.append(self._check_file_error(repository, index_path, error))
            index = None

        roots: set[ObjectId] = set()
        for set_id, snapshot_id in snapshots:
            path = repository.snapshot_file(set_id, snapshot_id)
            try:
                metadata = self._read_snapshot_metadata(repository, set_id, snapshot_id, lock)
            except (OSError, ValueError, OverflowError) as error:
                findings.append(self._check_file_error(repository, path, error))
                continue
            roots.add(metadata.root_id)
            try:
                self._check_catalogue(path, lock, metadata, index)
            except (OSError, ValueError) as error:
                findings.append(self._check_file_error(repository, path.with_suffix(".catalog"), error))

        if index is None:
            return
        for pack_id, unmatched in index.pack_entry_counts().items():
            path = repository.packs_root / str(key_id) / f"{pack_id}.pack"
            try:
                for offset, obj in Pack.iter_object_headers(lock.require_held_for(path)):
                    if index.get(obj.object_id) != ObjectLocation(pack_id, offset, OBJECT_HEADER_SIZE + obj.length):
                        continue
                    unmatched -= 1
                    if obj.object_type != TYPE_SNAPSHOT and obj.object_id in roots:
                        findings.append(f"{path.relative_to(repository.path)}: snapshot root {obj.object_id.hex()} has unexpected object type {obj.object_type}")
            except (OSError, ValueError) as error:
                findings.append(self._check_file_error(repository, path, error))
                continue
            if unmatched:
                findings.append(f"{path.relative_to(repository.path)}: indexed locations missing from pack headers: {unmatched}")

    def _check_file_error(self, repository: Repository, path: Path, error: Exception) -> str:
        detail = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
        return f"{path.relative_to(repository.path)}: {detail}"

    def _check_catalogue(self, path: Path, lock: RepositoryLock, metadata: _SnapshotMetadata, index: LocationIndex | None) -> None:
        seen = bytearray((len(index) + 7) // 8) if index is not None else bytearray()
        missing = duplicates = 0
        for object_id in self._iter_catalogue_ids(path, lock, metadata):
            if index is None:
                continue
            number = index.record_number(object_id)
            if number is None:
                missing += 1
                continue
            byte, bit = divmod(number, 8)
            mask = 1 << bit
            if seen[byte] & mask:
                duplicates += 1
            seen[byte] |= mask
        if missing or duplicates:
            raise ValueError(f"Invalid snapshot object catalogue (missing index references: {missing}, duplicate entries: {duplicates})")

    def prune(self, repository: Repository, backup_set: BackupSet, lock: RepositoryLock, *, prune_rule: str, protection_duration: timedelta) -> tuple[UUID, ...]:
        """Apply the calendar policy to the published snapshots of one active set."""
        backup_set_id = backup_set.backup_set_id
        if repository.get_active_backup_set(backup_set_id) != backup_set:
            raise ValueError(f"Backup set does not match the repository: {backup_set_id}")

        counts = parse_prune_rule(prune_rule)
        registry = self._read_registry(repository, lock)
        snapshots = self._read_set_snapshot_metadata(repository, backup_set_id, lock, registry)
        keep = retained_snapshots({snapshot_id: metadata.committed_at for snapshot_id, metadata in snapshots.items()}, counts)
        candidates = {snapshot_id: metadata for snapshot_id, metadata in snapshots.items() if snapshot_id not in keep}
        return self._delete_unprotected_snapshots(repository, backup_set_id, lock, registry, candidates, protection_duration=protection_duration)

    def remove_unprotected_snapshots(self, repository: Repository, backup_set: BackupSet, lock: RepositoryLock, *, protection_duration: timedelta) -> None:
        """Remove expired snapshot records from a deleted backup set."""
        backup_set_id = backup_set.backup_set_id
        if not backup_set.deleted or repository.backup_sets.get(backup_set_id) != backup_set:
            raise ValueError(f"Backup set is not deleted in this repository: {backup_set_id}")
        registry = self._read_registry(repository, lock)
        snapshots = self._read_set_snapshot_metadata(repository, backup_set_id, lock, registry)
        for snapshot_id, metadata in snapshots.items():
            self._read_catalogue(repository.snapshot_file(backup_set_id, snapshot_id), lock, metadata)
        self._delete_unprotected_snapshots(repository, backup_set_id, lock, registry, snapshots, protection_duration=protection_duration)

    def _read_set_snapshot_metadata(self, repository: Repository, backup_set_id: UUID, lock: RepositoryLock, registry: _SnapshotRegistry) -> dict[UUID, _SnapshotMetadata]:
        return {snapshot_id: self._read_snapshot_metadata(repository, set_id, snapshot_id, lock) for snapshot_id, set_id in registry.snapshots.items() if set_id == backup_set_id}

    def _delete_unprotected_snapshots(
        self,
        repository: Repository,
        backup_set_id: UUID,
        lock: RepositoryLock,
        registry: _SnapshotRegistry,
        candidates: dict[UUID, _SnapshotMetadata],
        *,
        protection_duration: timedelta,
    ) -> tuple[UUID, ...]:
        directory = lock.require_held_for(repository.snapshots_root / str(backup_set_id))
        now = trusted_time.server_clock.now()
        expired = [snapshot_id for snapshot_id, metadata in candidates.items() if not self._is_protected(metadata, now, protection_duration)]
        for snapshot_id in expired:
            registry.pending_deletions[snapshot_id] = registry.snapshots.pop(snapshot_id)

        pending = tuple(snapshot_id for snapshot_id, set_id in registry.pending_deletions.items() if set_id == backup_set_id)
        if not pending:
            return ()
        if expired:
            self._write_registry(repository, lock, registry)
        else:
            # An earlier attempt may have replaced the register before syncing its directory.
            sync_directory(repository.path)

        for snapshot_id in pending:
            path = directory / repository.snapshot_file(backup_set_id, snapshot_id).name
            path.unlink(missing_ok=True)
            path.with_suffix(".catalog").unlink(missing_ok=True)
        sync_directory(directory)
        for snapshot_id in pending:
            del registry.pending_deletions[snapshot_id]
        self._write_registry(repository, lock, registry)
        return pending

    def _read_registry(self, repository: Repository, lock: RepositoryLock) -> _SnapshotRegistry:
        path = lock.require_held_for(repository.path / SNAPSHOT_REGISTRY_NAME)
        try:
            with path.open("r", encoding="utf-8") as source:
                registry: object = json.load(source)
        except FileNotFoundError as error:
            raise InconsistentStateError("Snapshot register is missing") from error
        if not is_json_object(registry):
            raise ValueError("Invalid snapshot register")
        version = registry.get("version")
        if type(version) is not int or version != SNAPSHOT_REGISTRY_VERSION:
            raise ValueError("Invalid snapshot register")
        snapshots = self._read_registry_entries(repository, registry.get("snapshots"))
        pending_deletions = self._read_registry_entries(repository, registry.get("pending_deletions", {}))
        if snapshots.keys() & pending_deletions.keys():
            raise ValueError("Snapshot register contains published entries pending deletion")
        if not snapshots:
            for set_id in repository.backup_sets:
                directory = lock.require_held_for(repository.snapshots_root / str(set_id))
                if any(entry.suffix == ".snapshot" for entry in directory.iterdir()):
                    raise InconsistentStateError("Repository contains development snapshot records that require conversion")
        return _SnapshotRegistry(snapshots, pending_deletions)

    def _read_registry_entries(self, repository: Repository, entries: object) -> dict[UUID, UUID]:
        if not is_json_object(entries):
            raise ValueError("Invalid snapshot register")
        snapshots: dict[UUID, UUID] = {}
        for stored_id, stored_set in entries.items():
            if not isinstance(stored_id, str) or not isinstance(stored_set, str):
                raise ValueError("Invalid snapshot register entry")
            snapshot_id, set_id = UUID(stored_id), UUID(stored_set)
            if snapshot_id in snapshots or set_id not in repository.backup_sets:
                raise ValueError("Invalid snapshot register entry")
            snapshots[snapshot_id] = set_id
        return snapshots

    def _write_registry(self, repository: Repository, lock: RepositoryLock, registry: _SnapshotRegistry) -> None:
        data: dict[str, object] = {"version": SNAPSHOT_REGISTRY_VERSION}
        for key, entries in (("snapshots", registry.snapshots), ("pending_deletions", registry.pending_deletions)):
            if entries or key == "snapshots":
                data[key] = {str(snapshot_id): str(set_id) for snapshot_id, set_id in entries.items()}
        write_json(repository.path / SNAPSHOT_REGISTRY_NAME, lock, data)

    def _is_protected(self, metadata: _SnapshotMetadata, now: datetime, protection_duration: timedelta) -> bool:
        return now <= max(metadata.protected_until, metadata.committed_at + protection_duration)

    def _validate_comment(self, comment: object) -> str | None:
        if comment is None:
            return None
        if not isinstance(comment, str):
            raise ValueError("Invalid snapshot comment")
        if len(comment) > 40:
            raise ValueError("Snapshot comment must be at most 40 characters")
        return comment

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
        if not isinstance(stored_root, str) or not isinstance(stored_checksum, str) or not isinstance(committed, str) or not isinstance(protected, str):
            raise ValueError("Invalid snapshot metadata")
        comment = self._validate_comment(metadata.get("comment"))
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

    def _iter_catalogue_ids(self, path: Path, lock: RepositoryLock, metadata: _SnapshotMetadata) -> Iterator[ObjectId]:
        checksum = sha256()
        root_found = False
        with lock.require_held_for(path.with_suffix(".catalog")).open("rb") as source:
            while object_id := source.read(OBJECT_ID_SIZE):
                if len(object_id) != OBJECT_ID_SIZE:
                    raise ValueError("Invalid snapshot object catalogue: truncated object ID")
                checksum.update(object_id)
                root_found |= object_id == metadata.root_id
                yield object_id
        if checksum.digest() != metadata.catalogue_checksum:
            raise ValueError("Invalid snapshot object catalogue: checksum mismatch")
        if not root_found:
            raise ValueError("Invalid snapshot object catalogue: snapshot root is missing")

    def _read_catalogue(self, path: Path, lock: RepositoryLock, metadata: _SnapshotMetadata) -> ObjectCatalogue:
        object_ids: set[ObjectId] = set()
        for object_id in self._iter_catalogue_ids(path, lock, metadata):
            if object_id in object_ids:
                raise ValueError("Invalid snapshot object catalogue: duplicate object ID")
            object_ids.add(object_id)
        return ObjectCatalogue(frozenset(object_ids))

    def _load_snapshot(self, repository: Repository, backup_set_id: UUID, snapshot_id: UUID, lock: RepositoryLock, *, defer_catalogue: bool = False) -> Snapshot:
        """Read a published snapshot and load or defer its catalogue."""
        metadata = self._read_snapshot_metadata(repository, backup_set_id, snapshot_id, lock)
        load_catalogue = partial(self._read_catalogue, repository.snapshot_file(backup_set_id, snapshot_id), lock, metadata)
        return Snapshot(
            snapshot_id=snapshot_id,
            backup_set_id=backup_set_id,
            root_id=metadata.root_id,
            committed_at=metadata.committed_at,
            protected_until=metadata.protected_until,
            _catalogue=load_catalogue if defer_catalogue else load_catalogue(),
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
