"""Initialize server-side repositories and their storage scopes."""

import os
from pathlib import Path
from uuid import UUID

from p8472backup.server.repository import Repository
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.lock import RepositoryLock


class RepositoryService:
    """Manage storage for one client repository."""

    @classmethod
    def init_repository(cls, path: Path, client_id: UUID, backup_set_id: UUID, key_id: UUID) -> Repository:
        """Create a repository at a new path below an existing parent directory."""
        path.mkdir(mode=0o700)
        repository = Repository(path, client_id, backup_set_id, key_id)
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

        return repository
