import pytest

from p8472backup.archive.metacontent import MetaContent


class TestMetaContent:
    def test_plaintext_round_trip_preserves_chunk_order_and_repetition(self) -> None:
        first, second = b"a" * 32, b"b" * 32
        metadata = b"report.txt"
        obj = MetaContent(metadata, (first, second, first))
        plaintext = bytes.fromhex("0a 00 00 00 00 00 00 00") + metadata + first + second + first
        assert obj.to_plaintext() == plaintext
        assert MetaContent.from_plaintext(plaintext) == obj

    def test_entry_without_chunks(self) -> None:
        obj = MetaContent(b"entry")
        assert MetaContent.from_plaintext(obj.to_plaintext()) == obj

    @pytest.mark.parametrize(
        "plaintext",
        (
            bytes(7),
            b"\x01" + bytes(7),
            bytes(8) + b"x",
        ),
    )
    def test_rejects_invalid_plaintext(self, plaintext: bytes) -> None:
        with pytest.raises(ValueError):
            MetaContent.from_plaintext(plaintext)

    def test_rejects_wrong_chunk_id_length(self) -> None:
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            MetaContent(b"report.txt", (bytes(31),))
