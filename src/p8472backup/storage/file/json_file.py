"""Validate JSON objects and publish records durably in the file backend."""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypeGuard

from p8472backup.storage.file.lock import RepositoryLock


def is_json_object(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, lock: RepositoryLock, data: dict[str, object], *, temporary_prefix: str | None = None) -> None:
    path = lock.require_held_for(path)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=temporary_prefix or f".{path.stem}-", delete_on_close=False) as output:
        json.dump(data, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
        output.close()
        os.replace(output.name, path)
    sync_directory(path.parent)
