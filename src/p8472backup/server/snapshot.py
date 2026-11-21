"""Authoritative snapshot metadata and its local file format."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from struct import Struct
from tempfile import NamedTemporaryFile
from uuid import UUID

from p8472backup.archive.objects import OBJECT_ID_SIZE, Object, ObjectId
from p8472backup.storage.file.lock import RepositoryLock


_SNAPSHOT_VERSION = 1
_SNAPSHOT_HEADER = Struct(f"<H16s16s16s{OBJECT_ID_SIZE}sqqQ")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


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
    version: int = _SNAPSHOT_VERSION

    def save_to(self, path: Path, lock: RepositoryLock) -> None:
        """Durably publish the snapshot without replacing an existing record."""
        directory = lock.require_held_for(path.parent)
        path = directory / path.name
        if self.version != _SNAPSHOT_VERSION:
            raise ValueError("Unsupported snapshot format")
        if self.committed_at.utcoffset() is None or self.protected_until.utcoffset() is None:
            raise ValueError("Snapshot timestamps must include a time zone")
        if self.protected_until < self.committed_at:
            raise ValueError("Snapshot protection deadline precedes commit time")
        object_ids = self.object_catalog.object_ids
        if self.root_id not in object_ids:
            raise ValueError("Snapshot root is missing from the object catalogue")

        header = _SNAPSHOT_HEADER.pack(
            self.version,
            self.snapshot_id.bytes,
            self.client_id.bytes,
            self.backup_set_id.bytes,
            self.root_id,
            (self.committed_at - _EPOCH) // _MICROSECOND,
            (self.protected_until - _EPOCH) // _MICROSECOND,
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

    @classmethod
    def load_from(cls, path: Path, lock: RepositoryLock) -> Snapshot:
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

        return cls(
            snapshot_id=UUID(bytes=snapshot_id),
            client_id=UUID(bytes=client_id),
            backup_set_id=UUID(bytes=backup_set_id),
            root_id=root_id,
            committed_at=_EPOCH + timedelta(microseconds=committed_at),
            protected_until=_EPOCH + timedelta(microseconds=protected_until),
            object_catalog=ObjectCatalogue(object_ids),
            version=version,
        )
