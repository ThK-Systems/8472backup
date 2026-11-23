"""Represent the plaintext content of archive snapshot roots."""

from __future__ import annotations

from dataclasses import dataclass
from struct import Struct

from p8472backup.archive.objects import MAX_OBJECT_BODY_SIZE, OBJECT_ID_SIZE, Object, ObjectId


_ROOT_SUBHEADER = Struct("<Q")


@dataclass(frozen=True)
class SnapshotRootContent:
    """Client-side plaintext for a type 3 archive object."""

    description: bytes
    metadata_ids: tuple[ObjectId, ...] = ()

    def __post_init__(self) -> None:
        if _ROOT_SUBHEADER.size + len(self.description) + len(self.metadata_ids) * OBJECT_ID_SIZE > MAX_OBJECT_BODY_SIZE:
            raise ValueError("Snapshot root exceeds the stored body size limit")
        for metadata_id in self.metadata_ids:
            Object.validate_object_id(metadata_id)

    def to_plaintext(self) -> bytes:
        """Encode the subheader, snapshot description, and metadata references."""
        return b"".join((_ROOT_SUBHEADER.pack(len(self.description)), self.description, *self.metadata_ids))

    @classmethod
    def from_plaintext(cls, plaintext: bytes) -> SnapshotRootContent:
        """Decode authenticated snapshot root plaintext supplied by the client."""
        if not _ROOT_SUBHEADER.size <= len(plaintext) <= MAX_OBJECT_BODY_SIZE:
            raise ValueError(f"Invalid snapshot root plaintext length: {len(plaintext)}")
        description_length: int
        (description_length,) = _ROOT_SUBHEADER.unpack_from(plaintext)
        metadata_ids_offset = _ROOT_SUBHEADER.size + description_length
        if metadata_ids_offset > len(plaintext):
            raise ValueError("Snapshot description exceeds the plaintext length")
        if (len(plaintext) - metadata_ids_offset) % OBJECT_ID_SIZE:
            raise ValueError("Incomplete metadata ID in snapshot root")

        description = plaintext[_ROOT_SUBHEADER.size : metadata_ids_offset]
        metadata_ids = tuple(plaintext[offset : offset + OBJECT_ID_SIZE] for offset in range(metadata_ids_offset, len(plaintext), OBJECT_ID_SIZE))
        return cls(description, metadata_ids)
