# Project Structure Proposal

Status: proposal for Thomas's review, 2026-11-13. This document does not authorize implementation or establish an on-disk format. The main specification remains authoritative.

Explicitly approved increment, 2026-11-19: the immutable `Object` class and its focused tests occupy `src/p8472backup/archive/objects.py` and `test/p8472backup/unit/archive/test_objects.py`. Its `to_header()` and `from_header()` methods implement the common header codec; payload handling remains unimplemented. The remaining hierarchy is still a proposal.

Also explicitly approved: the immutable `Pack` header class in `src/p8472backup/storage/file/packs.py`, with four focused tests in `test/p8472backup/unit/storage/file/test_packs.py`. It implements the 14-byte header; record I/O and compaction remain unimplemented.

The immutable `Snapshot` and `ObjectCatalogue` classes live in `src/p8472backup/server/snapshot.py`. `repository.py` defines `Repository` and its `BackupSet` records. `repository_service.py` handles repository metadata and set registration. The stateless `SnapshotService` assembles object membership and reads and publishes local snapshot records.

Also explicitly approved: the data-only immutable `ObjectLocation` class in `src/p8472backup/storage/file/index.py`. Benchmark libraries are optional development tools.

The same module implements an in-memory `LocationIndex`. Its 32-bit buckets reference packed 56-byte records. Lookups probe linearly and compare the full object ID. `put(object_id, location)` inserts a location only if the supplied ID is absent. `get(object_id)` returns the location or `None`. The bucket array starts at 8,192 buckets and doubles before an insertion would exceed 50 percent occupancy. The `get` and `put` signatures support duck typing. `save_to` and `load_from` use a versioned checkpoint and require the exclusive `RepositoryLock` from `storage/file/lock.py`. `save_to` returns `True` after saving pending changes and `False` when unchanged. New indexes require an initial save; loaded indexes start unchanged. Failed saves preserve pending changes for retry.

## 1. Agreed starting point

- Python 3.12 is the minimum version.
- Start with real file contents, content-defined chunks, physical packs, and complete snapshots.
- Tests perform the client-side work and drive the real server repository operations. No placeholder client application is needed.
- SQLite is permitted for location-index benchmarking following Thomas's explicit reconsideration. No production database is selected, and a database must not substitute for real chunks, packs, or snapshots.
- Runtime dependencies must be available as Debian Trixie packages. Build and development/test tools are exempt from that availability restriction. Their installation method is not decided here; the existing no-pip instruction remains in force.
- The source-side processing and the keyless server retain their separate responsibilities even when the tests run them in one process.

## 2. Package hierarchy

The project, distribution, and eventual command names remain `8472backup`. Program names may begin with a digit; normal Python import identifiers may not. The agreed import-package name is `p8472backup`, where `p` stands for Project. The package hierarchy below remains a proposal.

```text
src/
  p8472backup/
    archive/
      chunks.py
      objects.py
      metadata.py
      compression.py
      crypto.py
    common/
      error.py
    server/
      snapshot.py
      repository.py
      repository_service.py
      snapshot_service.py
      catalogue.py
      retention/
        __init__.py
        policy.py
    storage/
      file/
        packs.py
        index.py
        recovery.py
    transport/
      protocol.py
      ssh.py
      gateway.py
      push.py
      pull.py
    cli/
      server.py
      backup.py
      restore.py
      mount.py
test/
  p8472backup/
    unit/
      archive/
      server/
      storage/
    integration/
      test_snapshots.py
    benchmark/
spec/
  10-8472backup-Spec.md
  10a-PurposeScopeArchitecture.md
  10b-CryptographyObjectsAndPacks.md
  10c-SnapshotsCommitAndRetention.md
  10d-CommunicationConfigurationAndOperations.md
  10e-VerificationAndDevelopment.md
  10f-FutureWorkAndReferences.md
  20-ProjectStructure.md
```

This is an intended organization as functionality grows. Create modules with their implementation increment; the tree is not a request for empty scaffolding. Regular packages will have the required `__init__.py` files, omitted from the diagram.

| Package | Responsibility |
| --- | --- |
| `archive` | Source-side chunking, identifiers and encrypted object envelopes, file metadata, compression, encryption, and restoration. These operations do not require a client CLI. |
| `common` | Shared exceptions. |
| `server` | Authenticated host/set scope, repository operations, authoritative snapshot membership, commit publication, protection, and retention decisions. |
| `storage.file` | Physical packs, object locations, bounded reads, durable writes, and recovery of storage indexes. Cleanup follows server policy. |
| `transport` | Shared protocol and the separate push/pull connection setup. |
| `cli` | Eventual command parsing and process entry points. |

Use concrete domain classes with explicit methods and ordinary package modules. `Object` owns its validated header state and serialization methods. Folder boundaries do not imply interfaces, dependency injection, or inheritance hierarchies. VS Code uses one workspace root and a consistent source path for navigation, type checking, and test discovery.

## 3. First real snapshot increment

Tests create representative files in temporary directories. They invoke the actual source-side processing to obtain chunks and objects, then submit those objects and the full membership declaration to the server core. The server writes real packs and publishes the snapshot only when all dependencies are durably present.

The first integration scenario is:

1. Create a source directory and snapshot S1.
2. Reopen the repository and restore S1 into a different directory; compare bytes.
3. Change part of a large file, leave another file unchanged, and create S2.
4. Verify actual chunk reuse and independent restoration of both snapshots.
5. Exercise missing dependencies, attempted overwrites, interrupted commits, and repository reopening.
6. Once pruning is implemented, remove an eligible S1 and verify that all objects needed by S2 remain available.

Boundary tests also cover host/key-domain isolation and preservation of committed membership. Add real process-interruption tests when persistent commit recovery exists; successful ordinary reopening alone does not establish crash durability.

The initial implementation needs the archive-processing modules, server repository/catalogue, and file pack/index code. Service startup, SSH, CLI commands, and scheduling follow in later increments.

## 4. File-based snapshot catalogue

Use a server-owned snapshot register and per-snapshot files. The snapshot JSON records scope, identity, commit time, and protection. The binary catalogue records the accepted membership. Paths, file attributes, and data references remain in client-encrypted objects.

The agreed repository layout is:

```text
<repo-root>/
  repository.json
  snapshot-registry.json
  backup-sets/<set-id>.json
  snapshots/<set-id>/<snapshot-id>.json
  snapshots/<set-id>/<snapshot-id>.catalog
  data/packs/<key-id>/<pack-id>.pack
  data/index/<key-id>
```

Chapter 11 defines the layout and commit rules. The register update publishes the snapshot after the packs, index, and both snapshot files are durable. Unfinished work is represented by the run state and provisional versions of the existing structures.

Review the remaining schema, recovery, and cryptography decisions before implementing the relevant increments. Document compatibility limits for experimental formats.

## 5. Existing project metadata

The current `pyproject.toml` sets both the Python requirement and mypy target to Python 3.12. Package discovery uses `src/`, import sorting recognizes `p8472backup`, and pytest discovers tests under `test/` with `src/` on its import path. Mypy's `mypy_path` uses `$MYPY_CONFIG_FILE_DIR/src`, independently of pytest's path configuration, so individual-file checks resolve `p8472backup`. The lint workflow installs `pytest>=8.3.0` alongside Ruff and mypy because `mypy .` also checks the test modules and needs to resolve their pytest imports and typed decorators. These settings support the first header codec and its tests.

The current dependency entries specify lower bounds, not exact version pins. They are not constrained to Trixie's tool versions under the clarified development-dependency policy. The build requirement for `setuptools-scm` is now explicitly `>=10.3.4`. A build or installed-tool compatibility check is separate from checking published version and Python metadata.

Create further modules with their approved implementation increments; the remaining proposal does not request empty scaffolding.
