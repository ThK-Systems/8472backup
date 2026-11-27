"""Shared exceptions."""


class InconsistentStateError(RuntimeError):
    """Indicate inconsistent repository state."""


class ServerBusyError(BlockingIOError):
    """Indicate that the server cannot admit another operation yet."""


class MemoryBusyError(ServerBusyError):
    """Indicate that another active operation is using required memory."""


class MemoryLimitError(RuntimeError):
    """Indicate that the operation exceeds the server memory budget on its own."""
