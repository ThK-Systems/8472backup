"""Shared exceptions."""


class InconsistentStateError(RuntimeError):
    """Indicate inconsistent repository state."""


class ServerBusyError(BlockingIOError):
    """Indicate that the server cannot admit another operation yet."""
