"""Initialize server-side repositories and their storage scopes."""

import json
import os
from collections.abc import Collection, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import NoReturn, TypeGuard
from uuid import UUID

from p8472backup.common.error import InconsistentStateError
from p8472backup.server.repository import BackupMode, BackupSet, Repository
from p8472backup.server.snapshot_service import SnapshotService
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.lock import RepositoryLock


_METADATA_NAME = "repository.json"
_METADATA_VERSION = 2
_SNAPSHOT_REGISTRY_NAME = "snapshot-registry.json"
_SNAPSHOT_REGISTRY_VERSION = 1
_INTERRUPTED_BACKUP_SET_REGISTRATION = "A previous registration may have been interrupted."


def _is_json_object(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


class RepositoryAdminService:
    """Manage storage for one client repository."""

    @classmethod
    def find_by_client_id(cls, storage_root: Path, client_id: UUID) -> Repository:
        """Load the repository for a verified client."""
        with cls().locked_repository(storage_root, client_id) as (repository, lock):
            return replace(repository, snapshots=SnapshotService().list_snapshots(repository, lock))

    @contextmanager
    def locked_repository(self, storage_root: Path, client_id: UUID) -> Generator[tuple[Repository, RepositoryLock], None, None]:
        """Load verified metadata and hold the lock through one repository operation."""
        path = storage_root / str(client_id)
        try:
            lock = RepositoryLock(path)
        except FileNotFoundError as error:
            raise FileNotFoundError(f"Backup repository not found for client {client_id}") from error
        with lock:
            repository = self._read_metadata(path, lock, client_id=client_id)
            yield repository, lock

    def init_repository(self, path: Path, client_id: UUID, backup_sets: Iterable[tuple[object, object]], *, mode: BackupMode = "pull") -> Repository:
        """Create the repository and register all its initial backup sets."""
        sets: dict[UUID, BackupSet] = {}
        for set_id, key_id in backup_sets:
            if not isinstance(set_id, UUID) or not isinstance(key_id, UUID):
                raise ValueError("Backup set and key domain IDs must be UUIDs")
            if set_id in sets:
                raise ValueError(f"Duplicate backup set ID: {set_id}")
            sets[set_id] = BackupSet(set_id, key_id)
        if not sets:
            raise ValueError("At least one backup set is required")
        repository = Repository(path, client_id, sets, mode=mode)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError as error:
            if not path.is_dir():
                raise
            try:
                (path / _METADATA_NAME).stat()
            except FileNotFoundError as missing_metadata:
                self._raise_missing_repository_metadata(missing_metadata)
            raise FileExistsError("Backup repository already exists.") from error
        with RepositoryLock(repository.path) as lock:
            directories = (repository.data_directory, repository.index_directory, repository.packs_root, repository.snapshots_root)
            for directory in directories:
                lock.require_held_for(directory).mkdir(mode=0o700)

            self._create_backup_set_storage(repository, sets.values(), lock)
            for directory in (repository.data_directory, repository.path, repository.path.parent):
                self._sync_directory(directory)

            self._write_json(repository.path / _SNAPSHOT_REGISTRY_NAME, lock, {"version": _SNAPSHOT_REGISTRY_VERSION, "snapshots": {}})
            self._write_metadata(repository, lock)
        return repository

    def add_backup_set(self, repository: Repository, backup_set_id: object, key_id: object, lock: RepositoryLock) -> None:
        """Register a set using the repository and lock from locked_repository()."""
        if not isinstance(backup_set_id, UUID) or not isinstance(key_id, UUID):
            raise ValueError("Backup set and key domain IDs must be UUIDs")

        lock.require_held_for(repository.path / _METADATA_NAME)
        if backup_set_id in repository.backup_sets:
            raise FileExistsError("Backup set already exists.")

        new_set = BackupSet(backup_set_id, key_id)
        existing_key_ids = {backup_set.key_id for backup_set in repository.backup_sets.values()}
        packs_directory = repository.packs_root / str(key_id)
        index_file = repository.index_directory / str(key_id)
        if os.path.lexists(repository.snapshots_root / str(backup_set_id)):
            raise InconsistentStateError(f"Backup set storage already exists. {_INTERRUPTED_BACKUP_SET_REGISTRATION}")
        if key_id in existing_key_ids:
            if not packs_directory.is_dir() or not index_file.is_file():
                raise InconsistentStateError("Existing key domain storage is incomplete.")
            LocationIndex.load_from(index_file, lock)
        elif os.path.lexists(packs_directory) or os.path.lexists(index_file):
            raise InconsistentStateError(f"Key domain storage already exists. {_INTERRUPTED_BACKUP_SET_REGISTRATION}")

        self._create_backup_set_storage(repository, (new_set,), lock, existing_key_ids=existing_key_ids)
        self._write_metadata(replace(repository, backup_sets={**repository.backup_sets, backup_set_id: new_set}), lock)

    def remove_backup_set(self, repository: Repository, backup_set: BackupSet, lock: RepositoryLock, *, protection_duration: timedelta) -> None:
        """Persist the deleted state before removing unprotected snapshots."""
        metadata_path = lock.require_held_for(repository.path / _METADATA_NAME)
        backup_set_id = backup_set.backup_set_id
        if repository.backup_sets.get(backup_set_id) != backup_set:
            raise ValueError(f"Backup set does not match the repository: {backup_set_id}")

        if backup_set.deleted:
            # An earlier attempt may have replaced the metadata before syncing its directory.
            self._sync_directory(metadata_path.parent)
        else:
            backup_set = replace(backup_set, deleted=True)
            repository = replace(repository, backup_sets={**repository.backup_sets, backup_set_id: backup_set})
            self._write_metadata(repository, lock)

        SnapshotService().remove_unprotected_snapshots(repository, backup_set, lock, protection_duration=protection_duration)

    @staticmethod
    def _raise_missing_repository_metadata(error: FileNotFoundError) -> NoReturn:
        raise InconsistentStateError("Repository metadata is missing. A previous initialization may have been interrupted.") from error

    def _create_backup_set_storage(self, repository: Repository, backup_sets: Collection[BackupSet], lock: RepositoryLock, *, existing_key_ids: Collection[UUID] = ()) -> None:
        """Create snapshot directories and storage for new key domains."""
        new_key_ids = dict.fromkeys(backup_set.key_id for backup_set in backup_sets if backup_set.key_id not in existing_key_ids)
        directories = [repository.snapshots_root / str(backup_set.backup_set_id) for backup_set in backup_sets]
        directories.extend(repository.packs_root / str(key_id) for key_id in new_key_ids)
        for directory in directories:
            lock.require_held_for(directory).mkdir(mode=0o700)

        parents = dict.fromkeys(directory.parent for directory in directories)
        for directory in (*directories, *parents):
            self._sync_directory(directory)

        for key_id in new_key_ids:
            LocationIndex().save_to(repository.index_directory / str(key_id), lock)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def set_mode(self, repository: Repository, mode: BackupMode, lock: RepositoryLock) -> None:
        """Persist the mode using the repository and lock from locked_repository()."""
        lock.require_held_for(repository.path / _METADATA_NAME)
        if not Repository.is_backup_mode(mode):
            raise ValueError(f"Unsupported backup mode: {mode!r}")
        if repository.mode != mode:
            self._write_metadata(replace(repository, mode=mode), lock)

    def _read_metadata(self, path: Path, lock: RepositoryLock, *, client_id: UUID) -> Repository:
        """Load the verified repository metadata under its lock."""
        metadata_path = lock.require_held_for(path / _METADATA_NAME)
        try:
            with metadata_path.open("r", encoding="utf-8") as source:
                metadata: object = json.load(source)
        except FileNotFoundError as error:
            self._raise_missing_repository_metadata(error)
        if not _is_json_object(metadata):
            raise ValueError("Unsupported repository metadata format")
        version = metadata.get("version")
        if type(version) is not int or version not in (1, _METADATA_VERSION):
            raise ValueError("Unsupported repository metadata format")

        stored_id = metadata.get("client_id")
        mode = metadata.get("mode")
        backup_sets = metadata.get("backup_sets")
        if not isinstance(stored_id, str) or not _is_json_object(backup_sets):
            raise ValueError("Invalid repository metadata")
        if UUID(stored_id) != client_id:
            raise ValueError("Repository client ID does not match")
        if not Repository.is_backup_mode(mode):
            raise ValueError(f"Unsupported backup mode: {mode!r}")

        sets: dict[UUID, BackupSet] = {}
        for set_id, entry in backup_sets.items():
            deleted: object
            if version == 1:
                key_id, deleted = entry, False
            elif _is_json_object(entry):
                key_id, deleted = entry.get("key_id"), entry.get("deleted")
            else:
                raise ValueError("Invalid repository backup set metadata")
            if not isinstance(set_id, str) or not isinstance(key_id, str) or not isinstance(deleted, bool):
                raise ValueError("Invalid repository backup set metadata")
            backup_set_id = UUID(set_id)
            if backup_set_id in sets:
                raise ValueError(f"Duplicate backup set ID: {backup_set_id}")
            sets[backup_set_id] = BackupSet(backup_set_id, UUID(key_id), deleted=deleted)
        return Repository(path, client_id, sets, mode=mode)

    def _write_metadata(self, repository: Repository, lock: RepositoryLock) -> None:
        sets: dict[str, dict[str, str | bool]] = {str(backup_set.backup_set_id): {"key_id": str(backup_set.key_id), "deleted": backup_set.deleted} for backup_set in repository.backup_sets.values()}
        self._write_json(repository.path / _METADATA_NAME, lock, {"version": _METADATA_VERSION, "client_id": str(repository.client_id), "mode": repository.mode, "backup_sets": sets})

    def _write_json(self, path: Path, lock: RepositoryLock, data: dict[str, object]) -> None:
        path = lock.require_held_for(path)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}-", delete_on_close=False) as output:
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.replace(output.name, path)
        self._sync_directory(path.parent)
