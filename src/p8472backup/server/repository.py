"""Filesystem paths for one client repository."""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class Repository:
    """Identify a client repository, backup set, and key domain."""

    path: Path
    client_id: UUID
    backup_set_id: UUID
    key_id: UUID

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", self.path.resolve())

    @property
    def index_path(self) -> Path:
        """Return the index checkpoint path for this key domain."""
        return self.path / "index" / str(self.key_id)

    @property
    def pack_directory(self) -> Path:
        """Return the pack directory for this key domain."""
        return self.path / "packs" / str(self.key_id)

    @property
    def snapshot_directory(self) -> Path:
        """Return the snapshot directory for this backup set."""
        return self.path / "snapshots" / str(self.backup_set_id)

    def snapshot_path(self, snapshot_id: UUID) -> Path:
        """Return the file path for a snapshot in this backup set."""
        return self.snapshot_directory / f"{snapshot_id}.snapshot"
