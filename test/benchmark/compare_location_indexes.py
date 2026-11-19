"""Opt-in comparison of location indexes, outside the ordinary test suite.

Run with src/ and the optional native benchmark libraries on PYTHONPATH.
All index operations and mount readers share one measured child process.
"""

from __future__ import annotations

import argparse
import gc
import importlib
import importlib.metadata
import json
import multiprocessing
import os
import platform
import random
import resource
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from multiprocessing.connection import Connection
from pathlib import Path
from struct import Struct
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter
from typing import Any, NamedTuple

from p8472backup.archive.objects import HEADER_SIZE as OBJECT_HEADER_SIZE, OBJECT_ID_SIZE
from p8472backup.storage.file.index import ObjectLocation
from p8472backup.storage.file.packs import HEADER_SIZE as PACK_HEADER_SIZE


_VALUE = Struct("<QQQ")  # Benchmark encoding only; not a repository format.
_MIB = 1024**2
_PLYVEL_OPTIONS = {
    "lru_cache_size": 4 * _MIB,
    "write_buffer_size": _MIB,
    "max_open_files": 64,
    "bloom_filter_bits": 10,
    "compression": None,
    "paranoid_checks": True,
}


class LocationValue(NamedTuple):
    """Value shape required by borghash's persistence API."""

    pack_id: int
    offset: int
    length: int


class LocationFormat(NamedTuple):
    """Field formats required by borghash's namedtuple API."""

    pack_id: str = "Q"
    offset: str = "Q"
    length: str = "Q"


class LocationIndexComparison:
    """Exercise independent client indexes with serial or concurrent backups."""

    def __init__(self, directory: Path, entries: int, clients: int, mounts: int, queries: int, batch_size: int) -> None:
        self.directory = directory
        self.entries = entries
        self.clients = clients
        self.mounts = mounts
        self.queries = queries
        self.batch_size = batch_size

    @staticmethod
    def location(client: int, number: int) -> ObjectLocation:
        """Generate reproducible synthetic IDs and values, including >4 GiB fields."""
        return ObjectLocation(
            object_id=sha256(f"{client}:{number}".encode("ascii")).digest(),
            pack_id=2**32 + client,
            offset=PACK_HEADER_SIZE + number * (2**34 + OBJECT_HEADER_SIZE),
            length=2**34 + OBJECT_HEADER_SIZE,
        )

    @staticmethod
    def encoded(location: ObjectLocation) -> bytes:
        return _VALUE.pack(location.pack_id, location.offset, location.length)

    def path(self, backend: str, client: int) -> Path:
        return self.directory / f"{backend}-{client}"

    def open_sqlite(self, client: int) -> sqlite3.Connection:
        """Configure one connection, handed to exactly one worker at a time."""
        connection = sqlite3.connect(self.path("sqlite3", client), isolation_level="DEFERRED", check_same_thread=False)
        try:
            if connection.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
                raise RuntimeError("SQLite did not enable WAL")
            for setting, requested in (("synchronous", 2), ("cache_size", -4096), ("mmap_size", 0)):
                connection.execute(f"PRAGMA {setting}={requested}")
                if connection.execute(f"PRAGMA {setting}").fetchone() != (requested,):
                    raise RuntimeError(f"SQLite rejected {setting}={requested}")
        except BaseException:
            connection.close()
            raise
        return connection

    def prepare(self, backend: str) -> dict[str, object]:
        """Build indexes serially, in a process separate from the measurements."""
        module = importlib.import_module(backend)
        started = perf_counter()
        for client in range(self.clients + self.mounts):
            if backend == "borghash":
                table = module.HashTableNT(key_size=OBJECT_ID_SIZE, value_type=LocationValue, value_format=LocationFormat())
                for number in range(self.entries):
                    location = self.location(client, number)
                    table[location.object_id] = LocationValue(location.pack_id, location.offset, location.length)
                with self.path(backend, client).open("wb") as output:
                    table.write(output)
                    output.flush()
                    os.fsync(output.fileno())
                del table
            elif backend == "plyvel":
                with module.DB(str(self.path(backend, client)), create_if_missing=True, **_PLYVEL_OPTIONS) as database:
                    for first in range(0, self.entries, 8192):
                        with database.write_batch(transaction=True, sync=True) as batch:
                            for number in range(first, min(first + 8192, self.entries)):
                                location = self.location(client, number)
                                batch.put(location.object_id, self.encoded(location))
            elif backend == "sqlite3":
                with closing(self.open_sqlite(client)) as connection:
                    connection.execute("CREATE TABLE locations (object_id BLOB PRIMARY KEY CHECK(length(object_id)=32), location BLOB NOT NULL CHECK(length(location)=24)) WITHOUT ROWID")
                    for first in range(0, self.entries, 8192):
                        with connection:
                            for number in range(first, min(first + 8192, self.entries)):
                                location = self.location(client, number)
                                connection.execute("INSERT INTO locations VALUES (?, ?)", (location.object_id, self.encoded(location)))
            else:
                # Virtual address space/file capacity, never a claimed RAM limit.
                with module.open(str(self.path(backend, client)), map_size=max(64 * _MIB, self.entries * 512)) as environment:
                    preparation_batch = 8192
                    for first in range(0, self.entries, preparation_batch):
                        with environment.begin(write=True) as transaction:
                            for number in range(first, min(first + preparation_batch, self.entries)):
                                location = self.location(client, number)
                                if not transaction.put(location.object_id, self.encoded(location), overwrite=False):
                                    raise RuntimeError("Duplicate generated object ID")
        return {"prepare_s": perf_counter() - started}

    def queries_for(self, client: int, generator: random.Random, count: int) -> list[tuple[bytes, bytes | None]]:
        """Generate one bounded batch outside the timed lookup section."""
        requests = []
        for number in range(count):
            missing = number % 10 == 0
            index = generator.randrange(self.entries) + (self.entries if missing else 0)
            location = self.location(client, index)
            requests.append((location.object_id, None if missing else self.encoded(location)))
        requests.sort(key=lambda request: request[0])
        return requests

    def exercise(self, backend: str, client: int, index: Any, mount: bool, start: Event) -> tuple[int, float, float]:
        """Check hits and misses; backup workers then persist one update batch."""
        generator = random.Random(client)  # noqa: S311 -- reproducible workload, not security.
        completed = 0
        lookup_s = 0.0
        start.wait()
        while completed < self.queries:
            requests = self.queries_for(client, generator, min(self.batch_size, self.queries - completed))
            keys = [key for key, _ in requests]
            expected_values = {key: value for key, value in requests if value is not None}
            started = perf_counter()
            if backend == "borghash":
                for key, expected in requests:
                    value = index.get(key)
                    actual = None if value is None else _VALUE.pack(*value)
                    if actual != expected:
                        raise RuntimeError("Hash lookup returned an incorrect location")
            elif backend == "plyvel":
                for key, expected in requests:
                    if index.get(key, verify_checksums=True) != expected:
                        raise RuntimeError("LevelDB lookup returned an incorrect location")
            elif backend == "sqlite3":
                placeholders = ",".join("?" for _ in keys)
                sql = "SELECT object_id, location FROM locations WHERE object_id IN (" + placeholders + ")"  # noqa: S608 -- only placeholders are generated.
                if dict(index.execute(sql, keys)) != expected_values:
                    raise RuntimeError("SQLite batch lookup returned incorrect locations")
            else:
                with index.begin() as transaction:
                    with transaction.cursor() as cursor:
                        if dict(cursor.getmulti(keys)) != expected_values:
                            raise RuntimeError("LMDB batch lookup returned incorrect locations")
            lookup_s += perf_counter() - started
            completed += len(requests)
        write_s = 0.0
        if not mount:
            updates = [self.location(client, 2 * self.entries + number) for number in range(self.batch_size)]
            started = perf_counter()
            if backend == "borghash":
                for location in updates:
                    index[location.object_id] = LocationValue(location.pack_id, location.offset, location.length)
                # Full-table checkpoint, not Borg's index-fragment protocol.
                with self.path(backend, client).open("wb") as output:
                    index.write(output)
                    output.flush()
                    os.fsync(output.fileno())
            elif backend == "plyvel":
                with index.write_batch(transaction=True, sync=True) as batch:
                    for location in updates:
                        batch.put(location.object_id, self.encoded(location))
            elif backend == "sqlite3":
                with index:
                    index.executemany(
                        "INSERT INTO locations VALUES (?, ?) ON CONFLICT(object_id) DO UPDATE SET location=excluded.location",
                        ((location.object_id, self.encoded(location)) for location in updates),
                    )
            else:
                with index.begin(write=True) as transaction:
                    for location in updates:
                        transaction.put(location.object_id, self.encoded(location))
            write_s = perf_counter() - started
        return completed, lookup_s, write_s

    def measure(self, backend: str, mode: str, cache: str) -> dict[str, object]:
        """Include all clients, readers, interpreter, native allocations, and maps."""
        module = importlib.import_module(backend)
        baseline_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        if cache == "advised":
            for path in self.directory.glob(f"{backend}-*"):
                files = list(path.iterdir()) if backend == "plyvel" else [path if path.is_file() else path / "data.mdb"]
                for data in files:
                    if data.is_file():
                        with data.open("rb") as source:
                            os.posix_fadvise(source.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        start = Event()
        opened: list[Any] = []
        load_s = 0.0

        def open_index(client: int) -> Any:
            nonlocal load_s
            started = perf_counter()
            if backend == "borghash":
                handle = module.HashTableNT.read(str(self.path(backend, client)))
            elif backend == "plyvel":
                handle = module.DB(str(self.path(backend, client)), **_PLYVEL_OPTIONS)
            elif backend == "sqlite3":
                handle = self.open_sqlite(client)
            else:
                handle = module.open(str(self.path(backend, client)), map_size=max(64 * _MIB, self.entries * 512))
            load_s += perf_counter() - started
            return handle

        results: list[tuple[int, float, float]] = []
        resident_mib: dict[str, float] = {}
        started = perf_counter()
        try:
            # Mount different client repositories: aggregate RAM is the constraint.
            for client in range(self.clients, self.clients + self.mounts):
                opened.append(open_index(client))
            # Load before starting workers so push clients really overlap.
            active = [open_index(client) for client in range(self.clients if mode == "push" else 1)]
            if mode == "push":
                opened.extend(active)
            with ThreadPoolExecutor(max_workers=self.mounts + (self.clients if mode == "push" else 1)) as pool:
                readers = [pool.submit(self.exercise, backend, client, opened[number], True, start) for number, client in enumerate(range(self.clients, self.clients + self.mounts))]
                try:
                    if mode == "push":
                        backups = [pool.submit(self.exercise, backend, client, handle, False, start) for client, handle in enumerate(active)]
                        start.set()
                        results.extend(future.result() for future in backups)
                    else:
                        for client in range(self.clients):
                            handle = active.pop() if client == 0 else open_index(client)
                            future = pool.submit(self.exercise, backend, client, handle, False, start)
                            start.set()
                            results.append(future.result())
                            if backend in ("lmdb", "plyvel", "sqlite3"):
                                handle.close()
                            del future, handle
                            gc.collect()
                finally:
                    start.set()
                results.extend(future.result() for future in readers)
            # Current memory before closing the remaining handles; not peak components.
            for line in Path("/proc/self/status").read_text().splitlines():
                fields = line.split()
                if fields[0] in ("RssAnon:", "RssFile:"):
                    resident_mib[fields[0][:-1]] = round(int(fields[1]) / 1024, 2)
        finally:
            for handle in opened:
                if backend in ("lmdb", "plyvel", "sqlite3"):
                    handle.close()
        elapsed = perf_counter() - started
        peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        counts, times, writes = zip(*results, strict=True)
        if backend == "sqlite3":
            version = sqlite3.sqlite_version
        else:
            try:
                version = importlib.metadata.version(backend)
            except importlib.metadata.PackageNotFoundError:
                version = "source build (record revision separately)"
        return {
            "backend": backend,
            "version": version,
            "plyvel_options": _PLYVEL_OPTIONS if backend == "plyvel" else None,
            "mode": mode,
            "cache": "OS cache uncontrolled" if cache == "warm" else "DONTNEED requested; not guaranteed cold",
            "entries_per_client": self.entries,
            "backup_clients": self.clients,
            "mount_clients": self.mounts,
            "baseline_mib": round(baseline_mib, 2),
            "peak_rss_mib": round(peak_mib, 2),
            "end_resident_mib": resident_mib,
            "within_64_mib": peak_mib <= 64,
            "within_128_mib": peak_mib <= 128,
            "load_s": round(load_s, 4),
            "elapsed_s": round(elapsed, 4),
            "checked_queries": sum(counts),
            "lookup_us_mean": round(sum(times) / sum(counts) * 1e6, 3),
            "persist_s_sum": round(sum(writes), 4),
        }

    def child(self, connection: Connection, backend: str, mode: str, cache: str) -> None:
        """Propagate failures instead of emitting successful-looking partial results."""
        try:
            result = self.prepare(backend) if mode == "prepare" else self.measure(backend, mode, cache)
            connection.send(result)
        except Exception as error:
            connection.send({"error": f"{type(error).__name__}: {error}"})
            raise
        finally:
            connection.close()

    def isolated(self, backend: str, mode: str, cache: str = "warm") -> dict[str, object]:
        """A fresh process prevents RSS high-water marks leaking across cases."""
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=self.child, args=(send, backend, mode, cache))
        process.start()
        send.close()
        try:
            result: dict[str, object] = receive.recv()
        finally:
            receive.close()
            process.join()
        if process.exitcode != 0 or "error" in result:
            raise RuntimeError(result)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="Parent directory for temporary index files")
    parser.add_argument("--backend", choices=("borghash", "lmdb", "plyvel", "sqlite3"), help="Measure only one candidate; default: lmdb, plyvel and sqlite3")
    parser.add_argument("--entries", type=int, default=100_000, help="Initial objects per client")
    parser.add_argument("--clients", type=int, default=3, help="Backup clients: serial for pull, concurrent for push")
    parser.add_argument("--mounts", type=int, choices=(0, 1, 2), default=2, help="Additional concurrently read client repositories")
    parser.add_argument("--queries", type=int, default=20_000, help="Minimum checked lookups per worker")
    parser.add_argument("--batch-size", type=int, default=256, help="Bounded request batch and final update count")
    args = parser.parse_args()
    if min(args.entries, args.clients, args.queries, args.batch_size) < 1 or args.batch_size > args.entries:
        parser.error("Counts must be positive and batch size must not exceed entries")
    backends = (args.backend,) if args.backend else ("lmdb", "plyvel", "sqlite3")
    for backend in backends:
        importlib.import_module(backend)  # Fail before creating data if a candidate is missing.
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(), "directory": str(args.directory.resolve()), "entry_bytes": OBJECT_ID_SIZE + _VALUE.size}), flush=True)
    with TemporaryDirectory(prefix="8472backup-index-", dir=args.directory) as temporary:
        comparison = LocationIndexComparison(Path(temporary), args.entries, args.clients, args.mounts, args.queries, args.batch_size)
        for backend in backends:
            print(json.dumps({"backend": backend, **comparison.isolated(backend, "prepare")}), flush=True)
            for mode in ("pull", "push"):
                for cache in ("warm", "advised"):
                    print(json.dumps(comparison.isolated(backend, mode, cache)), flush=True)


if __name__ == "__main__":
    main()
