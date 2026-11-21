from pathlib import Path
from unittest.mock import patch

import pytest

from p8472backup.storage.file.index import LocationIndex, ObjectLocation
from p8472backup.storage.file.lock import RepositoryLock


class TestLocationIndex:
    def test_resolves_collisions_and_replaces_by_full_object_id(self) -> None:
        index = LocationIndex()
        prefix = (7).to_bytes(4, "big")
        first = ObjectLocation(prefix + b"a" * 28, 1, 14, 44)
        second = ObjectLocation(prefix + b"b" * 28, 2, 2**40, 2**34 + 44)
        missing = prefix + b"c" * 28
        assert index.get(first.object_id) is None

        index.put(first)
        index.put(second)
        replacement = ObjectLocation(first.object_id, 2**64 - 1, 50, first.length)
        index.put(replacement)

        assert index.get(first.object_id) == replacement
        assert index.get(second.object_id) == second
        assert index.get(missing) is None

    def test_growth_preserves_existing_locations(self) -> None:
        index = LocationIndex()
        locations = [ObjectLocation((number * 7).to_bytes(4, "big") + bytes(28), 1, 14 + number * 44, 44) for number in range(40)]
        for location in locations:
            index.put(location)

        assert [index.get(location.object_id) for location in locations] == locations

    def test_invalid_values_do_not_replace_a_location(self) -> None:
        index = LocationIndex()
        original = ObjectLocation(bytes(32), 1, 14, 44)
        index.put(original)

        with pytest.raises(ValueError, match="exactly 32 bytes"):
            index.put(ObjectLocation(bytes(33), 1, 14, 44))
        with pytest.raises(ValueError, match="unsigned 64-bit"):
            index.put(ObjectLocation(original.object_id, 1, -1, 44))

        assert index.get(original.object_id) == original

    def test_checkpoint_round_trip_and_corruption_detection(self, tmp_path: Path) -> None:
        path = tmp_path / "locations"
        index = LocationIndex()
        locations = [ObjectLocation(bytes(31) + bytes([number]), 2**64 - 1, 2**40 + number, 2**34 + 44) for number in range(10)]
        for location in locations:
            index.put(location)

        with RepositoryLock(tmp_path) as lock:
            index.save_to(path, lock)
            restored = LocationIndex.load_from(path, lock)
            assert [restored.get(location.object_id) for location in locations] == locations

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

    def test_failed_replace_keeps_previous_checkpoint(self, tmp_path: Path) -> None:
        path = tmp_path / "locations"
        index = LocationIndex()
        original = ObjectLocation(bytes(32), 1, 14, 44)
        index.put(original)

        with RepositoryLock(tmp_path) as lock:
            index.save_to(path, lock)
            index.put(ObjectLocation(original.object_id, 2, 14, 44))
            with patch("p8472backup.storage.file.index.os.replace", side_effect=OSError("replace failed")), pytest.raises(OSError, match="replace failed"):
                index.save_to(path, lock)

            assert LocationIndex.load_from(path, lock).get(original.object_id) == original
            assert not list(tmp_path.glob(".location-index-*"))
