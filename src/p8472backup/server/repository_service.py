"""Initialize server-side repositories and their storage scopes."""

import json
import os
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import NoReturn, TypeGuard
from uuid import UUID

from p8472backup.common.error import InconsistentStateError
from p8472backup.server.repository import BackupMode, Repository
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.lock import RepositoryLock


_METADATA_NAME = "repository.json"
_METADATA_VERSION = 1
_SNAPSHOT_REGISTRY_NAME = "snapshot-registry.json"
_SNAPSHOT_REGISTRY_VERSION = 1
_INTERRUPTED_BACKUP_SET_REGISTRATION = "A previous registration may have been interrupted."


def _is_json_object(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


class RepositoryService:
    """Manage storage for one client repository."""

    @classmethod
    def find_by_client_id(cls, storage_root: Path, client_id: UUID) -> Repository:
        """Return the first registered set's repository for a verified client."""
        try:
            path = (storage_root / str(client_id)).resolve(strict=True)
        except FileNotFoundError as error:
            raise FileNotFoundError(f"Backup repository not found for client {client_id}") from error
        with RepositoryLock(path) as lock:
            _, mode, backup_sets = cls()._read_metadata(path, lock, client_id=client_id)
            if not backup_sets:
                raise InconsistentStateError("Repository has no registered backup sets.")
            backup_set_id, key_id = next(iter(backup_sets.items()))
            return Repository(path, client_id, backup_set_id, key_id, mode=mode)

    @classmethod
    def init_repository(cls, path: Path, client_id: UUID, backup_set_id: UUID, key_id: UUID, *, mode: BackupMode = "pull") -> Repository:
        """Create a repository with a single backup set."""
        return cls.init_repository_with_sets(path, client_id, ((backup_set_id, key_id),), mode=mode)

    @classmethod
    def init_repository_with_sets(cls, path: Path, client_id: UUID, backup_sets: Iterable[tuple[object, object]], *, mode: BackupMode = "pull") -> Repository:
        """Register the initial backup sets and return the first set's repository."""
        key_ids: dict[UUID, UUID] = {}
        for set_id, key_id in backup_sets:
            if not isinstance(set_id, UUID) or not isinstance(key_id, UUID):
                raise ValueError("Backup set and key domain IDs must be UUIDs")
            if set_id in key_ids:
                raise ValueError(f"Duplicate backup set ID: {set_id}")
            key_ids[set_id] = key_id
        if not key_ids:
            raise ValueError("At least one backup set is required")
        repositories = [Repository(path, client_id, set_id, key_id, mode=mode) for set_id, key_id in key_ids.items()]
        repository = repositories[0]
        try:
            path.mkdir(mode=0o700)
        except FileExistsError as error:
            if path.is_dir():
                try:
                    (path / _METADATA_NAME).stat()
                except FileNotFoundError as missing_metadata:
                    cls._raise_missing_repository_metadata(missing_metadata)
                raise FileExistsError("Backup repository already exists.") from error
            raise
        with RepositoryLock(repository.path) as lock:
            index_directory = repository.index_file.parent
            directories = (index_directory.parent, index_directory, repository.packs_directory.parent, repository.snapshots_directory.parent)
            for directory in directories:
                lock.require_held_for(directory).mkdir(mode=0o700)

            cls._create_backup_set_storage(repositories, lock)
            for directory in (index_directory.parent, repository.path, repository.path.parent):
                cls._sync_directory(directory)

            cls._write_json(repository.path / _SNAPSHOT_REGISTRY_NAME, lock, {"version": _SNAPSHOT_REGISTRY_VERSION, "snapshots": {}})
            cls._write_metadata(repository.path, lock, client_id, mode, key_ids)
        return repository

    def add_backup_set(self, repository: Repository, backup_set_id: object, key_id: object) -> None:
        """Register a backup set in an existing client repository."""
        if not isinstance(backup_set_id, UUID) or not isinstance(key_id, UUID):
            raise ValueError("Backup set and key domain IDs must be UUIDs")

        with RepositoryLock(repository.path) as lock:
            _, mode, backup_sets = self._read_metadata(repository.path, lock, client_id=repository.client_id)
            if backup_set_id in backup_sets:
                raise FileExistsError("Backup set already exists.")

            new_set = replace(repository, backup_set_id=backup_set_id, key_id=key_id, mode=mode)
            shared_key = key_id in backup_sets.values()
            if os.path.lexists(new_set.snapshots_directory):
                raise InconsistentStateError(f"Backup set storage already exists. {_INTERRUPTED_BACKUP_SET_REGISTRATION}")
            if shared_key:
                if not new_set.packs_directory.is_dir() or not new_set.index_file.is_file():
                    raise InconsistentStateError("Existing key domain storage is incomplete.")
                LocationIndex.load_from(new_set.index_file, lock)
            elif os.path.lexists(new_set.packs_directory) or os.path.lexists(new_set.index_file):
                raise InconsistentStateError(f"Key domain storage already exists. {_INTERRUPTED_BACKUP_SET_REGISTRATION}")

            self._create_backup_set_storage((new_set,), lock, existing_key_ids=backup_sets.values())
            backup_sets[backup_set_id] = key_id
            self._write_metadata(repository.path, lock, repository.client_id, mode, backup_sets)

    @staticmethod
    def _raise_missing_repository_metadata(error: FileNotFoundError) -> NoReturn:
        raise InconsistentStateError("Repository metadata is missing. A previous initialization may have been interrupted.") from error

    @classmethod
    def _create_backup_set_storage(cls, repositories: Sequence[Repository], lock: RepositoryLock, *, existing_key_ids: Collection[UUID] = ()) -> None:
        """Create snapshot directories and storage for new key domains."""
        new_key_domains = {repo.key_id: repo for repo in repositories if repo.key_id not in existing_key_ids}
        directories = [repository.snapshots_directory for repository in repositories]
        directories.extend(repository.packs_directory for repository in new_key_domains.values())
        for directory in directories:
            lock.require_held_for(directory).mkdir(mode=0o700)

        parents = dict.fromkeys(directory.parent for directory in directories)
        for directory in (*directories, *parents):
            cls._sync_directory(directory)

        for repository in new_key_domains.values():
            LocationIndex().save_to(repository.index_file, lock)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def open_repository(cls, path: Path, backup_set_id: UUID) -> Repository:
        """Open an existing backup set at an authorized repository path."""
        with RepositoryLock(path) as lock:
            client_id, mode, backup_sets = cls()._read_metadata(path, lock)
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
            client_id, current_mode, backup_sets = cls()._read_metadata(path, lock)
            if current_mode != mode:
                cls._write_metadata(path, lock, client_id, mode, backup_sets)

    def _read_metadata(self, path: Path, lock: RepositoryLock, *, client_id: UUID | None = None) -> tuple[UUID, BackupMode, dict[UUID, UUID]]:
        metadata_path = lock.require_held_for(path / _METADATA_NAME)
        try:
            with metadata_path.open("r", encoding="utf-8") as source:
                metadata: object = json.load(source)
        except FileNotFoundError as error:
            if client_id is not None:
                self._raise_missing_repository_metadata(error)
            raise
        if not _is_json_object(metadata):
            raise ValueError("Unsupported repository metadata format")
        version = metadata.get("version")
        if type(version) is not int or version != _METADATA_VERSION:
            raise ValueError("Unsupported repository metadata format")

        stored_id = metadata.get("client_id")
        mode = metadata.get("mode")
        backup_sets = metadata.get("backup_sets")
        if not isinstance(stored_id, str) or not _is_json_object(backup_sets):
            raise ValueError("Invalid repository metadata")
        stored_client_id = UUID(stored_id)
        if client_id is not None and stored_client_id != client_id:
            raise ValueError("Repository client ID does not match")
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
        return stored_client_id, parsed_mode, key_ids

    @classmethod
    def _write_metadata(cls, path: Path, lock: RepositoryLock, client_id: UUID, mode: BackupMode, backup_sets: dict[UUID, UUID]) -> None:
        key_ids_by_set = {str(set_id): str(key_id) for set_id, key_id in backup_sets.items()}
        cls._write_json(path / _METADATA_NAME, lock, {"version": _METADATA_VERSION, "client_id": str(client_id), "mode": mode, "backup_sets": key_ids_by_set})

    @staticmethod
    def _write_json(path: Path, lock: RepositoryLock, data: dict[str, object]) -> None:
        path = lock.require_held_for(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}-", delete_on_close=False) as output:
                json.dump(data, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
                output.close()
                os.replace(output.name, path)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
