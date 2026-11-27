"""Measure the in-memory location index with reproducible synthetic records."""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from resource import RUSAGE_SELF, getrusage
from struct import Struct
from threading import Event
from time import perf_counter

from p8472backup.archive.objects import ObjectId
from p8472backup.storage.file.index import LocationIndex, ObjectLocation


_RECORD = Struct("<32sQQQ")
_FIELDS = Struct("<QQQ")
_BATCH = 512
_QUERY_COUNT = 20_000


def entry_for(number: int) -> tuple[ObjectId, ObjectLocation]:
    object_id = sha256(number.to_bytes(8, "big")).digest()
    return object_id, ObjectLocation(number // 128, 14 + (number % 128) * 8192, 8192)


def generate_dataset(path: Path, entries: int) -> None:
    with path.open("xb") as output:
        for number in range(entries):
            object_id, location = entry_for(number)
            output.write(_RECORD.pack(object_id, location.pack_id, location.offset, location.length))


def build_index(backend: str, path: Path, entries: int) -> tuple[Callable[[ObjectId], ObjectLocation | None], Callable[[ObjectId, ObjectLocation], None], float]:
    started = perf_counter()
    if backend == "python":
        index = LocationIndex()
        get, put = index.get, index.put
    elif backend == "dict":
        dictionary: dict[ObjectId, ObjectLocation] = {}

        def get(object_id: ObjectId) -> ObjectLocation | None:
            return dictionary.get(object_id)

        def put(object_id: ObjectId, location: ObjectLocation) -> None:
            dictionary[object_id] = location
    else:
        table = importlib.import_module("borghash").HashTable(key_size=32, value_size=_FIELDS.size)

        def get(object_id: ObjectId) -> ObjectLocation | None:
            fields = table.get(object_id)
            if fields is None:
                return None
            return ObjectLocation(*_FIELDS.unpack(fields))

        def put(object_id: ObjectId, location: ObjectLocation) -> None:
            table[object_id] = _FIELDS.pack(location.pack_id, location.offset, location.length)

    with path.open("rb") as source:
        for first in range(0, entries, 4096):
            size = min(entries - first, 4096) * _RECORD.size
            data = source.read(size)
            if len(data) != size:
                raise ValueError("Dataset is shorter than the requested index")
            for object_id, pack_id, offset, length in _RECORD.iter_unpack(data):
                put(object_id, ObjectLocation(pack_id, offset, length))
    return get, put, perf_counter() - started


def exercise(
    get: Callable[[ObjectId], ObjectLocation | None],
    put: Callable[[ObjectId, ObjectLocation], None] | None,
    entries: int,
    worker: int,
    start: Event,
) -> tuple[float, float]:
    rng = random.Random(worker)  # noqa: S311 -- reproducible benchmark.
    lookup_seconds = 0.0
    start.wait()
    for first in range(0, _QUERY_COUNT, _BATCH):
        queries: list[tuple[ObjectId, ObjectLocation | None]] = []
        for number in range(first, min(first + _BATCH, _QUERY_COUNT)):
            hit = number % 10 != 0
            object_id, location = entry_for(rng.randrange(entries) if hit else entries + number)
            queries.append((object_id, location if hit else None))
        started = perf_counter()
        results = [get(key) for key, _ in queries]
        lookup_seconds += perf_counter() - started
        if results != [expected for _, expected in queries]:
            raise RuntimeError("Incorrect lookup result")
    update_seconds = 0.0
    if put is not None:
        updates = [entry_for(entries + _QUERY_COUNT + number) for number in range(256)]
        started = perf_counter()
        for object_id, location in updates:
            put(object_id, location)
        update_seconds = perf_counter() - started
        if any(get(object_id) != location for object_id, location in updates):
            raise RuntimeError("Incorrect update result")
    return lookup_seconds, update_seconds


def resident_mib() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    raise RuntimeError("VmRSS is unavailable")


def measure(backend: str, path: Path, entries: int, mode: str) -> dict[str, object]:
    if backend == "borghash":
        importlib.import_module("borghash")
    baseline_mib = resident_mib()
    started = perf_counter()
    start = Event()
    mounts = [build_index(backend, path, entries) for _ in range(0 if mode == "single" else 2)]
    builds = [duration for _, _, duration in mounts]
    results: list[tuple[float, float]]
    if mode in ("single", "push"):
        backups = [build_index(backend, path, entries) for _ in range(3 if mode == "push" else 1)]
        builds.extend(duration for _, _, duration in backups)
        with ThreadPoolExecutor(max_workers=len(mounts) + len(backups)) as pool:
            try:
                work = [pool.submit(exercise, get, None, entries, worker, start) for worker, (get, _, _) in enumerate(mounts)]
                work.extend(pool.submit(exercise, get, put, entries, worker + len(mounts), start) for worker, (get, put, _) in enumerate(backups))
            finally:
                start.set()
            results = [job.result() for job in work]
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            try:
                work = [pool.submit(exercise, get, None, entries, worker, start) for worker, (get, _, _) in enumerate(mounts)]
                results = []
                for worker in range(3):
                    get, put, duration = build_index(backend, path, entries)
                    builds.append(duration)
                    start.set()
                    results.append(exercise(get, put, entries, worker + len(mounts), start))
                    del get, put
                results.extend(job.result() for job in work)
            finally:
                start.set()

    queries = _QUERY_COUNT * len(results)
    return {
        "backend": backend,
        "mode": mode,
        "entries_per_index": entries,
        "checked_queries": queries,
        "build_seconds_sum": sum(builds),
        "lookup_us_mean": sum(lookup for lookup, _ in results) * 1e6 / queries,
        "update_seconds_sum": sum(update for _, update in results),
        "elapsed_seconds": perf_counter() - started,
        "baseline_rss_mib": baseline_mib,
        "end_rss_mib": resident_mib(),
        "peak_rss_mib": getrusage(RUSAGE_SELF).ru_maxrss / 1024,
        "python": platform.python_version(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--entries", type=int, required=True)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--backend", choices=("python", "dict", "borghash"), default="python")
    parser.add_argument("--mode", choices=("single", "pull", "push"), default="single")
    args = parser.parse_args()
    if args.entries < 1:
        parser.error("entries must be positive")
    if args.generate:
        generate_dataset(args.dataset, args.entries)
    else:
        print(json.dumps(measure(args.backend, args.dataset, args.entries, args.mode)))


if __name__ == "__main__":
    main()
