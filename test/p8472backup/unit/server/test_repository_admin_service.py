from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import cast
from unittest.mock import patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import TYPE_SNAPSHOT, Object
from p8472backup.archive.snapshotrootcontent import SnapshotRootContent
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository import BackupMode
from p8472backup.server.repository_admin_service import RepositoryAdminService
from p8472backup.server.snapshot_service import SnapshotService


_CLIENT_ID = UUID(int=1)
_BACKUP_SET_ID = UUID(int=2)
_KEY_ID = UUID(int=3)


def test_init_repository_rejects_second_init_and_preserves_backup(tmp_path: Path) -> None:
    path = tmp_path / str(_CLIENT_ID)
    other_set_id = UUID(int=6)
    admin_service = RepositoryAdminService()
    repository = admin_service.init_repository(path, _CLIENT_ID, ((other_set_id, _KEY_ID), (_BACKUP_SET_ID, _KEY_ID)))
    assert repository.mode == "pull"

    body = SnapshotRootContent(b"backup set configuration").to_plaintext()
    root = Object(TYPE_SNAPSHOT, b"r" * 32, len(body))
    snapshots = SnapshotService()
    with patch.object(trusted_time, "server_clock", create=True) as clock, admin_service.locked_repository(tmp_path, _CLIENT_ID) as (current, lock):
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        snapshot = snapshots.create_snapshot(current, _BACKUP_SET_ID, iter(((root, BytesIO(body)),)), root.object_id, lock=lock)
        other_snapshot = snapshots.create_snapshot(current, other_set_id, iter(((root, BytesIO(body)),)), root.object_id, lock=lock)
    (repository.snapshots_root / str(_BACKUP_SET_ID) / ".snapshot-interrupted").write_bytes(b"incomplete")
    files = (path / "repository.json", repository.index_directory / str(_KEY_ID), repository.packs_root / str(_KEY_ID) / "1.pack", repository.snapshot_file(_BACKUP_SET_ID, snapshot.snapshot_id))
    contents = tuple(file.read_bytes() for file in files)

    with pytest.raises(FileExistsError):
        RepositoryAdminService().init_repository(path, _CLIENT_ID, ((_BACKUP_SET_ID, _KEY_ID),))
    with pytest.raises(FileExistsError):
        RepositoryAdminService().init_repository(path, _CLIENT_ID, ((UUID(int=4), UUID(int=5)),))

    assert tuple(file.read_bytes() for file in files) == contents
    reopened = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
    assert reopened.snapshots == {snapshot, other_snapshot}
    assert snapshot.backup_set_id == _BACKUP_SET_ID
    assert other_snapshot.backup_set_id == other_set_id
    assert snapshots.get_snapshot(reopened, _BACKUP_SET_ID, snapshot.snapshot_id) == snapshot

    wrong_client = replace(repository, client_id=UUID(int=5))
    with pytest.raises(ValueError, match="Snapshot identity or scope"):
        snapshots.get_snapshot(wrong_client, _BACKUP_SET_ID, snapshot.snapshot_id)


@pytest.mark.parametrize("checkpoint", (None, b"damaged"))
def test_init_repository_leaves_missing_or_damaged_index_untouched(tmp_path: Path, checkpoint: bytes | None) -> None:
    path = tmp_path / "repo"
    repository = RepositoryAdminService().init_repository(path, _CLIENT_ID, ((_BACKUP_SET_ID, _KEY_ID),))
    if checkpoint is None:
        (repository.index_directory / str(_KEY_ID)).unlink()
    else:
        (repository.index_directory / str(_KEY_ID)).write_bytes(checkpoint)

    with pytest.raises(FileExistsError):
        RepositoryAdminService().init_repository(path, _CLIENT_ID, ((_BACKUP_SET_ID, _KEY_ID),))
    if checkpoint is None:
        assert not (repository.index_directory / str(_KEY_ID)).exists()
    else:
        assert (repository.index_directory / str(_KEY_ID)).read_bytes() == checkpoint


def test_init_repository_requires_a_new_path(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    with pytest.raises(InconsistentStateError):
        RepositoryAdminService().init_repository(path, _CLIENT_ID, ((_BACKUP_SET_ID, _KEY_ID),))
    assert not list(path.iterdir())


def test_repository_can_change_mode(tmp_path: Path) -> None:
    RepositoryAdminService().init_repository(tmp_path / str(_CLIENT_ID), _CLIENT_ID, ((_BACKUP_SET_ID, _KEY_ID),), mode="push")
    repository = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
    original_hash = hash(repository)
    initial_mode = repository.mode
    assert initial_mode == "push"

    repository.set_mode("pull")
    with pytest.raises(ValueError, match="Unsupported backup mode"):
        repository.set_mode(cast(BackupMode, "invalid"))

    assert repository.mode == "pull"
    assert hash(repository) == original_hash


def test_invalid_mode_does_not_create_repository(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    with pytest.raises(ValueError, match="Unsupported backup mode"):
        RepositoryAdminService().init_repository(path, _CLIENT_ID, ((_BACKUP_SET_ID, _KEY_ID),), mode=cast(BackupMode, "invalid"))
    assert not path.exists()


def test_find_by_client_id_loads_scope_and_persisted_mode(tmp_path: Path) -> None:
    service = RepositoryAdminService()
    service.init_repository(tmp_path / str(_CLIENT_ID), _CLIENT_ID, ((_BACKUP_SET_ID, _KEY_ID),))
    with service.locked_repository(tmp_path, _CLIENT_ID) as (repository, lock):
        service.set_mode(repository, "push", lock)

    reopened = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
    assert reopened == repository
    assert reopened.mode == "push"
    with pytest.raises(ValueError, match="Backup set is not registered"):
        reopened.get_backup_set(UUID(int=4))


def test_find_by_client_id_rejects_invalid_metadata(tmp_path: Path) -> None:
    path = tmp_path / str(_CLIENT_ID)
    path.mkdir()
    (path / "repository.json").write_bytes(b"{}")

    with pytest.raises(ValueError, match="Unsupported repository metadata format"):
        RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
