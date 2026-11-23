from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from p8472backup.archive.objects import TYPE_SNAPSHOT, Object
from p8472backup.archive.snapshotrootcontent import SnapshotRootContent
from p8472backup.common.error import InconsistentStateError
from p8472backup.server.repository import BackupMode, Repository
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.snapshot_service import SnapshotService


_CLIENT_ID = UUID(int=1)
_BACKUP_SET_ID = UUID(int=2)
_KEY_ID = UUID(int=3)


def test_init_repository_rejects_second_init_and_preserves_backup(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    repository = RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    assert repository.mode == "pull"

    body = SnapshotRootContent(b"backup set configuration").to_plaintext()
    root = Object(TYPE_SNAPSHOT, b"r" * 32, len(body))
    snapshots = SnapshotService(repository)
    snapshot = snapshots.create_snapshot(iter(((root, BytesIO(body)),)), root.object_id)
    files = (path / "repository.json", repository.index_file, repository.packs_directory / "1.pack", repository.snapshot_file(snapshot.snapshot_id))
    contents = tuple(file.read_bytes() for file in files)

    with pytest.raises(FileExistsError):
        RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    with pytest.raises(FileExistsError):
        RepositoryService.init_repository(path, _CLIENT_ID, UUID(int=4), UUID(int=5))

    assert tuple(file.read_bytes() for file in files) == contents
    reopened = RepositoryService.open_repository(path, _BACKUP_SET_ID)
    assert SnapshotService(reopened).get_snapshot(snapshot.snapshot_id) == snapshot

    wrong_client = Repository(repository.path, UUID(int=5), repository.backup_set_id, repository.key_id)
    with pytest.raises(ValueError, match="Snapshot identity or scope"):
        SnapshotService(wrong_client).get_snapshot(snapshot.snapshot_id)


@pytest.mark.parametrize("checkpoint", (None, b"damaged"))
def test_init_repository_leaves_missing_or_damaged_index_untouched(tmp_path: Path, checkpoint: bytes | None) -> None:
    path = tmp_path / "repo"
    repository = RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    if checkpoint is None:
        repository.index_file.unlink()
    else:
        repository.index_file.write_bytes(checkpoint)

    with pytest.raises(FileExistsError):
        RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    if checkpoint is None:
        assert not repository.index_file.exists()
    else:
        assert repository.index_file.read_bytes() == checkpoint


def test_init_repository_requires_a_new_path(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    with pytest.raises(InconsistentStateError):
        RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    assert not list(path.iterdir())


def test_repository_can_change_mode(tmp_path: Path) -> None:
    repository = RepositoryService.init_repository(tmp_path / "repo", _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID, mode="push")
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
        RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID, mode=cast(BackupMode, "invalid"))
    assert not path.exists()


def test_open_repository_loads_scope_and_persisted_mode(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    repository = RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    RepositoryService.set_mode(path, "push")

    reopened = RepositoryService.open_repository(path, _BACKUP_SET_ID)
    assert reopened == repository
    assert reopened.mode == "push"
    with pytest.raises(ValueError, match="Backup set is not registered"):
        RepositoryService.open_repository(path, UUID(int=4))


@pytest.mark.parametrize(("contents", "error"), ((None, FileNotFoundError), (b"{}", ValueError)))
def test_open_repository_rejects_missing_or_invalid_metadata(tmp_path: Path, contents: bytes | None, error: type[Exception]) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    if contents is not None:
        (path / "repository.json").write_bytes(contents)

    with pytest.raises(error):
        RepositoryService.open_repository(path, _BACKUP_SET_ID)
