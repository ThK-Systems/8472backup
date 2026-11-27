import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import TYPE_SNAPSHOT, Object
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository_admin_service import RepositoryAdminService
from p8472backup.server.server_service import ServerService
from p8472backup.server.snapshot import Snapshot
from p8472backup.server.snapshot_service import SnapshotService
from p8472backup.server.trusted_time import TrustedTime, TrustedTimeError
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

    assert repository.mode == "push"
    assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID) == repository
    assert repository.backup_sets.keys() == backup_sets.keys()
    assert not repository.snapshots
    assert json.loads((path / "repository.json").read_text()) == {
        "version": 2,
        "client_id": str(_CLIENT_ID),
        "mode": "push",
        "backup_sets": {str(set_id): {"key_id": str(key_id), "deleted": False} for set_id, key_id in backup_sets.items()},
    }
    assert json.loads((path / "snapshot-registry.json").read_text()) == {"version": 1, "snapshots": {}}
    for set_id, key_id in backup_sets.items():
        backup_set = repository.get_backup_set(set_id)
        assert backup_set.key_id == key_id
        snapshots_directory = repository.snapshots_root / str(set_id)
        packs_directory = repository.packs_root / str(key_id)
        index_file = repository.index_directory / str(key_id)
        assert snapshots_directory == path / "snapshots" / str(set_id)
        assert snapshots_directory.is_dir()
        assert packs_directory == path / "data" / "packs" / str(key_id)
        assert packs_directory.is_dir()
        assert index_file == path / "data" / "index" / str(key_id)
        with RepositoryLock(path) as lock:
            assert LocationIndex.load_from(index_file, lock).get(bytes(32)) is None
    assert {file.name for file in (path / "data" / "index").iterdir()} == {str(_KEY_ID), str(_OTHER_KEY_ID)}


def test_init_repository_keeps_existing_repositories_and_scopes_paths_by_client(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    first = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
    metadata_path = first.path / "repository.json"
    original_metadata = metadata_path.read_bytes()

    with pytest.raises(FileExistsError, match=r"^Backup repository already exists\.$"):
        service.init_repository(_CLIENT_ID, {_SECOND_SET: _OTHER_KEY_ID}, mode="push")

    other_client = UUID(int=7)
    service.init_repository(other_client, {_FIRST_SET: _OTHER_KEY_ID})
    second = RepositoryAdminService.find_by_client_id(tmp_path, other_client)
    assert second.path == tmp_path / str(other_client)
    assert second.get_backup_set(_FIRST_SET).key_id == _OTHER_KEY_ID
    assert first.mode == "pull"
    assert metadata_path.read_bytes() == original_metadata


def test_init_repository_requires_a_backup_set_before_creating_anything(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="At least one backup set is required"):
        ServerService(tmp_path).init_repository(_CLIENT_ID, {})

    assert not (tmp_path / str(_CLIENT_ID)).exists()


def test_init_repository_rejects_invalid_set_ids_before_creating_anything(tmp_path: Path) -> None:
    path = tmp_path / str(_CLIENT_ID)
    with pytest.raises(ValueError, match="IDs must be UUIDs"):
        RepositoryAdminService().init_repository(path, _CLIENT_ID, ((_FIRST_SET, _KEY_ID), ("invalid", _KEY_ID)))

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
    repository = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
    index_file = repository.index_directory / str(_KEY_ID)
    registry = (path / "snapshot-registry.json").read_bytes()
    marker = repository.packs_root / str(_KEY_ID) / "1.pack"
    marker.write_bytes(b"existing")
    with RepositoryLock(path) as lock:
        index = LocationIndex.load_from(index_file, lock)
        index.put(b"o" * 32, ObjectLocation(pack_id=1, offset=0, length=8))
        index.save_to(index_file, lock)
    checkpoint = index_file.read_bytes()

    service.add_backup_set(_CLIENT_ID, _SECOND_SET, _KEY_ID)
    service.add_backup_set(_CLIENT_ID, _THIRD_SET, _OTHER_KEY_ID)

    reopened = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
    assert reopened.get_backup_set(_SECOND_SET).key_id == _KEY_ID
    assert reopened.get_backup_set(_THIRD_SET).key_id == _OTHER_KEY_ID
    assert (reopened.snapshots_root / str(_SECOND_SET)).is_dir()
    assert index_file.read_bytes() == checkpoint
    assert marker.read_bytes() == b"existing"
    assert (reopened.snapshots_root / str(_THIRD_SET)).is_dir()
    assert (reopened.packs_root / str(_OTHER_KEY_ID)).is_dir()
    assert reopened.mode == "push"
    with RepositoryLock(path) as lock:
        assert LocationIndex.load_from(reopened.index_directory / str(_OTHER_KEY_ID), lock).get(bytes(32)) is None
    assert (path / "snapshot-registry.json").read_bytes() == registry

    with pytest.raises(FileExistsError, match="Backup set already exists"):
        service.add_backup_set(_CLIENT_ID, _SECOND_SET, _OTHER_KEY_ID)


def test_add_backup_set_checks_client_identity(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    path = tmp_path / str(_CLIENT_ID)
    metadata_path = path / "repository.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["client_id"] = str(UUID(int=99))
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="client ID does not match"):
        service.add_backup_set(_CLIENT_ID, _SECOND_SET, _KEY_ID)
    assert not (path / "snapshots" / str(_SECOND_SET)).exists()


def test_find_by_client_id_requires_initialized_repository(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Backup repository not found for client"):
        RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)

    (tmp_path / str(_CLIENT_ID)).mkdir()
    with pytest.raises(InconsistentStateError, match="Repository metadata is missing"):
        RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)


def test_add_backup_set_rejects_invalid_ids(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    repository_admin_service = RepositoryAdminService()
    with repository_admin_service.locked_repository(tmp_path, _CLIENT_ID) as (repository, lock):
        with pytest.raises(ValueError, match="IDs must be UUIDs"):
            repository_admin_service.add_backup_set(repository, "invalid", _KEY_ID, lock)
        with pytest.raises(ValueError, match="IDs must be UUIDs"):
            repository_admin_service.add_backup_set(repository, _SECOND_SET, "invalid", lock)
    assert not (tmp_path / str(_CLIENT_ID) / "snapshots" / str(_SECOND_SET)).exists()


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
    assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).get_backup_set(_FIRST_SET).key_id == _KEY_ID


def test_add_backup_set_rejects_incomplete_shared_key_domain(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    path = tmp_path / str(_CLIENT_ID)
    repository = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
    (repository.index_directory / str(_KEY_ID)).unlink()
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


def test_remove_backup_set_respects_effective_protection_and_shared_packs(tmp_path: Path) -> None:
    service = ServerService(tmp_path, protection_duration=timedelta(days=10))
    repository = service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID, _SECOND_SET: _KEY_ID})
    snapshots = SnapshotService()
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    started_at = datetime(2026, 1, 1, tzinfo=UTC)

    def take_snapshot(set_id: UUID, days: int = 7) -> Snapshot:
        with RepositoryAdminService().locked_repository(tmp_path, _CLIENT_ID) as (current, lock):
            return snapshots.create_snapshot(current, set_id, iter(((root, BytesIO(b"r")),)), root.object_id, lock=lock, protection_duration=timedelta(days=days))

    with patch.object(trusted_time, "server_clock", spec=TrustedTime) as clock:
        clock.now.return_value = started_at
        expired = take_snapshot(_FIRST_SET)
        stored_protection = take_snapshot(_FIRST_SET, days=12)
        other_set = take_snapshot(_SECOND_SET)
        clock.now.return_value = started_at + timedelta(days=1)
        extended_protection = take_snapshot(_FIRST_SET)

        index_file = repository.index_directory / str(_KEY_ID)
        pack_file = repository.packs_root / str(_KEY_ID) / "1.pack"
        index_bytes, pack_bytes = index_file.read_bytes(), pack_file.read_bytes()

        clock.now.return_value = started_at + timedelta(days=11)
        service.remove_backup_set(_CLIENT_ID, _FIRST_SET)
        reopened = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
        assert reopened.get_backup_set(_FIRST_SET).deleted
        assert not reopened.get_backup_set(_SECOND_SET).deleted
        assert reopened.snapshots == {stored_protection, extended_protection, other_set}
        assert not repository.snapshot_file(_FIRST_SET, expired.snapshot_id).exists()
        assert snapshots.get_snapshot(reopened, _FIRST_SET, extended_protection.snapshot_id) == extended_protection

        assert not repository.get_backup_set(_FIRST_SET).deleted
        with pytest.raises(ValueError, match="Backup set is deleted"):
            take_snapshot(_FIRST_SET)

        clock.now.return_value = started_at + timedelta(days=11, microseconds=1)
        service.remove_backup_set(_CLIENT_ID, _FIRST_SET)
        assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).snapshots == {stored_protection, other_set}

    assert index_file.read_bytes() == index_bytes
    assert pack_file.read_bytes() == pack_bytes


def test_remove_backup_set_persists_state_and_can_retry_failed_unlink(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    repository = service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)
    with patch.object(trusted_time, "server_clock", spec=TrustedTime) as clock:
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        with RepositoryAdminService().locked_repository(tmp_path, _CLIENT_ID) as (current, lock):
            snapshot = SnapshotService().create_snapshot(current, _FIRST_SET, iter(((root, BytesIO(b"r")),)), root.object_id, lock=lock)
        path = repository.snapshot_file(_FIRST_SET, snapshot.snapshot_id)

        clock.now.return_value = snapshot.protected_until + timedelta(seconds=1)
        with patch.object(Path, "unlink", side_effect=PermissionError("Unlink failed")), pytest.raises(PermissionError, match="Unlink failed"):
            service.remove_backup_set(_CLIENT_ID, _FIRST_SET)
        assert path.is_file()
        assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).get_backup_set(_FIRST_SET).deleted
        service.remove_backup_set(_CLIENT_ID, _FIRST_SET)

    assert not path.exists()
    service.add_backup_set(_CLIENT_ID, _SECOND_SET, _KEY_ID)
    reopened = RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID)
    assert reopened.get_backup_set(_FIRST_SET).deleted
    assert not reopened.get_backup_set(_SECOND_SET).deleted


def test_invalid_trusted_time_blocks_snapshot_publication_and_deletion(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    admin = RepositoryAdminService()
    snapshots = SnapshotService()
    root = Object(TYPE_SNAPSHOT, b"r" * 32, 1)

    with patch.object(trusted_time, "server_clock", spec=TrustedTime) as clock:
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        with admin.locked_repository(tmp_path, _CLIENT_ID) as (repository, lock):
            committed = snapshots.create_snapshot(repository, _FIRST_SET, iter(((root, BytesIO(b"r")),)), root.object_id, lock=lock)
            clock.now.side_effect = TrustedTimeError("Trusted time reference expired")
            with pytest.raises(TrustedTimeError, match="reference expired"):
                snapshots.create_snapshot(repository, _FIRST_SET, iter(((root, BytesIO(b"r")),)), root.object_id, lock=lock)

        with pytest.raises(TrustedTimeError, match="reference expired"):
            service.remove_backup_set(_CLIENT_ID, _FIRST_SET)
    assert admin.find_by_client_id(tmp_path, _CLIENT_ID).snapshots == {committed}


def test_remove_backup_set_reads_metadata_version_one_and_requires_lock(tmp_path: Path) -> None:
    service = ServerService(tmp_path)
    repository = service.init_repository(_CLIENT_ID, {_FIRST_SET: _KEY_ID})
    metadata_file = repository.path / "repository.json"
    metadata_v1 = {"version": 1, "client_id": str(_CLIENT_ID), "mode": "pull", "backup_sets": {str(_FIRST_SET): str(_KEY_ID)}}
    metadata_file.write_text(json.dumps(metadata_v1), encoding="utf-8")

    assert not RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).get_backup_set(_FIRST_SET).deleted
    with RepositoryLock(repository.path), pytest.raises(BlockingIOError):
        service.remove_backup_set(_CLIENT_ID, _FIRST_SET)
    assert json.loads(metadata_file.read_text(encoding="utf-8")) == metadata_v1

    with patch.object(trusted_time, "server_clock", spec=TrustedTime) as clock:
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        service.remove_backup_set(_CLIENT_ID, _FIRST_SET)
    assert RepositoryAdminService.find_by_client_id(tmp_path, _CLIENT_ID).get_backup_set(_FIRST_SET).deleted
