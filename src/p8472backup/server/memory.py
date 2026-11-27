"""Reserve memory for active server operations."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock

from p8472backup.common.error import MemoryBusyError, MemoryLimitError


DEFAULT_MEMORY_BUDGET = 128 * 1024 * 1024
_IO_WORKSPACE = 1024 * 1024
_budget_lock = Lock()
_reserved_memory = 0


@dataclass
class _OperationMemory:
    reserved: int = 0
    active: bool = True
    workspace: dict[str, int] = field(default_factory=dict[str, int])

    def change(self, difference: int) -> None:
        global _reserved_memory

        with _budget_lock:
            if not self.active:
                return
            if difference > 0:
                if self.reserved + difference > DEFAULT_MEMORY_BUDGET:
                    raise MemoryLimitError("Operation exceeds the server memory budget")
                if _reserved_memory + difference > DEFAULT_MEMORY_BUDGET:
                    raise MemoryBusyError("Server memory is currently reserved by other operations")
            self.reserved += difference
            _reserved_memory += difference


_operation_memory = ContextVar[_OperationMemory | None]("operation_memory", default=None)


@contextmanager
def operation_memory() -> Generator[None, None, None]:
    """Account for one server operation until its resources are closed."""
    global _reserved_memory

    operation = _OperationMemory()
    token = _operation_memory.set(operation)
    try:
        reserve_workspace("io", _IO_WORKSPACE)
        yield
    finally:
        with _budget_lock:
            _reserved_memory -= operation.reserved
            operation.reserved = 0
            operation.active = False
        _operation_memory.reset(token)


class MemoryReservation:
    """Track the changing size of an allocation owned by the current operation."""

    def __init__(self) -> None:
        self._operation = _operation_memory.get()
        self._amount = 0

    def resize(self, amount: int) -> None:
        """Reserve the new size before allocating it, or return freed capacity."""
        if self._operation is not None and amount != self._amount:
            self._operation.change(amount - self._amount)
            self._amount = amount

    def close(self) -> None:
        """Return the reservation once the allocation has been freed."""
        self.resize(0)


def reserve_workspace(name: str, size: int) -> None:
    """Reserve the peak for reusable working data until the operation ends."""
    operation = _operation_memory.get()
    if operation is not None and size > (current := operation.workspace.get(name, 0)):
        operation.change(size - current)
        operation.workspace[name] = size
