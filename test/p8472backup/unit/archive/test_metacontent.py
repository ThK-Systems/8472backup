import pytest

from p8472backup.archive.metacontent import MetaContent


class TestMetaContent:
    def test_plaintext_round_trip_preserves_chunk_order_and_repetition(self) -> None:
        first, second = b"a" * 32, b"b" * 32
        metadata = b"report.txt"
        obj = MetaContent(metadata, (first, second, first))
        plaintext = bytes.fromhex("0a 00 00 00 00 00 00 00 00 00 00 00") + metadata + first + second + first
        assert obj.to_plaintext() == plaintext
        assert MetaContent.from_plaintext(plaintext) == obj

    def test_entry_without_chunks(self) -> None:
        obj = MetaContent(b"entry")
        assert MetaContent.from_plaintext(obj.to_plaintext()) == obj

    def test_padding_round_trip_and_invalid_length(self) -> None:
        obj = MetaContent(b"entry", (b"a" * 32,), b"padding")
        plaintext = obj.to_plaintext()
        assert plaintext[8:12] == (7).to_bytes(4, "little")
        assert MetaContent.from_plaintext(plaintext) == obj
        with pytest.raises(ValueError, match="Padding exceeds"):
            MetaContent.from_plaintext(plaintext[:8] + (100).to_bytes(4, "little") + plaintext[12:])

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
