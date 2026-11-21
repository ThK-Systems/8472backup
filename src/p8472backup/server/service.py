"""Create and retrieve snapshots in an authorized repository scope."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain, count
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from p8472backup.archive.objects import TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.server.snapshot import Snapshot
from p8472backup.server.snapshots import store_snapshot_objects
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import DEFAULT_TARGET_SIZE


class SnapshotService:
    """Use one repository, backup set, and key domain for snapshot operations."""

    def __init__(
        self,
        repository: Path,
        *,
        client_id: UUID,
        backup_set_id: UUID,
        key_domain_id: UUID,
        protection_duration: timedelta = timedelta(days=7),
        target_size: int = DEFAULT_TARGET_SIZE,
    ) -> None:
        if protection_duration < timedelta():
            raise ValueError("Snapshot protection duration must be nonnegative")
        if target_size <= 0:
            raise ValueError("Pack target size must be positive")

        self._repository = repository.resolve(strict=True)
        self._client_id = client_id
        self._backup_set_id = backup_set_id
        self._protection_duration = protection_duration
        self._target_size = target_size
        self._index_path = self._repository / "index" / str(key_domain_id)
        self._pack_directory = self._repository / "packs" / str(key_domain_id)
        self._snapshot_directory = self._repository / "snapshots" / str(backup_set_id)

    def create_snapshot(
        self,
        records: Iterator[tuple[Object, BinaryIO]],
        root_id: ObjectId,
        *,
        snapshot_id: UUID | None = None,
    ) -> Snapshot:
        """Write a snapshot and return it once its record is durable."""
        Object.validate_object_id(root_id)
        if snapshot_id is None:
            snapshot_id = uuid4()

        with RepositoryLock(self._repository) as lock:
            directory = lock.require_held_for(self._snapshot_directory)
            if not directory.is_dir():
                raise NotADirectoryError(directory)
            path = directory / f"{snapshot_id}.snapshot"
            if os.path.lexists(path):
                raise FileExistsError(f"Snapshot already exists: {path}")

            index = LocationIndex.load_from(self._index_path, lock)
            first = next(records, None)
            if first is None or first[0].object_type != TYPE_SNAPSHOT or first[0].object_id != root_id:
                raise ValueError("The first record must be the declared type 3 snapshot root")

            pack_directory = lock.require_held_for(self._pack_directory)
            catalogue = store_snapshot_objects(
                chain((first,), records),
                index,
                lock,
                self._pack_destinations(pack_directory),
                self._target_size,
            )
            index.save_to(self._index_path, lock)

            committed_at = datetime.now(UTC)
            snapshot = Snapshot(
                snapshot_id=snapshot_id,
                client_id=self._client_id,
                backup_set_id=self._backup_set_id,
                root_id=root_id,
                committed_at=committed_at,
                protected_until=committed_at + self._protection_duration,
                object_catalog=catalogue,
            )
            snapshot.save_to(path, lock)
            return snapshot

    def get_snapshot(self, snapshot_id: UUID) -> Snapshot:
        """Load a committed snapshot in this service's client and backup set."""
        with RepositoryLock(self._repository) as lock:
            snapshot = Snapshot.load_from(self._snapshot_directory / f"{snapshot_id}.snapshot", lock)
            if (snapshot.snapshot_id, snapshot.client_id, snapshot.backup_set_id) != (snapshot_id, self._client_id, self._backup_set_id):
                raise ValueError("Snapshot identity or scope does not match the requested record")
            return snapshot

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
