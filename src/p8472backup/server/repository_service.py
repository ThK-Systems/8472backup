"""Initialize server-side repositories and their storage scopes."""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypeGuard
from uuid import UUID

from p8472backup.server.repository import BackupMode, Repository
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.lock import RepositoryLock


_METADATA_NAME = "repository.json"
_METADATA_VERSION = 1


def _is_json_object(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


class RepositoryService:
    """Manage storage for one client repository."""

    @classmethod
    def init_repository(cls, path: Path, client_id: UUID, backup_set_id: UUID, key_id: UUID, *, mode: BackupMode = "pull") -> Repository:
        """Create a repository at a new path below an existing parent directory."""
        repository = Repository(path, client_id, backup_set_id, key_id, mode=mode)
        path.mkdir(mode=0o700)
        with RepositoryLock(repository.path) as lock:
            index_path = repository.index_path
            index_directory = lock.require_held_for(index_path.parent)
            pack_directory = lock.require_held_for(repository.pack_directory)
            snapshot_directory = lock.require_held_for(repository.snapshot_directory)

            directories = (index_directory, pack_directory.parent, pack_directory, snapshot_directory.parent, snapshot_directory)
            for directory in directories:
                directory.mkdir(mode=0o700)
            for directory in (*reversed(directories), repository.path, repository.path.parent):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

            LocationIndex().save_to(index_path, lock)
            cls._write_metadata(repository.path, lock, client_id, mode, {backup_set_id: key_id})

        return repository

    @classmethod
    def open_repository(cls, path: Path, backup_set_id: UUID) -> Repository:
        """Open an existing backup set at an authorized repository path."""
        with RepositoryLock(path) as lock:
            client_id, mode, backup_sets = cls._read_metadata(path, lock)
            key_id = backup_sets.get(backup_set_id)
            if key_id is None:
                raise ValueError(f"Backup set is not registered in this repository: {backup_set_id}")
            return Repository(path, client_id, backup_set_id, key_id, mode=mode)

    @classmethod
    def set_mode(cls, path: Path, mode: BackupMode) -> None:
        """Persist the host mode for all sets in an existing repository."""
        if mode not in ("pull", "push"):
            raise ValueError(f"Unsupported backup mode: {mode!r}")

        with RepositoryLock(path) as lock:
            client_id, current_mode, backup_sets = cls._read_metadata(path, lock)
            if current_mode != mode:
                cls._write_metadata(path, lock, client_id, mode, backup_sets)

    @staticmethod
    def _read_metadata(path: Path, lock: RepositoryLock) -> tuple[UUID, BackupMode, dict[UUID, UUID]]:
        metadata_path = lock.require_held_for(path / _METADATA_NAME)
        with metadata_path.open("r", encoding="utf-8") as source:
            metadata: object = json.load(source)
        if not _is_json_object(metadata):
            raise ValueError("Unsupported repository metadata format")
        version = metadata.get("version")
        if type(version) is not int or version != _METADATA_VERSION:
            raise ValueError("Unsupported repository metadata format")

        client_id = metadata.get("client_id")
        mode = metadata.get("mode")
        backup_sets = metadata.get("backup_sets")
        if not isinstance(client_id, str) or not _is_json_object(backup_sets):
            raise ValueError("Invalid repository metadata")
        if mode == "pull":
            parsed_mode: BackupMode = "pull"
        elif mode == "push":
            parsed_mode = "push"
        else:
            raise ValueError(f"Unsupported backup mode: {mode!r}")

        key_ids: dict[UUID, UUID] = {}
        for set_id, key_id in backup_sets.items():
            if not isinstance(set_id, str) or not isinstance(key_id, str):
                raise ValueError("Invalid repository backup set mapping")
            backup_set_id = UUID(set_id)
            if backup_set_id in key_ids:
                raise ValueError(f"Duplicate backup set ID: {backup_set_id}")
            key_ids[backup_set_id] = UUID(key_id)
        return UUID(client_id), parsed_mode, key_ids

    @staticmethod
    def _write_metadata(path: Path, lock: RepositoryLock, client_id: UUID, mode: BackupMode, backup_sets: dict[UUID, UUID]) -> None:
        metadata_path = lock.require_held_for(path / _METADATA_NAME)
        data: dict[str, object] = {
            "version": _METADATA_VERSION,
            "client_id": str(client_id),
            "mode": mode,
            "backup_sets": {str(set_id): str(key_id) for set_id, key_id in backup_sets.items()},
        }
        directory_fd = os.open(metadata_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            output = NamedTemporaryFile(mode="w", encoding="utf-8", dir=metadata_path.parent, prefix=".repository-", delete=False)
            temporary_path = Path(output.name)
            try:
                with output:
                    json.dump(data, output, indent=2)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary_path, metadata_path)
            finally:
                temporary_path.unlink(missing_ok=True)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
