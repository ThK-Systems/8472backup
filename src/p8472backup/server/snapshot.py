"""Server-readable snapshot data."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from p8472backup.archive.objects import ObjectId


SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class ObjectCatalogue:
    """The immutable set of object IDs required by a snapshot."""

    object_ids: frozenset[ObjectId]


@dataclass(frozen=True)
class Snapshot:
    """Server-readable metadata and membership of a committed snapshot."""

    snapshot_id: UUID
    backup_set_id: UUID
    root_id: ObjectId
    committed_at: datetime
    protected_until: datetime
    object_catalog: ObjectCatalogue
    version: int = SNAPSHOT_VERSION
