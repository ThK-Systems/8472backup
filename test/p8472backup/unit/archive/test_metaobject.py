import pytest

from p8472backup.archive.metaobject import ENTRY_TYPE_DIRECTORY, ENTRY_TYPE_FILE, ENTRY_TYPE_SYMLINK, MetaObject


class TestMetaObject:
    def test_plaintext_round_trip_preserves_chunk_order_and_repetition(self) -> None:
        first, second = b"a" * 32, b"b" * 32
        metadata = b"report.txt"
        obj = MetaObject(ENTRY_TYPE_FILE, metadata, (first, second, first))
        plaintext = bytes.fromhex("01 0a 00 00 00 00 00 00 00") + metadata + first + second + first
        assert obj.to_plaintext() == plaintext
        assert MetaObject.from_plaintext(plaintext) == obj

    @pytest.mark.parametrize("entry_type", (ENTRY_TYPE_FILE, ENTRY_TYPE_DIRECTORY, ENTRY_TYPE_SYMLINK))
    def test_entry_without_chunks(self, entry_type: int) -> None:
        obj = MetaObject(entry_type, b"entry")
        assert MetaObject.from_plaintext(obj.to_plaintext()) == obj

    @pytest.mark.parametrize(
        "plaintext",
        (
            bytes(8),
            b"\xff" + bytes(8),
            b"\x01\x01" + bytes(7),
            b"\x01" + bytes(8) + b"x",
            b"\x02" + bytes(8) + bytes(32),
        ),
    )
    def test_rejects_invalid_plaintext(self, plaintext: bytes) -> None:
        with pytest.raises(ValueError):
            MetaObject.from_plaintext(plaintext)

    def test_rejects_wrong_chunk_id_length(self) -> None:
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            MetaObject(ENTRY_TYPE_FILE, b"report.txt", (bytes(31),))
