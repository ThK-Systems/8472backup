# Project Structure Proposal

Status: proposal for Thomas's review, 2026-11-13. This document does not authorize implementation or establish an on-disk format. The main specification remains authoritative.

## 1. Agreed starting point

- Python 3.12 is the minimum version.
- Start with real file contents, content-defined chunks, physical packs, and complete snapshots.
- Tests perform the client-side work and drive the real server repository operations. No placeholder client application is needed.
- SQLite is excluded.
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
    server/
      repository.py
      catalogue.py
      retention.py
      service.py
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
tests/
  unit/
    archive/
    server/
    storage/
  integration/
    test_snapshots.py
spec/
  01-8472backup-SPEC.md
  02-PROJECT-STRUCTURE.md
```

This is an intended organization as functionality grows. Create modules with their implementation increment; the tree is not a request for empty scaffolding. Regular packages will have the required `__init__.py` files, omitted from the diagram.

| Package | Responsibility |
| --- | --- |
| `archive` | Source-side chunking, identifiers and encrypted object envelopes, file metadata, compression, encryption, and restoration. These operations do not require a client CLI. |
| `server` | Authenticated host/set scope, repository operations, authoritative snapshot membership, commit publication, protection, and retention decisions. |
| `storage.file` | Physical packs, object locations, bounded reads, durable writes, and recovery of storage indexes. Cleanup follows server policy. |
| `transport` | Shared protocol and the separate push/pull connection setup. |
| `cli` | Eventual command parsing and process entry points. |

Use ordinary modules and explicit calls. Folder boundaries do not imply interfaces, dependency injection, or class hierarchies. VS Code uses one workspace root and a consistent source path for navigation, type checking, and test discovery.

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

## 4. File-based catalogue proposal

Use a server-owned file-based snapshot catalogue, distinct from the rebuildable pack location index. One committed snapshot record contains its authenticated scope, root identity, complete membership, server commit time, and protection information. Paths, file attributes, and data references remain in client-encrypted objects.

A possible logical repository layout is:

```text
repository/
  packs/<key-domain>/
  index/<key-domain>/
  snapshots/<set-id>/
  staging/<run-id>/
```

These are roles and namespaces, not a chosen serialization or naming scheme. Snapshot publication follows durable pack and index persistence and a durable write of the complete server record. The publication operation must be crash-safe, must not replace an existing committed snapshot, and must have defined retry semantics.

Before coding, review the minimal object/pack framing, snapshot record serialization, catalogue publication/recovery sequence, and first concrete chunker/hash/compression/AEAD choices. Real packs require a concrete initial format; any experimental format and its compatibility limits must be explicit. Do not fill those gaps by silently choosing a format or by substituting synthetic end-to-end payloads.

## 5. Existing project metadata

The current `pyproject.toml` parses as TOML and sets both the Python requirement and mypy target to Python 3.12. Its `py-modules` setting describes a single-file module; the proposed hierarchy instead needs package discovery under `src/`. When creating the packages, align package discovery, import sorting, and test/source-path configuration with `p8472backup`.

The current dependency entries specify lower bounds, not exact version pins. They are not constrained to Trixie's tool versions under the clarified development-dependency policy. The build requirement for `setuptools-scm` is now explicitly `>=10.3.4`. A build or installed-tool compatibility check is separate from checking published version and Python metadata.

No changes to `pyproject.toml`, CI, application modules, or test code are made by this proposal.
