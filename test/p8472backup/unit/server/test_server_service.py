import json
from pathlib import Path
from uuid import UUID

import pytest

from p8472backup.common.error import InconsistentStateError
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.server_service import ServerService
from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.lock import RepositoryLock


_CLIENT_ID = UUID(int=1)
_FIRST_SET = UUID(int=2)
_SECOND_SET = UUID(int=3)
_THIRD_SET = UUID(int=4)
_KEY_ID = UUID(int=5)
_OTHER_KEY_ID = UUID(int=6)


def test_init_repository_registers_sets_and_prepares_each_key_domain(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    backup_sets = {_FIRST_SET: _KEY_ID, _SECOND_SET: _KEY_ID, _THIRD_SET: _OTHER_KEY_ID}
    service.init_repository(_CLIENT_ID, backup_sets, mode="push")
    path = tmp_path / str(_CLIENT_ID)

    assert json.loads((path / "snapshot-registry.json").read_text()) == {"version": 1, "snapshots": {}}
    for set_id, key_id in backup_sets.items():
        repo = RepositoryService.open_repository(path, set_id)
        assert repo.client_id == _CLIENT_ID
        assert repo.mode == "push"
        assert repo.snapshots_directory == path / "snapshots" / str(set_id)
        assert repo.snapshots_directory.is_dir()
        assert repo.packs_directory == path / "data" / "packs" / str(key_id)
        assert repo.packs_directory.is_dir()
        assert repo.index_file == path / "data" / "index" / str(key_id)
        with RepositoryLock(path) as lock:
            assert LocationIndex.load_from(repo.index_file, lock).get(bytes(32)) is None
    assert {file.name for file in (path / "data" / "index").iterdir()} == {str(_KEY_ID), str(_OTHER_KEY_ID)}


def test_init_repository_keeps_existing_repositories_and_scopes_paths_by_client(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    first = RepositoryService.open_repository(tmp_path / str(_CLIENT_ID), _FIRST_SET)
    metadata_path = first.path / "repository.json"
    original_metadata = metadata_path.read_bytes()

    with pytest.raises(FileExistsError, match=r"^Backup repository already exists\.$"):
        service.init_repository(_CLIENT_ID, {_SECOND_SET: _OTHER_KEY_ID}, mode="push")

    other_client = UUID(int=7)
    service.init_repository(other_client, {_FIRST_SET: _OTHER_KEY_ID})
    second = RepositoryService.open_repository(tmp_path / str(other_client), _FIRST_SET)
    assert second.path == tmp_path / str(other_client)
    assert second.key_id == _OTHER_KEY_ID
    assert first.mode == "pull"
    assert metadata_path.read_bytes() == original_metadata


def test_init_repository_requires_a_backup_set_before_creating_anything(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="At least one backup set is required"):
        ServerService(tmp_path).init_repository(_CLIENT_ID, {})

    assert not (tmp_path / str(_CLIENT_ID)).exists()


def test_init_repository_rejects_invalid_set_ids_before_creating_anything(tmp_path: Path) -> None:
    path = tmp_path / str(_CLIENT_ID)
    with pytest.raises(ValueError, match="IDs must be UUIDs"):
        RepositoryService.init_repository_with_sets(path, _CLIENT_ID, ((_FIRST_SET, _KEY_ID), ("invalid", _KEY_ID)))

    assert not path.exists()


def test_init_repository_does_not_publish_metadata_on_index_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_save(index: LocationIndex, path: Path, lock: RepositoryLock) -> bool:
        lock.require_held_for(path)
        raise OSError("Index write failed")

    monkeypatch.setattr(LocationIndex, "save_to", fail_save)
    service = ServerService(tmp_path)
    with pytest.raises(OSError, match="Index write failed"):
        service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})

    path = tmp_path / str(_CLIENT_ID)
    with pytest.raises(InconsistentStateError, match=r"^Repository metadata is missing\. A previous initialization may have been interrupted\.$"):
        service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})

    assert path.is_dir()
    assert not (path / "repository.json").exists()
    assert not (path / "snapshot-registry.json").exists()
