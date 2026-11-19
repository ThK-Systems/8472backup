"""Represent archive objects and their common headers."""

from __future__ import annotations

from dataclasses import dataclass
from struct import Struct


type ObjectId = bytes

OBJECT_ID_SIZE = 32

_HEADER = Struct(f"<HH{OBJECT_ID_SIZE}sQ")

HEADER_SIZE = _HEADER.size
FORMAT_VERSION = 1
MAX_OBJECT_BODY_SIZE = 16 * 1024**3

TYPE_DATA = 1
TYPE_METADATA = 2
TYPE_SNAPSHOT = 3


@dataclass(frozen=True)
class Object:
    """Immutable object header state; payload handling is not implemented yet."""

    object_type: int
    object_id: ObjectId
    length: int
    version: int = FORMAT_VERSION

    def __post_init__(self) -> None:
        """Validate the header state when constructing an object."""
        if self.version != FORMAT_VERSION:
            raise ValueError(f"Unsupported object format version: {self.version}")
        if self.object_type not in (TYPE_DATA, TYPE_METADATA, TYPE_SNAPSHOT):
            raise ValueError(f"Unsupported object type: {self.object_type}")
        if type(self.object_id) is not bytes:
            raise TypeError("Object ID must be bytes")
        if len(self.object_id) != OBJECT_ID_SIZE:
            raise ValueError(f"Object ID must contain exactly {OBJECT_ID_SIZE} bytes")
        if not 0 <= self.length <= MAX_OBJECT_BODY_SIZE:
            raise ValueError("Object body length must be between 0 and 16 GiB")

    def to_header(self) -> bytes:
        """Serialize the object's validated state as a 44-byte header."""
        return _HEADER.pack(self.object_type, self.version, self.object_id, self.length)

    @classmethod
    def from_header(cls, header: bytes) -> Object:
        """Construct an object from exactly 44 bytes, without reading its body."""
        if len(header) != HEADER_SIZE:
            raise ValueError(f"Object header must contain exactly {HEADER_SIZE} bytes")

        object_type: int
        version: int
        object_id: ObjectId
        length: int
        object_type, version, object_id, length = _HEADER.unpack(header)
        return cls(object_type=object_type, object_id=object_id, length=length, version=version)
