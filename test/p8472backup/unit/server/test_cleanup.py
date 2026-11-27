import json
import os
import signal
from datetime import UTC, datetime
from io import BytesIO
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import OBJECT_ID_SIZE, TYPE_SNAPSHOT, Object
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository import BackupMode, Repository
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.snapshot import Snapshot
from p8472backup.server.snapshot_service import SNAPSHOT_REGISTRY_NAME, SnapshotService
from p8472backup.storage.file.json_file import sync_directory
from p8472backup.storage.file.lock import RepositoryLock


_CLIENT_ID = UUID(int=1)
_SET_ID = UUID(int=2)
_OTHER_SET_ID = UUID(int=3)
_KEY_ID = UUID(int=4)
_INTERRUPTED_ID = UUID(int=5)
_FQDN = "backup.example.net"
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_ROOT = Object(TYPE_SNAPSHOT, b"r" * OBJECT_ID_SIZE, 1)


@pytest.fixture
def initialized_repository(tmp_path: Path) -> tuple[ServerFacade, Repository]:
    facade = ServerFacade(tmp_path, server_fqdn=_FQDN)
    return facade, facade.init_repository(_CLIENT_ID, {_SET_ID: _KEY_ID, _OTHER_SET_ID: _KEY_ID})


def _backup(facade: ServerFacade, set_id: UUID = _SET_ID, *, auto_cleanup: bool = False, prune: str = "") -> Snapshot:
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _NOW
        return facade.backup(_CLIENT_ID, set_id, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune=prune, auto_cleanup=auto_cleanup)


def _marker(repository: Repository, set_id: UUID = _SET_ID, snapshot_id: UUID = _INTERRUPTED_ID) -> Path:
    return repository.snapshots_root / str(set_id) / f".in-progress-{snapshot_id}"


def _backup_until_killed(storage_root: Path, operation: str, destination: Path, after: bool) -> None:
    publish = os.replace if operation == "replace" else os.link

    def publish_or_kill(source: str | Path, target: str | Path) -> None:
        if Path(target) == destination:
            if after:
                publish(source, target)
            os.kill(os.getpid(), signal.SIGKILL)
        publish(source, target)

    with patch.object(os, operation, publish_or_kill), patch("p8472backup.server.snapshot_service.uuid4", return_value=_INTERRUPTED_ID):
        _backup(ServerFacade(storage_root, server_fqdn=_FQDN))


def _interrupt_backup(repository: Repository, operation: str, destination: Path, *, after: bool = False) -> None:
    process = get_context("spawn").Process(target=_backup_until_killed, args=(repository.path.parent, operation, destination, after))
    process.start()
    try:
        process.join(timeout=10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        process.close()


@pytest.mark.parametrize("mode", ("pull", "push"))
def test_cleanup_on_a_new_repository_is_a_noop(tmp_path: Path, mode: BackupMode) -> None:
    facade = ServerFacade(tmp_path, server_fqdn=_FQDN)
    facade.init_repository(_CLIENT_ID, {_SET_ID: _KEY_ID}, mode=mode)

    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 0


def test_a_successful_backup_retires_its_marker(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    snapshot = _backup(facade)

    assert not _marker(repository, snapshot_id=snapshot.snapshot_id).exists()
    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 0
    facade.check(_CLIENT_ID)


def test_cleanup_requires_a_registered_set(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    marker = _marker(repository)
    marker.touch()

    with pytest.raises(ValueError, match="Backup set is not registered"):
        facade.cleanup(_CLIENT_ID, UUID(int=100))
    assert marker.is_file()


def test_cleanup_fails_immediately_while_the_repository_is_locked(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    marker = _marker(repository)
    marker.touch()

    with RepositoryLock(repository.path), pytest.raises(BlockingIOError):
        facade.cleanup(_CLIENT_ID, _SET_ID)
    assert marker.is_file()
    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1


@pytest.mark.parametrize(
    ("phase", "after"),
    (
        pytest.param("prune_rule", False, id="writing-prune-rule"),
        pytest.param("pack", False, id="writing-pack"),
        pytest.param("index", False, id="writing-index"),
        pytest.param("catalogue", False, id="writing-catalogue"),
        pytest.param("metadata", False, id="writing-snapshot-metadata"),
        pytest.param("register", False, id="before-publication"),
        pytest.param("register", True, id="after-publication"),
    ),
)
def test_cleanup_recovers_a_killed_backup(initialized_repository: tuple[ServerFacade, Repository], phase: str, after: bool) -> None:
    facade, repository = initialized_repository
    metadata = repository.snapshot_file(_SET_ID, _INTERRUPTED_ID)
    catalogue = metadata.with_suffix(".catalog")
    index = repository.index_directory / str(_KEY_ID)
    pack = repository.packs_root / str(_KEY_ID) / "1.pack"
    register = repository.path / SNAPSHOT_REGISTRY_NAME
    prune_rule = repository.backup_set_file(_SET_ID)
    operation, destination = {
        "prune_rule": ("replace", prune_rule),
        "pack": ("link", pack),
        "index": ("replace", index),
        "catalogue": ("link", catalogue),
        "metadata": ("replace", metadata),
        "register": ("replace", register),
    }[phase]
    _interrupt_backup(repository, operation, destination, after=after)
    marker = _marker(repository)
    assert marker.is_file()
    temporary_files = tuple(repository.path.rglob(f".backup-{_INTERRUPTED_ID}-*"))
    if not after:
        assert any(path.parent == destination.parent for path in temporary_files)
    finalized = {path: path.read_bytes() for path in (index, pack, register, prune_rule) if path.exists()}
    if after:
        finalized[metadata] = metadata.read_bytes()
        finalized[catalogue] = catalogue.read_bytes()

    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1
    assert not marker.exists()
    assert all(not path.exists() for path in temporary_files)
    assert metadata.is_file() == catalogue.is_file() == after
    assert {path: path.read_bytes() for path in finalized} == finalized
    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 0
    facade.check(_CLIENT_ID)


def test_cleanup_limits_removal_to_the_selected_set_with_a_shared_index(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    other_run = UUID(int=6)
    pack_directory = repository.packs_root / str(_KEY_ID)
    selected = pack_directory / f".backup-{_INTERRUPTED_ID}-pack"
    other = pack_directory / f".backup-{other_run}-pack"
    unowned = pack_directory / ".pack-unowned"
    selected_marker = _marker(repository)
    other_marker = _marker(repository, _OTHER_SET_ID, other_run)
    unowned_snapshot = repository.snapshot_file(_SET_ID, UUID(int=7))
    for path in (selected, other, unowned, selected_marker, other_marker, unowned_snapshot):
        path.touch()

    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1
    assert not selected.exists()
    assert not selected_marker.exists()
    assert all(path.is_file() for path in (other, other_marker, unowned, unowned_snapshot))
    assert facade.cleanup(_CLIENT_ID, _OTHER_SET_ID) == 1
    assert not other.exists()
    assert unowned.is_file()


def test_cleanup_keeps_a_pack_already_reused_by_another_set(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    register = repository.path / SNAPSHOT_REGISTRY_NAME
    _interrupt_backup(repository, "replace", register)
    pack = repository.packs_root / str(_KEY_ID) / "1.pack"
    contents = pack.read_bytes()
    published = _backup(facade, _OTHER_SET_ID)
    assert list(pack.parent.iterdir()) == [pack]

    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1
    assert pack.read_bytes() == contents
    assert SnapshotService().get_snapshot(repository, _OTHER_SET_ID, published.snapshot_id).object_catalog == published.object_catalog
    facade.check(_CLIENT_ID)


@pytest.mark.parametrize("pending_deletion", (True, False))
def test_cleanup_keeps_registered_catalogues_when_unlinking_temporary_names(initialized_repository: tuple[ServerFacade, Repository], pending_deletion: bool) -> None:
    facade, repository = initialized_repository
    snapshot = _backup(facade)
    marker = _marker(repository, snapshot_id=snapshot.snapshot_id)
    marker.touch()
    metadata = repository.snapshot_file(_SET_ID, snapshot.snapshot_id)
    catalogue = metadata.with_suffix(".catalog")
    temporary = catalogue.with_name(f".backup-{snapshot.snapshot_id}-catalogue")
    os.link(catalogue, temporary)
    register = repository.path / SNAPSHOT_REGISTRY_NAME
    if pending_deletion:
        register.write_text(json.dumps({"version": 1, "snapshots": {}, "pending_deletions": {str(snapshot.snapshot_id): str(_SET_ID)}}), encoding="utf-8")
    files = {path: path.read_bytes() for path in (metadata, catalogue, register)}

    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1
    assert not marker.exists()
    assert not temporary.exists()
    assert {path: path.read_bytes() for path in files} == files


@pytest.mark.parametrize("contents", (None, b"{", b'{"version":2,"snapshots":{}}', b'{"version":1,"snapshots":[]}', b'{"version":1,"snapshots":{},"pending_deletions":42}'))
def test_cleanup_refuses_to_delete_files_with_an_unusable_register(initialized_repository: tuple[ServerFacade, Repository], contents: bytes | None) -> None:
    facade, repository = initialized_repository
    register = repository.path / SNAPSHOT_REGISTRY_NAME
    marker = _marker(repository)
    snapshot_file = repository.snapshot_file(_SET_ID, _INTERRUPTED_ID)
    temporary = repository.path / f".backup-{_INTERRUPTED_ID}-register"
    for path in (marker, snapshot_file, temporary):
        path.touch()
    if contents is None:
        register.unlink()
    else:
        register.write_bytes(contents)

    with pytest.raises(InconsistentStateError, match="snapshot register|Snapshot register"):
        facade.cleanup(_CLIENT_ID, _SET_ID)
    assert all(path.is_file() for path in (marker, snapshot_file, temporary))


def test_cleanup_rejects_a_marker_for_a_snapshot_registered_to_another_set(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    snapshot = _backup(facade, _OTHER_SET_ID)
    marker = _marker(repository, snapshot_id=snapshot.snapshot_id)
    marker.touch()
    temporary = repository.path / f".backup-{snapshot.snapshot_id}-register"
    temporary.touch()

    with pytest.raises(InconsistentStateError, match="snapshot in another set"):
        facade.cleanup(_CLIENT_ID, _SET_ID)
    assert marker.is_file()
    assert temporary.is_file()


def test_cleanup_refuses_a_redirected_snapshot_register(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    snapshot = _backup(facade)
    marker = _marker(repository, snapshot_id=snapshot.snapshot_id)
    marker.touch()
    register = repository.path / SNAPSHOT_REGISTRY_NAME
    register.rename(register.with_suffix(".saved"))
    empty_register = repository.path / "empty.json"
    empty_register.write_text('{"version":1,"snapshots":{}}', encoding="utf-8")
    register.symlink_to(empty_register)

    with pytest.raises(InconsistentStateError, match="Unexpected snapshot register path"):
        facade.cleanup(_CLIENT_ID, _SET_ID)
    assert marker.is_file()
    assert repository.snapshot_file(_SET_ID, snapshot.snapshot_id).is_file()


@pytest.mark.parametrize("name", (".in-progress-invalid", f".in-progress-{_INTERRUPTED_ID.hex}"))
def test_cleanup_refuses_invalid_marker_names(initialized_repository: tuple[ServerFacade, Repository], name: str) -> None:
    facade, repository = initialized_repository
    path = _marker(repository).with_name(name)
    path.touch()

    with pytest.raises(InconsistentStateError, match="Invalid backup run marker"):
        facade.cleanup(_CLIENT_ID, _SET_ID)
    assert path.is_file()


def test_cleanup_refuses_to_follow_a_redirected_set_directory(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    other = _marker(repository, _OTHER_SET_ID)
    other.touch()
    directory = _marker(repository).parent
    directory.rmdir()
    directory.symlink_to(other.parent, target_is_directory=True)

    with pytest.raises(InconsistentStateError, match="Unexpected backup directory"):
        facade.cleanup(_CLIENT_ID, _SET_ID)
    assert other.is_file()


def test_cleanup_can_resume_after_an_unlink_failure(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    marker = _marker(repository)
    metadata = repository.snapshot_file(_SET_ID, _INTERRUPTED_ID)
    catalogue = metadata.with_suffix(".catalog")
    for path in (marker, metadata, catalogue):
        path.touch()
    unlink = Path.unlink

    def unlink_until_catalogue(path: Path, missing_ok: bool = False) -> None:
        if path == catalogue:
            raise PermissionError("Cannot unlink catalogue")
        unlink(path, missing_ok=missing_ok)

    with patch.object(Path, "unlink", unlink_until_catalogue), pytest.raises(PermissionError, match="Cannot unlink catalogue"):
        facade.cleanup(_CLIENT_ID, _SET_ID)
    assert not metadata.exists()
    assert catalogue.exists()
    assert marker.is_file()

    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1
    assert not catalogue.exists()
    assert not marker.exists()


def test_cleanup_syncs_previously_modified_directories_before_removing_the_marker(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    marker = _marker(repository)
    marker.touch()
    pack_directory = repository.packs_root / str(_KEY_ID)
    temporary = pack_directory / f".backup-{_INTERRUPTED_ID}-pack"
    temporary.touch()
    with patch("p8472backup.server.snapshot_service.sync_directory", side_effect=OSError("Directory sync failed")), pytest.raises(OSError, match="Directory sync failed"):
        facade.cleanup(_CLIENT_ID, _SET_ID)
    assert marker.is_file()
    assert not temporary.exists()

    synced: list[Path] = []

    def sync_before_marker_removal(path: Path) -> None:
        if path == pack_directory or path == repository.path:
            assert marker.is_file()
        synced.append(path)
        sync_directory(path)

    with patch("p8472backup.server.snapshot_service.sync_directory", side_effect=sync_before_marker_removal):
        assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1
    assert pack_directory in synced
    assert repository.path in synced
    assert not marker.exists()


def test_invalid_prune_input_does_not_reserve_a_backup_run(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    with pytest.raises(ValueError, match="Invalid prune rule"):
        _backup(facade, prune="invalid")

    assert list(_marker(repository).parent.iterdir()) == []
    assert not repository.backup_set_file(_SET_ID).exists()


@pytest.mark.parametrize("auto_cleanup", (True, False))
def test_backup_applies_automatic_cleanup_only_when_enabled(initialized_repository: tuple[ServerFacade, Repository], auto_cleanup: bool) -> None:
    facade, repository = initialized_repository
    with (
        patch("p8472backup.server.snapshot_service.uuid4", return_value=_INTERRUPTED_ID),
        patch.object(SnapshotService, "_write_registry", side_effect=OSError("Publication failed")),
        pytest.raises(OSError, match="Publication failed"),
    ):
        _backup(facade, auto_cleanup=auto_cleanup, prune="7d")
    marker = _marker(repository)
    metadata = repository.snapshot_file(_SET_ID, _INTERRUPTED_ID)
    catalogue = metadata.with_suffix(".catalog")
    assert marker.is_file() == metadata.is_file() == catalogue.is_file() == (not auto_cleanup)

    if not auto_cleanup:
        with pytest.raises(InconsistentStateError, match="Cleanup required for backup set"):
            _backup(facade, prune="8d")
        assert json.loads(repository.backup_set_file(_SET_ID).read_bytes()) == {"prune": "7d"}
        assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1
    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 0
    _backup(facade)


def test_automatic_cleanup_resumes_a_killed_run_before_the_next_backup(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    _interrupt_backup(repository, "replace", repository.path / SNAPSHOT_REGISTRY_NAME)
    metadata = repository.snapshot_file(_SET_ID, _INTERRUPTED_ID)
    assert metadata.is_file()

    snapshot = _backup(facade, auto_cleanup=True)
    assert not metadata.exists()
    assert not metadata.with_suffix(".catalog").exists()
    assert not _marker(repository).exists()
    assert repository.snapshot_file(_SET_ID, snapshot.snapshot_id).is_file()
    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 0


def test_failed_automatic_cleanup_preserves_the_backup_error(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    with (
        patch("p8472backup.server.snapshot_service.uuid4", return_value=_INTERRUPTED_ID),
        patch.object(SnapshotService, "_write_registry", side_effect=OSError("Publication failed")),
        patch.object(SnapshotService, "cleanup", side_effect=PermissionError("Cleanup denied")),
        pytest.raises(OSError, match="Publication failed") as failure,
    ):
        _backup(facade, auto_cleanup=True)

    assert failure.value.__notes__ == ["Automatic cleanup failed: Cleanup denied"]
    assert _marker(repository).is_file()
    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1


def test_marker_removal_failure_reports_the_committed_snapshot(initialized_repository: tuple[ServerFacade, Repository]) -> None:
    facade, repository = initialized_repository
    marker = _marker(repository)
    unlink = Path.unlink

    def unlink_except_marker(path: Path, missing_ok: bool = False) -> None:
        if path == marker:
            raise PermissionError("Cannot unlink run marker")
        unlink(path, missing_ok=missing_ok)

    with (
        patch("p8472backup.server.snapshot_service.uuid4", return_value=_INTERRUPTED_ID),
        patch.object(Path, "unlink", unlink_except_marker),
        pytest.raises(PermissionError, match="Cannot unlink run marker") as failure,
    ):
        _backup(facade)
    assert failure.value.__notes__ == [f"Snapshot {_INTERRUPTED_ID} committed before run marker removal completed."]
    assert marker.is_file()

    assert facade.cleanup(_CLIENT_ID, _SET_ID) == 1
    assert SnapshotService().get_snapshot(repository, _SET_ID, _INTERRUPTED_ID).root_id == _ROOT.object_id
