"""Maintain a durable copy of authoritative snapshot metadata."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4

from p8472backup.common.error import InconsistentStateError
from p8472backup.server.json_file import is_json_object, read_json, read_json_file, sync_directory, write_json, write_json_file
from p8472backup.server.lock import RepositoryLock


DEFAULT_STATE_DIRECTORY = Path("/var/lib/8472backup")
_IDENTITY_FILE = ".metadata-recovery"
_CONFIRMED_FILE = "confirmed.json"
_PREPARED_FILE = "prepared.json"
_COPY_BLOCK_SIZE = 64 * 1024


@dataclass(frozen=True)
class _RegisterGeneration:
    number: int
    register: dict[str, object]

    def record(self) -> dict[str, object]:
        data: dict[str, object] = {"generation": self.number, "register": self.register}
        checksum = sha256()
        for part in json.JSONEncoder(sort_keys=True, separators=(",", ":")).iterencode(data):
            checksum.update(part.encode("utf-8"))
        return {**data, "sha256": checksum.hexdigest()}


class MetadataRecovery:
    """The recovery copy for one repository incarnation under its repository lock."""

    def __init__(self, repository: Path, client_id: UUID, identity: UUID, lock: RepositoryLock) -> None:
        self._repository = repository
        self._lock = lock
        self._root = DEFAULT_STATE_DIRECTORY.resolve()
        self._directory = self._root / "recovery" / str(client_id) / str(identity)

    @classmethod
    def open(cls, repository: Path, client_id: UUID, lock: RepositoryLock, *, detached: bool = False) -> MetadataRecovery | None:
        """Read the identity of the copy associated with this repository."""
        path = repository / _IDENTITY_FILE
        if path.is_symlink():
            raise InconsistentStateError("Unexpected metadata recovery identity path")
        try:
            entry = read_json(path, lock, workspace="recovery identity")
        except FileNotFoundError:
            client_directory = DEFAULT_STATE_DIRECTORY.resolve() / "recovery" / str(client_id)
            if not detached and client_directory.is_dir() and any(client_directory.iterdir()):
                raise InconsistentStateError("Metadata recovery identity is missing") from None
            return None
        if not is_json_object(entry) or not isinstance(identity := entry.get("id"), str):
            raise InconsistentStateError("Invalid metadata recovery identity")
        if entry.get("client_id") != str(client_id):
            raise ValueError("Snapshot identity or scope does not match the requested record")
        return cls(repository, client_id, UUID(identity), lock)

    @classmethod
    def initialize(cls, repository: Path, client_id: UUID, lock: RepositoryLock, register: dict[str, object], snapshots: Iterable[Path] = ()) -> MetadataRecovery:
        """Install a copy before enabling recovery for a new or verified repository."""
        identity = uuid4()
        recovery = cls(repository, client_id, identity, lock)
        directory = recovery._path(recovery._directory)
        for parent in (recovery._root, directory.parent.parent, directory.parent, directory):
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            sync_directory(parent.parent)
        for path in snapshots:
            recovery.save_snapshot(path)
        write_json_file(directory / _CONFIRMED_FILE, _RegisterGeneration(0, register).record())
        write_json(repository / _IDENTITY_FILE, lock, {"client_id": str(client_id), "id": str(identity)})
        return recovery

    def _path(self, path: Path) -> Path:
        self._lock.require_held_for(self._repository / _IDENTITY_FILE)
        if path.resolve() != path:
            raise InconsistentStateError("Unexpected metadata recovery path")
        return path

    def _read_generation(self, name: str) -> _RegisterGeneration:
        entry = read_json_file(self._path(self._directory / name), workspace=f"recovery {name}")
        if not is_json_object(entry):
            raise InconsistentStateError(f"Invalid metadata recovery record: {name}")
        number, data = entry.get("generation"), entry.get("register")
        if type(number) is not int or number < 0 or not is_json_object(data):
            raise InconsistentStateError(f"Invalid metadata recovery record: {name}")
        register: dict[str, object] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                raise InconsistentStateError(f"Invalid metadata recovery record: {name}")
            register[key] = value
        generation = _RegisterGeneration(number, register)
        if entry != generation.record():
            raise InconsistentStateError(f"Metadata recovery checksum mismatch: {name}")
        return generation

    def _generations(self) -> tuple[_RegisterGeneration, _RegisterGeneration | None]:
        confirmed = self._read_generation(_CONFIRMED_FILE)
        try:
            prepared = self._read_generation(_PREPARED_FILE)
        except FileNotFoundError:
            return confirmed, None
        if prepared != confirmed and prepared.number != confirmed.number + 1:
            raise InconsistentStateError("Invalid prepared metadata recovery generation")
        return confirmed, prepared

    def _confirm(self, primary: object, *, persist: bool) -> _RegisterGeneration:
        confirmed, prepared = self._generations()
        if primary == confirmed.register:
            current = confirmed
        elif prepared is not None and primary == prepared.register:
            current = prepared
        else:
            raise InconsistentStateError("Snapshot register disagrees with the metadata recovery copy")
        if persist and prepared is not None:
            # A previously interrupted replacement may only be visible in memory.
            sync_directory(self._repository)
            if current != confirmed:
                write_json_file(self._path(self._directory / _CONFIRMED_FILE), current.record())
            else:
                sync_directory(self._directory)
            self._path(self._directory / _PREPARED_FILE).unlink()
            sync_directory(self._directory)
        return current

    def verify_register(self, primary: object) -> None:
        """Require the current primary register to match a verified generation."""
        self._confirm(primary, persist=False)

    def confirm_register(self, path: Path) -> None:
        """Resolve a prepared generation before reclamation."""
        primary = read_json(path, self._lock, workspace="snapshot register")
        self._confirm(primary, persist=True)

    def publish_register(self, path: Path, register: dict[str, object], *, temporary_prefix: str | None = None) -> None:
        """Prepare the copy, publish the primary, and confirm the new generation."""
        primary = read_json(path, self._lock, workspace="snapshot register")
        current = self._confirm(primary, persist=True)
        generation = _RegisterGeneration(current.number + 1, register).record()
        prepared = self._path(self._directory / _PREPARED_FILE)
        confirmed = self._path(self._directory / _CONFIRMED_FILE)
        write_json_file(prepared, generation)
        write_json(path, self._lock, register, temporary_prefix=temporary_prefix)
        write_json_file(confirmed, generation)
        prepared.unlink()
        sync_directory(self._directory)

    def register_for_repair(self, primary: object) -> dict[str, object]:
        """Choose a known publication or stop when a crash left it ambiguous."""
        confirmed, prepared = self._generations()
        if prepared is None or prepared == confirmed or primary == confirmed.register:
            return confirmed.register
        if primary == prepared.register:
            return prepared.register
        raise InconsistentStateError("Cannot determine the published snapshot register generation")

    def snapshot_file(self, primary: Path) -> Path:
        """Return the corresponding copy of a snapshot metadata file."""
        if self._lock.require_held_for(primary) != primary:
            raise InconsistentStateError("Unexpected snapshot metadata path")
        return self._path(self._directory / primary.relative_to(self._repository))

    def _digest(self, path: Path) -> str:
        with path.open("rb") as source:
            return file_digest(source, "sha256").hexdigest()

    def verify_snapshot(self, primary: Path, *, copy: bool = False) -> None:
        """Check the original or copied snapshot JSON against its stored digest."""
        stored = self.snapshot_file(primary)
        digest = read_json_file(self._path(stored.with_suffix(".sha256")), workspace="recovery snapshot digest")
        if not is_json_object(digest) or digest.get("sha256") != self._digest(stored if copy else primary):
            raise InconsistentStateError(f"Snapshot metadata recovery checksum mismatch: {primary.name}")

    def save_snapshot(self, primary: Path) -> None:
        """Copy the immutable JSON and catalogue before register publication."""
        stored = self.snapshot_file(primary)
        for directory in (stored.parent.parent, stored.parent):
            directory.mkdir(mode=0o700, exist_ok=True)
            sync_directory(directory.parent)
        self._copy_file(self._lock.require_held_for(primary.with_suffix(".catalog")), self._path(stored.with_suffix(".catalog")))
        self._copy_file(primary, stored)
        write_json_file(self._path(stored.with_suffix(".sha256")), {"sha256": self._digest(primary)})

    def remove_snapshot(self, primary: Path) -> None:
        """Remove a snapshot copy after its register deletion is confirmed."""
        stored = self.snapshot_file(primary)
        for path in (stored, stored.with_suffix(".catalog"), stored.with_suffix(".sha256")):
            self._path(path).unlink(missing_ok=True)
        if stored.parent.is_dir():
            sync_directory(stored.parent)

    def restore_file(self, primary: Path) -> None:
        """Atomically install a snapshot file validated by the recovery operation."""
        source = self.snapshot_file(primary)
        for directory in (primary.parent.parent, primary.parent):
            if not directory.is_dir():
                directory.mkdir(mode=0o700)
                sync_directory(directory.parent)
        self._copy_file(source, primary)

    def matches_copy(self, primary: Path) -> bool:
        """Check whether a primary file already matches the verified copy."""
        stored = self.snapshot_file(primary)
        try:
            digest = self._digest(primary)
        except FileNotFoundError:
            return False
        return digest == self._digest(stored)

    def _copy_file(self, source: Path, target: Path) -> None:
        with source.open("rb") as original, NamedTemporaryFile(dir=target.parent, prefix=".metadata-copy-", delete_on_close=False) as output:
            shutil.copyfileobj(original, output, _COPY_BLOCK_SIZE)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.replace(output.name, target)
        sync_directory(target.parent)

    def remove(self) -> None:
        """Remove a detached repository incarnation's recovery copy."""
        directory = self._path(self._directory)
        if directory.exists():
            shutil.rmtree(directory)
        if directory.parent.is_dir():
            sync_directory(directory.parent)
