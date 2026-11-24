"""Server operations scoped to a verified backup client."""

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from p8472backup.server.repository import BackupMode, Repository
from p8472backup.server.repository_admin_service import RepositoryAdminService
from p8472backup.server.snapshot_service import DEFAULT_PROTECTION_DURATION


class ServerService:
    """Provide the server facade for operations on client repositories."""

    def __init__(self, storage_root: Path, *, protection_duration: timedelta = DEFAULT_PROTECTION_DURATION) -> None:
        if protection_duration < timedelta():
            raise ValueError("Snapshot protection duration must be nonnegative")
        self._storage_root = storage_root.resolve(strict=True)
        self._protection_duration = protection_duration

    def init_repository(self, client_id: UUID, backup_sets: Mapping[UUID, UUID], *, mode: BackupMode = "pull") -> Repository:
        """Register the initial backup sets and return the repository."""
        return RepositoryAdminService().init_repository(self._storage_root / str(client_id), client_id, backup_sets.items(), mode=mode)

    def add_backup_set(self, client_id: UUID, backup_set_id: UUID, key_id: UUID) -> None:
        """Register a backup set for a verified client."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id) as (repository, lock):
            service.add_backup_set(repository, backup_set_id, key_id, lock)

    def remove_backup_set(self, client_id: UUID, backup_set_id: UUID) -> None:
        """Retire a registered set for a verified client."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id) as (repository, lock):
            service.remove_backup_set(repository, backup_set_id, lock, protection_duration=self._protection_duration)
