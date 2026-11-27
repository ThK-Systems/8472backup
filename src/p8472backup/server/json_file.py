"""Read and durably publish local server JSON records."""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypeGuard

from p8472backup.server.lock import RepositoryLock
from p8472backup.server.memory import reserve_workspace


def read_json(path: Path, lock: RepositoryLock, *, workspace: str = "JSON document") -> object:
    """Read repository JSON, rejecting duplicate keys at any depth."""
    return read_json_file(lock.require_held_for(path), workspace=workspace)


def read_json_file(path: Path, *, workspace: str = "JSON document") -> object:
    """Decode a JSON file at a path already checked by its caller."""
    with path.open("r", encoding="utf-8") as source:
        reserve_workspace(workspace, 4096 + 48 * os.fstat(source.fileno()).st_size)
        data: object = json.load(source, object_pairs_hook=_unique_members)
    return data


def _unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    members: dict[str, object] = {}
    for key, value in pairs:
        if key in members:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        members[key] = value
    return members


def is_json_object(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, lock: RepositoryLock, data: dict[str, object], *, temporary_prefix: str | None = None) -> None:
    write_json_file(lock.require_held_for(path), data, temporary_prefix=temporary_prefix)


def write_json_file(path: Path, data: dict[str, object], *, temporary_prefix: str | None = None) -> None:
    """Publish a JSON file durably at a path already checked by its caller."""
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=temporary_prefix or f".{path.stem}-", delete_on_close=False) as output:
        json.dump(data, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
        output.close()
        os.replace(output.name, path)
    sync_directory(path.parent)
