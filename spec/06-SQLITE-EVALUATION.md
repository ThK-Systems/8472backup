# SQLite Location-Index Evaluation

Status: approved for comparison, 2026-11-19. The earlier blanket exclusion is lifted for this location-index experiment. No production engine, catalogue persistence, or repository format is selected.

## Setup and scope

The manual comparison now accepts `--backend sqlite3`, using Python's standard-library binding to the installed C library. Tested versions: CPython 3.13.5, SQLite 3.46.1, Debian `libsqlite3-0` 3.46.1-7+deb13u2. No package installation or additional project dependency is required.

Each client index uses a temporary `WITHOUT ROWID` table with a 32-byte BLOB primary key and a 24-byte BLOB location value. The latter uses the comparison's existing three unsigned 64-bit fields, retaining the full range rather than mapping them to SQLite's signed integers. Both BLOB lengths are checked by the experimental schema. This is a synthetic test encoding, not an approved repository format.

Connections use WAL, `synchronous=FULL`, `cache_size=-4096` (a suggested 4 MiB page-cache limit per connection), and `mmap_size=0` for database-file mappings. Every requested setting is read back and verified. The WAL shared-memory index is separate from database-file mapping. The nominal page caches of five concurrent indexes total 20 MiB, excluding interpreter and other SQLite allocations. No hard process-memory limit is imposed.

There is one connection per active client index in the benchmark. Each handle is transferred to exactly one worker at a time; it is not used concurrently. `check_same_thread=False` permits that transfer, not arbitrary concurrent use. Real simultaneous readers of the same client repository would need their own connections or serialized access, and additional connections would consume additional cache memory. The proposed client-repository mutation lock still serializes writers.

Lookups use bounded sorted batches and a parameterized `SELECT ... IN (...)`; result dictionaries are checked against expected hits and misses. Initial inserts commit in batches of 8,192. Each measured backup persists 256 entries in one synchronous transaction. Preparation is isolated from measured operations. Opening, lookup verification, updates, closing, and associated checkpoints are included in operation wall time. WAL checkpointing remains enabled at its default threshold.

The workload matches the earlier comparison: three serial pull or concurrent push clients, two additional mount repositories, 100,000 verified lookups in total, and either 100,000 or 1,000,000 initial entries per client. Mount readers issue one bounded burst; this is not sustained mount traffic or pack I/O.

## Correctness and version checks

A focused temporary-database check used one writer and two reader connections on the same file. Six write transactions overlapped 100 read transactions; each read transaction checked that repeated queries saw one consistent generation. An intentional exception rolled back an update. Committed data survived reopening and `PRAGMA integrity_check` returned `ok`. No concurrent checkpoint was launched by another connection. This is ordinary-operation verification, not a power-loss test.

Upstream documents a WAL-reset corruption bug fixed in 3.51.3, with selected earlier backports. Its triggering pattern requires at least two connections to the same file with overlapping writes/checkpoints. The installed Debian changelog does not document this fix, and its backport status has not been established. The main benchmark uses one connection per file and does not exercise that pattern. Confirm a fixed Debian build or an explicitly reviewed safe connection/checkpoint model before selecting WAL for production. Passing these tests does not establish that the installed build contains the fix.

## Reproduction and memory interpretation

From the repository root, using the same interpreter as the project:

```sh
PYTHONPATH=src python3 test/benchmark/compare_location_indexes.py --directory . --backend sqlite3 --entries 100000
PYTHONPATH=src python3 test/benchmark/compare_location_indexes.py --directory . --backend sqlite3 --entries 1000000
```

No optional native benchmark libraries are needed when selecting only SQLite. Use a parent directory on the target filesystem, not tmpfs. The script creates and removes only its own temporary directory.

The measurements use the same WSL2 ext4 virtual disk as the earlier runs. The physical backing medium is unknown. Existing-cache and advisory `DONTNEED` cases are recorded separately; neither proves a cold physical HDD test. RSS includes all operation threads and active connections, but excludes the controller, preparation, and unmapped filesystem page cache. In particular, unmapped cached database pages still consume system memory even though they do not appear in SQLite's process RSS. Comparing RSS across mapped and unmapped engines alone is insufficient to judge total physical-memory cost under a cgroup limit.

The raw measurements are stored in `06-SQLITE-RESULTS.json`. The earlier engine measurements remain unchanged and are exploratory historical comparisons, not simultaneous trials. The configured caches are not a hard 64–128 MiB bound; other server functions still need their share of that budget. The threading/GIL and binding costs in this harness are not isolated from lookup time.

## Recorded results

| Entries/client | Mode, plus two mounts | Wall time, s | Peak process RSS, MiB |
| --- | --- | ---: | ---: |
| 100,000 | Pull | 1.053 | 45.10 |
| 100,000 | Push | 12.261 | 46.32 |
| 1,000,000 | Pull | 1.524 | 44.88 |
| 1,000,000 | Push | 12.812 | 46.50 |

These existing-cache cases stay below 64 MiB process RSS at both entry counts. This does not demonstrate a hard total physical-memory bound, because unmapped filesystem page cache is excluded. At one million entries, the advisory DONTNEED cases took 11.835 s for pull and 8.605 s for push. These are individual runs with uncontrolled host caches and thread scheduling; the faster advisory push run is not evidence that physical cold reads are faster.

The existing-cache push runs are slower than the earlier Plyvel runs (4.197 s and 5.130 s) and in the same broad range as LMDB (10.175 s and 12.067 s). These observations do not isolate SQL, binding/GIL, scheduling, and storage costs. SQLite remains a candidate; selecting it requires a representative constrained-memory test and resolution of the WAL-version question. No production schema or engine is adopted.

## References

- [Python sqlite3 binding and connection/transaction semantics](https://docs.python.org/3.13/library/sqlite3.html)
- [SQLite WAL, concurrency, checkpointing, and WAL-reset bug](https://www.sqlite.org/wal.html)
- [SQLite cache and mapping pragmas](https://www.sqlite.org/pragma.html)
- [WITHOUT ROWID tables](https://www.sqlite.org/withoutrowid.html)
