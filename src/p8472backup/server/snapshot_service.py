"""Create and retrieve snapshots for an authorized backup set."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain, count
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from p8472backup.archive.objects import TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.server.repository import Repository
from p8472backup.server.snapshot import ObjectCatalogue, Snapshot
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import DEFAULT_TARGET_SIZE, Pack


class SnapshotService:
    """Use one repository, backup set, and key domain for snapshot operations."""

    def __init__(
        self,
        repository: Repository,
        *,
        protection_duration: timedelta = timedelta(days=7),
        target_size: int = DEFAULT_TARGET_SIZE,
    ) -> None:
        if protection_duration < timedelta():
            raise ValueError("Snapshot protection duration must be nonnegative")
        if target_size <= 0:
            raise ValueError("Pack target size must be positive")

        repository.path.resolve(strict=True)
        self._repository = repository
        self._protection_duration = protection_duration
        self._target_size = target_size

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

        repository = self._repository
        with RepositoryLock(repository.path) as lock:
            directory = lock.require_held_for(repository.snapshots_directory)
            if not directory.is_dir():
                raise NotADirectoryError(directory)
            path = repository.snapshot_file(snapshot_id)
            if os.path.lexists(path):
                raise FileExistsError(f"Snapshot already exists: {path}")

            index = LocationIndex.load_from(repository.index_file, lock)
            first = next(records, None)
            if first is None or first[0].object_type != TYPE_SNAPSHOT or first[0].object_id != root_id:
                raise ValueError("The first record must be the declared type 3 snapshot root")

            catalogue = self._store_snapshot_objects(chain((first,), records), index, lock)
            index.save_to(repository.index_file, lock)

            committed_at = datetime.now(UTC)
            snapshot = Snapshot(
                snapshot_id=snapshot_id,
                root_id=root_id,
                committed_at=committed_at,
                protected_until=committed_at + self._protection_duration,
                object_catalog=catalogue,
            )
            snapshot.save_to(repository, lock)
            return snapshot

    def get_snapshot(self, snapshot_id: UUID) -> Snapshot:
        """Load a committed snapshot in this service's client and backup set."""
        with RepositoryLock(self._repository.path) as lock:
            return Snapshot.load_from(self._repository, snapshot_id, lock)

    def _store_snapshot_objects(
        self,
        records: Iterator[tuple[Object, BinaryIO]],
        index: LocationIndex,
        lock: RepositoryLock,
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
        pack_directory = lock.require_held_for(self._repository.packs_directory)
        destinations = self._pack_destinations(pack_directory)
        while (first := next(missing, None)) is not None:
            pack_id, path = next(destinations)
            if pack_id >= 2**64:
                raise ValueError(f"Pack ID exceeds the supported range: {pack_id}")
            path = lock.require_held_for(path)

            def record_location(object_id: ObjectId, offset: int, length: int) -> None:
                index.put(object_id, ObjectLocation(pack_id, offset, length))

            Pack.write_to(path, chain((first,), missing), self._target_size, on_stored=record_location)

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
