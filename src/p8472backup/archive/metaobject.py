"""Represent the plaintext content of archive metadata objects."""

from __future__ import annotations

from dataclasses import dataclass
from struct import Struct

from p8472backup.archive.objects import MAX_OBJECT_BODY_SIZE, OBJECT_ID_SIZE, Object, ObjectId


_META_SUBHEADER = Struct("<Q")


@dataclass(frozen=True)
class MetaObject:
    """Client-side plaintext for one filesystem entry."""

    metadata: bytes
    chunk_ids: tuple[ObjectId, ...] = ()

    def __post_init__(self) -> None:
        if _META_SUBHEADER.size + len(self.metadata) + len(self.chunk_ids) * OBJECT_ID_SIZE > MAX_OBJECT_BODY_SIZE:
            raise ValueError("Metadata object exceeds the stored body size limit")
        for chunk_id in self.chunk_ids:
            Object.validate_object_id(chunk_id)

    def to_plaintext(self) -> bytes:
        """Encode the subheader, metadata block, and ordered chunk references."""
        return b"".join((_META_SUBHEADER.pack(len(self.metadata)), self.metadata, *self.chunk_ids))

    @classmethod
    def from_plaintext(cls, plaintext: bytes) -> MetaObject:
        """Decode authenticated metadata plaintext supplied by the client."""
        if not _META_SUBHEADER.size <= len(plaintext) <= MAX_OBJECT_BODY_SIZE:
            raise ValueError(f"Invalid metadata object plaintext length: {len(plaintext)}")
        metadata_length: int
        (metadata_length,) = _META_SUBHEADER.unpack_from(plaintext)
        chunk_ids_offset = _META_SUBHEADER.size + metadata_length
        if chunk_ids_offset > len(plaintext):
            raise ValueError("Metadata block exceeds the plaintext length")
        if (len(plaintext) - chunk_ids_offset) % OBJECT_ID_SIZE:
            raise ValueError("Incomplete chunk ID in metadata object")

        metadata = plaintext[_META_SUBHEADER.size : chunk_ids_offset]
        chunk_ids = tuple(plaintext[offset : offset + OBJECT_ID_SIZE] for offset in range(chunk_ids_offset, len(plaintext), OBJECT_ID_SIZE))
        return cls(metadata, chunk_ids)
