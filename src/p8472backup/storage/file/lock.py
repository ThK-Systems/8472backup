"""Exclusive access to a client repository on a local filesystem."""

from __future__ import annotations

import os
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from types import TracebackType
from typing import Self


class RepositoryLock:
    """Acquire exclusive repository access for one operation."""

    def __init__(self, repository: Path) -> None:
        self._repository = repository.resolve(strict=True)
        self._path = self._repository / ".repository.lock"
        self._fd: int | None = None

    def __enter__(self) -> Self:
        """Acquire immediately or raise BlockingIOError if the repository is busy."""
        if self._fd is not None:
            raise RuntimeError("Repository lock is already held by this instance")
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            flock(fd, LOCK_EX | LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Keep the lock file in place so contenders always lock the same inode.
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    def require_held_for(self, path: Path) -> Path:
        """Return the resolved repository file path if this lock is held for it."""
        if self._fd is None:
            raise RuntimeError("Repository lock is not held")
        path = path.resolve()
        if self._repository not in path.parents or path == self._path:
            raise ValueError("Path must name a repository file other than the lock file")
        return path
