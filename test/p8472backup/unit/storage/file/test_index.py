from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, TYPE_DATA, Object
from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock
from p8472backup.storage.file.packs import HEADER_SIZE as PACK_HEADER_SIZE, Pack


class TestLocationIndex:
    def test_rebuild_keeps_the_first_record_for_duplicate_ids(self, tmp_path: Path) -> None:
        earlier = tmp_path / "2.pack"
        later = tmp_path / "10.pack"
        first = Object(TYPE_DATA, b"a" * 32, 3)
        duplicate = Object(TYPE_DATA, first.object_id, 1)
        second = Object(TYPE_DATA, b"b" * 32, 0)
        third = Object(TYPE_DATA, b"c" * 32, 2)
        Pack.write_to(earlier, iter(((first, BytesIO(b"abc")), (duplicate, BytesIO(b"x")), (second, BytesIO()))))
        Pack.write_to(later, iter(((duplicate, BytesIO(b"y")), (third, BytesIO(b"de")))))

        with RepositoryLock(tmp_path) as lock, patch.object(Object, "read_body", side_effect=AssertionError("Rebuild must only read headers")):
            index = LocationIndex.rebuild_from_packs({10: later, 2: earlier}, lock)

        assert len(index) == 3
        assert index.get(first.object_id) == ObjectLocation(2, PACK_HEADER_SIZE, OBJECT_HEADER_SIZE + 3)
        assert index.get(second.object_id) == ObjectLocation(2, PACK_HEADER_SIZE + 2 * OBJECT_HEADER_SIZE + 4, OBJECT_HEADER_SIZE)
        assert index.get(third.object_id) == ObjectLocation(10, PACK_HEADER_SIZE + OBJECT_HEADER_SIZE + 1, OBJECT_HEADER_SIZE + 2)

    def test_rebuild_requires_the_lock_even_with_no_packs(self, tmp_path: Path) -> None:
        lock = RepositoryLock(tmp_path)
        with pytest.raises(RuntimeError, match="lock is not held"):
            LocationIndex.rebuild_from_packs({}, lock)
        with lock:
            assert len(LocationIndex.rebuild_from_packs({}, lock)) == 0

    def test_rebuild_rejects_a_pack_outside_the_locked_repository(self, tmp_path: Path) -> None:
        repository = tmp_path / "repository"
        repository.mkdir()
        with RepositoryLock(repository) as lock, pytest.raises(ValueError, match="must name a repository file"):
            LocationIndex.rebuild_from_packs({1: tmp_path / "1.pack"}, lock)

    @pytest.mark.parametrize("pack_id", (0, 2**64 - 1))
    def test_rebuild_accepts_the_checkpoint_pack_id_boundaries(self, tmp_path: Path, pack_id: int) -> None:
        obj = Object(TYPE_DATA, b"a" * 32, 1)
        path = tmp_path / f"{pack_id}.pack"
        Pack.write_to(path, iter(((obj, BytesIO(b"a")),)))

        with RepositoryLock(tmp_path) as lock:
            index = LocationIndex.rebuild_from_packs({pack_id: path}, lock)
        assert index.get(obj.object_id) == ObjectLocation(pack_id, PACK_HEADER_SIZE, OBJECT_HEADER_SIZE + 1)

    @pytest.mark.parametrize("pack_id", (-1, 2**64))
    def test_rebuild_rejects_pack_ids_outside_the_checkpoint_range(self, tmp_path: Path, pack_id: int) -> None:
        obj = Object(TYPE_DATA, b"a" * 32, 1)
        path = tmp_path / f"{pack_id}.pack"
        Pack.write_to(path, iter(((obj, BytesIO(b"a")),)))

        with RepositoryLock(tmp_path) as lock, pytest.raises(ValueError, match="Pack ID, offset, and length must fit"):
            LocationIndex.rebuild_from_packs({pack_id: path}, lock)

    def test_put_and_get_three_object_ids(self) -> None:
        index = LocationIndex()
        first_id = (1).to_bytes(4, "big") + b"A" * 28
        first = ObjectLocation(pack_id=1, offset=14, length=100)
        second_id = (3).to_bytes(4, "big") + b"C" * 28
        second = ObjectLocation(pack_id=1, offset=114, length=200)
        third_id = (8_193).to_bytes(4, "big") + b"I" * 28
        third = ObjectLocation(pack_id=2, offset=14, length=300)

        index.put(first_id, first)
        index.put(second_id, second)
        # Both 1 % 8192 and 8193 % 8192 are 1. The third record goes in bucket 2.
        index.put(third_id, third)

        found_first = index.get(first_id)
        found_second = index.get(second_id)
        found_third = index.get(third_id)

        assert found_first == first
        assert found_second == second
        assert found_third == third

    def test_resolves_collisions_and_keeps_first_location_per_object_id(self) -> None:
        index = LocationIndex()
        prefix = (7).to_bytes(4, "big")
        first_id = prefix + b"a" * 28
        first = ObjectLocation(1, 14, 44)
        second_id = prefix + b"b" * 28
        second = ObjectLocation(2, 2**40, 2**34 + 44)
        missing = prefix + b"c" * 28
        assert index.get(first_id) is None

        index.put(first_id, first)
        index.put(second_id, second)
        index.put(second_id, ObjectLocation(2**64 - 1, 50, second.length))

        assert index.get(first_id) == first
        assert index.get(second_id) == second
        assert index.get(missing) is None

    def test_growth_preserves_existing_locations(self) -> None:
        index = LocationIndex()
        assert len(index._buckets) == 8_192  # pyright: ignore[reportPrivateUsage]
        locations = {(number * 7).to_bytes(4, "big") + bytes(28): ObjectLocation(1, 14 + number * 44, 44) for number in range(4_096)}
        for object_id, location in locations.items():
            index.put(object_id, location)
        assert len(index._buckets) == 8_192  # pyright: ignore[reportPrivateUsage]

        next_id = (4_096 * 7).to_bytes(4, "big") + bytes(28)
        next_location = ObjectLocation(1, 14 + 4_096 * 44, 44)
        index.put(next_id, next_location)

        assert len(index._buckets) == 16_384  # pyright: ignore[reportPrivateUsage]
        assert index.get(next_id) == next_location
        assert all(index.get(object_id) == location for object_id, location in locations.items())

    def test_invalid_values_do_not_replace_a_location(self) -> None:
        index = LocationIndex()
        original_id = bytes(32)
        original = ObjectLocation(1, 14, 44)
        index.put(original_id, original)

        with pytest.raises(ValueError, match="exactly 32 bytes"):
            index.put(bytes(33), original)
        with pytest.raises(ValueError, match="unsigned 64-bit"):
            index.put(original_id, ObjectLocation(1, -1, 44))

        assert index.get(original_id) == original

    def test_checkpoint_round_trip_and_corruption_detection(self, tmp_path: Path) -> None:
        path = tmp_path / "locations"
        index = LocationIndex()
        locations = {bytes(31) + bytes([number]): ObjectLocation(2**64 - 1, 2**40 + number, 2**34 + 44) for number in range(10)}
        for object_id, location in locations.items():
            index.put(object_id, location)

        with RepositoryLock(tmp_path) as lock:
            index.save_to(path, lock)
            restored = LocationIndex.load_from(path, lock)
            assert all(restored.get(object_id) == location for object_id, location in locations.items())

            damaged = bytearray(path.read_bytes())
            damaged[-33] ^= 1
            path.write_bytes(damaged)
            with pytest.raises(ValueError, match="checksum mismatch"):
                LocationIndex.load_from(path, lock)

    def test_checkpoint_requires_exclusive_lock_for_its_repository(self, tmp_path: Path) -> None:
        lock = RepositoryLock(tmp_path)
        index = LocationIndex()
        path = tmp_path / "locations"
        with pytest.raises(RuntimeError, match="lock is not held"):
            index.save_to(path, lock)
        with pytest.raises(RuntimeError, match="lock is not held"):
            LocationIndex.load_from(path, lock)

        with lock:
            with pytest.raises(BlockingIOError), RepositoryLock(tmp_path):
                pytest.fail("Acquired a second exclusive lock")
            with pytest.raises(ValueError, match="must name a repository file"):
                index.save_to(tmp_path.parent / "locations", lock)
            with pytest.raises(FileNotFoundError):
                LocationIndex.load_from(path, lock)

        with RepositoryLock(tmp_path) as reacquired:
            index.save_to(path, reacquired)
            assert LocationIndex.load_from(path, reacquired).get(bytes(32)) is None

    def test_save_skips_unchanged_indexes_and_duplicate_ids(self, tmp_path: Path) -> None:
        path = tmp_path / "locations"
        index = LocationIndex()
        object_id = b"A" * 32
        location = ObjectLocation(1, 14, 44)

        with RepositoryLock(tmp_path) as lock:
            assert index.save_to(path, lock) is True
            loaded = LocationIndex.load_from(path, lock)
            with patch("p8472backup.storage.file.index.NamedTemporaryFile") as create_file:
                assert index.save_to(path, lock) is False
                assert loaded.save_to(path, lock) is False
                create_file.assert_not_called()

            loaded.put(object_id, location)
            assert loaded.save_to(path, lock) is True
            loaded.put(object_id, ObjectLocation(2, 58, 100))
            with patch("p8472backup.storage.file.index.NamedTemporaryFile") as create_file:
                assert loaded.save_to(path, lock) is False
                create_file.assert_not_called()
            assert LocationIndex.load_from(path, lock).get(object_id) == location

        with pytest.raises(RuntimeError, match="lock is not held"):
            loaded.save_to(path, lock)

    def test_failed_directory_sync_keeps_pending_changes(self, tmp_path: Path) -> None:
        path = tmp_path / "locations"
        index = LocationIndex()
        with RepositoryLock(tmp_path) as lock:
            with patch("p8472backup.storage.file.index.os.fsync", side_effect=[None, OSError("sync failed")]), pytest.raises(OSError, match="sync failed"):
                index.save_to(path, lock)
            assert index.save_to(path, lock) is True
            assert index.save_to(path, lock) is False

    def test_failed_replace_keeps_previous_checkpoint(self, tmp_path: Path) -> None:
        path = tmp_path / "locations"
        index = LocationIndex()
        original_id = bytes(32)
        original = ObjectLocation(1, 14, 44)
        index.put(original_id, original)

        with RepositoryLock(tmp_path) as lock:
            index.save_to(path, lock)
            additional_id = b"a" * 32
            additional = ObjectLocation(2, 14, 44)
            index.put(additional_id, additional)
            with patch("p8472backup.storage.file.index.os.replace", side_effect=OSError("replace failed")), pytest.raises(OSError, match="replace failed"):
                index.save_to(path, lock)

            restored = LocationIndex.load_from(path, lock)
            assert restored.get(original_id) == original
            assert restored.get(additional_id) is None
            assert not list(tmp_path.glob(".location-index-*"))
            assert index.save_to(path, lock) is True
            assert LocationIndex.load_from(path, lock).get(additional_id) == additional
            assert index.save_to(path, lock) is False
