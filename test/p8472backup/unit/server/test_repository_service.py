from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from p8472backup.archive.objects import TYPE_SNAPSHOT, Object
from p8472backup.archive.snapshotroot import SnapshotRoot
from p8472backup.server.repository import Repository
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.snapshot_service import SnapshotService


_CLIENT_ID = UUID(int=1)
_BACKUP_SET_ID = UUID(int=2)
_KEY_ID = UUID(int=3)


def test_init_repository_rejects_second_init_and_preserves_backup(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    repository = RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)

    body = SnapshotRoot(b"backup set configuration").to_plaintext()
    root = Object(TYPE_SNAPSHOT, b"r" * 32, len(body))
    snapshots = SnapshotService(repository)
    snapshot = snapshots.create_snapshot(iter(((root, BytesIO(body)),)), root.object_id)
    files = (repository.index_path, repository.pack_directory / "1.pack", repository.snapshot_path(snapshot.snapshot_id))
    contents = tuple(file.read_bytes() for file in files)

    with pytest.raises(FileExistsError):
        RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    with pytest.raises(FileExistsError):
        RepositoryService.init_repository(path, _CLIENT_ID, UUID(int=4), UUID(int=5))

    assert tuple(file.read_bytes() for file in files) == contents
    assert snapshots.get_snapshot(snapshot.snapshot_id) == snapshot

    wrong_client = Repository(repository.path, UUID(int=5), repository.backup_set_id, repository.key_id)
    with pytest.raises(ValueError, match="Snapshot identity or scope"):
        SnapshotService(wrong_client).get_snapshot(snapshot.snapshot_id)


@pytest.mark.parametrize("checkpoint", (None, b"damaged"))
def test_init_repository_leaves_missing_or_damaged_index_untouched(tmp_path: Path, checkpoint: bytes | None) -> None:
    path = tmp_path / "repo"
    repository = RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    if checkpoint is None:
        repository.index_path.unlink()
    else:
        repository.index_path.write_bytes(checkpoint)

    with pytest.raises(FileExistsError):
        RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    if checkpoint is None:
        assert not repository.index_path.exists()
    else:
        assert repository.index_path.read_bytes() == checkpoint


def test_init_repository_requires_a_new_path(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    with pytest.raises(FileExistsError):
        RepositoryService.init_repository(path, _CLIENT_ID, _BACKUP_SET_ID, _KEY_ID)
    assert not list(path.iterdir())
