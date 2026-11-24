"""Server operations scoped to a verified backup client."""

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from p8472backup.server.repository import BackupMode, Repository
from p8472backup.server.repository_service import RepositoryService


class ServerService:
    """Provide the server facade for operations on client repositories."""

    def __init__(self, storage_root: Path) -> None:
        self._storage_root = storage_root.resolve(strict=True)

    def init_repository(self, client_id: UUID, backup_sets: Mapping[UUID, UUID], *, mode: BackupMode = "pull") -> Repository:
        """Register the initial backup sets and return the first set's repository."""
        return RepositoryService.init_repository_with_sets(self._storage_root / str(client_id), client_id, backup_sets.items(), mode=mode)

    def add_backup_set(self, client_id: UUID, backup_set_id: UUID, key_id: UUID) -> None:
        """Register a backup set for a verified client."""
        repository = RepositoryService.find_by_client_id(self._storage_root, client_id)
        RepositoryService().add_backup_set(repository, backup_set_id, key_id)
