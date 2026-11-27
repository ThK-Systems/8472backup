import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import TYPE_SNAPSHOT, Object
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import metadata_recovery, trusted_time
from p8472backup.server.repository import Repository
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.snapshot import Snapshot
from p8472backup.server.snapshot_service import SNAPSHOT_REGISTRY_NAME
from p8472backup.server.trusted_time import TrustedTime


_CLIENT = UUID(int=1)
_SET = UUID(int=2)
_KEY = UUID(int=3)
_ROOT = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
_OTHER_ROOT = Object(TYPE_SNAPSHOT, b"s" * 32, 1)
_START = datetime(2026, 1, 1, tzinfo=UTC)


def _backup(facade: ServerFacade, root: Object = _ROOT, *, prune: str = "") -> Snapshot:
    return facade.backup(_CLIENT, _SET, iter(((root, BytesIO(b"r")),)), prune=prune)


def _copy_directory() -> Path:
    (directory,) = (metadata_recovery.DEFAULT_STATE_DIRECTORY / "recovery" / str(_CLIENT)).iterdir()
    return directory


@pytest.fixture
def backed_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ServerFacade, Repository, Snapshot, Mock]:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = _START
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    repo = facade.init_repository(_CLIENT, {_SET: _KEY})
    return facade, repo, _backup(facade), clock


@pytest.mark.parametrize("damage", ("missing-register", "stale-register", "expired-metadata", "damaged-catalogue", "missing-directory"))
def test_repair_restores_verified_protected_metadata(backed_up: tuple[ServerFacade, Repository, Snapshot, Mock], damage: str) -> None:
    facade, repo, snapshot, _ = backed_up
    register = repo.path / SNAPSHOT_REGISTRY_NAME
    metadata = repo.snapshot_file(_SET, snapshot.snapshot_id)
    catalogue = metadata.with_suffix(".catalog")
    original = tuple(path.read_bytes() for path in (register, metadata, catalogue))
    if damage == "missing-register":
        register.unlink()
    elif damage == "stale-register":
        register.write_text('{"version":1,"snapshots":{}}', encoding="utf-8")
    elif damage == "expired-metadata":
        record: dict[str, object] = json.loads(metadata.read_text(encoding="utf-8"))
        record["protected_until"] = snapshot.committed_at.isoformat()
        metadata.write_text(json.dumps(record), encoding="utf-8")
    elif damage == "damaged-catalogue":
        catalogue.write_bytes(b"broken")
    else:
        shutil.rmtree(metadata.parent)

    with pytest.raises(InconsistentStateError):
        facade.check(_CLIENT)
    assert facade.repair_snapshot_metadata(_CLIENT) == (2 if damage == "missing-directory" else 1)
    assert tuple(path.read_bytes() for path in (register, metadata, catalogue)) == original
    assert facade.repair_snapshot_metadata(_CLIENT) == 0
    facade.check(_CLIENT)
    listing = facade.list(_CLIENT).backup_sets[0].snapshots[0]
    assert listing.snapshot_id == snapshot.snapshot_id and listing.protected
    with facade.restore(_CLIENT, _SET, snapshot.snapshot_id) as reader:
        output = BytesIO()
        assert reader.read_object(snapshot.root_id, output) == _ROOT
        assert output.getvalue() == b"r"


@pytest.mark.parametrize("damage", ("generation", "snapshot-json", "catalogue", "pack"))
def test_repair_rejects_a_damaged_copy_or_missing_objects(backed_up: tuple[ServerFacade, Repository, Snapshot, Mock], damage: str) -> None:
    facade, repo, snapshot, _ = backed_up
    register = repo.path / SNAPSHOT_REGISTRY_NAME
    metadata = repo.snapshot_file(_SET, snapshot.snapshot_id)
    copied_metadata = _copy_directory() / metadata.relative_to(repo.path)
    catalogue = metadata.with_suffix(".catalog")
    register.unlink()
    catalogue.unlink()
    if damage == "generation":
        confirmed = _copy_directory() / "confirmed.json"
        data: dict[str, object] = json.loads(confirmed.read_text(encoding="utf-8"))
        data["generation"] = 42
        confirmed.write_text(json.dumps(data), encoding="utf-8")
    elif damage == "snapshot-json":
        copied_metadata.write_bytes(copied_metadata.read_bytes() + b"\n")
    elif damage == "catalogue":
        copied_metadata.with_suffix(".catalog").write_bytes(bytes(32))
    else:
        (repo.packs_root / str(_KEY) / "1.pack").unlink()

    with pytest.raises(InconsistentStateError):
        facade.repair_snapshot_metadata(_CLIENT)
    assert not register.exists()
    assert not catalogue.exists()


@pytest.mark.parametrize("phase", ("before-primary", "after-primary", "after-confirmation"))
def test_repair_recovers_the_correct_register_after_interrupted_publication(backed_up: tuple[ServerFacade, Repository, Snapshot, Mock], phase: str) -> None:
    facade, repo, old, _ = backed_up
    register = repo.path / SNAPSHOT_REGISTRY_NAME
    confirmed = _copy_directory() / "confirmed.json"
    prepared = confirmed.with_name("prepared.json")
    replace = os.replace

    def interrupt(source: str | Path, destination: str | Path) -> None:
        target = Path(destination)
        stop_at = confirmed if phase == "after-confirmation" else register
        if target != stop_at or phase != "before-primary":
            replace(source, destination)
        if target == stop_at:
            raise OSError("Commit interrupted")

    with patch.object(os, "replace", interrupt), pytest.raises(OSError, match="Commit interrupted"):
        _backup(facade, _OTHER_ROOT)
    assert prepared.is_file()

    if phase == "after-confirmation":
        register.unlink()
    else:
        published = register.read_bytes()
        register.unlink()
        with pytest.raises(InconsistentStateError, match="determine the published snapshot register generation"):
            facade.repair_snapshot_metadata(_CLIENT)
        register.write_bytes(published)

    facade.repair_snapshot_metadata(_CLIENT)
    assert not prepared.exists()
    assert len(facade.list(_CLIENT).backup_sets[0].snapshots) == (1 if phase == "before-primary" else 2)
    assert any(entry.snapshot_id == old.snapshot_id for entry in facade.list(_CLIENT).backup_sets[0].snapshots)


def test_repair_preserves_prune_and_does_not_resurrect_deleted_snapshots(backed_up: tuple[ServerFacade, Repository, Snapshot, Mock]) -> None:
    facade, repo, old, clock = backed_up
    clock.now.return_value = _START + timedelta(days=8)
    current = _backup(facade, _OTHER_ROOT, prune="1d")
    old_file = repo.snapshot_file(_SET, old.snapshot_id)
    assert not (_copy_directory() / old_file.relative_to(repo.path)).exists()
    (repo.path / SNAPSHOT_REGISTRY_NAME).unlink()

    assert facade.repair_snapshot_metadata(_CLIENT) == 1

    snapshots = facade.list(_CLIENT).backup_sets[0].snapshots
    assert len(snapshots) == 1 and snapshots[0].snapshot_id == current.snapshot_id
    assert not old_file.exists()


def test_existing_repository_builds_its_first_recovery_generation(backed_up: tuple[ServerFacade, Repository, Snapshot, Mock]) -> None:
    facade, repo, old, _ = backed_up
    shutil.rmtree(_copy_directory())
    (repo.path / ".metadata-recovery").unlink()

    new = _backup(facade, _OTHER_ROOT)

    (repo.path / SNAPSHOT_REGISTRY_NAME).unlink()
    assert facade.repair_snapshot_metadata(_CLIENT) == 1
    snapshots = RepositoryService.find_by_client_id(repo.path.parent, _CLIENT).snapshots
    assert {snapshot.snapshot_id for snapshot in snapshots} == {old.snapshot_id, new.snapshot_id}


def test_initializing_a_copy_refuses_to_omit_old_snapshot_metadata(backed_up: tuple[ServerFacade, Repository, Snapshot, Mock]) -> None:
    facade, repo, old, _ = backed_up
    shutil.rmtree(_copy_directory())
    identity = repo.path / ".metadata-recovery"
    identity.unlink()
    register = repo.path / SNAPSHOT_REGISTRY_NAME
    register.write_text('{"version":1,"snapshots":{}}', encoding="utf-8")
    path = repo.snapshot_file(_SET, old.snapshot_id)
    original = path.read_bytes(), path.with_suffix(".catalog").read_bytes()

    with pytest.raises(InconsistentStateError, match="Unregistered snapshot metadata"):
        _backup(facade, _OTHER_ROOT)

    assert not identity.exists()
    assert (path.read_bytes(), path.with_suffix(".catalog").read_bytes()) == original
