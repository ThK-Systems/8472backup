import pytest

from p8472backup.storage.file.index import LocationIndex, ObjectLocation


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
