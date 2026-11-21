# 8472backup — Verification and Development

Specification version: 0.45 — Lock-protected location index checkpoints, 2026-11-21

requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

## 18. Verification and release acceptance

Tests must exercise behaviour and invariants, not merely mirror implementation. Include at least:

| Scenario | Required outcome |
| --- | --- |
| Client initiates an authorized SSH operation | Restricted gateway relays to the running server service over the local socket without a TCP tunnel |
| Client claims another host identity | Gateway and server derive authority from authenticated credentials; cross-host access is denied |
| Client invokes an unauthorized operation | Server rejects it; protected snapshots and repository state remain unchanged |
| Client disconnects during a gateway session | Incomplete work is handled safely and committed history remains intact |
| Equivalent source data backed up through push and pull | Common protocol and archive logic give the same reuse, restoration, commit, and protection behaviour |
| Registration with mode omitted | Pull is the default; server schedules the host |
| Registration with `mode = "push"` | Server records push mode and never schedules the host for pull; push backup remains available |
| Initially push host requests pull | Dedicated authenticated client command reactivates pull scheduling |
| Registration configuration is invalid or unavailable | Registration fails without silently falling back to pull or activating scheduling |
| Scheduled pull observes `mode = "push"` | Server persists push state, removes the host from the pull scheduling list, and does not initiate a pull backup |
| Host in push scheduling state | Server does not poll it for mode changes; authorized client-initiated backups can succeed |
| Client requests return to pull | Dedicated authenticated command adds the host back to pull scheduling; repeated requests are safe |
| Mode configuration is invalid or unavailable | No silent switch to push or removal from pull scheduling |
| Push client requests shorter retention or deletion | Server protects all unexpired snapshots and their required objects |
| Push backup fails or disconnects | No incomplete snapshot is published and existing protected history remains intact |
| Push backup uses reused old objects | A protected new snapshot keeps all required objects, regardless of object age |
| Incremental backup after source edits | Changed content restored correctly; unchanged objects reused |
| Earlier snapshot removed | Every retained snapshot still restores independently |
| Old chunk referenced by new protected snapshot | Old chunk remains available |
| Client requests shorter retention | Unexpired server protection wins |
| Client supplies old/future timestamps | Trusted protection clock is unaffected |
| Client floods new snapshots | Existing snapshots cannot be displaced inside their protection period |
| Client submits deletion/overwrite/foreign IDs | Existing committed data and other scopes remain protected |
| Client modifies description during a run | The run uses its captured configuration |
| Crash/disconnect around commit | Recovery has a defined durable result; no published incomplete snapshot |
| Retry after lost commit acknowledgement | No corruption or ambiguous duplicate semantics |
| Concurrent GC and backup/restore | Required objects remain accessible |
| Disk fills during backup or compaction | Existing protected history remains intact |
| Data/index corruption | Detected failure; no speculative destructive cleanup |
| Client cache lost | Documented rebuild/recovery works with required keys |
| Wrong or modified ciphertext | Restore fails verification rather than returning accepted corrupt data |
| Cross-host and independent-set keys | No shared key or deduplication scope |
| Same plaintext in different encryption sessions | Stable logical ID and reuse within the same key scope |
| Parallel writing, retry, or restart | No repeated session-key/nonce pair for a new encryption |
| Ciphertext, object role, ID, or authenticated context modified | Verification fails; no accepted corrupt restoration |
| Restore from another authorized recovery client | Durable recovery secrets and stored metadata suffice; original source host not required |
| Client tries to replace committed membership | Earlier retained history and its dependencies remain unchanged |
| Pack location index lost | Documented rebuild without inventing authoritative protection state |
| Version 1.0 client supplies no backend selection | Default `file` works without a client selector |
| Server defines `file` with a chosen path | Backup and restore use the server-defined location |
| Storage path is on a suitable mounted filesystem | Same `file` backend, subject to the documented filesystem contract |

Use integration tests and targeted fault injection for durability claims. Compare restored bytes and agreed metadata, not only command exit codes.

Benchmark representative unchanged backups, local edits in large files, many small files, restore, mount, and cleanup. Record environment, storage, cache state, CPU, memory, throughput, and repository growth. Do not claim Borg-equivalent performance without comparable measurements.

Plyvel/LevelDB is an additional evaluation candidate, not an adopted runtime dependency or repository format. SQLite has also been approved for evaluation. The manual comparison defaults to LMDB, Plyvel, and SQLite; rejected borghash remains available only as an explicit historical reference.

The opt-in `test/benchmark/compare_location_indexes.py` compares the current Borg 2 hash-table library with LMDB using synthetic 32-byte keys and 24-byte location values. Measure serial pull clients and concurrent push clients, both with up to two additional mount readers. Apply the 64–128 MiB target to the combined operation process, including its interpreter, native allocations, and resident mapped pages. Report budget violations; this benchmark does not enforce an operating-system memory limit. Dataset preparation is isolated from operation measurements. Keep it outside normal pytest collection.

Use purpose-written Python tests, temporary directories, representative data, and fault injection alongside each increment. Add actual process interruption and restart tests when persistent commit/recovery exists: simulated failures alone do not establish crash durability.

Include candidate ciphers, keyed hashes, chunkers, object overhead, and pack behaviour in these measurements. Tests for later explicit backend selection must eventually verify rejection of unknown or unauthorized choices; do not implement that client feature merely to test it in version 1.0.
## 19. Development workflow for Codex

- Work in small coherent increments Thomas can review completely.
- Explain consequential format, protocol, cryptographic, and durability decisions before implementing them. Resolve routine reversible choices without repeated permission requests.
- Keep comments, identifiers, tests, and project documentation in English.
- Keep modules explicit and comprehensible. Avoid speculative frameworks, clever metaprogramming, or hidden generated logic.
- Record accepted decisions and recovery procedures in Git; chat history must not be essential project documentation.
- Use established libraries through Python modules under the Debian Trixie dependency policy in chapter 3. Check the packaged version, native implementation, license compatibility, platform availability, maintenance, and data-copy overhead before selecting them.
- Use appropriate automated tests, Ruff, and mypy; exact configuration and supported Python matrix remain to be chosen.
- Report what changed, why, what was checked, and what remains unresolved.
- Preserve existing work and public interfaces. Never silently change safety semantics to get tests passing.
- No implementation, build, tests, or release may require an AI service to operate.
- Do not expand into QuorumBD, non-Linux completeness, hosting, a web UI, or a migration suite without an explicit scope change.

Resolve routine reversible details autonomously while keeping consequential OPEN choices visible. The newer architectural directions in this consolidated specification are the baseline for review; do not resurrect superseded alternatives implicitly. Preserve the backend boundary in version 1.0 while leaving explicit client backend selection deferred.
## 20. Incremental implementation — archive design first

1. Start with archive/repository design: agree independent snapshot roots, object membership, key boundaries, durable commit invariants, and the small `file` backend boundary; prototype uncertain library choices.
2. Begin implementation with the server-side repository core for snapshot creation and persistence, driven directly by tests using real file contents, real chunking, physical packs, and complete snapshots. Exercise a local round trip: backup, second snapshot with reuse, verify, and restore.
3. Establish transactional repository behaviour, protected retention, GC, and failure tests.
4. Add authenticated SSH operations, registration, server-initiated pull and client-initiated push backup sessions, and set management.
5. Integrate configuration, scheduling, service operation, and operational failure handling.
6. Add read-only FUSE mounting and refine restore fidelity.
7. Benchmark, harden, package, document recovery, and validate in controlled real use.

This order may change by agreement. Early prototypes are not production backup tools. Safety boundaries must shape the initial model even when features arrive later.

The immediate focus is the server-side snapshot core with real chunks, packs, and snapshots. Tests take the client role and exercise the actual archive-processing and server-storage code; a placeholder `client.py` or dummy-client application is not part of the initial scope. The source-side processing used by tests remains separate from the keyless server. Review the first object/pack format, catalogue persistence, cryptographic choices, and commit sequence before implementation; any experimental format must be identified as such. Incomplete platform support or other explicitly bounded gaps may remain, but synthetic opaque payloads alone are not the main end-to-end test. Use a package hierarchy that leaves clear homes for archive processing, server policy, storage, and transport as the project grows. The exact module layout remains a proposal; no implementation scaffolding or test code is authorized by this planning step alone.

Explicitly approved on 2026-11-19: implement the common header codec as the immutable concrete class `Object` in `src/p8472backup/archive/objects.py`, with focused tests in `test/unit/archive/test_objects.py`. `to_header()` returns the serialized header; the class method `from_header()` returns an `Object`, not a tuple of fields. Construction validates the header state. Configure mypy's source search path relative to `pyproject.toml` so individual-file checking finds the package. Payload handling is discussion-only and is not authorized for implementation by this increment. The remaining pack and snapshot implementation still requires explicit approval.

The non-normative [project structure proposal](20-ProjectStructure.md) describes the proposed package hierarchy and first test-driven snapshot increment. Package naming, file-based catalogue details, and the exact module breakdown require review; this specification remains the requirements authority.

### 20.1 Initial focus: archive design and dedicated tests

Start with the design of the archive and repository model. Describe how complete snapshots, encrypted roots and metadata, immutable objects, shared chunks, and authoritative server-side membership fit together. Work through small concrete examples of creating snapshots, reusing objects, restoring, pruning, and reclaiming storage. Use the architectural decisions in chapters 6–12 as the baseline and keep explicitly OPEN choices open; evaluate format, index, pack layout, database, cryptographic construction, and durability options with Thomas before committing to consequential decisions.

Develop small, purpose-written Python tests alongside each implementation increment. Prefer behavioural assertions and restored content over tests that merely mirror implementation. The initial archive test suite should progressively cover:

- Full round-trip restoration of a completed snapshot.
- Reuse of unchanged objects in a subsequent snapshot within the same key scope.
- Independent restoration of a later snapshot after an earlier snapshot is removed.
- Retention of an old chunk referenced by a protected snapshot, including after the older snapshot expires.
- An interrupted write or failed commit never appearing as a successfully completed snapshot.
- Authentication or integrity failure when encrypted content is modified or corrupted.

Use temporary directories, representative test data, and targeted fault injection where appropriate. Add real process-interruption and restart tests when persistent commit and recovery are implemented; simulated failures alone do not establish crash durability. Design tests as the relevant components become available rather than building the entire product in the first increment. Thomas reviews the design and code and retains ownership of architectural decisions.
