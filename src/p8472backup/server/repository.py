"""Filesystem paths for one client repository."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID


type BackupMode = Literal["pull", "push"]


@dataclass(frozen=True)
class Repository:
    """Identify a client repository, backup set, and key domain."""

    path: Path
    client_id: UUID
    backup_set_id: UUID
    key_id: UUID
    mode: BackupMode = field(default="pull", compare=False)

    def __post_init__(self) -> None:
        self.set_mode(self.mode)
        object.__setattr__(self, "path", self.path.resolve())

    def set_mode(self, mode: BackupMode) -> None:
        """Set the active backup mode for this repository."""
        if mode not in ("pull", "push"):
            raise ValueError(f"Unsupported backup mode: {mode!r}")
        object.__setattr__(self, "mode", mode)

    @property
    def index_file(self) -> Path:
        """Return the location index file for this key domain."""
        return self.path / "data" / "index" / str(self.key_id)

    @property
    def packs_directory(self) -> Path:
        """Return the directory containing packs for this key domain."""
        return self.path / "data" / "packs" / str(self.key_id)

    @property
    def snapshots_directory(self) -> Path:
        """Return the directory containing snapshots for this backup set."""
        return self.path / "snapshots" / str(self.backup_set_id)

    def snapshot_file(self, snapshot_id: UUID) -> Path:
        """Return the file for the given snapshot in this backup set."""
        return self.snapshots_directory / f"{snapshot_id}.snapshot"
