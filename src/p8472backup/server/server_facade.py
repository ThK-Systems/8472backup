"""Server operations scoped to a verified backup client."""

from collections.abc import Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from p8472backup.archive.objects import Object
from p8472backup.common.error import InconsistentStateError
from p8472backup.common.log import Log
from p8472backup.server.repository import BackupMode, Repository
from p8472backup.server.repository_admin_service import RepositoryAdminService
from p8472backup.server.retention.policy import parse_prune_rule
from p8472backup.server.snapshot import Snapshot
from p8472backup.server.snapshot_listing import SnapshotListing
from p8472backup.server.snapshot_service import DEFAULT_PROTECTION_DURATION, SnapshotService, backup_temporary_prefix


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
        try:
            return RepositoryAdminService().init_repository(self._storage_root / str(client_id), client_id, backup_sets.items(), mode=mode, backup_set_names=backup_set_names)
        except (OSError, ValueError, InconsistentStateError) as error:
            Log.warning("Operation init_repository failed client_id=%s reason=%s", client_id, type(error).__name__)
            raise

    def add_backup_set(self, client_id: UUID, backup_set_id: UUID, key_id: UUID, *, name: str | None = None) -> None:
        """Register a backup set for a verified client."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id, operation="add_backup_set", backup_set_id=backup_set_id) as (repository, lock):
            service.add_backup_set(repository, backup_set_id, key_id, lock, name=name)

    def remove_backup_set(self, client_id: UUID, backup_set_id: UUID) -> None:
        """Retire a registered set for a verified client."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id, operation="remove_backup_set", backup_set_id=backup_set_id) as (repository, lock):
            backup_set = repository.get_backup_set(backup_set_id)
            service.remove_backup_set(repository, backup_set, lock, protection_duration=self._protection_duration)

    def remove_repository(self, client_id: UUID) -> None:
        """Retire the verified client's repository."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id, operation="remove_repository") as (repository, lock):
            service.remove_repository(repository, lock, protection_duration=self._protection_duration)

    def set_mode(self, client_id: UUID, mode: BackupMode) -> None:
        """Set the backup mode for all sets of the verified client."""
        service = RepositoryAdminService()
        with service.locked_repository(self._storage_root, client_id, operation="set_mode") as (repository, lock):
            service.set_mode(repository, mode, lock)

    def backup(self, client_id: UUID, backup_set_id: UUID, records: Iterator[tuple[Object, BinaryIO]], *, prune: str, comment: str | None = None, auto_cleanup: bool = False) -> Snapshot:
        """Commit client-supplied records through the final snapshot root and prune."""
        repo_admin_service = RepositoryAdminService()
        snapshots = SnapshotService()
        snapshot_id: UUID | None = None
        try:
            with repo_admin_service.locked_repository(self._storage_root, client_id) as (repository, lock):
                backup_set = repository.get_active_backup_set(backup_set_id)
                snapshots.validate_comment(comment)
                # Reject invalid rules before reserving a run marker.
                parse_prune_rule(prune)
                with snapshots.backup_run(repository, backup_set, lock, auto_cleanup=auto_cleanup) as snapshot_id:
                    repo_admin_service.save_prune_rule(repository, backup_set, lock, prune, temporary_prefix=backup_temporary_prefix(snapshot_id))
                    snapshot = snapshots.create_snapshot(repository, backup_set_id, records, lock=lock, snapshot_id=snapshot_id, protection_duration=self._protection_duration, comment=comment)
                try:
                    snapshots.prune(repository, backup_set, lock, prune_rule=prune, protection_duration=self._protection_duration)
                except Exception as error:
                    error.add_note(f"Snapshot {snapshot.snapshot_id} committed before prune failed.")
                    raise
                return snapshot
        except (OSError, ValueError, InconsistentStateError) as error:
            scope = f"client_id={client_id} backup_set_id={backup_set_id}"
            if snapshot_id is not None:
                scope += f" snapshot_id={snapshot_id}"
            Log.warning("Operation backup failed %s reason=%s", scope, type(error).__name__)
            raise

    def cleanup(self, client_id: UUID, backup_set_id: UUID) -> int:
        """Clean up stopped backup runs for the selected set."""
        with RepositoryAdminService().locked_repository(self._storage_root, client_id, operation="cleanup", backup_set_id=backup_set_id) as (repository, lock):
            return SnapshotService().cleanup(repository, repository.get_backup_set(backup_set_id), lock)

    def prune(self, client_id: UUID, backup_set_id: UUID) -> tuple[UUID, ...]:
        """Prune one backup set using its stored rule and return the removed snapshot IDs."""
        repo_admin_service = RepositoryAdminService()
        with repo_admin_service.locked_repository(self._storage_root, client_id, operation="prune", backup_set_id=backup_set_id) as (repository, lock):
            backup_set = repository.get_active_backup_set(backup_set_id)
            rule = repo_admin_service.read_prune_rule(repository, backup_set, lock)
            return SnapshotService().prune(repository, backup_set, lock, prune_rule=rule, protection_duration=self._protection_duration)

    def list(self, client_id: UUID, *, backup_set_id: UUID | None = None) -> SnapshotListing:
        """List published snapshots for the verified client."""
        with RepositoryAdminService().locked_repository(self._storage_root, client_id, operation="list", backup_set_id=backup_set_id) as (repository, lock):
            groups = SnapshotService().list_snapshot_entries(repository, lock, backup_set_id=backup_set_id, protection_duration=self._protection_duration)
            return SnapshotListing(f"{self._server_fqdn}:8472backup/{repository.client_id}", groups)

    def check(self, client_id: UUID) -> None:
        """Verify repository consistency under the usual exclusive lock."""
        try:
            with RepositoryAdminService().locked_repository(self._storage_root, client_id, operation="check") as (repository, lock):
                SnapshotService().check(repository, lock)
        except ValueError as error:
            raise InconsistentStateError(f"repository.json: {error}") from error

    @staticmethod
    def version() -> str:
        """Return the running server's version string."""
        from p8472backup.server.main import VERSION

        return VERSION
