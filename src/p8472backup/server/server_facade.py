"""Server operations scoped to a verified backup client."""

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from p8472backup.server.repository import BackupMode, Repository
from p8472backup.server.repository_admin_service import RepositoryAdminService
from p8472backup.server.snapshot_listing import SnapshotListing
from p8472backup.server.snapshot_service import DEFAULT_PROTECTION_DURATION, SnapshotService


class ServerFacade:
    """Provide the server facade for operations on client repositories."""

    def __init__(self, storage_root: Path, *, server_fqdn: str, protection_duration: timedelta = DEFAULT_PROTECTION_DURATION) -> None:
        if not server_fqdn or not server_fqdn.isprintable():
            raise ValueError("A server FQDN is required")
        if protection_duration < timedelta():
            raise ValueError("Snapshot protection duration must be nonnegative")
        self._storage_root = storage_root.resolve(strict=True)
        self._protection_duration = protection_duration
        self._server_fqdn = server_fqdn

    def init_repository(self, client_id: UUID, backup_sets: Mapping[UUID, UUID], *, mode: BackupMode = "pull", backup_set_names: Mapping[UUID, str] | None = None) -> Repository:
        """Register the initial backup sets and return the repository."""
        return RepositoryAdminService().init_repository(self._storage_root / str(client_id), client_id, backup_sets.items(), mode=mode, backup_set_names=backup_set_names)

    def add_backup_set(self, client_id: UUID, backup_set_id: UUID, key_id: UUID, *, name: str | None = None) -> None:
        """Register a backup set for a verified client."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id) as (repository, lock):
            service.add_backup_set(repository, backup_set_id, key_id, lock, name=name)

    def remove_backup_set(self, client_id: UUID, backup_set_id: UUID) -> None:
        """Retire a registered set for a verified client."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id) as (repository, lock):
            backup_set = repository.get_backup_set(backup_set_id)
            service.remove_backup_set(repository, backup_set, lock, protection_duration=self._protection_duration)

    def remove_repository(self, client_id: UUID) -> None:
        """Retire the verified client's repository."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id) as (repository, lock):
            service.remove_repository(repository, lock, protection_duration=self._protection_duration)

    def set_mode(self, client_id: UUID, mode: BackupMode) -> None:
        """Set the backup mode for all sets of the verified client."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id) as (repository, lock):
            service.set_mode(repository, mode, lock)

    def list(self, client_id: UUID, *, backup_set_id: UUID | None = None) -> SnapshotListing:
        """List published snapshots for the verified client."""
        with RepositoryAdminService().locked_repository(self._storage_root, client_id) as (repository, lock):
            groups = SnapshotService().list_snapshot_entries(repository, lock, backup_set_id=backup_set_id, protection_duration=self._protection_duration)
            return SnapshotListing(f"{self._server_fqdn}:8472backup/{repository.client_id}", groups)

    @staticmethod
    def version() -> str:
        """Return the running server's version string."""
        from p8472backup.server.main import VERSION

        return VERSION
