import pytest

from p8472backup.archive.datacontent import COMPRESSION_LZ4, COMPRESSION_NONE, COMPRESSION_ZSTD, DataContent


class TestDataContent:
    def test_plaintext_round_trip(self) -> None:
        chunk = bytes(range(256))
        obj = DataContent(compression=0, chunk_size=len(chunk), data=chunk)
        plaintext = bytes.fromhex("00 00 01 00 00 00 00 00 00") + chunk
        assert obj.to_plaintext() == plaintext
        assert DataContent.from_plaintext(plaintext) == obj

    @pytest.mark.parametrize("plaintext", (bytes(8), bytes.fromhex("ff") + bytes(8), bytes(9) + b"x"))
    def test_rejects_invalid_plaintext(self, plaintext: bytes) -> None:
        with pytest.raises(ValueError):
            DataContent.from_plaintext(plaintext)

    @pytest.mark.parametrize(("codec", "module"), ((COMPRESSION_LZ4, "lz4.frame"), (COMPRESSION_ZSTD, "zstandard")))
    def test_compressed_plaintext(self, codec: int, module: str) -> None:
        pytest.importorskip(module)
        for chunk in (b"", b"8472backup" * 128):
            obj = DataContent(compression=codec, chunk_size=len(chunk), data=chunk)
            plaintext = obj.to_plaintext()
            assert plaintext[0] == codec
            assert DataContent.from_plaintext(plaintext, compression=codec) == obj
            with pytest.raises(ValueError, match="expected compression"):
                DataContent.from_plaintext(plaintext, compression=COMPRESSION_NONE)
            for invalid in (plaintext[:-1], plaintext + b"x", plaintext[:1] + (len(chunk) + 1).to_bytes(8, "little") + plaintext[9:]):
                with pytest.raises(ValueError):
                    DataContent.from_plaintext(invalid)
