"""Represent the plaintext content of archive data objects."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from struct import Struct

from p8472backup.archive.objects import MAX_OBJECT_BODY_SIZE


_DATA_SUBHEADER = Struct("<BQ")

COMPRESSION_NONE = 0
COMPRESSION_LZ4 = 1
COMPRESSION_ZSTD = 2


@dataclass(frozen=True)
class DataObject:
    """Client-side plaintext content of a type 1 archive object."""

    compression: int
    chunk_size: int
    data: bytes

    def __post_init__(self) -> None:
        if self.compression not in (COMPRESSION_NONE, COMPRESSION_LZ4, COMPRESSION_ZSTD):
            raise ValueError(f"Unsupported chunk compression: {self.compression}")
        if self.chunk_size != len(self.data):
            raise ValueError("Uncompressed data length must match chunk_size")
        if _DATA_SUBHEADER.size + len(self.data) > MAX_OBJECT_BODY_SIZE:
            raise ValueError("Data object exceeds the stored body size limit")

    def to_plaintext(self) -> bytes:
        """Encode the subheader and chunk together for client-side encryption."""
        data = self.data
        if self.compression == COMPRESSION_LZ4:
            data = import_module("lz4.frame").compress(data)
        elif self.compression == COMPRESSION_ZSTD:
            zstd = import_module("zstandard")
            data = zstd.ZstdCompressor(write_content_size=bool(data)).compress(data)

        if _DATA_SUBHEADER.size + len(data) > MAX_OBJECT_BODY_SIZE:
            raise ValueError("Data object exceeds the stored body size limit")
        return _DATA_SUBHEADER.pack(self.compression, self.chunk_size) + data

    @classmethod
    def from_plaintext(cls, plaintext: bytes, compression: int | None = None) -> DataObject:
        """Decode authenticated plaintext and optionally verify its compression ID."""
        if not _DATA_SUBHEADER.size <= len(plaintext) <= MAX_OBJECT_BODY_SIZE:
            raise ValueError(f"Invalid data object plaintext length: {len(plaintext)}")
        stored_compression: int
        chunk_size: int
        stored_compression, chunk_size = _DATA_SUBHEADER.unpack_from(plaintext)
        if compression is not None and compression != stored_compression:
            raise ValueError("Stored compression does not match expected compression")
        if _DATA_SUBHEADER.size + chunk_size > MAX_OBJECT_BODY_SIZE:
            raise ValueError(f"Invalid chunk size: {chunk_size}")

        data = plaintext[_DATA_SUBHEADER.size :]
        if stored_compression == COMPRESSION_LZ4:
            decoder = import_module("lz4.frame").LZ4FrameDecompressor()
            try:
                data = decoder.decompress(data, max_length=chunk_size + 1)
            except RuntimeError as error:
                raise ValueError("Invalid LZ4 chunk") from error
            if not decoder.eof or decoder.unused_data:
                raise ValueError("Expected exactly one complete LZ4 frame")
        elif stored_compression == COMPRESSION_ZSTD:
            zstd = import_module("zstandard")
            try:
                frame_size = zstd.frame_content_size(data)
                expected_frame_size = chunk_size if chunk_size else -1
                if frame_size != expected_frame_size:
                    raise ValueError("Invalid Zstandard frame content size")
                decoder = zstd.ZstdDecompressor()
                decoded: bytes = decoder.decompress(data, max_output_size=max(1, chunk_size), allow_extra_data=False)
                if not chunk_size and not decoded:
                    # The bounded call above checks the output. For a frame without a
                    # content size, python-zstandard 0.23 misses trailing input.
                    frame = decoder.decompressobj()
                    frame.decompress(data)
                    if not frame.eof or frame.unused_data:
                        raise ValueError("Expected exactly one complete Zstandard frame")
                data = decoded
            except zstd.ZstdError as error:
                raise ValueError("Invalid Zstandard chunk") from error

        return cls(stored_compression, chunk_size, data)
