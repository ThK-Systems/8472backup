# Plyvel / LevelDB Evaluation

Status: investigated and benchmarked, 2026-11-19. Candidate only; no production dependency, repository encoding, or index implementation is adopted.

## Compatibility and maintenance

Trixie provides `python3-plyvel` 1.5.1-1+b3, `libleveldb1d` 1.23-5+b2, and `libsnappy1v5` 1.2.2-1 on amd64. Plyvel uses a Cython binding to the native C++ library. The binary Python binding requires CPython 3.13; the project minimum remains 3.12, which upstream Plyvel 1.5.1 also supports. The actual operation tests used CPython 3.13.5.

LevelDB explicitly treats API stability as a contribution requirement, but also states that maintenance is very limited. Plyvel exposes a documented public API and has a long 1.x release history; its documented 1.5.1 release is dated 2024-01-15. No equivalent explicit long-term Python API compatibility commitment was found. Plyvel describes itself as a spare-time project. These are maintenance considerations, not evidence that its API is currently declared unstable like borghash's.

The test packages were downloaded and extracted under `/tmp`, without pip or system installation. Runtime dependencies in `pyproject.toml` are unchanged.

## Fit with the server model

LevelDB allows one process to own a database and multiple threads to share its database handle. This fits the proposed server service: mount and backup requests must go through that owner rather than independently open the same index in separate processes. Handles must remain open until their users finish; batches and iterators must not be shared concurrently without coordination.

Atomic write batches and consistent read snapshots are available. A write batch is not a general read/write transaction and does not atomically publish pack files and the authoritative snapshot catalogue. The existing client-repository mutation lock remains relevant; repository commit and recovery still need their own reviewed design.

Synchronous writes require explicit `sync=True`; the Plyvel default is asynchronous. The benchmark uses synchronous batches, exception-safe `transaction=True`, checksums on reads, and paranoid checks. The database's internal checksums do not replace object authentication or pack integrity design.

A focused temporary-database check passed with one writer and two readers sharing one handle: six atomic updates, 100 consistent read snapshots, rollback of a batch whose context raised an exception, and reopening after synchronous writes. This tests ordinary operation, not power-loss durability.

## Memory and HDD considerations

The comparison uses these explicit per-database settings:

| Setting | Value |
| --- | --- |
| Block cache | 4 MiB |
| Write buffer | 1 MiB |
| Maximum open files | 64 |
| Bloom filter | 10 bits per key |
| Index-block compression | Disabled for this experiment |

LevelDB may retain two write buffers at once. With five simultaneously open indexes, the nominal block caches and two write buffers total about 30 MiB. This excludes interpreter state, table metadata, filters, allocator overhead, background compaction, and file mappings; it is not a bound on total memory.

In particular, LevelDB 1.23's POSIX implementation permits read-only memory mappings on 64-bit systems. Resident file pages can therefore increase RSS despite the configured block cache. `end_resident_mib` records current anonymous and file-backed resident memory before the remaining handles are closed. These components are not components of the historical RSS peak. In pull mode, completed backup indexes have already closed at this point; the mount indexes remain open. RSS also excludes unmapped kernel file cache, as explained in the earlier comparison.

LevelDB flushes and compacts sorted table files in the background. These operations add I/O and can affect HDD latency. The short benchmark includes ordinary opening/recovery and any work that overlaps operations/closing, but does not establish sustained compaction performance or a compaction memory ceiling. A long mixed-update workload under an enforced shared limit on actual HDD storage remains necessary before adoption.

## Reproduction and results

Use the existing [comparison setup](04-LOCATION-INDEX-BENCHMARK.md), replacing the borghash build with extraction of `python3-plyvel`, `libleveldb1d`, and `libsnappy1v5`. Keep the extracted package directory on `PYTHONPATH` and the native library directory on `LD_LIBRARY_PATH`. No compilation is needed for the Trixie CPython 3.13 packages.

```sh
PYTHONPATH="src:$PYTHONPATH" python3 test/benchmark/compare_location_indexes.py --directory . --backend plyvel --entries 100000
PYTHONPATH="src:$PYTHONPATH" python3 test/benchmark/compare_location_indexes.py --directory . --backend plyvel --entries 1000000
```

Use a parent directory on the intended storage filesystem, not tmpfs. Runs use the same synthetic IDs, 56-byte records, three backup clients, two additional mount repositories, and 100,000 verified lookups as the original comparison. Writes persist one 256-entry batch per backup. Library snapshots in the focused concurrency check are database read views, unrelated to backup snapshots.

The raw results are recorded separately in `05-PLYVEL-RESULTS.json`, retaining the original measurements. These are exploratory runs on the same WSL2 ext4 virtual disk, with no enforced memory limit and no physical HDD measurement. Historical LMDB timings are comparison references, not simultaneous controlled trials. The manual comparison was subsequently extended to include [SQLite](06-SQLITE-EVALUATION.md); borghash requires explicit selection as a historical reference.

| Entries/client | Mode, plus two mounts | Wall time, s | Peak process RSS, MiB |
| --- | --- | ---: | ---: |
| 100,000 | Pull | 1.017 | 52.62 |
| 100,000 | Push | 4.197 | 57.99 |
| 1,000,000 | Pull | 1.952 | 208.32 |
| 1,000,000 | Push | 5.130 | 340.23 |

At 100,000 entries per client these runs fit the 128 MiB process-RSS target; the largest dataset exceeds it. The million-entry push case ended with 13.07 MiB anonymous RSS and 297.26 MiB file-backed RSS, compared with a 340.23 MiB earlier peak. Thus the block-cache setting must not be presented as a process-memory bound. File-backed pages are a major part of the measured footprint; suitability under an enforced limit and the associated HDD I/O cost remain unproven.

Compared with the earlier LMDB push timings of 10.175 s and 12.067 s, Plyvel is faster in this harness. Both candidates incur significant concurrent-thread overhead; the benchmark does not isolate its cause. The assessment supports keeping Plyvel on the shortlist, not selecting it for production. No unconditional Python API stability guarantee was established.

## References

- [Trixie package and native dependencies](https://packages.debian.org/trixie/python3-plyvel)
- [Plyvel public API, cache options, checksums, and synchronous writes](https://plyvel.readthedocs.io/en/latest/api.html)
- [Plyvel release history](https://plyvel.readthedocs.io/en/latest/news.html)
- [Plyvel project maintenance statement](https://plyvel.readthedocs.io/en/latest/)
- [LevelDB API stability and maintenance policy](https://github.com/google/leveldb#contributing-to-the-leveldb-project)
- [LevelDB concurrency and write semantics](https://github.com/google/leveldb/blob/1.23/doc/index.md)
- [LevelDB buffer and cache options](https://github.com/google/leveldb/blob/1.23/include/leveldb/options.h)
- [LevelDB POSIX memory mappings](https://github.com/google/leveldb/blob/1.23/util/env_posix.cc)
