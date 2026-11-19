import pytest

from p8472backup.storage.file.packs import Pack


_VALID_HEADER = bytes.fromhex("0100020000005800000008000000")


class TestPack:
    def test_header_matches_wire_format(self) -> None:
        pack = Pack(object_count=2, body_length=2 * (44 + 16 * 1024**3))
        assert pack.to_header() == _VALID_HEADER
        assert Pack.from_header(_VALID_HEADER) == pack

    def test_rejects_wrong_header_size(self) -> None:
        for header in (_VALID_HEADER[:-1], _VALID_HEADER + b"\x00"):
            with pytest.raises(ValueError, match="Pack header must contain exactly 14 bytes"):
                Pack.from_header(header)

    def test_rejects_unsupported_version(self) -> None:
        with pytest.raises(ValueError, match="Unsupported pack format version"):
            Pack.from_header(b"\x02\x00" + _VALID_HEADER[2:])

    def test_rejects_invalid_counts_and_lengths(self) -> None:
        for object_count, body_length in ((0, 0), (2**32, 44), (1, -1), (1, 2**64), (2, 87), (1, 44 + 16 * 1024**3 + 1)):
            with pytest.raises(ValueError):
                Pack(object_count=object_count, body_length=body_length)
