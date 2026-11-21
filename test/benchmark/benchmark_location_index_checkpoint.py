"""Measure durable location index saves and warm-cache loads."""

import argparse
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from benchmark_in_memory_location_index import entry_for

from p8472backup.storage.file.index import LocationIndex
from p8472backup.storage.file.lock import RepositoryLock


def measure(directory: Path, entries: int, repeats: int) -> dict[str, object]:
    index = LocationIndex()
    for number in range(entries):
        object_id, location = entry_for(number)
        index.put(object_id, location)
    expected = [entry_for(number) for number in (0, entries // 2, entries - 1)]
    missing, _ = entry_for(entries)
    save_seconds = []
    load_seconds = []

    with TemporaryDirectory(prefix="index-checkpoint-", dir=directory) as temporary:
        repository = Path(temporary)
        checkpoint = repository / "locations"
        with RepositoryLock(repository) as lock:
            for _ in range(repeats):
                started = perf_counter()
                index.save_to(checkpoint, lock)
                save_seconds.append(perf_counter() - started)
            checkpoint_bytes = checkpoint.stat().st_size
            del index

            for _ in range(repeats):
                started = perf_counter()
                loaded = LocationIndex.load_from(checkpoint, lock)
                load_seconds.append(perf_counter() - started)
                if any(loaded.get(object_id) != location for object_id, location in expected) or loaded.get(missing) is not None:
                    raise RuntimeError("Loaded index returned unexpected locations")
                del loaded

    return {
        "entries": entries,
        "checkpoint_bytes": checkpoint_bytes,
        "save_seconds": save_seconds,
        "load_seconds": load_seconds,
        "median_save_seconds": median(save_seconds),
        "median_load_seconds": median(load_seconds),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("playground"), help="Existing directory for temporary checkpoint files")
    parser.add_argument("--entries", type=int, nargs="+", default=(100_000, 300_000, 1_000_000))
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1 or any(entries < 1 for entries in args.entries):
        parser.error("Entries and repeats must be positive")
    if not args.directory.is_dir():
        parser.error("--directory must name an existing directory")
    for entries in args.entries:
        print(json.dumps(measure(args.directory, entries, args.repeats)), flush=True)


if __name__ == "__main__":
    main()
