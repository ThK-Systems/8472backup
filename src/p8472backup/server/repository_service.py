"""Initialize server-side repositories and their storage scopes."""

import os
import shutil
from collections.abc import Collection, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from p8472backup.common.error import InconsistentStateError
from p8472backup.common.log import Log
from p8472backup.server import trusted_time
from p8472backup.server.admission import OperationKind, reserve_operation
from p8472backup.server.repository import BackupMode, BackupSet, Repository
from p8472backup.server.snapshot_service import SNAPSHOT_REGISTRY_NAME, SNAPSHOT_REGISTRY_VERSION, SnapshotService
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.json_file import is_json_object, read_json, sync_directory, write_json
from p8472backup.storage.file.lock import RepositoryLock


_METADATA_NAME = "repository.json"
_METADATA_VERSION = 1
_INTERRUPTED_BACKUP_SET_REGISTRATION = "A previous registration may have been interrupted."
_DELETION_PREFIX = ".deleting-"


class RepositoryService:
    """Manage storage for one client repository."""

    def list_client_ids(self, storage_root: Path, *, deleting: bool = False) -> tuple[UUID, ...]:
        """Find registered repositories or detached repositories awaiting removal."""
        client_ids: list[UUID] = []
        with os.scandir(storage_root) as entries:
            for entry in entries:
                name = entry.name.removeprefix(_DELETION_PREFIX) if deleting else entry.name
                if deleting and name == entry.name:
                    continue
                try:
                    client_id = UUID(name)
                except ValueError:
                    continue
                if name != str(client_id):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False) and (deleting or (Path(entry.path) / _METADATA_NAME).is_file()):
                        client_ids.append(client_id)
                except OSError as error:
                    Log.warning("Repository discovery failed client_id=%s reason=%s", client_id, type(error).__name__)
        return tuple(sorted(client_ids))

    @classmethod
    def find_by_client_id(cls, storage_root: Path, client_id: UUID) -> Repository:
        """Load repository and published snapshot metadata for a verified client."""
        with cls().locked_repository(storage_root, client_id) as (repository, lock):
            return replace(repository, snapshots=SnapshotService().list_snapshots(repository, lock))

    @contextmanager
    def locked_repository(
        self,
        storage_root: Path,
        client_id: UUID,
        *,
        operation: str | None = None,
        backup_set_id: UUID | None = None,
        snapshot_id: UUID | None = None,
        kind: OperationKind = "repository",
    ) -> Generator[tuple[Repository, RepositoryLock], None, None]:
        """Admit the operation, load verified metadata, and hold the repository lock."""
        path = storage_root / str(client_id)
        try:
            with reserve_operation(kind):
                if path.is_symlink():
                    raise InconsistentStateError("Unexpected repository path")
                try:
                    lock = RepositoryLock(path)
                except FileNotFoundError as error:
                    raise FileNotFoundError(f"Backup repository not found for client {client_id}") from error
                with lock:
                    repository = self._read_metadata(path, lock, client_id=client_id)
                    yield repository, lock
        except (OSError, ValueError, InconsistentStateError) as error:
            if operation is not None:
                scope = f"client_id={client_id}"
                if backup_set_id is not None:
                    scope += f" backup_set_id={backup_set_id}"
                if snapshot_id is not None:
                    scope += f" snapshot_id={snapshot_id}"
                Log.warning("Operation %s failed %s reason=%s", operation, scope, type(error).__name__)
            raise

    def init_repository(self, path: Path, client_id: UUID, backup_sets: Iterable[tuple[object, object]], *, mode: BackupMode = "pull", backup_set_names: Mapping[UUID, str] | None = None) -> Repository:
        """Create the repository and register all its initial backup sets."""
        names = backup_set_names or {}
        sets: dict[UUID, BackupSet] = {}
        for set_id, key_id in backup_sets:
            if not isinstance(set_id, UUID) or not isinstance(key_id, UUID):
                raise ValueError("Backup set and key domain IDs must be UUIDs")
            if set_id in sets:
                raise ValueError(f"Duplicate backup set ID: {set_id}")
            sets[set_id] = BackupSet(set_id, key_id, name=names.get(set_id))
        if not sets:
            raise ValueError("At least one backup set is required")
        if not names.keys() <= sets.keys():
            raise ValueError("Backup set names must refer to registered sets")
        repository = Repository(path, client_id, sets, mode=mode)
        with reserve_operation():
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
                    sync_directory(directory)

                write_json(repository.path / SNAPSHOT_REGISTRY_NAME, lock, {"version": SNAPSHOT_REGISTRY_VERSION, "snapshots": {}})
                self._write_metadata(repository, lock)
        Log.info("Repository initialized client_id=%s mode=%s backup_sets=%d", client_id, mode, len(sets))
        return repository

    def add_backup_set(self, repository: Repository, backup_set_id: object, key_id: object, lock: RepositoryLock, *, name: str | None = None) -> None:
        """Register a set using the repository and lock from locked_repository()."""
        if not isinstance(backup_set_id, UUID) or not isinstance(key_id, UUID):
            raise ValueError("Backup set and key domain IDs must be UUIDs")

        lock.require_held_for(repository.path / _METADATA_NAME)
        if repository.deleted:
            raise ValueError(f"Repository is deleted: {repository.client_id}")
        if backup_set_id in repository.backup_sets:
            raise FileExistsError("Backup set already exists.")

        new_set = BackupSet(backup_set_id, key_id, name=name)
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
        Log.info("Backup set registered client_id=%s backup_set_id=%s", repository.client_id, backup_set_id)

    def save_prune_rule(self, repository: Repository, backup_set: BackupSet, lock: RepositoryLock, prune_rule: str, *, temporary_prefix: str | None = None) -> None:
        """Durably save the rule validated at backup admission."""
        path = lock.require_held_for(repository.backup_set_file(backup_set.backup_set_id))
        path.parent.mkdir(mode=0o700, exist_ok=True)
        write_json(path, lock, {"prune": prune_rule}, temporary_prefix=temporary_prefix)
        sync_directory(repository.path)

    def read_prune_rule(self, repository: Repository, backup_set: BackupSet, lock: RepositoryLock) -> str:
        """Read the prune rule most recently saved for a set."""
        try:
            data = read_json(repository.backup_set_file(backup_set.backup_set_id), lock)
        except FileNotFoundError as error:
            raise ValueError(f"No prune rule has been stored for backup set {backup_set.backup_set_id}") from error

        if not is_json_object(data) or not isinstance(prune_rule := data.get("prune"), str):
            raise ValueError(f"Invalid prune rule for backup set {backup_set.backup_set_id}")
        return prune_rule

    def remove_backup_set(self, repository: Repository, backup_set: BackupSet, lock: RepositoryLock, *, protection_duration: timedelta) -> None:
        """Persist the deleted state before removing unprotected snapshots."""
        metadata_path = lock.require_held_for(repository.path / _METADATA_NAME)
        backup_set_id = backup_set.backup_set_id
        if repository.backup_sets.get(backup_set_id) != backup_set:
            raise ValueError(f"Backup set does not match the repository: {backup_set_id}")

        if backup_set.deleted:
            # An earlier attempt may have replaced the metadata before syncing its directory.
            sync_directory(metadata_path.parent)
        else:
            backup_set = replace(backup_set, deleted=True)
            repository = replace(repository, backup_sets={**repository.backup_sets, backup_set_id: backup_set})
            self._write_metadata(repository, lock)
            Log.info("Backup set retired client_id=%s backup_set_id=%s", repository.client_id, backup_set_id)

        SnapshotService().remove_unprotected_snapshots(repository, (backup_set,), lock, protection_duration=protection_duration)

    def remove_repository(self, repository: Repository, lock: RepositoryLock, *, protection_duration: timedelta) -> None:
        """Retire the repository and remove its unprotected snapshots."""
        metadata_path = lock.require_held_for(repository.path / _METADATA_NAME)
        if repository.deleted:
            sync_directory(metadata_path.parent)
        else:
            backup_sets = {set_id: replace(backup_set, deleted=True) for set_id, backup_set in repository.backup_sets.items()}
            repository = replace(repository, backup_sets=backup_sets, deleted=True)
            self._write_metadata(repository, lock)
            Log.info("Repository retired client_id=%s", repository.client_id)

        SnapshotService().remove_unprotected_snapshots(repository, repository.backup_sets.values(), lock, protection_duration=protection_duration)

    def maintain_repository(self, storage_root: Path, client_id: UUID, *, protection_duration: timedelta, compact_threshold_percent: int) -> None:
        """Finish due deletions and compact the remaining repository under its lock."""
        detached = False
        try:
            with self.locked_repository(storage_root, client_id, operation="maintenance", kind="maintenance") as (repository, lock):
                if repository.deleted:
                    detached = self.delete(repository, lock, protection_duration=protection_duration)
                else:
                    for backup_set in repository.backup_sets.values():
                        if backup_set.deleted:
                            repository = self.delete_backup_set(repository, backup_set, lock, protection_duration=protection_duration)
                if not detached:
                    snapshots = SnapshotService()
                    if snapshots.compaction_pending(repository, lock):
                        snapshots.compact(repository, lock, threshold_percent=compact_threshold_percent)
        except (OSError, ValueError, InconsistentStateError):
            # The repository context logs the failure once.
            return
        if detached:
            self.finish_deletion(storage_root, client_id)

    def delete_backup_set(self, repository: Repository, backup_set: BackupSet, lock: RepositoryLock, *, protection_duration: timedelta) -> Repository:
        """Finish deleting a retired set once all its snapshot records are gone."""
        snapshots = SnapshotService()
        sets = snapshots.remove_unprotected_snapshots(repository, (backup_set,), lock, protection_duration=protection_duration)
        set_id, key_id = backup_set.backup_set_id, backup_set.key_id
        if set_id not in sets:
            return repository

        remaining_sets = {other_id: other for other_id, other in repository.backup_sets.items() if other_id != set_id}
        snapshot_directory = repository.snapshots_root / str(set_id)
        directories = [snapshot_directory]
        files = [repository.backup_set_file(set_id)]
        if all(other.key_id != key_id for other in remaining_sets.values()):
            directories.append(repository.packs_root / str(key_id))
            files.append(repository.index_directory / str(key_id))
        for path in (*directories, *files):
            if lock.require_held_for(path) != path:
                raise InconsistentStateError(f"Unexpected retired backup set path: {path.name}")

        # Confirm retirement and the register before removing any of their storage.
        sync_directory(repository.path)
        if snapshot_directory.is_dir():
            snapshots.cleanup(repository, backup_set, lock)
        for directory in directories:
            if os.path.lexists(directory):
                shutil.rmtree(directory)
            sync_directory(directory.parent)
        for path in files:
            path.unlink(missing_ok=True)
            if path.parent.is_dir():
                sync_directory(path.parent)
        current = replace(repository, backup_sets=remaining_sets)
        self._write_metadata(current, lock)
        Log.info("Backup set deleted client_id=%s backup_set_id=%s", repository.client_id, set_id)
        return current

    def delete(self, repository: Repository, lock: RepositoryLock, *, protection_duration: timedelta) -> bool:
        """Detach a retired repository after its last protected snapshot expires."""
        lock.require_held_for(repository.path / _METADATA_NAME)
        if not repository.deleted or repository.path.name != str(repository.client_id):
            raise ValueError(f"Repository is not retired for deletion: {repository.client_id}")
        sets = SnapshotService().remove_unprotected_snapshots(repository, repository.backup_sets.values(), lock, protection_duration=protection_duration)
        if sets != set(repository.backup_sets):
            return False

        detached = repository.path.with_name(f"{_DELETION_PREFIX}{repository.client_id}")
        if os.path.lexists(detached):
            raise FileExistsError("Another retired repository is awaiting final deletion")
        sync_directory(repository.path)
        repository.path.rename(detached)
        sync_directory(detached.parent)
        return True

    def finish_deletion(self, storage_root: Path, client_id: UUID) -> None:
        """Remove a detached repository or log a recoverable failure for retry."""
        try:
            with reserve_operation("maintenance"):
                directory = storage_root.resolve(strict=True) / f"{_DELETION_PREFIX}{client_id}"
                if directory.is_symlink():
                    raise InconsistentStateError("Unexpected detached repository path")
                with RepositoryLock(directory):
                    trusted_time.server_clock.now()
                    # A rename can be visible after a failed parent directory sync.
                    sync_directory(directory.parent)
                    shutil.rmtree(directory)
                    sync_directory(directory.parent)
        except (OSError, ValueError, InconsistentStateError) as error:
            Log.warning("Final repository deletion failed client_id=%s reason=%s", client_id, type(error).__name__)
        else:
            Log.info("Repository deleted client_id=%s", client_id)

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
            sync_directory(directory)

        for key_id in new_key_ids:
            LocationIndex().save_to(repository.index_directory / str(key_id), lock)

    def set_mode(self, repository: Repository, mode: BackupMode, lock: RepositoryLock) -> None:
        """Persist the mode using the repository and lock from locked_repository()."""
        metadata_path = lock.require_held_for(repository.path / _METADATA_NAME)
        if repository.deleted:
            raise ValueError(f"Repository is deleted: {repository.client_id}")
        if not Repository.is_backup_mode(mode):
            raise ValueError(f"Unsupported backup mode: {mode!r}")
        if repository.mode == mode:
            # A prior replacement can be visible after a failed directory sync.
            sync_directory(metadata_path.parent)
        else:
            self._write_metadata(replace(repository, mode=mode), lock)
            Log.info("Backup mode changed client_id=%s mode=%s", repository.client_id, mode)

    def _read_metadata(self, path: Path, lock: RepositoryLock, *, client_id: UUID) -> Repository:
        """Load the verified repository metadata under its lock."""
        try:
            metadata = read_json(path / _METADATA_NAME, lock)
        except FileNotFoundError as error:
            self._raise_missing_repository_metadata(error)
        if not is_json_object(metadata):
            raise ValueError("Unsupported repository metadata format")
        version = metadata.get("version")
        if type(version) is not int or version != _METADATA_VERSION:
            raise ValueError("Unsupported repository metadata format")

        stored_id = metadata.get("client_id")
        mode = metadata.get("mode")
        backup_sets = metadata.get("backup_sets")
        if not isinstance(stored_id, str) or not is_json_object(backup_sets):
            raise ValueError("Invalid repository metadata")
        if UUID(stored_id) != client_id:
            raise ValueError("Repository client ID does not match")
        if not Repository.is_backup_mode(mode):
            raise ValueError(f"Unsupported backup mode: {mode!r}")

        repository_deleted = metadata.get("deleted")
        if not isinstance(repository_deleted, bool):
            raise ValueError("Invalid repository deletion state")

        sets: dict[UUID, BackupSet] = {}
        for set_id, entry in backup_sets.items():
            if not isinstance(set_id, str) or not is_json_object(entry):
                raise ValueError("Invalid repository backup set metadata")
            key_id, deleted = entry.get("key_id"), entry.get("deleted")
            name = entry.get("name")
            if not isinstance(key_id, str) or not isinstance(deleted, bool) or (name is not None and not isinstance(name, str)):
                raise ValueError("Invalid repository backup set metadata")
            backup_set_id = UUID(set_id)
            if backup_set_id in sets:
                raise ValueError(f"Duplicate backup set ID: {backup_set_id}")
            sets[backup_set_id] = BackupSet(backup_set_id, UUID(key_id), deleted=deleted, name=name)
        if repository_deleted and any(not backup_set.deleted for backup_set in sets.values()):
            raise InconsistentStateError("Deleted repository contains active backup sets.")
        return Repository(path, client_id, sets, mode=mode, deleted=repository_deleted)

    def _write_metadata(self, repository: Repository, lock: RepositoryLock) -> None:
        sets: dict[str, dict[str, str | bool]] = {}
        for backup_set in repository.backup_sets.values():
            entry: dict[str, str | bool] = {"key_id": str(backup_set.key_id), "deleted": backup_set.deleted}
            if backup_set.name is not None:
                entry["name"] = backup_set.name
            sets[str(backup_set.backup_set_id)] = entry
        write_json(repository.path / _METADATA_NAME, lock, {"version": _METADATA_VERSION, "client_id": str(repository.client_id), "mode": repository.mode, "deleted": repository.deleted, "backup_sets": sets})
