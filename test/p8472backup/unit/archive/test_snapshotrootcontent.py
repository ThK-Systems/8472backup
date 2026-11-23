import pytest

from p8472backup.archive.snapshotrootcontent import SnapshotRootContent


class TestSnapshotRootContent:
    def test_plaintext_round_trip_matches_wire_format(self) -> None:
        description = b"config"
        first, second = b"a" * 32, b"b" * 32
        root = SnapshotRootContent(description, (first, second))
        plaintext = b"\x06" + bytes(11) + description + first + second
        assert root.to_plaintext() == plaintext
        assert SnapshotRootContent.from_plaintext(plaintext) == root

    def test_accepts_empty_metadata_list(self) -> None:
        root = SnapshotRootContent(b"config")
        assert SnapshotRootContent.from_plaintext(root.to_plaintext()) == root

    def test_padding_round_trip_and_invalid_length(self) -> None:
        root = SnapshotRootContent(b"config", (b"a" * 32,), b"padding")
        plaintext = root.to_plaintext()
        assert plaintext[8:12] == (7).to_bytes(4, "little")
        assert SnapshotRootContent.from_plaintext(plaintext) == root
        with pytest.raises(ValueError, match="Padding exceeds"):
            SnapshotRootContent.from_plaintext(plaintext[:8] + (100).to_bytes(4, "little") + plaintext[12:])

    @pytest.mark.parametrize("plaintext", (bytes(7), b"\x01" + bytes(7), bytes(8) + b"x"))
    def test_rejects_invalid_plaintext(self, plaintext: bytes) -> None:
        with pytest.raises(ValueError):
            SnapshotRootContent.from_plaintext(plaintext)

    def test_rejects_wrong_metadata_id_length(self) -> None:
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            SnapshotRootContent(b"config", (bytes(31),))
