"""Server-readable snapshot data."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
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
    _catalogue: ObjectCatalogue | Callable[[], ObjectCatalogue] = field(compare=False, repr=False)
    version: int = SNAPSHOT_VERSION
    comment: str | None = None

    @cached_property
    def object_catalog(self) -> ObjectCatalogue:
        """Return the catalogue, using the original repository lock for a deferred read."""
        if isinstance(self._catalogue, ObjectCatalogue):
            return self._catalogue
        return self._catalogue()
