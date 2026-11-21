"""Object locations within a client's repository."""

from __future__ import annotations

import struct
from array import array
from dataclasses import dataclass

from p8472backup.archive.objects import OBJECT_ID_SIZE, Object, ObjectId


_LOCATION_FIELDS = struct.Struct("<QQQ")
_RECORD_SIZE = OBJECT_ID_SIZE + _LOCATION_FIELDS.size
_INITIAL_BUCKET_COUNT = 8


@dataclass(frozen=True)
class ObjectLocation:
    """An object ID and its pack number, header offset, and total record length.

    The ID occupies 32 bytes; each integer requires an unsigned 64-bit field.
    This is an in-memory record, not a selected persistent index format.
    """

    object_id: ObjectId
    pack_id: int
    offset: int
    length: int


class LocationIndex:
    """In-memory index for one repository and key domain.

    Callers synchronize access when sharing an index with a writer.
    """

    def __init__(self) -> None:
        self._buckets: array[int] = array("I", [0]) * _INITIAL_BUCKET_COUNT
        if self._buckets.itemsize != 4:
            raise RuntimeError("Location index requires 32-bit unsigned array elements")
        self._records = bytearray()
        self._count = 0

    def _find_bucket(self, object_id: ObjectId, buckets: array[int]) -> int:
        """Find the matching bucket or the first free bucket in its probe sequence."""
        capacity = len(buckets)
        bucket = int.from_bytes(object_id[:4], "big") % capacity
        while position := buckets[bucket]:
            if self._records.startswith(object_id, (position - 1) * _RECORD_SIZE):
                break
            bucket = (bucket + 1) % capacity
        return bucket

    def _grow_buckets(self) -> None:
        """Rebuild the bucket table without moving the packed records."""
        buckets: array[int] = array("I", [0]) * (2 * len(self._buckets))
        for record_index in range(self._count):
            offset = record_index * _RECORD_SIZE
            object_id = bytes(self._records[offset : offset + OBJECT_ID_SIZE])
            buckets[self._find_bucket(object_id, buckets)] = record_index + 1
        self._buckets = buckets

    def get(self, object_id: ObjectId) -> ObjectLocation | None:
        """Return the location for an object ID, or None if it is absent.

        This method is part of the public location index API.
        """
        Object.validate_object_id(object_id)
        position = self._buckets[self._find_bucket(object_id, self._buckets)]
        if position == 0:
            return None
        offset = (position - 1) * _RECORD_SIZE + OBJECT_ID_SIZE
        pack_id, pack_offset, length = _LOCATION_FIELDS.unpack_from(self._records, offset)
        return ObjectLocation(object_id, pack_id, pack_offset, length)

    def put(self, location: ObjectLocation) -> None:
        """Insert or replace a location using its object ID as the key.

        This method is part of the public location index API.
        """
        Object.validate_object_id(location.object_id)
        try:
            fields = _LOCATION_FIELDS.pack(location.pack_id, location.offset, location.length)
        except struct.error as error:
            raise ValueError("Pack ID, offset, and length must fit unsigned 64-bit integers") from error

        bucket = self._find_bucket(location.object_id, self._buckets)
        position = self._buckets[bucket]
        if position:
            offset = (position - 1) * _RECORD_SIZE + OBJECT_ID_SIZE
            self._records[offset : offset + _LOCATION_FIELDS.size] = fields
            return

        # Zero is reserved for empty buckets. Record positions start at one.
        position = self._count + 1
        if position >= 2**32:
            raise OverflowError("Location index has exhausted its 32-bit record positions")
        if position * 2 > len(self._buckets):
            self._grow_buckets()
            bucket = self._find_bucket(location.object_id, self._buckets)
        self._records.extend(location.object_id + fields)
        self._buckets[bucket] = position
        self._count = position
