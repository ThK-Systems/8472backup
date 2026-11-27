"""Snapshot metadata for the server facade and its text output."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class SnapshotListEntry:
    """One published snapshot and its protection status at listing time."""

    snapshot_id: UUID
    comment: str | None
    committed_at: datetime
    protected: bool


@dataclass(frozen=True)
class BackupSetListing:
    """Published snapshots belonging to a single backup set."""

    backup_set_id: UUID
    name: str
    snapshots: tuple[SnapshotListEntry, ...]


@dataclass(frozen=True)
class SnapshotListing:
    """Snapshot listing with the public address of its repository."""

    repository_address: str
    backup_sets: tuple[BackupSetListing, ...]

    def __str__(self) -> str:
        lines = [f"# Repository  {self.repository_address}"]
        for backup_set in self.backup_sets:
            lines.append("")
            backup_set_id = str(backup_set.backup_set_id)
            label = f"{backup_set.name} ({backup_set_id})" if backup_set.name != backup_set_id else backup_set_id
            lines.append(f"## Backup set  {label}")
            comments = ["".join(character if character.isprintable() else " " for character in (snapshot.comment or "")) for snapshot in backup_set.snapshots]
            comment_width = max(map(len, comments), default=0)
            header = [f"{'SNAPSHOT ID':36}"]
            if comment_width:
                comment_width = max(comment_width, len("COMMENT"))
                header.append(f"{'COMMENT':{comment_width}}")
            header.extend((f"{'COMMITTED AT (UTC)':25}", "PROTECTED"))
            lines.append("  ".join(header))
            for snapshot, comment in zip(backup_set.snapshots, comments, strict=True):
                fields = [str(snapshot.snapshot_id)]
                if comment_width:
                    fields.append(f"{comment:{comment_width}}")
                committed_at = snapshot.committed_at.astimezone(UTC)
                fields.extend((f"{committed_at:%Y-%m-%d %H:%M:%S} ({_WEEKDAYS[committed_at.weekday()]})", str(snapshot.protected).lower()))
                lines.append("  ".join(fields))
        return "\n".join(lines)
