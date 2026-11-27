"""Account for repository operations across the server process."""

from collections.abc import Generator
from contextlib import contextmanager
from threading import Lock
from typing import Literal

from p8472backup.common.error import ServerBusyError
from p8472backup.server.memory import operation_memory


type OperationKind = Literal["repository", "backup", "maintenance"]

DEFAULT_MAX_REPOSITORY_OPERATIONS = 10
DEFAULT_MAX_BACKUP_JOBS = 3

_counter_lock = Lock()
_active_operations = 0
_active_backups = 0


def _has_capacity(kind: OperationKind) -> bool:
    if _active_operations >= DEFAULT_MAX_REPOSITORY_OPERATIONS:
        return False
    if kind == "backup":
        return _active_backups < DEFAULT_MAX_BACKUP_JOBS
    return kind != "maintenance" or _active_backups == 0


def has_capacity(kind: OperationKind) -> bool:
    """Check whether the scheduler can try dispatching this type of job."""
    with _counter_lock:
        return _has_capacity(kind)


@contextmanager
def reserve_operation(kind: OperationKind = "repository") -> Generator[None, None, None]:
    """Reserve one operation and, for a backup, one backup slot until completion."""
    global _active_operations, _active_backups

    backup_slots = int(kind == "backup")
    with _counter_lock:
        if not _has_capacity(kind):
            raise ServerBusyError("Server operation capacity is currently unavailable")
        _active_operations += 1
        _active_backups += backup_slots
    try:
        with operation_memory():
            yield
    finally:
        with _counter_lock:
            _active_operations -= 1
            _active_backups -= backup_slots
