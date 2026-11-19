# Location Index Comparison

Status: exploratory measurement, 2026-11-19. No production index engine or persistent repository format is selected. The later [Plyvel assessment](05-PLYVEL-EVALUATION.md) extends this comparison; the original raw results remain unchanged.

Thomas subsequently rejected `borghash` as a production dependency because its [upstream stability notice](https://github.com/borgbackup/borghash#state-of-this-project) explicitly declares the API unstable and provides no migration tools for serialized tables during that phase. The measurements below remain historical comparison evidence. This decision does not authorize a custom index implementation.

## Scope

The opt-in [comparison](../test/benchmark/compare_location_indexes.py) exercises the approved `ObjectLocation` fields: a 32-byte key and three unsigned 64-bit integers, totaling 56 raw bytes. Integer values deliberately exceed 32-bit limits. Uniform synthetic SHA-256 keys stand in for object IDs; this does not change the development HMAC-SHA-256 decision. No pack payloads or snapshots are generated.

The candidates are:

- `borghash` 0.2.1, the native Cython hash-table library in the current Borg 2 dependency line. It stores the complete active index in RAM. The benchmark uses its `HashTableNT` API and library checkpoint format, with our smaller 24-byte values. This is not Borg's complete repository index implementation.
- `python3-lmdb` 1.4.1 with `liblmdb` 0.9.31 from Debian Trixie, using the C extension. Read transactions use `Cursor.getmulti()` on bounded, sorted batches. Default synchronous write transactions remain enabled.

Neither library is added to `pyproject.toml` or required by ordinary tests, builds, or runtime.

## Workload and accounting

By default, three backup clients each perform 20,000 checked lookups, approximately 90% hits and 10% misses, then write 256 locations. Two mount clients each perform another 20,000 lookups. The mounts refer to different client repositories, deliberately exposing the aggregate memory cost. Pull runs the three backups serially; push admits all three together. Mount readers start with the first backup and issue one bounded burst; their indexes remain open until all backups finish. This is not a simulation of sustained mount traffic or reads of pack contents.

Requests are reproducible. Both candidates receive the same keys and expected values. Query generation retains at most one batch per worker, not a Python object for every stored entry. Every lookup result is checked, including misses. Repeated measurement cases reuse the base dataset and the same added update IDs; the original base keys and expected misses remain unchanged.

Each case runs in a fresh child process. All its backup and mount workers run as threads in that process. Linux `ru_maxrss` includes the interpreter, native allocations, all active indexes, temporary request batches, and resident mapped pages. The 64 MiB and 128 MiB checks apply to this combined peak, not individual workers. Data preparation and the benchmark controller are separate processes and are excluded. Unmapped filesystem page cache is not attributed by this RSS metric; it is not cgroup memory accounting. The benchmark **reports** budget violations; it does not impose a kernel memory limit. A future server must also budget catalogues, transport and payload buffers, and its other runtime state.

`elapsed_s` is operation wall time including index opening, queries, validation, writes, and checkpoints. `lookup_us_mean` divides summed worker lookup time by checked requests, excluding request generation, index opening, and persistence. It includes verification and thread scheduling, so it is neither a pure C-library latency nor a throughput estimate. `persist_s_sum` sums worker persistence intervals, which may overlap during push.

The hash candidate checkpoints the full table once per backup, followed by `fsync`. LMDB persists the changed locations in one transaction per backup. These are different persistence costs: Borg's immutable index fragments are not implemented by this test. Neither crash consistency nor a production pack/index commit protocol is tested.

## Storage and cache limitations

The recorded operation runs use the project's ext4 filesystem on a WSL2 virtual disk, not `/tmp` (which is tmpfs here). The physical backing medium is unknown; no HDD path was available.

Each mode is measured with the existing OS cache and after requesting `POSIX_FADV_DONTNEED` for the index data files. The latter is advisory and does not flush the host's virtual-disk caches. Neither case proves a cold physical HDD measurement. The existing-cache case is not guaranteed fully warm.

LMDB maps files into virtual memory. Its mapping size is not a memory cap; clean resident mapped pages can be reclaimed by the operating system. A high uncapped RSS therefore does not prove that LMDB cannot operate under 128 MiB. It does establish that this run did not stay below that bound. A real constrained run must measure reclamation and the resulting I/O costs. Conversely, a full hash index needs its key/value arrays resident for normal RAM-speed lookup; several million active entries exceed the budget even before Python overhead.

These thread-based measurements include CPython's GIL and binding overhead. They do not establish the performance of a different server scheduler or multiple worker processes. Batched LMDB calls still need measurement under concurrent load; batching alone does not guarantee faster results.

## Reproducing without pip

Use Linux, a C compiler, and matching Python development headers. The example uses CPython 3.13 to match Trixie's binary `python3-lmdb` extension; other Python versions need a matching build of the binding. The project's Python minimum remains 3.12. Debian Trixie's `python3-lmdb`, `liblmdb0`, and `cython3` packages can be extracted into a temporary directory without system installation. Its `python3-borghash` 0.1.0 is not used as a proxy for current Borg 2.

Example preparation from the repository root, with `gcc` and Python headers already installed:

```sh
export INDEX_BENCH_ROOT=$(mktemp -d /tmp/8472backup-index-benchmark.XXXXXX)
cd "$INDEX_BENCH_ROOT"
apt-get download python3-lmdb liblmdb0 cython3
for package in ./*.deb; do dpkg-deb -x "$package" deps; done
curl -fL -o borghash-0.2.1.tar.gz https://github.com/borgbackup/borghash/archive/refs/tags/0.2.1.tar.gz
tar -xzf borghash-0.2.1.tar.gz
export PYTHONPATH="$INDEX_BENCH_ROOT/deps/usr/lib/python3/dist-packages:$INDEX_BENCH_ROOT/borghash-0.2.1/src"
export LD_LIBRARY_PATH="$INDEX_BENCH_ROOT/deps/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
```

Build the unchanged upstream extensions locally, using the same interpreter that will run the comparison:

```sh
python3 - <<'PY'
import os
import pathlib
import subprocess
import sys
import sysconfig

source = pathlib.Path(os.environ["INDEX_BENCH_ROOT"]) / "borghash-0.2.1/src/borghash"
for module in ("HashTable", "HashTableNT"):
    subprocess.run([sys.executable, "-m", "cython", "-3", str(source / f"{module}.pyx")], check=True)
    subprocess.run([
        "gcc", "-O3", "-fPIC", "-shared",
        "-I" + sysconfig.get_path("include"), str(source / f"{module}.c"),
        "-o", str(source / f"{module}{sysconfig.get_config_var('EXT_SUFFIX')}"),
    ], check=True)
PY
```

Return to the repository root. Choose a parent directory on the intended storage device, not a RAM filesystem. The script creates and removes only its own temporary subdirectory:

```sh
PYTHONPATH="src:$PYTHONPATH" python3 test/benchmark/compare_location_indexes.py --directory . --backend borghash --entries 100000
PYTHONPATH="src:$PYTHONPATH" python3 test/benchmark/compare_location_indexes.py --directory . --backend borghash --entries 1000000
```

`--clients`, `--mounts`, `--queries`, and `--batch-size` adjust the workload; `--mounts` accepts 0, 1, or 2. `--backend borghash`, `--backend lmdb`, `--backend plyvel`, or `--backend sqlite3` runs just one candidate. To reproduce the original LMDB measurements, use `--backend lmdb` with both entry counts. The current default compares LMDB, Plyvel, and SQLite; see the [SQLite assessment](06-SQLITE-EVALUATION.md). Do not launch unbounded datasets based on the nominal 56-byte entry size. Ordinary `pytest` does not collect this manual benchmark.

## Recorded environment

2026-11-19: CPython 3.13.5, Linux 6.6.87.2-microsoft-standard-WSL2, Intel Core i7-9750H, eight visible logical CPUs, GCC 14.2.0, Cython 3.0.11. The operation files reside under `/home/tkn/8472backup`, on ext4 backed by `/dev/sdd`. The benchmark does not change system cache settings or install packages globally.

The borghash source is the official `0.2.1` tag archive, SHA-256 `f3b51e2b6ef8d214587a22c2e47b73f311daec06db99d4be6f2704cea010b878`. As it was built in place rather than installed, the JSON output labels its distribution version as a source build.

## Recorded results

The [raw results](04-LOCATION-INDEX-RESULTS.json) contain all 16 measured cases. Each case checks 100,000 lookups across three backup clients and two mount readers. The table below shows the existing-cache cases, with no enforced memory limit. These are individual exploratory runs, not statistical estimates.

| Entries per client | Mode, plus two mounts | Hash peak RSS, MiB | LMDB peak RSS, MiB | Hash wall time, s | LMDB wall time, s |
| --- | --- | ---: | ---: | ---: | ---: |
| 100,000 | Pull | 42.10 | 78.18 | 0.693 | 0.611 |
| 100,000 | Push | 55.12 | 113.25 | 0.753 | 10.175 |
| 1,000,000 | Pull | 206.75 | 479.02 | 3.689 | 0.888 |
| 1,000,000 | Push | 329.75 | 762.06 | 4.412 | 12.067 |

At 100,000 entries per client both candidates remain below 128 MiB in these runs; only the hash candidate remains below 64 MiB. At one million entries neither run stays below 128 MiB. Three complete hash tables already contain about 160 MiB of raw keys and values, before table and interpreter overhead, so this layout cannot satisfy the budget for that pull-and-mount case. LMDB's uncapped RSS includes reclaimable file-backed pages; it still requires a constrained-memory test before judging its suitability.

The observed LMDB push times are substantially worse in this thread-based harness despite batched reads. They are evidence for this binding, API usage, and workload only. Further work must distinguish GIL/binding effects from storage I/O and measure actual HDD behavior under an enforced shared limit. No engine is adopted by this comparison.

## References

- [BorgHash source and API](https://github.com/borgbackup/borghash/tree/0.2.1)
- [Current Borg dependency declarations](https://github.com/borgbackup/borg/blob/master/pyproject.toml)
- [LMDB Python API and memory accounting](https://lmdb.readthedocs.io/en/release/)
- [Trixie python3-lmdb package](https://packages.debian.org/trixie/python3-lmdb)
