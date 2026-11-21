import pytest

from p8472backup.archive.dataobject import COMPRESSION_LZ4, COMPRESSION_NONE, COMPRESSION_ZSTD, DataObject


class TestDataObject:
    def test_plaintext_round_trip(self) -> None:
        chunk = bytes(range(256))
        obj = DataObject(compression=0, chunk_size=len(chunk), data=chunk)
        plaintext = bytes.fromhex("00 00 01 00 00 00 00 00 00") + chunk
        assert obj.to_plaintext() == plaintext
        assert DataObject.from_plaintext(plaintext) == obj

    @pytest.mark.parametrize("plaintext", (bytes(8), bytes.fromhex("ff") + bytes(8), bytes(9) + b"x"))
    def test_rejects_invalid_plaintext(self, plaintext: bytes) -> None:
        with pytest.raises(ValueError):
            DataObject.from_plaintext(plaintext)

    @pytest.mark.parametrize(("codec", "module"), ((COMPRESSION_LZ4, "lz4.frame"), (COMPRESSION_ZSTD, "zstandard")))
    def test_compressed_plaintext(self, codec: int, module: str) -> None:
        pytest.importorskip(module)
        for chunk in (b"", b"8472backup" * 128):
            obj = DataObject(compression=codec, chunk_size=len(chunk), data=chunk)
            plaintext = obj.to_plaintext()
            assert plaintext[0] == codec
            assert DataObject.from_plaintext(plaintext, compression=codec) == obj
            with pytest.raises(ValueError, match="expected compression"):
                DataObject.from_plaintext(plaintext, compression=COMPRESSION_NONE)
            for invalid in (plaintext[:-1], plaintext + b"x", plaintext[:1] + (len(chunk) + 1).to_bytes(8, "little") + plaintext[9:]):
                with pytest.raises(ValueError):
                    DataObject.from_plaintext(invalid)
