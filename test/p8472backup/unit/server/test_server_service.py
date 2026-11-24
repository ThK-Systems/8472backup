import json
from pathlib import Path
from uuid import UUID

import pytest

from p8472backup.common.error import InconsistentStateError
from p8472backup.server.repository_service import RepositoryService
from p8472backup.server.server_service import ServerService
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
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
    repository = service.init_repository(_CLIENT_ID, backup_sets, mode="push")
    path = tmp_path / str(_CLIENT_ID)

    assert repository == RepositoryService.open_repository(path, _FIRST_SET)
    assert repository.mode == "push"
    assert RepositoryService.find_by_client_id(tmp_path, _CLIENT_ID) == repository
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


def test_add_backup_set_with_shared_and_new_key_domains(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID}, mode="push")
    path = tmp_path / str(_CLIENT_ID)
    first = RepositoryService.open_repository(path, _FIRST_SET)
    registry = (path / "snapshot-registry.json").read_bytes()
    marker = first.packs_directory / "1.pack"
    marker.write_bytes(b"existing")
    with RepositoryLock(path) as lock:
        index = LocationIndex.load_from(first.index_file, lock)
        index.put(b"o" * 32, ObjectLocation(pack_id=1, offset=0, length=8))
        index.save_to(first.index_file, lock)
    checkpoint = first.index_file.read_bytes()

    service.add_backup_set(_CLIENT_ID, _SECOND_SET, _KEY_ID)
    service.add_backup_set(_CLIENT_ID, _THIRD_SET, _OTHER_KEY_ID)

    shared = RepositoryService.open_repository(path, _SECOND_SET)
    independent = RepositoryService.open_repository(path, _THIRD_SET)
    assert shared.snapshots_directory.is_dir()
    assert shared.index_file.read_bytes() == checkpoint
    assert marker.read_bytes() == b"existing"
    assert independent.snapshots_directory.is_dir()
    assert independent.packs_directory.is_dir()
    assert independent.mode == "push"
    with RepositoryLock(path) as lock:
        assert LocationIndex.load_from(independent.index_file, lock).get(bytes(32)) is None
    assert (path / "snapshot-registry.json").read_bytes() == registry

    with pytest.raises(FileExistsError, match="Backup set already exists"):
        service.add_backup_set(_CLIENT_ID, _SECOND_SET, _OTHER_KEY_ID)


def test_add_backup_set_checks_client_identity(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    repository = service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    path = tmp_path / str(_CLIENT_ID)
    metadata_path = path / "repository.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["client_id"] = str(UUID(int=99))
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="client ID does not match"):
        service.add_backup_set(_CLIENT_ID, _SECOND_SET, _KEY_ID)
    with pytest.raises(ValueError, match="client ID does not match"):
        RepositoryService().add_backup_set(repository, _SECOND_SET, _KEY_ID)
    assert not (path / "snapshots" / str(_SECOND_SET)).exists()


def test_find_by_client_id_requires_initialized_repository(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Backup repository not found for client"):
        RepositoryService.find_by_client_id(tmp_path, _CLIENT_ID)

    (tmp_path / str(_CLIENT_ID)).mkdir()
    with pytest.raises(InconsistentStateError, match="Repository metadata is missing"):
        RepositoryService.find_by_client_id(tmp_path, _CLIENT_ID)


def test_add_backup_set_rejects_invalid_ids(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    repository = RepositoryService.find_by_client_id(tmp_path, _CLIENT_ID)
    repository_service = RepositoryService()
    with pytest.raises(ValueError, match="IDs must be UUIDs"):
        repository_service.add_backup_set(repository, "invalid", _KEY_ID)
    with pytest.raises(ValueError, match="IDs must be UUIDs"):
        repository_service.add_backup_set(repository, _SECOND_SET, "invalid")
    assert not (repository.path / "snapshots" / str(_SECOND_SET)).exists()


def test_add_backup_set_detects_interrupted_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    path = tmp_path / str(_CLIENT_ID)
    metadata = (path / "repository.json").read_bytes()

    def fail_save(index: LocationIndex, index_path: Path, lock: RepositoryLock) -> bool:
        lock.require_held_for(index_path)
        raise OSError("Index write failed")

    monkeypatch.setattr(LocationIndex, "save_to", fail_save)
    with pytest.raises(OSError, match="Index write failed"):
        service.add_backup_set(_CLIENT_ID, _SECOND_SET, _OTHER_KEY_ID)
    with pytest.raises(InconsistentStateError, match="previous registration may have been interrupted"):
        service.add_backup_set(_CLIENT_ID, _SECOND_SET, _OTHER_KEY_ID)
    assert (path / "repository.json").read_bytes() == metadata
    assert RepositoryService.open_repository(path, _FIRST_SET).key_id == _KEY_ID


def test_add_backup_set_rejects_incomplete_shared_key_domain(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    path = tmp_path / str(_CLIENT_ID)
    repository = RepositoryService.open_repository(path, _FIRST_SET)
    repository.index_file.unlink()
    with pytest.raises(InconsistentStateError, match="key domain storage is incomplete"):
        service.add_backup_set(_CLIENT_ID, _SECOND_SET, _KEY_ID)
    assert not (path / "snapshots" / str(_SECOND_SET)).exists()


def test_add_backup_set_rejects_abandoned_key_domain_and_locked_repository(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    path = tmp_path / str(_CLIENT_ID)
    abandoned = path / "data" / "packs" / str(_OTHER_KEY_ID)
    abandoned.mkdir()
    with pytest.raises(InconsistentStateError, match="Key domain storage already exists"):
        service.add_backup_set(_CLIENT_ID, _SECOND_SET, _OTHER_KEY_ID)
    assert not (path / "snapshots" / str(_SECOND_SET)).exists()
    with RepositoryLock(path), pytest.raises(BlockingIOError):
        service.add_backup_set(_CLIENT_ID, _SECOND_SET, _KEY_ID)
