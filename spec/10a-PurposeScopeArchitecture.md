# 8472backup — Purpose, Scope, and Architecture

Specification version: 0.69 — Set the backup mode per client, 2026-11-27

requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

## 1. Purpose, authority, and precedence

The authoritative Codex specification for 8472backup consists of the versioned entry point `10-8472backup-Spec.md` and its companion parts `10a-PurposeScopeArchitecture.md` through `10f-FutureWorkAndReferences.md`. Together, these files consolidate and supersede the former project specification (`8472backup-SPEC.md`, also previously named `10-8472backup-Spec.md`) and `8472backup-SPEC-ADDENDUM-crypto-snapshots-storage.md`. The entry point defines the current version and links to every part.

The architectural directions adopted after reviewing Borg 2 take precedence over older, less specific ideas: AEAD, session-derived encryption keys, independent snapshot roots, late server-controlled publication, and a storage-backend boundary. This precedence does not import Borg's different trust model or silently approve unresolved algorithms, formats, pack layouts, or dependencies. The requirements below express the resolved consolidation; do not combine them with superseded drafts.

MUST denotes a requirement, SHOULD a recommendation, OPEN an unresolved decision, and DEFERRED work outside the current implementation scope. An algorithm candidate is not an approved implementation dependency.

Use the terminology in the [English glossary](99-Glossary.md), especially the distinction between chunks, objects, and packs. The glossary explains the existing model; it does not select unresolved formats or algorithms.

The [configuration roadmap](70-ConfigurationRoadmap.md) records planned client/server settings and unresolved configuration candidates. It does not define configuration syntax or authorize implementation.

Thomas Kuhlmann is the project owner, architect, and principal developer. He retains ownership of requirements, architecture, review, and acceptance. He must be able to understand every line of project code and maintain the project without Codex or any AI service.

This specification supports iterative implementation in small reviewable increments. It is not authorization to scaffold or implement the entire product at once. Proposed implementation choices must not silently become requirements.
## 2. Product intent and operating goal

8472backup is a focused client/server backup system with pull as the default and optional push mode, informed by Borg, especially the adopted Borg 2 architectural ideas, and borgmatic. It combines client-side encryption, deduplicated incremental storage, complete snapshots, and server-enforced minimum retention.

The central use case is protecting existing backup history when a source host is compromised, without giving the backup server decryption keys. Routine retention and space reclamation must remain automatic and practical.

The project deliberately keeps configuration and routine operation small and comprehensible. This is an independent implementation, not a Borg fork, wrapper, protocol-compatible replacement, or repository-format-compatible implementation.

After setup, scheduled backup, protection enforcement, retention, and eligible space reclamation MUST operate automatically. In push mode an external client-side scheduler invokes the backup client. Normal maintenance must not require regularly disabling protection or using a manual maintenance key to reclaim eligible space. Failures must be observable; low operator attention must not mean silent failure. These are design and release goals, not claims about an existing implementation.
## 3. Scope and platform

- Implementation language: Python.
- Use object-oriented domain code with concrete classes that own their state and relevant methods. `Object` represents an archive object's current header state and provides `to_header()` and `from_header()`. Prefer simple concrete classes over speculative interfaces or inheritance hierarchies.
- Project, distribution, and command name: `8472backup`. Python import-package name: `p8472backup`, where `p` stands for Project. Program names may begin with a digit; normal Python import identifiers may not.
- Keep the dependency set small and use the Python standard library where practical.
- xxHash is excluded as a project algorithm. Do not add a direct dependency on `libxxhash` or Python xxHash bindings.
- All external runtime dependencies MUST be available as Debian 13 (`trixie`) packages. Evaluate the APIs and versions actually packaged there. Build and development/test dependencies are exempt from this package-availability restriction. The existing no-pip instruction remains in force; this exception does not select an installation method for development tools.
- Performance-critical runtime dependencies SHOULD expose established C or C++ implementations through Python bindings. Other native implementations require an explicit review against this preference.
- Server: Linux, running as a systemd service.
- Version 1.0 reference client: Linux.
- Minimum interpreter: Python 3.12. This minimum is agreed; implementations must also respect the APIs available in the Debian Trixie dependency versions.
- Later clients: other operating systems where the required Python modules and operating-system facilities are available. Identical feature availability across platforms is not required.
- Platform-specific paths, file metadata, process operations, and mounting SHOULD have clear boundaries without speculative abstraction frameworks.
- Established Python modules may bind native libraries. The project does not require a pure-Python dependency stack. Do not introduce project-owned C, C++, Cython, Rust, or Go code without a demonstrated need and an explicit architectural decision.
- A Go library may be considered only as a last resort when suitable packaged C/C++ options cannot meet the requirements. Establish the concrete need and review Debian availability, Python integration, and maintenance before adopting it.
- Full non-Linux support is outside version 1.0.

Prioritize integrity, security, storage efficiency, and maintainability over peak backup speed. Evaluate performance and storage efficiency with representative benchmarks.

Design for HDD-backed backup storage and low available RAM on modest server hardware. The location-index design must not assume that complete indexes for all clients fit in physical memory; an individual index may also exceed available RAM. Evaluate bounded application buffers, batched lookups, and disk-access locality. Compare warm-cache and cold-HDD behavior and measure actual memory use before selecting the index engine. LMDB and the current Borg 2 in-memory hash-index approach are evaluation references, not adopted implementations or dependencies. Thomas explicitly excludes `borghash` as a production dependency because upstream declares its API unstable and provides no migration tools for its serialized tables. Its recorded measurements remain comparison evidence; a custom replacement is not authorized by this exclusion.

Target 64–128 MiB of working memory across all server operations combined, not per client, index, or request. Include simultaneous push operations and one or two mount readers; pull backups may process clients serially. Indexes, catalogues, payload/transport buffers, and interpreter overhead share this budget. The initial index benchmark measures the complete operation process, but does not implement a production memory limiter or account for unimplemented server functions. A memory-mapping size is not a RAM limit. Reclaimable mapped file pages must be distinguished from anonymous allocations when assessing whether an engine can work under a hard limit.
## 4. Domain model and identity

The agreed interpretation is:

- A registered backup host has a stable host UUID and exactly one logical server-side repository.
- A host has one or more backup sets within that repository.
- Each set has a stable set ID and a human-readable name. The default set is named `default`.
- Each completed backup produces a distinct snapshot for its set.
- A snapshot describes a complete recoverable file selection, while its stored objects may be shared with other snapshots in the same deduplication scope.
- Names, network addresses, and hostnames must not serve as authentication credentials.
- Renaming a host or set must not implicitly replace its identity or discard its history.

Key scope MUST be configurable as one independent key domain per host or independent key domains per set. Never share host key domains. Independent set keys partition deduplication within a host repository and require independent identifier keys/namespaces. There is no cross-host deduplication or shared cross-host key material.

A backup set provides the grouping purpose of an archive series. Each snapshot has its own identity; it does not require a unique user-facing timestamp name. Descriptive names, optional future labels, hostnames, and comments do not define authentication or history identity.
## 5. Trust model and required guarantees

### 5.1 Separation of responsibilities

The source client is trusted to supply correct new backup contents only while it remains uncompromised. It must never be trusted to preserve existing server-side history.

The backup server is trusted to enforce retention, preserve committed data, and perform correct cleanup. It MUST NOT possess or receive data decryption keys.

A compromised server can destroy data or deny service. Client-side encryption protects confidentiality; it does not make a hostile storage server available or reliable.

A compromised source host may disclose its locally accessible keys and data and submit useless new snapshots. The system cannot certify that such new contents are legitimate. The guarantee concerns previously committed history.

### 5.2 Required invariants

1. A client cannot overwrite or delete committed objects needed by retained snapshots.
2. A client cannot replace the server's authoritative snapshot catalogue.
3. A client cannot shorten the server-configured protection period.
4. A client cannot modify an earlier snapshot's committed object references.
5. A client cannot backdate the trusted protection clock through metadata or timestamps it supplies.
6. A snapshot is committed only after all declared dependencies are durably available according to a defined commit protocol.
7. Garbage collection cannot remove any object required by a retained snapshot or an active operation requiring that object.
8. Client permissions and object lookup are restricted to the authenticated host and applicable set/key scope.
9. Out-of-space handling cannot override protection. Reject or abort new work rather than delete protected history.
10. Malformed input, disconnects, failed backups, or invalid configuration cannot themselves authorize pruning of existing history.

Transport authentication is separate from encryption keys and stable UUIDs. SSH forced commands must be combined with application-level authorization and disabled unneeded SSH capabilities.
## 6. Architectural decision summary

| Area | Direction | Remaining decisions |
| --- | --- | --- |
| Backup encryption | AES-256 with established AEAD; only clients hold data decryption keys | GCM versus OCB, library, envelope format, usage limits |
| Key lifecycle | Session-derived encryption keys, separate stable identity key material | KDF, session identifier format, concurrency rules |
| Passphrase protection | Argon2id required for the client's master passphrase | Library, parameters, key wrapping, unattended unlocking |
| Deduplication identifiers | Development uses HMAC-SHA-256 over plaintext; fixed 32-byte binary IDs; no user-configurable algorithm | Pre-release algorithm review, key derivation, domain separation, authenticated context, format; migration deferred |
| Snapshots | Independent snapshot roots, server snapshot JSON, binary object catalogues, and publication through a server-owned register | Exact JSON schemas, registry integrity and recovery, development-format transition |
| Physical storage | Packs of independently encrypted objects from the first snapshot increment, following Borg 2's logical/physical separation | Layout, sizes, recovery, concurrency |
| Cleanup | Asynchronous jobs reclaim retired scopes and compact packs during idle periods. The server validates registered snapshot catalogues and marks referenced objects for compaction. | Idle admission, frequency, threshold, crash recovery, and whether a persistent reference index is needed later |
| Client/server communication | Common framed binary request/response protocol over SSH, with a separate push/pull connection-setup boundary | Framing, serialization, operation sequencing, and authenticated session context |
| Storage backend | Server defines available backends; client selects from them or uses defaults. Version 1.0 only provides `file` (`file`) | Interface, server definitions, future selection syntax and scope |
| Additional native backends | Future server-side cloud, SFTP, S3, and similar implementations | DEFERRED: much, much later; not version 1.0 |

These directions supersede generic older options. In particular, do not introduce a global long-lived encryption nonce counter, a client-replaceable global snapshot manifest, or client-controlled protection timestamps. The remaining pack and index details are tracked in chapters 8 and 11.
