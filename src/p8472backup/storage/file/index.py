"""Object locations within a client's repository."""

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
