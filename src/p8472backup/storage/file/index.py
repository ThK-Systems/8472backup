"""Object locations within a client's repository."""

from __future__ import annotations

import os
import struct
from array import array
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import Self

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, OBJECT_ID_SIZE, Object, ObjectId
from p8472backup.server.lock import RepositoryLock
from p8472backup.server.memory import MemoryReservation, reserve_workspace
from p8472backup.storage.file.packs import Pack


_LOCATION_FIELDS = struct.Struct("<QQQ")
_RECORD_SIZE = OBJECT_ID_SIZE + _LOCATION_FIELDS.size
_INITIAL_BUCKET_COUNT = 8_192
_MAX_RECORD_COUNT = 2**32 - 1
_CHECKPOINT_HEADER = struct.Struct("<8sHQ")
_CHECKPOINT_MAGIC = b"8472IDX\x00"
_CHECKPOINT_VERSION = 1
_CHECKSUM_SIZE = sha256().digest_size
_INDEX_OVERHEAD = 1024
_BUCKET_SIZE = 4


@dataclass(frozen=True)
class ObjectLocation:
    """Pack number, header offset, and total record length of an object."""

    pack_id: int
    offset: int
    length: int


class LocationIndex:
    """In-memory index for one repository and key domain.

    Callers synchronize access when sharing an index with a writer.
    """

    def __init__(self) -> None:
        self._memory = MemoryReservation()
        self._memory.resize(_INITIAL_BUCKET_COUNT * _BUCKET_SIZE + _INDEX_OVERHEAD)
        self._buckets: array[int] = array("I", [0]) * _INITIAL_BUCKET_COUNT
        if self._buckets.itemsize != _BUCKET_SIZE:
            raise RuntimeError("Location index requires 32-bit unsigned array elements")
        self._records = bytearray()
        self._count = 0
        self._dirty = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        """Free the index buffers and return their working memory reservation."""
        self._buckets = array("I")
        self._records = bytearray()
        self._count = 0
        self._memory.close()

    def _allocated_bytes(self) -> int:
        return self._records.__alloc__() + len(self._buckets) * _BUCKET_SIZE + _INDEX_OVERHEAD

    def __len__(self) -> int:
        """Return the number of indexed objects."""
        return self._count

    def _find_bucket(self, object_id: ObjectId, buckets: array[int]) -> int:
        """Find the matching bucket or the first free bucket in its probe sequence."""
        # Count the available buckets.
        capacity = len(buckets)
        # Map the first four hash bytes to an existing bucket.
        bucket = int.from_bytes(object_id[:4], "big") % capacity
        # Read one-based record positions until a free bucket (zero) is found.
        while position := buckets[bucket]:
            # Compare the entire ID in the referenced zero-based record.
            if self._records.startswith(object_id, (position - 1) * _RECORD_SIZE):
                # Stop when the bucket points to the requested ID.
                break
            # Probe the next bucket, wrapping around at the end.
            bucket = (bucket + 1) % capacity
        # Return the matching bucket or the first free bucket.
        return bucket

    def _rebuild_buckets(self, capacity: int) -> None:
        """Rebuild the bucket table without moving the packed records."""
        self._memory.resize(self._allocated_bytes() + capacity * _BUCKET_SIZE)
        try:
            buckets: array[int] = array("I", [0]) * capacity
            for record_index in range(self._count):
                offset = record_index * _RECORD_SIZE
                object_id = bytes(self._records[offset : offset + OBJECT_ID_SIZE])
                bucket = self._find_bucket(object_id, buckets)
                if buckets[bucket]:
                    raise ValueError("Duplicate object ID in location index")
                buckets[bucket] = record_index + 1
            self._buckets = buckets
        finally:
            self._memory.resize(self._allocated_bytes())

    def get(self, object_id: ObjectId) -> ObjectLocation | None:
        """Return the location for an object ID, or None if it is absent.

        This method is part of the public location index API.
        """
        # Reject IDs that are not exactly 32 bytes.
        Object.validate_object_id(object_id)
        # Find either the bucket for this ID or the first free bucket.
        position = self._buckets[self._find_bucket(object_id, self._buckets)]
        # A free bucket means the ID was not found.
        if position == 0:
            # Signal that no location exists for the ID.
            return None
        # Convert the one-based position to an offset after the record's ID.
        offset = (position - 1) * _RECORD_SIZE + OBJECT_ID_SIZE
        # Decode the three stored 64-bit location fields.
        pack_id, pack_offset, length = _LOCATION_FIELDS.unpack_from(self._records, offset)
        # Return the decoded location.
        return ObjectLocation(pack_id, pack_offset, length)

    def record_number(self, object_id: ObjectId) -> int | None:
        """Find an object's zero-based record number in this index."""
        Object.validate_object_id(object_id)
        position = self._buckets[self._find_bucket(object_id, self._buckets)]
        return position - 1 if position else None

    def pack_entry_counts(self) -> dict[int, int]:
        """Count indexed records by pack without materializing the locations."""
        counts: dict[int, int] = {}
        for offset in range(OBJECT_ID_SIZE, len(self._records), _RECORD_SIZE):
            pack_id: int
            pack_id, _, _ = _LOCATION_FIELDS.unpack_from(self._records, offset)
            if pack_id not in counts and len(counts) % 256 == 0:
                reserve_workspace("index pack counts", (len(counts) + 256) * 256)
            counts[pack_id] = counts.get(pack_id, 0) + 1
        return counts

    def put(self, object_id: ObjectId, location: ObjectLocation) -> None:
        """Insert a location only if the supplied object ID is absent.

        This method is part of the public location index API.
        """
        # Reject IDs that are not exactly 32 bytes.
        Object.validate_object_id(object_id)
        # Pack and validate the location before changing the index.
        try:
            # Encode the pack ID, offset, and length as unsigned 64-bit values.
            fields = _LOCATION_FIELDS.pack(location.pack_id, location.offset, location.length)
        # Report values that cannot be represented in the packed record.
        except struct.error as error:
            # Keep the original packing error as the cause.
            raise ValueError("Pack ID, offset, and length must fit unsigned 64-bit integers") from error

        # The probe finds either this ID's bucket or a free bucket for a new ID.
        bucket = self._find_bucket(object_id, self._buckets)
        # A nonzero bucket points to the existing record for this object ID.
        if self._buckets[bucket]:
            # Keep the first location registered for this ID.
            return

        # Zero is reserved for empty buckets. Record positions start at one.
        position = self._count + 1
        # Bucket positions must fit in an unsigned 32-bit integer.
        if position > _MAX_RECORD_COUNT:
            # Refuse insertion before changing the index.
            raise OverflowError("Location index has exhausted its 32-bit record positions")
        # Grow the table before insertion would exceed 50% occupancy.
        if position * 2 > len(self._buckets):
            # Double the bucket count and redistribute existing references.
            self._rebuild_buckets(2 * len(self._buckets))
            # Recalculate the new ID's bucket using the new capacity.
            bucket = self._find_bucket(object_id, self._buckets)
        # Append the ID and location as a 56-byte record.
        new_size = len(self._records) + _RECORD_SIZE
        if new_size >= self._records.__alloc__():
            # Allow for bytearray over-allocation and both buffers during reallocation.
            self._memory.resize(self._allocated_bytes() + new_size + new_size // 4 + 32)
            try:
                self._records.extend(object_id + fields)
            finally:
                self._memory.resize(self._allocated_bytes())
        else:
            self._records.extend(object_id + fields)
        # Publish the new record's one-based position in its bucket.
        self._buckets[bucket] = position
        # Record that the index contains one more entry.
        self._count = position
        # Mark the inserted entry as requiring persistence.
        self._dirty = True

    def relocate(self, object_id: ObjectId, location: ObjectLocation) -> None:
        """Update an existing record after a byte-for-byte copy to another pack."""
        number = self.record_number(object_id)
        if number is None:
            raise ValueError("Cannot relocate an object missing from the location index")
        offset = number * _RECORD_SIZE + OBJECT_ID_SIZE
        _, _, length = _LOCATION_FIELDS.unpack_from(self._records, offset)
        if location.length != length:
            raise ValueError("A relocated object must have the same record length")
        _LOCATION_FIELDS.pack_into(self._records, offset, location.pack_id, location.offset, location.length)
        self._dirty = True

    def discard_packs(self, pack_ids: set[int]) -> None:
        """Remove entries still pointing at superseded packs in place."""
        next_offset = 0
        reserved_rebuild = False
        for offset in range(0, len(self._records), _RECORD_SIZE):
            pack_id, _, _ = _LOCATION_FIELDS.unpack_from(self._records, offset + OBJECT_ID_SIZE)
            if pack_id in pack_ids:
                if not reserved_rebuild:
                    # Reserve both phases before moving the first retained record.
                    self._memory.resize(self._allocated_bytes() + max(len(self._records) + 1, len(self._buckets) * _BUCKET_SIZE))
                    reserved_rebuild = True
                continue
            if next_offset != offset:
                self._records[next_offset : next_offset + _RECORD_SIZE] = self._records[offset : offset + _RECORD_SIZE]
            next_offset += _RECORD_SIZE
        if next_offset < len(self._records):
            del self._records[next_offset:]
            self._count = next_offset // _RECORD_SIZE
            self._rebuild_buckets(len(self._buckets))
            self._dirty = True

    def save_to(self, path: Path, lock: RepositoryLock, *, temporary_prefix: str = ".location-index-") -> bool:
        """Save pending changes and return True, or return False if unchanged and unsaved.

        A new index requires an initial save, including when it is empty.
        Failed saves retain pending changes for a retry.

        This method is part of the public location index API.
        """
        path = lock.require_held_for(path)
        if not self._dirty:
            return False
        header = _CHECKPOINT_HEADER.pack(_CHECKPOINT_MAGIC, _CHECKPOINT_VERSION, self._count)
        checksum = sha256(header)
        checksum.update(self._records)

        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            output = NamedTemporaryFile(dir=path.parent, prefix=temporary_prefix, delete=False)
            temporary_path = Path(output.name)
            try:
                with output:
                    output.write(header)
                    output.write(self._records)
                    output.write(checksum.digest())
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary_path, path)
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self._dirty = False
        return True

    @classmethod
    def load_from(cls, path: Path, lock: RepositoryLock) -> Self:
        """Read and validate a checkpoint while the caller holds the repository lock.

        This method is part of the public location index API.
        """
        path = lock.require_held_for(path)
        with path.open("rb") as source:
            header = source.read(_CHECKPOINT_HEADER.size)
            if len(header) != _CHECKPOINT_HEADER.size:
                raise ValueError("Truncated location index header")
            magic, version, count = _CHECKPOINT_HEADER.unpack(header)
            if magic != _CHECKPOINT_MAGIC or version != _CHECKPOINT_VERSION:
                raise ValueError("Unsupported location index format")
            if count > _MAX_RECORD_COUNT:
                raise ValueError("Location index entry count exceeds the bucket range")
            record_bytes = count * _RECORD_SIZE
            if os.fstat(source.fileno()).st_size != len(header) + record_bytes + _CHECKSUM_SIZE:
                raise ValueError("Location index size does not match its entry count")
            required_buckets = max(_INITIAL_BUCKET_COUNT, 2 * count)
            capacity = 1 << (required_buckets - 1).bit_length()
            index = cls()
            try:
                index._memory.resize(index._allocated_bytes() + record_bytes + 1 + capacity * _BUCKET_SIZE)
                index._records = bytearray(record_bytes)
                if source.readinto(index._records) != record_bytes:
                    raise ValueError("Truncated location index records")
                checksum = sha256(header)
                checksum.update(index._records)
                if source.read(_CHECKSUM_SIZE) != checksum.digest():
                    raise ValueError("Location index checksum mismatch")
                index._count = count
                index._rebuild_buckets(capacity)
            except BaseException:
                index.close()
                raise
            index._dirty = False
            return index

    @classmethod
    def rebuild_from_packs(cls, packs: Mapping[int, Path], lock: RepositoryLock) -> Self:
        """Rebuild the location index from finalized packs in one key domain.

        For repeated IDs, keep the first record in ascending pack order.
        This method is part of the public location index API.
        """
        lock.require_held()
        index = cls()
        try:
            for pack_id in sorted(packs):
                path = lock.require_held_for(packs[pack_id])
                try:
                    for offset, obj in Pack.iter_object_headers(path):
                        index.put(obj.object_id, ObjectLocation(pack_id, offset, OBJECT_HEADER_SIZE + obj.length))
                except (ValueError, OverflowError) as error:
                    raise ValueError(f"{path.name}: {error}") from error
        except BaseException:
            index.close()
            raise
        return index
