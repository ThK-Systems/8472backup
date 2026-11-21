"""Object locations within a client's repository."""

from array import array
from dataclasses import dataclass

from p8472backup.archive.objects import ObjectId


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
    """Location index for one repository and key domain."""

    def __init__(self) -> None:
        self._buckets: array[int] = array("I")
        self._records = bytearray()
        self._count = 0

    def get(self, object_id: ObjectId) -> ObjectLocation | None:
        """Return the location for an object ID, or None if it is absent.

        This method is part of the public location index API.
        """
        raise NotImplementedError

    def put(self, location: ObjectLocation) -> None:
        """Insert an object location or replace its existing location.

        This method is part of the public location index API.
        """
        raise NotImplementedError
