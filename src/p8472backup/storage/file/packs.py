"""Read and write packs of archive objects."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from struct import Struct
from tempfile import NamedTemporaryFile, SpooledTemporaryFile
from typing import BinaryIO

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, MAX_OBJECT_BODY_SIZE, OBJECT_ID_SIZE, Object, ObjectId


_HEADER = Struct("<HIQ")
_LOCATION_RECORD = Struct(f"<{OBJECT_ID_SIZE}sQQ")

HEADER_SIZE = _HEADER.size
FORMAT_VERSION = 1
DEFAULT_TARGET_SIZE = 50_000_000
_COPY_BUFFER_SIZE = 64 * 1024
_LOCATION_BUFFER_SIZE = 256 * 1024


@dataclass(frozen=True)
class Pack:
    """Immutable pack header and operations on stored object records."""

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

    @classmethod
    def write_to(
        cls,
        path: Path,
        records: Iterator[tuple[Object, BinaryIO]],
        target_size: int = DEFAULT_TARGET_SIZE,
        *,
        on_stored: Callable[[ObjectId, int, int], None] | None = None,
        temporary_prefix: str = ".pack-",
    ) -> Pack:
        """Publish one pack, leaving later records in the iterator for the next pack.

        Each stream supplies exactly the declared body bytes from its current
        position. The caller owns the streams and uses an unused destination path.
        After the pack and directory are synced, on_stored receives each object
        ID, header offset, and total record length in pack order.
        """
        if target_size <= 0:
            raise ValueError("Pack target size must be positive")

        with SpooledTemporaryFile(max_size=_LOCATION_BUFFER_SIZE, dir=str(path.parent)) as locations:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                output = NamedTemporaryFile(dir=path.parent, prefix=temporary_prefix, delete=False)
                temporary_path = Path(output.name)
                try:
                    with output:
                        output.write(bytes(HEADER_SIZE))
                        object_count = 0
                        body_length = 0
                        for obj, body in records:
                            output.write(obj.to_header())
                            remaining = obj.length
                            while remaining:
                                block = body.read(min(remaining, _COPY_BUFFER_SIZE))
                                if not block:
                                    raise ValueError("Truncated object body")
                                output.write(block)
                                remaining -= len(block)

                            length = OBJECT_HEADER_SIZE + obj.length
                            if on_stored is not None:
                                locations.write(_LOCATION_RECORD.pack(obj.object_id, HEADER_SIZE + body_length, length))
                            object_count += 1
                            body_length += length
                            if body_length >= target_size or object_count == 2**32 - 1:
                                break

                        pack = cls(object_count=object_count, body_length=body_length)
                        output.seek(0)
                        output.write(pack.to_header())
                        output.flush()
                        os.fsync(output.fileno())
                    os.link(temporary_path, path)
                finally:
                    temporary_path.unlink()
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

            if on_stored is not None:
                locations.seek(0)
                while entry := locations.read(_LOCATION_RECORD.size):
                    object_id: ObjectId
                    offset: int
                    object_id, offset, length = _LOCATION_RECORD.unpack(entry)
                    on_stored(object_id, offset, length)
        return pack

    @classmethod
    def load_from(cls, path: Path) -> Pack:
        """Load the header of a stored pack and validate the file size."""
        with path.open("rb") as source:
            return cls._load_header(source)

    @classmethod
    def _load_header(cls, source: BinaryIO) -> Pack:
        pack = cls.from_header(source.read(HEADER_SIZE))
        if os.fstat(source.fileno()).st_size != HEADER_SIZE + pack.body_length:
            raise ValueError("Pack file length does not match its header")
        return pack

    @classmethod
    def iter_object_headers(cls, path: Path) -> Iterator[tuple[int, Object]]:
        """Validate a pack's record boundaries as its object headers are consumed."""
        with path.open("rb") as source:
            pack = cls._load_header(source)
            end = HEADER_SIZE + pack.body_length
            offset = HEADER_SIZE
            for _ in range(pack.object_count):
                source.seek(offset)
                obj = Object.from_header(source.read(OBJECT_HEADER_SIZE))
                next_offset = offset + OBJECT_HEADER_SIZE + obj.length
                if next_offset > end:
                    raise ValueError("Object record extends past the pack body")
                yield offset, obj
                offset = next_offset
            if offset != end:
                raise ValueError("Object records do not fill the declared pack body")

    @classmethod
    def read_object(cls, path: Path, offset: int, length: int, output: BinaryIO) -> Object:
        """Read a record at a known location and copy its body to output."""
        with path.open("rb") as source:
            pack = cls._load_header(source)
            pack_end = HEADER_SIZE + pack.body_length
            if offset < HEADER_SIZE or length < OBJECT_HEADER_SIZE or offset + length > pack_end:
                raise ValueError("Object location lies outside the pack body")

            source.seek(offset)
            obj = Object.from_header(source.read(OBJECT_HEADER_SIZE))
            if OBJECT_HEADER_SIZE + obj.length != length:
                raise ValueError("Object record length does not match its location")

            remaining = obj.length
            while remaining:
                block = source.read(min(remaining, _COPY_BUFFER_SIZE))
                if not block:
                    raise ValueError("Truncated object body")
                if output.write(block) != len(block):
                    raise OSError("Short write while copying object body")
                remaining -= len(block)
        return obj
