"""Snapshot metadata, object membership, and deduplication."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from p8472backup.archive.objects import Object, ObjectId
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import DEFAULT_TARGET_SIZE, Pack


@dataclass(frozen=True)
class ObjectCatalogue:
    """The immutable set of object IDs required by a snapshot."""

    object_ids: frozenset[ObjectId]


@dataclass(frozen=True)
class Snapshot:
    """Server-readable metadata and membership of a committed snapshot."""

    snapshot_id: UUID
    client_id: UUID
    backup_set_id: UUID
    root_id: ObjectId
    committed_at: datetime
    protected_until: datetime
    object_catalog: ObjectCatalogue
    version: int = 1


def iter_missing_objects(
    records: Iterator[tuple[Object, BinaryIO]],
    index: LocationIndex,
    object_ids: set[ObjectId],
) -> Iterator[tuple[Object, BinaryIO]]:
    """Collect snapshot membership and yield each missing object once.

    Start with an empty set for each snapshot. After exhausting this iterator,
    the caller can freeze the set for ObjectCatalogue. The input iterator must
    allow advancing past an unread body.
    """
    for obj, body in records:
        if obj.object_id in object_ids:
            continue
        object_ids.add(obj.object_id)
        if index.get(obj.object_id) is None:
            yield obj, body


def store_snapshot_objects(
    records: Iterator[tuple[Object, BinaryIO]],
    index: LocationIndex,
    lock: RepositoryLock,
    pack_destinations: Iterator[tuple[int, Path]],
    target_size: int = DEFAULT_TARGET_SIZE,
) -> ObjectCatalogue:
    """Store missing objects and return the catalogue for snapshot assembly.

    The caller supplies unused pack IDs and paths for the locked repository.
    Successful packs are recorded in the in-memory index as they are finalized.
    """
    lock.require_held()
    if target_size <= 0:
        raise ValueError("Pack target size must be positive")

    object_ids: set[ObjectId] = set()
    missing = iter_missing_objects(records, index, object_ids)
    used_pack_ids: set[int] = set()
    while (first := next(missing, None)) is not None:
        destination = next(pack_destinations, None)
        if destination is None:
            raise ValueError("No destination available for the next pack")
        pack_id, path = destination
        if not 0 <= pack_id < 2**64 or pack_id in used_pack_ids:
            raise ValueError(f"Invalid or repeated pack ID: {pack_id}")

        path = lock.require_held_for(path)

        def record_location(object_id: ObjectId, offset: int, length: int) -> None:
            index.put(object_id, ObjectLocation(pack_id, offset, length))

        Pack.write_to(path, chain((first,), missing), target_size, on_stored=record_location)
        used_pack_ids.add(pack_id)

    return ObjectCatalogue(frozenset(object_ids))
