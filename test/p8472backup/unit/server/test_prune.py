import json
import os
import signal
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from multiprocessing import get_context
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import TYPE_SNAPSHOT, Object
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository_admin_service import RepositoryAdminService
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.snapshot_service import SnapshotService
from p8472backup.storage.file.lock import RepositoryLock


_CLIENT_ID = UUID(int=1)
_BACKUP_SET_ID = UUID(int=2)
_OTHER_SET_ID = UUID(int=3)
_KEY_ID = UUID(int=4)
_SERVER_FQDN = "backup.example.net"
_ROOT = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
_START = datetime(2026, 1, 1, tzinfo=UTC)


def test_backup_prunes_its_set_after_commit_and_keeps_shared_objects(tmp_path: Path) -> None:
    facade = ServerFacade(tmp_path, server_fqdn=_SERVER_FQDN)
    repository = facade.init_repository(_CLIENT_ID, {_BACKUP_SET_ID: _KEY_ID, _OTHER_SET_ID: _KEY_ID})
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _START
        old = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="")
        other = facade.backup(_CLIENT_ID, _OTHER_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="")
        index_file = repository.index_directory / str(_KEY_ID)
        pack_file = repository.packs_root / str(_KEY_ID) / "1.pack"
        index_contents, pack_contents = index_file.read_bytes(), pack_file.read_bytes()

        clock.now.return_value = _START + timedelta(days=8)
        newest = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="1d")

    old_metadata = repository.snapshot_file(_BACKUP_SET_ID, old.snapshot_id)
    assert not old_metadata.exists()
    assert not old_metadata.with_suffix(".catalog").exists()
    assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).snapshots == {newest, other}
    assert json.loads(repository.backup_set_file(_BACKUP_SET_ID).read_bytes()) == {"prune": "1d"}
    assert json.loads(repository.backup_set_file(_OTHER_SET_ID).read_bytes()) == {"prune": ""}
    assert index_file.read_bytes() == index_contents
    assert pack_file.read_bytes() == pack_contents
    assert list(pack_file.parent.iterdir()) == [pack_file]


@pytest.mark.parametrize(
    ("root_id", "comment", "error"),
    (
        (_ROOT.object_id, "x" * 41, "Snapshot comment must be at most 40 characters"),
        (b"short", None, "Object ID must contain exactly 32 bytes"),
    ),
)
def test_invalid_backup_arguments_preserve_the_saved_prune_rule(tmp_path: Path, root_id: bytes, comment: str | None, error: str) -> None:
    facade = ServerFacade(tmp_path, server_fqdn=_SERVER_FQDN)
    repository = facade.init_repository(_CLIENT_ID, {_BACKUP_SET_ID: _KEY_ID})
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _START
        snapshot = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="7d")

    with pytest.raises(ValueError, match=error):
        facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(()), root_id, prune="1d", comment=comment)

    assert json.loads(repository.backup_set_file(_BACKUP_SET_ID).read_bytes()) == {"prune": "7d"}
    assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).snapshots == {snapshot}


def test_prune_preserves_an_older_snapshot_if_the_retained_catalogue_is_missing(tmp_path: Path) -> None:
    facade = ServerFacade(tmp_path, server_fqdn=_SERVER_FQDN)
    repository = facade.init_repository(_CLIENT_ID, {_BACKUP_SET_ID: _KEY_ID})
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _START
        old = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="")
        clock.now.return_value = _START + timedelta(days=1)
        newest = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="1d")
        old_metadata = repository.snapshot_file(_BACKUP_SET_ID, old.snapshot_id)
        repository.snapshot_file(_BACKUP_SET_ID, newest.snapshot_id).with_suffix(".catalog").unlink()
        register = repository.path / "snapshot-registry.json"
        original = register.read_bytes()

        clock.now.return_value = _START + timedelta(days=9)
        with pytest.raises(InconsistentStateError, match="Retained snapshot catalogue is missing"):
            facade.prune(_CLIENT_ID, _BACKUP_SET_ID)

    assert register.read_bytes() == original
    assert old_metadata.is_file()
    assert old_metadata.with_suffix(".catalog").is_file()


def test_prune_uses_the_latest_valid_rule_and_honors_protection_at_the_deadline(tmp_path: Path) -> None:
    facade = ServerFacade(tmp_path, server_fqdn=_SERVER_FQDN)
    repository = facade.init_repository(_CLIENT_ID, {_BACKUP_SET_ID: _KEY_ID})
    with pytest.raises(ValueError, match="No prune rule has been stored"):
        facade.prune(_CLIENT_ID, _BACKUP_SET_ID)

    def interrupted_upload() -> Iterator[tuple[Object, BinaryIO]]:
        yield _ROOT, BytesIO(b"r")
        raise OSError("Upload interrupted")

    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _START
        old = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="")
        clock.now.return_value = _START + timedelta(days=1)
        newest = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="")
        clock.now.return_value = _START + timedelta(days=10)
        with pytest.raises(OSError, match="Upload interrupted"):
            facade.backup(_CLIENT_ID, _BACKUP_SET_ID, interrupted_upload(), _ROOT.object_id, prune="1d")
        with pytest.raises(ValueError, match="Prune rule requires one positive count"):
            facade.backup(_CLIENT_ID, _BACKUP_SET_ID, interrupted_upload(), _ROOT.object_id, prune="1d2d")

        assert json.loads(repository.backup_set_file(_BACKUP_SET_ID).read_bytes()) == {"prune": "1d"}
        assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).snapshots == {old, newest}
        configured = ServerFacade(tmp_path, server_fqdn=_SERVER_FQDN, protection_duration=timedelta(days=10))
        with RepositoryLock(repository.path), pytest.raises(BlockingIOError):
            configured.prune(_CLIENT_ID, _BACKUP_SET_ID)
        assert configured.prune(_CLIENT_ID, _BACKUP_SET_ID) == ()

        clock.now.return_value = _START + timedelta(days=10, microseconds=1)
        with patch.object(SnapshotService, "_read_catalogue", side_effect=AssertionError("Prune must use snapshot metadata")):
            assert configured.prune(_CLIENT_ID, _BACKUP_SET_ID) == (old.snapshot_id,)
            assert configured.prune(_CLIENT_ID, _BACKUP_SET_ID) == ()
    assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).snapshots == {newest}


@pytest.mark.parametrize(
    ("stored_rule", "error"),
    (
        pytest.param('{"prune": "1d2d"}', "Prune rule requires one positive count", id="duplicate-unit"),
        pytest.param('{"prune": 7}', "Invalid prune rule for backup set", id="wrong-type"),
    ),
)
def test_prune_rejects_an_invalid_stored_rule_before_changing_snapshots(tmp_path: Path, stored_rule: str, error: str) -> None:
    facade = ServerFacade(tmp_path, server_fqdn=_SERVER_FQDN)
    repository = facade.init_repository(_CLIENT_ID, {_BACKUP_SET_ID: _KEY_ID})
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _START
        old = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="")
        clock.now.return_value = _START + timedelta(days=1)
        newest = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="1d")

        old_metadata = repository.snapshot_file(_BACKUP_SET_ID, old.snapshot_id)
        new_metadata = repository.snapshot_file(_BACKUP_SET_ID, newest.snapshot_id)
        files = (repository.path / "snapshot-registry.json", old_metadata, old_metadata.with_suffix(".catalog"), new_metadata, new_metadata.with_suffix(".catalog"))
        contents = [path.read_bytes() for path in files]
        repository.backup_set_file(_BACKUP_SET_ID).write_text(stored_rule, encoding="utf-8")
        clock.now.return_value = _START + timedelta(days=8)

        with pytest.raises(ValueError, match=error):
            facade.prune(_CLIENT_ID, _BACKUP_SET_ID)

    assert [path.read_bytes() for path in files] == contents


def test_prune_resumes_a_partial_deletion_after_the_next_backup(tmp_path: Path) -> None:
    facade = ServerFacade(tmp_path, server_fqdn=_SERVER_FQDN, protection_duration=timedelta())
    repository = facade.init_repository(_CLIENT_ID, {_BACKUP_SET_ID: _KEY_ID, _OTHER_SET_ID: _KEY_ID})
    unlink = Path.unlink

    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _START
        old = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="")
        metadata = repository.snapshot_file(_BACKUP_SET_ID, old.snapshot_id)
        catalogue = metadata.with_suffix(".catalog")

        def fail_catalogue_unlink(path: Path, missing_ok: bool = False) -> None:
            if path == catalogue:
                raise PermissionError("Catalogue unlink failed")
            unlink(path, missing_ok=missing_ok)

        clock.now.return_value = _START + timedelta(days=1)
        with patch.object(Path, "unlink", fail_catalogue_unlink), pytest.raises(PermissionError, match="Catalogue unlink failed") as failure:
            facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="1d")

        (newest,) = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).snapshots
        assert failure.value.__notes__ == [f"Snapshot {newest.snapshot_id} committed before prune failed."]
        assert not metadata.exists()
        assert catalogue.is_file()

        other = facade.backup(_CLIENT_ID, _OTHER_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="")
        assert catalogue.is_file()
        assert facade.prune(_CLIENT_ID, _BACKUP_SET_ID) == (old.snapshot_id,)

    assert not catalogue.exists()
    assert "pending_deletions" not in json.loads((repository.path / "snapshot-registry.json").read_bytes())
    assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).snapshots == {newest, other}


def _prune_until_killed(storage_root: Path, now: datetime, register: Path, replace_number: int | None, unlink_target: Path | None) -> None:
    replace, unlink = os.replace, Path.unlink
    replacements = 0

    def replace_or_stop(source: str, destination: Path) -> None:
        nonlocal replacements
        if destination == register:
            replacements += 1
            if replacements == replace_number:
                os.kill(os.getpid(), signal.SIGKILL)
        replace(source, destination)

    def unlink_or_stop(path: Path, missing_ok: bool = False) -> None:
        if path == unlink_target:
            os.kill(os.getpid(), signal.SIGKILL)
        unlink(path, missing_ok=missing_ok)

    with patch.object(trusted_time, "server_clock", create=True) as clock, patch.object(os, "replace", replace_or_stop), patch.object(Path, "unlink", unlink_or_stop):
        clock.now.return_value = now
        ServerFacade(storage_root, server_fqdn=_SERVER_FQDN).prune(_CLIENT_ID, _BACKUP_SET_ID)


@pytest.mark.parametrize(
    ("replace_number", "unlink_suffix", "published", "remaining_suffixes"),
    (
        pytest.param(1, None, True, (".json", ".catalog"), id="before-unpublish"),
        pytest.param(None, ".json", False, (".json", ".catalog"), id="after-unpublish"),
        pytest.param(None, ".catalog", False, (".catalog",), id="between-unlinks"),
        pytest.param(2, None, False, (), id="before-clearing-pending"),
    ),
)
def test_prune_recovers_after_a_process_is_killed(tmp_path: Path, replace_number: int | None, unlink_suffix: str | None, published: bool, remaining_suffixes: tuple[str, ...]) -> None:
    facade = ServerFacade(tmp_path, server_fqdn=_SERVER_FQDN)
    repository = facade.init_repository(_CLIENT_ID, {_BACKUP_SET_ID: _KEY_ID})
    register = repository.path / "snapshot-registry.json"
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _START
        old = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="")
        clock.now.return_value = _START + timedelta(days=1)
        newest = facade.backup(_CLIENT_ID, _BACKUP_SET_ID, iter(((_ROOT, BytesIO(b"r")),)), _ROOT.object_id, prune="1d")
        now = _START + timedelta(days=8)
        clock.now.return_value = now
        metadata = repository.snapshot_file(_BACKUP_SET_ID, old.snapshot_id)
        files = {path: path.read_bytes() for path in (metadata, metadata.with_suffix(".catalog"))}
        unlink_target = metadata.with_suffix(unlink_suffix) if unlink_suffix is not None else None

        process = get_context("spawn").Process(target=_prune_until_killed, args=(tmp_path, now, register, replace_number, unlink_target))
        process.start()
        try:
            process.join(timeout=10)
            assert process.exitcode == -signal.SIGKILL
        finally:
            if process.is_alive():
                process.kill()
                process.join()
            process.close()

        state = json.loads(register.read_bytes())
        expected_snapshots = {str(newest.snapshot_id): str(_BACKUP_SET_ID)}
        old_entry = {str(old.snapshot_id): str(_BACKUP_SET_ID)}
        if published:
            expected_snapshots.update(old_entry)
        assert state["snapshots"] == expected_snapshots
        assert state.get("pending_deletions", {}) == ({} if published else old_entry)
        for path, contents in files.items():
            if path.suffix in remaining_suffixes:
                assert path.read_bytes() == contents
            else:
                assert not path.exists()

        restarted = ServerFacade(tmp_path, server_fqdn=_SERVER_FQDN)
        assert restarted.prune(_CLIENT_ID, _BACKUP_SET_ID) == (old.snapshot_id,)
        assert restarted.prune(_CLIENT_ID, _BACKUP_SET_ID) == ()

    assert not any(path.exists() for path in files)
    assert "pending_deletions" not in json.loads(register.read_bytes())
    reopened = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
    assert reopened.snapshots == {newest}
    assert SnapshotService().get_snapshot(reopened, _BACKUP_SET_ID, newest.snapshot_id).object_catalog == newest.object_catalog
