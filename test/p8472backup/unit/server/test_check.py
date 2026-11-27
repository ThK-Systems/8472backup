import json
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, OBJECT_ID_SIZE, TYPE_DATA, TYPE_SNAPSHOT, Object, ObjectId
from p8472backup.common.error import InconsistentStateError
from p8472backup.server import trusted_time
from p8472backup.server.repository import Repository
from p8472backup.server.server_facade import ServerFacade
from p8472backup.server.snapshot import Snapshot
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import HEADER_SIZE as PACK_HEADER_SIZE, Pack


_CLIENT_ID = UUID(int=1)
_SET_ID = UUID(int=2)
_KEY_ID = UUID(int=3)
_OTHER_SET_ID = UUID(int=4)
_OTHER_KEY_ID = UUID(int=5)
_ROOT_ID = b"r" * OBJECT_ID_SIZE
_DATA_ID = b"d" * OBJECT_ID_SIZE
_STARTED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _backup(facade: ServerFacade, set_id: UUID = _SET_ID, *, root_id: ObjectId = _ROOT_ID) -> Snapshot:
    records = ((Object(TYPE_SNAPSHOT, root_id, 1), BytesIO(b"r")), (Object(TYPE_DATA, _DATA_ID, 3), BytesIO(b"abc")))
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _STARTED_AT
        return facade.backup(_CLIENT_ID, set_id, iter(records), root_id, prune="")


@pytest.fixture
def saved_backup(tmp_path: Path) -> tuple[ServerFacade, Repository, Path]:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    repository = facade.init_repository(_CLIENT_ID, {_SET_ID: _KEY_ID})
    snapshot = _backup(facade)
    return facade, repository, repository.snapshot_file(_SET_ID, snapshot.snapshot_id)


def _repo_files(repository: Repository) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in repository.path.rglob("*") if path.is_file()}


def test_check_accepts_a_new_repository(tmp_path: Path) -> None:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")
    facade.init_repository(_CLIENT_ID, {_SET_ID: _KEY_ID})

    facade.check(_CLIENT_ID)


def test_check_requires_an_existing_repository(tmp_path: Path) -> None:
    facade = ServerFacade(tmp_path, server_fqdn="backup.example.net")

    with pytest.raises(FileNotFoundError, match="Backup repository not found"):
        facade.check(_CLIENT_ID)

    assert not (tmp_path / str(_CLIENT_ID)).exists()


def test_check_uses_the_existing_repository_lock(saved_backup: tuple[ServerFacade, Repository, Path]) -> None:
    facade, repository, _ = saved_backup
    with RepositoryLock(repository.path), pytest.raises(BlockingIOError):
        facade.check(_CLIENT_ID)
    facade.check(_CLIENT_ID)


def test_check_accepts_shared_objects_and_leaves_files_unchanged(saved_backup: tuple[ServerFacade, Repository, Path]) -> None:
    facade, repository, _ = saved_backup
    third_set_id = UUID(int=6)
    facade.add_backup_set(_CLIENT_ID, _OTHER_SET_ID, _KEY_ID)
    facade.add_backup_set(_CLIENT_ID, third_set_id, _OTHER_KEY_ID)
    _backup(facade, _OTHER_SET_ID, root_id=b"s" * OBJECT_ID_SIZE)
    _backup(facade, third_set_id)
    # A pack written before an interrupted index save is outside the checkpoint.
    Pack.write_to(repository.packs_root / str(_KEY_ID) / "3.pack", iter(((Object(TYPE_DATA, _DATA_ID, 3), BytesIO(b"abc")),)))
    before = _repo_files(repository)

    facade.check(_CLIENT_ID)
    assert _repo_files(repository) == before


@pytest.mark.parametrize("present", (True, False))
def test_check_rejects_unusable_repository_metadata(saved_backup: tuple[ServerFacade, Repository, Path], present: bool) -> None:
    facade, repository, _ = saved_backup
    path = repository.path / "repository.json"
    if present:
        path.write_text("{", encoding="utf-8")
    else:
        path.unlink()

    with pytest.raises(InconsistentStateError):
        facade.check(_CLIENT_ID)


@pytest.mark.parametrize(
    "registry",
    (
        None,
        "{",
        {"version": 2, "snapshots": {}},
        {"version": 1, "snapshots": {str(UUID(int=10)): str(_OTHER_SET_ID)}},
        {"version": 1, "snapshots": {str(UUID(int=10)): str(_SET_ID)}, "pending_deletions": {str(UUID(int=10)): str(_SET_ID)}},
    ),
)
def test_check_stops_if_the_snapshot_register_is_unusable(saved_backup: tuple[ServerFacade, Repository, Path], registry: dict[str, object] | str | None) -> None:
    facade, repository, _ = saved_backup
    path = repository.path / "snapshot-registry.json"
    if registry is None:
        path.unlink()
    else:
        path.write_text(registry if isinstance(registry, str) else json.dumps(registry), encoding="utf-8")
    (repository.index_directory / str(_KEY_ID)).unlink()

    with pytest.raises(InconsistentStateError, match=r"^snapshot-registry\.json:") as failure:
        facade.check(_CLIENT_ID)
    assert len(str(failure.value).splitlines()) == 1


@pytest.mark.parametrize(("remove_json", "remove_catalogue"), ((False, False), (True, False), (False, True), (True, True)))
def test_check_accepts_pending_snapshot_deletions(saved_backup: tuple[ServerFacade, Repository, Path], remove_json: bool, remove_catalogue: bool) -> None:
    facade, repository, path = saved_backup
    register = {"version": 1, "snapshots": {}, "pending_deletions": {path.stem: str(_SET_ID)}}
    (repository.path / "snapshot-registry.json").write_text(json.dumps(register), encoding="utf-8")
    if remove_json:
        path.unlink()
    if remove_catalogue:
        path.with_suffix(".catalog").unlink()

    facade.check(_CLIENT_ID)


@pytest.mark.parametrize(("suffix", "payload"), ((".json", None), (".json", b"{"), (".catalog", None), (".catalog", b"x")))
def test_check_reports_missing_or_invalid_snapshot_files(saved_backup: tuple[ServerFacade, Repository, Path], suffix: str, payload: bytes | None) -> None:
    facade, repository, metadata = saved_backup
    path = metadata.with_suffix(suffix)
    if payload is None:
        path.unlink()
    else:
        path.write_bytes(payload)

    with pytest.raises(InconsistentStateError) as failure:
        facade.check(_CLIENT_ID)
    assert str(failure.value).startswith(f"{path.relative_to(repository.path)}:")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("backup_set_id", str(_OTHER_SET_ID), "Snapshot identity or scope does not match"),
        ("committed_at", "2026-01-01T00:00:00", "Snapshot timestamps must include a time zone"),
        ("committed_at", "0001-01-01T00:00:00+01:00", "date value out of range"),
        ("protected_until", "2025-12-31T23:59:59+00:00", "Snapshot protection deadline precedes commit time"),
    ),
)
def test_check_validates_snapshot_metadata(saved_backup: tuple[ServerFacade, Repository, Path], field: str, value: str, message: str) -> None:
    facade, _, path = saved_backup
    metadata: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    metadata[field] = value
    path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(InconsistentStateError, match=message):
        facade.check(_CLIENT_ID)


@pytest.mark.parametrize(
    ("catalogue", "message"),
    (
        (_DATA_ID, "snapshot root is missing"),
        (_ROOT_ID + _ROOT_ID + _DATA_ID, "duplicate entries: 1"),
        (_ROOT_ID + b"x" * OBJECT_ID_SIZE, "missing index references: 1"),
    ),
)
def test_check_validates_catalogue_membership_with_a_matching_checksum(saved_backup: tuple[ServerFacade, Repository, Path], catalogue: bytes, message: str) -> None:
    facade, _, path = saved_backup
    path.with_suffix(".catalog").write_bytes(catalogue)
    metadata: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    metadata["catalogue_sha256"] = sha256(catalogue).hexdigest()
    path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(InconsistentStateError, match=message):
        facade.check(_CLIENT_ID)


def test_check_includes_published_snapshots_of_retired_repositories(saved_backup: tuple[ServerFacade, Repository, Path]) -> None:
    facade, _, path = saved_backup
    with patch.object(trusted_time, "server_clock", create=True) as clock:
        clock.now.return_value = _STARTED_AT
        facade.remove_repository(_CLIENT_ID)
    path.with_suffix(".catalog").unlink()

    with pytest.raises(InconsistentStateError, match=r"\.catalog:"):
        facade.check(_CLIENT_ID)


def test_check_collects_independent_findings_and_leaves_files_unchanged(saved_backup: tuple[ServerFacade, Repository, Path]) -> None:
    facade, repository, path = saved_backup
    facade.add_backup_set(_CLIENT_ID, _OTHER_SET_ID, _OTHER_KEY_ID)
    other = _backup(facade, _OTHER_SET_ID)
    other_catalogue = repository.snapshot_file(_OTHER_SET_ID, other.snapshot_id).with_suffix(".catalog")
    other_catalogue.unlink()
    catalogue = path.with_suffix(".catalog")
    catalogue.write_bytes(_ROOT_ID)
    index_path = repository.index_directory / str(_KEY_ID)
    checkpoint = bytearray(index_path.read_bytes())
    checkpoint[-1] ^= 1
    index_path.write_bytes(checkpoint)
    bad_pack = repository.packs_root / str(_OTHER_KEY_ID) / "1.pack"
    bad_pack.unlink()
    before = _repo_files(repository)

    with pytest.raises(InconsistentStateError) as failure:
        facade.check(_CLIENT_ID)
    findings = str(failure.value).splitlines()
    assert len(findings) == 4
    assert f"{index_path.relative_to(repository.path)}: Location index checksum mismatch" in findings
    assert any(f"{catalogue.relative_to(repository.path)}:" in entry and "checksum mismatch" in entry for entry in findings)
    assert any(entry.startswith(f"{other_catalogue.relative_to(repository.path)}:") for entry in findings)
    assert any(entry.startswith(f"{bad_pack.relative_to(repository.path)}:") for entry in findings)
    assert _repo_files(repository) == before


def test_check_reports_a_missing_index_once(saved_backup: tuple[ServerFacade, Repository, Path]) -> None:
    facade, repository, _ = saved_backup
    _backup(facade)
    index_path = repository.index_directory / str(_KEY_ID)
    index_path.unlink()

    with pytest.raises(InconsistentStateError) as failure:
        facade.check(_CLIENT_ID)
    assert str(failure.value).startswith(f"{index_path.relative_to(repository.path)}:")
    assert len(str(failure.value).splitlines()) == 1


def test_check_reports_a_missing_pack_once_across_snapshot_references(saved_backup: tuple[ServerFacade, Repository, Path]) -> None:
    facade, repository, _ = saved_backup
    _backup(facade)
    pack = repository.packs_root / str(_KEY_ID) / "1.pack"
    pack.unlink()

    with pytest.raises(InconsistentStateError) as failure:
        facade.check(_CLIENT_ID)
    assert str(failure.value).startswith(f"{pack.relative_to(repository.path)}:")
    assert len(str(failure.value).splitlines()) == 1


@pytest.mark.parametrize(("offset_delta", "length_delta"), ((1, 0), (0, 1)))
def test_check_verifies_locations_even_when_the_index_checksum_matches(saved_backup: tuple[ServerFacade, Repository, Path], offset_delta: int, length_delta: int) -> None:
    facade, repository, _ = saved_backup
    index_path = repository.index_directory / str(_KEY_ID)
    with RepositoryLock(repository.path) as lock:
        original = LocationIndex.load_from(index_path, lock)
        root, data = original.get(_ROOT_ID), original.get(_DATA_ID)
        assert root is not None and data is not None
        index = LocationIndex()
        index.put(_ROOT_ID, root)
        index.put(_DATA_ID, ObjectLocation(data.pack_id, data.offset + offset_delta, data.length + length_delta))
        index.save_to(index_path, lock)

    with pytest.raises(InconsistentStateError, match="indexed locations missing from pack headers: 1"):
        facade.check(_CLIENT_ID)


@pytest.mark.parametrize(
    ("obj", "message"),
    (
        (Object(TYPE_DATA, _ROOT_ID, 1), "has unexpected object type 1"),
        (Object(TYPE_SNAPSHOT, b"x" * OBJECT_ID_SIZE, 1), "indexed locations missing from pack headers: 1"),
        (Object(TYPE_SNAPSHOT, _ROOT_ID, 1_024), "Object record extends past the pack body"),
    ),
)
def test_check_validates_pack_object_headers(saved_backup: tuple[ServerFacade, Repository, Path], obj: Object, message: str) -> None:
    facade, repository, _ = saved_backup
    path = repository.packs_root / str(_KEY_ID) / "1.pack"
    with path.open("r+b") as source:
        source.seek(PACK_HEADER_SIZE)
        source.write(obj.to_header())

    with pytest.raises(InconsistentStateError, match=message):
        facade.check(_CLIENT_ID)


@pytest.mark.parametrize("truncated", (True, False))
def test_check_validates_pack_length_and_record_count(saved_backup: tuple[ServerFacade, Repository, Path], truncated: bool) -> None:
    facade, repository, _ = saved_backup
    path = repository.packs_root / str(_KEY_ID) / "1.pack"
    if truncated:
        path.write_bytes(path.read_bytes()[:-1])
        message = "Pack file length does not match its header"
    else:
        with path.open("r+b") as source:
            source.write(Pack(1, 2 * OBJECT_HEADER_SIZE + 4).to_header())
        message = "Object records do not fill the declared pack body"

    with pytest.raises(InconsistentStateError, match=message):
        facade.check(_CLIENT_ID)
