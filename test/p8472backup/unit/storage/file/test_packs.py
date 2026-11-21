from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, TYPE_DATA, TYPE_METADATA, TYPE_SNAPSHOT, Object
from p8472backup.storage.file.packs import HEADER_SIZE, Pack


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

    def test_writes_and_reads_records_up_to_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "pack"
        first_body = b"a" * (128 * 1024)
        second_body = b"opaque metadata"
        first = Object(TYPE_DATA, b"a" * 32, len(first_body))
        second = Object(TYPE_METADATA, b"b" * 32, len(second_body))
        third = Object(TYPE_SNAPSHOT, b"c" * 32, 0)
        records = iter(((first, BytesIO(first_body)), (second, BytesIO(second_body)), (third, BytesIO())))
        first_length = OBJECT_HEADER_SIZE + first.length
        second_length = OBJECT_HEADER_SIZE + second.length

        monkeypatch.setattr("p8472backup.storage.file.packs._LOCATION_BUFFER_SIZE", 48)
        on_stored = Mock()
        pack = Pack.write_to(path, records, target_size=first_length + 1, on_stored=on_stored)

        assert pack == Pack(2, first_length + second_length)
        assert Pack.load_from(path) == pack
        assert on_stored.call_args_list == [
            call(first.object_id, HEADER_SIZE, first_length),
            call(second.object_id, HEADER_SIZE + first_length, second_length),
        ]
        assert path.read_bytes() == pack.to_header() + first.to_header() + first_body + second.to_header() + second_body
        output = BytesIO()
        assert Pack.read_object(path, HEADER_SIZE + first_length, second_length, output) == second
        assert output.getvalue() == second_body
        with pytest.raises(ValueError, match="record length"):
            Pack.read_object(path, HEADER_SIZE + first_length, second_length - 1, BytesIO())

        next_path = tmp_path / "next-pack"
        assert Pack.write_to(next_path, records, target_size=1) == Pack(1, OBJECT_HEADER_SIZE)
        assert Pack.read_object(next_path, HEADER_SIZE, OBJECT_HEADER_SIZE, BytesIO()) == third

    def test_truncated_body_never_publishes_pack(self, tmp_path: Path) -> None:
        path = tmp_path / "pack"
        obj = Object(TYPE_DATA, b"a" * 32, 2)
        with pytest.raises(ValueError, match="Truncated object body"):
            Pack.write_to(path, iter(((obj, BytesIO(b"a")),)))
        assert list(tmp_path.iterdir()) == []

    def test_directory_sync_failure_does_not_report_locations(self, tmp_path: Path) -> None:
        obj = Object(TYPE_DATA, b"a" * 32, 0)
        on_stored = Mock()
        with patch("p8472backup.storage.file.packs.os.fsync", side_effect=[None, OSError("sync failed")]), pytest.raises(OSError, match="sync failed"):
            Pack.write_to(tmp_path / "pack", iter(((obj, BytesIO()),)), on_stored=on_stored)
        on_stored.assert_not_called()

    def test_existing_destination_is_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "pack"
        path.write_bytes(b"existing")
        obj = Object(TYPE_DATA, b"a" * 32, 0)
        with pytest.raises(FileExistsError):
            Pack.write_to(path, iter(((obj, BytesIO()),)))
        assert path.read_bytes() == b"existing"
        assert list(tmp_path.iterdir()) == [path]
