import errno
import os
import shutil
import signal
from datetime import UTC, datetime, timedelta
from fcntl import flock
from io import BytesIO
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, TYPE_DATA, TYPE_SNAPSHOT, Object
from p8472backup.server import metadata_recovery, trusted_time
from p8472backup.server.lock import RepositoryLock
from p8472backup.server.repository import Repository
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.snapshot import Snapshot
from p8472backup.server.snapshot_service import DEFAULT_PROTECTION_DURATION, SnapshotService
from p8472backup.server.trusted_time import TrustedTime, TrustedTimeError
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.packs import HEADER_SIZE as PACK_HEADER_SIZE, Pack


_CLIENT = UUID(int=1)
_SET = UUID(int=2)
_KEY = UUID(int=3)
_OTHER_SET = UUID(int=4)
_OTHER_KEY = UUID(int=5)
_START = datetime(2026, 1, 1, tzinfo=UTC)
_FQDN = "backup.example.net"
_OLD_ROOT = Object(TYPE_SNAPSHOT, b"o" * 32, 3)
_NEW_ROOT = Object(TYPE_SNAPSHOT, b"n" * 32, 3)
# The shared record and pack header occupy exactly three quarters of pack 1.
_SHARED_BODY = bytes(range(3 * (OBJECT_HEADER_SIZE + _OLD_ROOT.length) - OBJECT_HEADER_SIZE - PACK_HEADER_SIZE))
_SHARED = Object(TYPE_DATA, b"s" * 32, len(_SHARED_BODY))


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ServerFacade, Repository, Mock]:
    clock = Mock(spec=TrustedTime)
    clock.now.return_value = _START
    monkeypatch.setattr(trusted_time, "server_clock", clock, raising=False)
    facade = ServerFacade(tmp_path, server_fqdn=_FQDN)
    return facade, facade.init_repository(_CLIENT, {_SET: _KEY}), clock


@pytest.fixture
def pruned(repository: tuple[ServerFacade, Repository, Mock]) -> tuple[ServerFacade, Repository, Mock, Snapshot]:
    facade, repo, clock = repository
    old = facade.backup(_CLIENT, _SET, iter(((_SHARED, BytesIO(_SHARED_BODY)), (_OLD_ROOT, BytesIO(b"old")))), prune="1d")
    clock.now.return_value = _START + timedelta(days=8)
    snapshot = facade.backup(_CLIENT, _SET, iter(((_SHARED, BytesIO(_SHARED_BODY)), (_NEW_ROOT, BytesIO(b"new")))), prune="1d")
    assert not repo.snapshot_file(_SET, old.snapshot_id).exists()
    return facade, repo, clock, snapshot


def _maintain(repo: Repository, *, threshold: int = 25, protection_duration: timedelta = DEFAULT_PROTECTION_DURATION) -> None:
    RepositoryService().maintain_repository(repo.path.parent, repo.client_id, protection_duration=protection_duration, compact_threshold_percent=threshold)


def _assert_restore(facade: ServerFacade, snapshot: Snapshot, obj: Object, contents: bytes) -> None:
    with facade.restore(_CLIENT, snapshot.backup_set_id, snapshot.snapshot_id) as reader:
        output = BytesIO()
        assert reader.read_object(obj.object_id, output) == obj
        assert output.getvalue() == contents


@pytest.mark.parametrize(("threshold", "compacted"), ((0, True), (25, True), (26, False), (100, False)))
def test_threshold_is_inclusive_per_pack_and_relocation_preserves_bytes(pruned: tuple[ServerFacade, Repository, Mock, Snapshot], threshold: int, compacted: bool) -> None:
    facade, repo, _, snapshot = pruned
    directory = repo.packs_root / str(_KEY)
    first, second, replacement = (directory / f"{number}.pack" for number in (1, 2, 3))
    before = second.read_bytes(), second.stat().st_ino
    metadata = repo.snapshot_file(_SET, snapshot.snapshot_id)
    catalogue = metadata.with_suffix(".catalog")
    records_before = metadata.read_bytes(), catalogue.read_bytes()
    index_path = repo.index_directory / str(_KEY)

    _maintain(repo, threshold=threshold)

    assert first.is_file() != compacted
    assert replacement.is_file() == compacted
    assert (second.read_bytes(), second.stat().st_ino) == before
    assert (metadata.read_bytes(), catalogue.read_bytes()) == records_before
    with RepositoryLock(repo.path) as lock:
        index = LocationIndex.load_from(index_path, lock)
        location = index.get(_SHARED.object_id)
        assert location is not None and location.pack_id == (3 if compacted else 1)
        assert (index.get(_OLD_ROOT.object_id) is None) == compacted
    _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)
    _assert_restore(facade, snapshot, _NEW_ROOT, b"new")
    facade.check(_CLIENT)
    with patch.object(SnapshotService, "_analyze_key_domain", side_effect=AssertionError("Maintenance must skip an unchanged repository")):
        _maintain(repo, threshold=threshold)


def test_a_hundred_percent_threshold_reclaims_only_wholly_dead_packs(pruned: tuple[ServerFacade, Repository, Mock, Snapshot]) -> None:
    facade, repo, _, snapshot = pruned
    directory = repo.packs_root / str(_KEY)
    Pack.write_to(directory / "3.pack", iter(((_SHARED, BytesIO(_SHARED_BODY)),)))
    _maintain(repo, threshold=100)

    assert sorted(path.name for path in directory.iterdir()) == ["1.pack", "2.pack"]
    _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)


def test_successful_backup_does_not_schedule_an_unchanged_repository(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, _ = repository
    for body, root in ((b"old", _OLD_ROOT), (b"new", _NEW_ROOT)):
        facade.backup(_CLIENT, _SET, iter(((root, BytesIO(body)),)), prune="")
    with patch.object(SnapshotService, "_analyze_key_domain", side_effect=AssertionError("No object has become unreferenced")):
        _maintain(repo)


@pytest.mark.parametrize("auto_cleanup", (False, True))
def test_interrupted_backup_schedules_orphan_pack_reclamation(repository: tuple[ServerFacade, Repository, Mock], auto_cleanup: bool) -> None:
    facade, repo, _ = repository
    service = SnapshotService()
    with RepositoryLock(repo.path) as lock, pytest.raises(ValueError, match="final snapshot root"):
        with service.backup_run(repo, repo.get_backup_set(_SET), lock, auto_cleanup=auto_cleanup) as snapshot_id:
            service.create_snapshot(repo, _SET, iter(((_SHARED, BytesIO(_SHARED_BODY)),)), lock=lock, snapshot_id=snapshot_id, target_size=1)
    pack = repo.packs_root / str(_KEY) / "1.pack"
    assert pack.exists()

    _maintain(repo)

    assert not pack.exists()
    assert facade.cleanup(_CLIENT, _SET) == (0 if auto_cleanup else 1)


def test_corruption_in_a_second_key_domain_stops_compaction_before_any_rewrite(pruned: tuple[ServerFacade, Repository, Mock, Snapshot]) -> None:
    facade, repo, _, snapshot = pruned
    facade.add_backup_set(_CLIENT, _OTHER_SET, _OTHER_KEY)
    other = facade.backup(_CLIENT, _OTHER_SET, iter(((_NEW_ROOT, BytesIO(b"new")),)), prune="")
    other_catalogue = repo.snapshot_file(_OTHER_SET, other.snapshot_id).with_suffix(".catalog")
    original_catalogue = other_catalogue.read_bytes()
    other_catalogue.unlink()
    pack = repo.packs_root / str(_KEY) / "1.pack"
    index = repo.index_directory / str(_KEY)
    before = pack.read_bytes(), index.read_bytes()

    _maintain(repo)

    assert (pack.read_bytes(), index.read_bytes()) == before
    _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)
    other_catalogue.write_bytes(original_catalogue)
    _maintain(repo)
    assert not pack.exists()
    _assert_restore(facade, other, _NEW_ROOT, b"new")


@pytest.mark.parametrize("commit_failure", (False, True), ids=("before-copying", "before-checkpoint"))
def test_compaction_fails_closed_when_trusted_time_is_invalid(pruned: tuple[ServerFacade, Repository, Mock, Snapshot], commit_failure: bool) -> None:
    facade, repo, clock, snapshot = pruned
    now = clock.now.return_value
    error = TrustedTimeError("Trusted time reference expired")
    clock.now.side_effect = [now, error] if commit_failure else error
    pack = repo.packs_root / str(_KEY) / "1.pack"
    index = repo.index_directory / str(_KEY)
    before = pack.read_bytes(), index.read_bytes()

    with pytest.raises(TrustedTimeError, match="reference expired"):
        _maintain(repo)

    assert (pack.read_bytes(), index.read_bytes()) == before
    _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)
    clock.now.side_effect = None
    _maintain(repo)
    assert not pack.exists()


def test_compaction_preserves_protected_objects_in_a_retired_repository(pruned: tuple[ServerFacade, Repository, Mock, Snapshot]) -> None:
    facade, repo, _, snapshot = pruned
    facade.remove_repository(_CLIENT)

    _maintain(repo)

    assert RepositoryService.find_by_client_id(repo.path.parent, _CLIENT).deleted
    assert not (repo.packs_root / str(_KEY) / "1.pack").exists()
    _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)
    _assert_restore(facade, snapshot, _NEW_ROOT, b"new")


def _compact_until_killed(storage_root: Path, checkpoint: Path, phase: str) -> None:
    metadata_recovery.DEFAULT_STATE_DIRECTORY = storage_root / ".server-state"
    trusted_time.server_clock = Mock(spec=TrustedTime)
    with patch.object(trusted_time.server_clock, "now", return_value=_START + timedelta(days=8)):
        operation = "link" if phase == "before-pack" else "replace"
        publish = os.link if operation == "link" else os.replace

        def publish_or_kill(source: str | Path, destination: str | Path) -> None:
            if Path(destination) == checkpoint:
                if phase == "after-index":
                    publish(source, destination)
                os.kill(os.getpid(), signal.SIGKILL)
            publish(source, destination)

        with patch.object(os, operation, publish_or_kill):
            with RepositoryService().locked_repository(storage_root, _CLIENT) as (repository, lock):
                SnapshotService().compact(repository, lock, threshold_percent=25)


@pytest.mark.parametrize("phase", ("before-pack", "before-index", "after-index"))
def test_compaction_resumes_after_a_killed_process(pruned: tuple[ServerFacade, Repository, Mock, Snapshot], phase: str) -> None:
    facade, repo, _, snapshot = pruned
    directory = repo.packs_root / str(_KEY)
    checkpoint = directory / "3.pack" if phase == "before-pack" else repo.index_directory / str(_KEY)
    process = get_context("spawn").Process(target=_compact_until_killed, args=(repo.path.parent, checkpoint, phase))
    process.start()
    try:
        process.join(timeout=10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        process.close()
    assert (directory / "1.pack").is_file()
    _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)

    _maintain(repo)

    assert not (directory / "1.pack").exists()
    assert len(list(directory.glob("*.pack"))) == 2
    assert not tuple(repo.path.rglob(".compact-*"))
    _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)
    facade.check(_CLIENT)


def test_insufficient_space_keeps_the_old_checkpoint_and_packs(pruned: tuple[ServerFacade, Repository, Mock, Snapshot]) -> None:
    facade, repo, _, snapshot = pruned
    directory = repo.packs_root / str(_KEY)
    checkpoint = repo.index_directory / str(_KEY)
    before = {path: path.read_bytes() for path in (*directory.iterdir(), checkpoint)}

    with patch.object(Pack, "write_to", side_effect=OSError(errno.ENOSPC, "Disk full")):
        _maintain(repo)

    assert {path: path.read_bytes() for path in (*directory.iterdir(), checkpoint)} == before
    _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)
    _maintain(repo)
    assert not (directory / "1.pack").exists()


def test_checkpoint_sync_failure_keeps_old_packs_until_a_durable_retry(pruned: tuple[ServerFacade, Repository, Mock, Snapshot]) -> None:
    facade, repo, _, snapshot = pruned
    old_pack = repo.packs_root / str(_KEY) / "1.pack"
    checkpoint = repo.index_directory / str(_KEY)
    original = checkpoint.read_bytes()
    directory_stat = checkpoint.parent.stat()
    fsync = os.fsync

    def fsync_or_fail(fd: int) -> None:
        if os.path.samestat(os.fstat(fd), directory_stat) and checkpoint.read_bytes() != original:
            raise OSError(errno.EIO, "Index directory sync failed")
        fsync(fd)

    with patch.object(os, "fsync", fsync_or_fail):
        _maintain(repo)
        assert checkpoint.read_bytes() != original
        assert old_pack.is_file()
        _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)
        _maintain(repo)
        assert old_pack.is_file()

    _maintain(repo)
    assert not old_pack.exists()
    _assert_restore(facade, snapshot, _SHARED, _SHARED_BODY)


@pytest.mark.parametrize("whole_repository", (False, True), ids=("backup-set", "repository"))
@pytest.mark.parametrize(("stored_days", "configured_days"), ((12, 7), (7, 12)), ids=("stored-deadline", "extended-server-deadline"))
def test_scheduled_retirement_uses_the_later_trusted_deadline(repository: tuple[ServerFacade, Repository, Mock], whole_repository: bool, stored_days: int, configured_days: int) -> None:
    _, repo, clock = repository
    facade = ServerFacade(repo.path.parent, server_fqdn=_FQDN, protection_duration=timedelta(days=stored_days))
    snapshot = facade.backup(_CLIENT, _SET, iter(((_OLD_ROOT, BytesIO(b"old")),)), prune="")
    assert snapshot.protected_until == _START + timedelta(days=stored_days)
    protection_duration = timedelta(days=configured_days)
    facade = ServerFacade(repo.path.parent, server_fqdn=_FQDN, protection_duration=protection_duration)
    clock.now.return_value = _START + timedelta(days=8)
    if whole_repository:
        facade.remove_repository(_CLIENT)
    else:
        facade.remove_backup_set(_CLIENT, _SET)

    clock.now.return_value = _START + timedelta(days=12)
    clock.now.reset_mock()
    _maintain(repo, protection_duration=protection_duration)
    clock.now.assert_called()
    _assert_restore(facade, snapshot, _OLD_ROOT, b"old")
    assert (repo.packs_root / str(_KEY) / "1.pack").is_file()

    clock.now.return_value += timedelta(microseconds=1)
    _maintain(repo, protection_duration=protection_duration)
    assert repo.path.is_dir() != whole_repository
    assert not (repo.packs_root / str(_KEY)).exists()
    assert not (repo.index_directory / str(_KEY)).exists()
    assert not repo.snapshot_file(_SET, snapshot.snapshot_id).parent.exists()
    assert not repo.backup_set_file(_SET).exists()
    if not whole_repository:
        assert RepositoryService.find_by_client_id(repo.path.parent, _CLIENT).backup_sets == {}


def test_facade_protection_default_ignores_environment(repository: tuple[ServerFacade, Repository, Mock], monkeypatch: pytest.MonkeyPatch) -> None:
    _, repo, _ = repository
    monkeypatch.setenv("P8472BACKUP_PROTECTION_SECONDS", "0")
    facade = ServerFacade(repo.path.parent, server_fqdn=_FQDN)

    snapshot = facade.backup(_CLIENT, _SET, iter(((_OLD_ROOT, BytesIO(b"old")),)), prune="")

    assert snapshot.protected_until == _START + timedelta(days=7)


@pytest.mark.parametrize("whole_repository", (False, True))
def test_retirement_cannot_delete_with_an_invalid_trusted_clock(repository: tuple[ServerFacade, Repository, Mock], whole_repository: bool) -> None:
    facade, repo, clock = repository
    snapshot = facade.backup(_CLIENT, _SET, iter(((_OLD_ROOT, BytesIO(b"old")),)), prune="")
    if whole_repository:
        facade.remove_repository(_CLIENT)
    else:
        facade.remove_backup_set(_CLIENT, _SET)
    before = {path: path.read_bytes() for path in repo.path.rglob("*") if path.is_file()}
    clock.now.side_effect = TrustedTimeError("Trusted time reference expired")

    with pytest.raises(TrustedTimeError, match="reference expired"):
        _maintain(repo)

    assert {path: path.read_bytes() for path in repo.path.rglob("*") if path.is_file()} == before
    _assert_restore(facade, snapshot, _OLD_ROOT, b"old")


def test_deleting_a_retired_set_preserves_another_sets_protected_objects(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, clock = repository
    facade.add_backup_set(_CLIENT, _OTHER_SET, _KEY)
    facade.backup(_CLIENT, _SET, iter(((_SHARED, BytesIO(_SHARED_BODY)), (_OLD_ROOT, BytesIO(b"old")))), prune="")
    clock.now.return_value = _START + timedelta(days=8)
    protected = facade.backup(_CLIENT, _OTHER_SET, iter(((_SHARED, BytesIO(_SHARED_BODY)), (_NEW_ROOT, BytesIO(b"new")))), prune="")
    facade.remove_backup_set(_CLIENT, _SET)

    _maintain(repo)

    current = RepositoryService.find_by_client_id(repo.path.parent, _CLIENT)
    assert set(current.backup_sets) == {_OTHER_SET}
    assert not (repo.snapshots_root / str(_SET)).exists()
    assert (repo.packs_root / str(_KEY)).is_dir()
    _assert_restore(facade, protected, _SHARED, _SHARED_BODY)
    _assert_restore(facade, protected, _NEW_ROOT, b"new")


def test_active_mount_holds_the_lock_against_final_deletion(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, clock = repository
    snapshot = facade.backup(_CLIENT, _SET, iter(((_OLD_ROOT, BytesIO(b"old")),)), prune="")
    facade.remove_repository(_CLIENT)
    clock.now.return_value = _START + timedelta(days=8)
    with facade.mount(_CLIENT, _SET, snapshot.snapshot_id) as reader:
        _maintain(repo)
        output = BytesIO()
        assert reader.read_object(reader.root_id, output) == _OLD_ROOT
        assert output.getvalue() == b"old"

    _maintain(repo)
    assert not repo.path.exists()


def test_final_set_deletion_cleans_only_its_own_abandoned_temporary_files(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, _ = repository
    facade.add_backup_set(_CLIENT, _OTHER_SET, _KEY)
    temporary_files: list[Path] = []
    for set_id, run_id in ((_SET, UUID(int=6)), (_OTHER_SET, UUID(int=7))):
        (repo.snapshots_root / str(set_id) / f".in-progress-{run_id}").touch()
        temporary = repo.packs_root / str(_KEY) / f".backup-{run_id}-partial"
        temporary.write_bytes(b"partial pack")
        temporary_files.append(temporary)
    facade.remove_backup_set(_CLIENT, _SET)

    _maintain(repo)

    removed, remaining = temporary_files
    assert not removed.exists()
    assert remaining.read_bytes() == b"partial pack"
    assert facade.cleanup(_CLIENT, _OTHER_SET) == 1


def test_final_set_deletion_retries_after_storage_is_removed_before_metadata_write(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, _ = repository
    facade.remove_backup_set(_CLIENT, _SET)
    with patch.object(RepositoryService, "_write_metadata", side_effect=PermissionError("Metadata read-only")):
        _maintain(repo)
    current = RepositoryService.find_by_client_id(repo.path.parent, _CLIENT)
    assert current.get_backup_set(_SET).deleted
    assert not (repo.snapshots_root / str(_SET)).exists()
    assert not (repo.packs_root / str(_KEY)).exists()

    _maintain(repo)

    assert RepositoryService.find_by_client_id(repo.path.parent, _CLIENT).backup_sets == {}
    facade.add_backup_set(_CLIENT, _SET, _KEY)


def test_final_deletion_refuses_unretired_scopes_and_requires_the_lock(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    _, repo, _ = repository
    service = RepositoryService()
    with RepositoryLock(repo.path) as lock:
        with pytest.raises(ValueError, match="not retired"):
            service.delete(repo, lock, protection_duration=timedelta())
        with pytest.raises(ValueError, match="not deleted"):
            service.delete_backup_set(repo, repo.get_backup_set(_SET), lock, protection_duration=timedelta())
    with pytest.raises(RuntimeError, match="lock is not held"):
        service.delete(repo, lock, protection_duration=timedelta())


def test_final_deletion_refuses_a_redirected_scope(repository: tuple[ServerFacade, Repository, Mock], tmp_path: Path) -> None:
    facade, repo, _ = repository
    facade.remove_backup_set(_CLIENT, _SET)
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "keep").write_bytes(b"important")
    pack_directory = repo.packs_root / str(_KEY)
    pack_directory.rmdir()
    pack_directory.symlink_to(outside, target_is_directory=True)

    _maintain(repo)

    assert (outside / "keep").read_bytes() == b"important"
    assert (repo.snapshots_root / str(_SET)).is_dir()


def test_final_repository_deletion_reports_a_failed_removal_and_can_resume(repository: tuple[ServerFacade, Repository, Mock], caplog: pytest.LogCaptureFixture) -> None:
    facade, repo, _ = repository
    facade.remove_repository(_CLIENT)
    detached = repo.path.with_name(f".deleting-{_CLIENT}")
    remove = shutil.rmtree

    def interrupt_removal(path: Path) -> None:
        if path != detached:
            remove(path)
            return
        (path / "repository.json").unlink()
        raise PermissionError("Removal interrupted")

    with patch("p8472backup.server.repository_service.shutil.rmtree", interrupt_removal):
        _maintain(repo)

    assert not repo.path.exists()
    assert detached.is_dir()
    assert caplog.messages.count(f"Final repository deletion failed client_id={_CLIENT} reason=PermissionError") == 1

    RepositoryService().finish_deletion(repo.path.parent, _CLIENT)
    assert not detached.exists()


def test_final_repository_deletion_refuses_a_detached_path_alias(repository: tuple[ServerFacade, Repository, Mock], caplog: pytest.LogCaptureFixture) -> None:
    _, repo, _ = repository
    detached = repo.path.with_name(f".deleting-{_CLIENT}")
    detached.symlink_to(repo.path, target_is_directory=True)

    RepositoryService().finish_deletion(repo.path.parent, _CLIENT)

    assert detached.is_symlink()
    assert (repo.path / "repository.json").is_file()
    assert caplog.messages.count(f"Final repository deletion failed client_id={_CLIENT} reason=InconsistentStateError") == 1


def test_maintenance_completes_prune_with_a_pending_register_deletion(repository: tuple[ServerFacade, Repository, Mock]) -> None:
    facade, repo, clock = repository
    old = facade.backup(_CLIENT, _SET, iter(((_OLD_ROOT, BytesIO(b"old")),)), prune="1d")
    metadata = repo.snapshot_file(_SET, old.snapshot_id)
    clock.now.return_value = _START + timedelta(days=8)
    unlink = Path.unlink

    def unlink_or_fail(path: Path, missing_ok: bool = False) -> None:
        if path == metadata:
            raise PermissionError("Snapshot metadata read-only")
        unlink(path, missing_ok=missing_ok)

    with patch.object(Path, "unlink", unlink_or_fail), pytest.raises(PermissionError, match="read-only"):
        facade.backup(_CLIENT, _SET, iter(((_NEW_ROOT, BytesIO(b"new")),)), prune="1d")
    assert metadata.exists()

    _maintain(repo)

    assert not metadata.exists()
    assert not metadata.with_suffix(".catalog").exists()
    assert not (repo.packs_root / str(_KEY) / "1.pack").exists()
    (current,) = RepositoryService.find_by_client_id(repo.path.parent, _CLIENT).snapshots
    _assert_restore(facade, current, _NEW_ROOT, b"new")


def test_a_lock_opened_before_repository_detachment_cannot_guard_a_new_repository(tmp_path: Path) -> None:
    path = tmp_path / str(_CLIENT)
    path.mkdir()

    def detach_before_locking(fd: int, operation: int) -> None:
        path.rename(tmp_path / f".deleting-{_CLIENT}")
        path.mkdir()
        (path / ".repository.lock").touch()
        flock(fd, operation)

    with patch("p8472backup.server.lock.flock", detach_before_locking), pytest.raises(BlockingIOError, match="lock changed"), RepositoryLock(path):
        pytest.fail("A lock from the old repository guarded the new path")

    with RepositoryLock(path):
        assert path.is_dir()
