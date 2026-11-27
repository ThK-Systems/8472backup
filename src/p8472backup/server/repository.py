"""Client repository and backup-set data."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeGuard
from uuid import UUID

from p8472backup.server.snapshot import Snapshot


type BackupMode = Literal["pull", "push"]


@dataclass(frozen=True)
class BackupSet:
    """Describe a registered backup set and its key domain."""

    backup_set_id: UUID
    key_id: UUID
    deleted: bool = False
    name: str | None = None

    def __post_init__(self) -> None:
        if self.name is not None and (not self.name.strip() or not self.name.isprintable()):
            raise ValueError("Backup set name must be a single printable line")


@dataclass(frozen=True)
class Repository:
    """Describe a client repository and all its registered sets."""

    path: Path
    client_id: UUID
    backup_sets: Mapping[UUID, BackupSet] = field(hash=False)
    mode: BackupMode = field(default="pull", compare=False)
    snapshots: frozenset[Snapshot] = field(default_factory=frozenset[Snapshot], hash=False)
    deleted: bool = False

    def __post_init__(self) -> None:
        self.set_mode(self.mode)
        object.__setattr__(self, "path", self.path.resolve())

    def set_mode(self, mode: BackupMode) -> None:
        """Set the active backup mode for this repository."""
        if not self.is_backup_mode(mode):
            raise ValueError(f"Unsupported backup mode: {mode!r}")
        object.__setattr__(self, "mode", mode)

    @staticmethod
    def is_backup_mode(mode: object) -> TypeGuard[BackupMode]:
        """Return whether the value is a supported backup mode."""
        return isinstance(mode, str) and mode in ("pull", "push")

    def get_backup_set(self, backup_set_id: UUID) -> BackupSet:
        """Return the registered set with this ID."""
        backup_set = self.backup_sets.get(backup_set_id)
        if backup_set is None:
            raise ValueError(f"Backup set is not registered in this repository: {backup_set_id}")
        return backup_set

    def get_active_backup_set(self, backup_set_id: UUID) -> BackupSet:
        """Resolve a set that can accept backup or prune operations."""
        if self.deleted:
            raise ValueError(f"Repository is deleted: {self.client_id}")
        backup_set = self.get_backup_set(backup_set_id)
        if backup_set.deleted:
            raise ValueError(f"Backup set is deleted: {backup_set_id}")
        return backup_set

    def backup_set_file(self, backup_set_id: UUID) -> Path:
        """Return the operational metadata file for the selected set."""
        return self.path / "backup-sets" / f"{backup_set_id}.json"

    @property
    def data_directory(self) -> Path:
        """Return the directory containing indexes and packs."""
        return self.path / "data"

    @property
    def index_directory(self) -> Path:
        """Return the directory containing the key-domain indexes."""
        return self.data_directory / "index"

    @property
    def packs_root(self) -> Path:
        """Return the directory containing the key-domain pack directories."""
        return self.data_directory / "packs"

    @property
    def snapshots_root(self) -> Path:
        """Return the directory containing the backup-set snapshot directories."""
        return self.path / "snapshots"

    def snapshot_file(self, backup_set_id: UUID, snapshot_id: UUID) -> Path:
        """Return the metadata file for the snapshot in the selected backup set."""
        return self.snapshots_root / str(backup_set_id) / f"{snapshot_id}.json"
