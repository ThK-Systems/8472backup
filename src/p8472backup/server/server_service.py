"""Server operations scoped to a verified backup client."""

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from p8472backup.server.repository import BackupMode
from p8472backup.server.repository_service import RepositoryService


class ServerService:
    """Provide the server facade for operations on client repositories."""

    def __init__(self, storage_root: Path) -> None:
        self._storage_root = storage_root.resolve(strict=True)

    def init_repository(self, client_id: UUID, backup_sets: Mapping[UUID, UUID], *, mode: BackupMode = "pull") -> None:
        """Register all validated set-to-key mappings for a verified client."""
        RepositoryService.init_repository_with_sets(self._storage_root / str(client_id), client_id, backup_sets.items(), mode=mode)
