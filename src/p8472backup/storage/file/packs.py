"""Represent packs and their common headers."""

from __future__ import annotations

from dataclasses import dataclass
from struct import Struct

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, MAX_OBJECT_BODY_SIZE


_HEADER = Struct("<HIQ")

HEADER_SIZE = _HEADER.size
FORMAT_VERSION = 1


@dataclass(frozen=True)
class Pack:
    """Immutable pack header state; record I/O is not implemented yet."""

    object_count: int
    body_length: int
    version: int = FORMAT_VERSION

    def __post_init__(self) -> None:
        """Reject unsupported formats and impossible record counts or sizes."""
        if self.version != FORMAT_VERSION:
            raise ValueError(f"Unsupported pack format version: {self.version}")
        if not 1 <= self.object_count <= 2**32 - 1:
            raise ValueError("Pack object count must be between 1 and 2^32 - 1")
        if not 0 <= self.body_length <= 2**64 - 1:
            raise ValueError("Pack body length must fit an unsigned 64-bit integer")
        if self.body_length < self.object_count * OBJECT_HEADER_SIZE:
            raise ValueError("Pack body is too short for its declared object count")
        if self.body_length > self.object_count * (OBJECT_HEADER_SIZE + MAX_OBJECT_BODY_SIZE):
            raise ValueError("Pack body exceeds the maximum size of its declared objects")

    def to_header(self) -> bytes:
        """Serialize the validated header state as 14 bytes."""
        return _HEADER.pack(self.version, self.object_count, self.body_length)

    @classmethod
    def from_header(cls, header: bytes) -> Pack:
        """Construct a pack from exactly 14 bytes without reading its records."""
        if len(header) != HEADER_SIZE:
            raise ValueError(f"Pack header must contain exactly {HEADER_SIZE} bytes")

        version: int
        object_count: int
        body_length: int
        version, object_count, body_length = _HEADER.unpack(header)
        return cls(object_count=object_count, body_length=body_length, version=version)
