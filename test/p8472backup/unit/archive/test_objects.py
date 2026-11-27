from dataclasses import FrozenInstanceError
from io import BytesIO
from typing import cast
from unittest.mock import patch

import pytest

from p8472backup.archive.objects import Object


_OBJECT_ID = bytes(range(32))
_BODY_LENGTH = 0x01020304
_VALID_HEADER = bytes.fromhex("01000100000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f0403020100000000")


class TestObject:
    def test_read_body_rejects_a_short_write(self) -> None:
        obj = Object(1, _OBJECT_ID, 3)
        output = BytesIO()
        with patch.object(output, "write", return_value=2), pytest.raises(OSError, match="Short write while copying object body"):
            obj.read_body(BytesIO(b"abc"), output)

    def test_to_header_matches_wire_format(self) -> None:
        assert Object(1, _OBJECT_ID, _BODY_LENGTH).to_header() == _VALID_HEADER

    def test_from_header_matches_wire_format(self) -> None:
        assert Object.from_header(_VALID_HEADER) == Object(1, _OBJECT_ID, _BODY_LENGTH)

    @pytest.mark.parametrize("object_type", (1, 2, 3))
    def test_header_round_trip_for_each_object_type(self, object_type: int) -> None:
        assert Object.from_header(Object(object_type, _OBJECT_ID, _BODY_LENGTH).to_header()) == Object(object_type, _OBJECT_ID, _BODY_LENGTH)

    @pytest.mark.parametrize("length", (0, 16 * 1024**3))
    def test_header_accepts_body_length_boundaries(self, length: int) -> None:
        assert Object.from_header(Object(1, _OBJECT_ID, length).to_header()) == Object(1, _OBJECT_ID, length)

    @pytest.mark.parametrize("object_type", (-1, 0, 4, 65536))
    def test_constructor_rejects_unknown_object_type(self, object_type: int) -> None:
        with pytest.raises(ValueError, match="Unsupported object type"):
            Object(object_type, _OBJECT_ID, _BODY_LENGTH)

    @pytest.mark.parametrize("object_type", (0, 4, 65535))
    def test_from_header_rejects_unknown_object_type(self, object_type: int) -> None:
        header = object_type.to_bytes(2, "little") + _VALID_HEADER[2:]
        with pytest.raises(ValueError, match="Unsupported object type"):
            Object.from_header(header)

    @pytest.mark.parametrize("version", (0, 2, 65535))
    def test_from_header_rejects_unsupported_version(self, version: int) -> None:
        header = _VALID_HEADER[:2] + version.to_bytes(2, "little") + _VALID_HEADER[4:]
        with pytest.raises(ValueError, match="Unsupported object format version"):
            Object.from_header(header)

    @pytest.mark.parametrize("object_id", (b"", bytes(31), bytes(33)))
    def test_constructor_rejects_wrong_id_length(self, object_id: bytes) -> None:
        with pytest.raises(ValueError, match="Object ID must contain exactly 32 bytes"):
            Object(1, object_id, _BODY_LENGTH)

    @pytest.mark.parametrize("header", (b"", _VALID_HEADER[:2], _VALID_HEADER[:-1], _VALID_HEADER + b"\x00"))
    def test_from_header_rejects_wrong_header_length(self, header: bytes) -> None:
        with pytest.raises(ValueError, match="Object header must contain exactly 44 bytes"):
            Object.from_header(header)

    @pytest.mark.parametrize("length", (-1, 16 * 1024**3 + 1, 2**64))
    def test_constructor_rejects_invalid_body_length(self, length: int) -> None:
        with pytest.raises(ValueError, match="Object body length must be between 0 and 16 GiB"):
            Object(1, _OBJECT_ID, length)

    @pytest.mark.parametrize("length", (16 * 1024**3 + 1, 2**64 - 1))
    def test_from_header_rejects_oversized_body(self, length: int) -> None:
        header = _VALID_HEADER[:36] + length.to_bytes(8, "little")
        with pytest.raises(ValueError, match="Object body length must be between 0 and 16 GiB"):
            Object.from_header(header)

    def test_constructor_rejects_unsupported_version(self) -> None:
        with pytest.raises(ValueError, match="Unsupported object format version"):
            Object(object_type=1, object_id=_OBJECT_ID, length=_BODY_LENGTH, version=2)

    def test_constructor_rejects_mutable_id(self) -> None:
        with pytest.raises(TypeError, match="Object ID must be bytes"):
            Object(object_type=1, object_id=cast(bytes, bytearray(_OBJECT_ID)), length=_BODY_LENGTH)

    def test_object_is_immutable(self) -> None:
        obj = Object(object_type=1, object_id=_OBJECT_ID, length=_BODY_LENGTH)
        with pytest.raises(FrozenInstanceError):
            setattr(obj, "length", 0)
