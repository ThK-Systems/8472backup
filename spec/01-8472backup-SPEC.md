# 8472backup — Project Specification for Codex

Version: 0.8 — SSH access to server service, 2026-11-02

Status: requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

## 1. Purpose, authority, and precedence

This is the single authoritative Codex specification for 8472backup. It consolidates and supersedes the former project specification (`8472backup-SPEC.md`, also previously named `01-8472backup-SPEC.md`) and `8472backup-SPEC-ADDENDUM-crypto-snapshots-storage.md`. No companion specification is required.

The architectural directions adopted after reviewing Borg 2 take precedence over older, less specific ideas: AEAD, session-derived encryption keys, independent snapshot roots, late server-controlled publication, and a storage-backend boundary. This precedence does not import Borg's different trust model or silently approve unresolved algorithms, formats, pack layouts, or dependencies. The requirements below express the resolved consolidation; do not combine them with superseded drafts.

MUST denotes a requirement, SHOULD a recommendation, OPEN an unresolved decision, and DEFERRED work outside the current implementation scope. An algorithm candidate is not an approved implementation dependency.

Thomas Kuhlmann is the project owner, architect, and principal developer. He retains ownership of requirements, architecture, review, and acceptance. He must be able to understand every line of project code and maintain the project without Codex or any AI service.

This specification supports iterative implementation in small reviewable increments. It is not authorization to scaffold or implement the entire product at once. Proposed implementation choices must not silently become requirements.

## 2. Product intent and operating goal

8472backup is a focused client/server backup system with pull as the default and optional push mode, informed by Borg, especially the adopted Borg 2 architectural ideas, and borgmatic. It combines client-side encryption, deduplicated incremental storage, complete snapshots, and server-enforced minimum retention.

The central use case is protecting existing backup history when a source host is compromised, without giving the backup server decryption keys. Routine retention and space reclamation must remain automatic and practical.

The project deliberately keeps configuration and routine operation small and comprehensible. This is an independent implementation, not a Borg fork, wrapper, protocol-compatible replacement, or repository-format-compatible implementation.

After setup, scheduled backup, protection enforcement, retention, and eligible space reclamation MUST operate automatically. In push mode an external client-side scheduler invokes the backup client. Normal maintenance must not require regularly disabling protection or using a manual maintenance key to reclaim eligible space. Failures must be observable; low operator attention must not mean silent failure. These are design and release goals, not claims about an existing implementation.

## 3. Scope and platform

- Implementation language: Python.
- Server: Linux, running as a systemd service.
- Version 1.0 reference client: Linux.
- Tentative minimum interpreter: Python 3.12; the exact minimum remains OPEN until dependencies and useful language/library features are evaluated.
- Later clients: other operating systems where the required Python modules and operating-system facilities are available. Identical feature availability across platforms is not required.
- Platform-specific paths, file metadata, process operations, and mounting SHOULD have clear boundaries without speculative abstraction frameworks.
- Established Python modules may bind native libraries. The project does not require a pure-Python dependency stack. Do not introduce project-owned C, Cython, Rust, or Go code without a demonstrated need and an explicit architectural decision.
- Full non-Linux support is outside version 1.0.

Performance and storage efficiency comparable to Borg are goals requiring benchmarks, not pre-existing guarantees.

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
| Backup encryption | Established AEAD implementation; only clients hold data decryption keys | Cipher, library, envelope format, usage limits |
| Key lifecycle | Session-derived encryption keys, separate stable identity key material | KDF, session identifier format, concurrency rules |
| Passphrase protection | Argon2id as the intended password-based KDF | Library, parameters, key wrapping, unattended unlocking |
| Deduplication identifiers | Keyed plaintext identifiers; keyed BLAKE3 is a strong candidate | Final algorithm, domain separation, format |
| Snapshots | Independent snapshot roots and a final server-controlled publication step | Serialization, index engine, durable commit mechanism |
| Physical storage | Evaluate packs of independently encrypted objects | Whether to use packs initially; layout, sizes, recovery |
| Cleanup | Server uses committed object membership without decryption | Reference counting versus tracing; concurrency protocol |
| Storage backend | Server defines available backends; client selects from them or uses defaults. Version 1.0 only provides `file` (`file`) | Interface, server definitions, future selection syntax and scope |
| Additional native backends | Future server-side cloud, SFTP, S3, and similar implementations | DEFERRED: much, much later; not version 1.0 |

These directions supersede generic older options. In particular, do not introduce a global long-lived encryption nonce counter, a client-replaceable global snapshot manifest, or client-controlled protection timestamps. Pack adoption, the index engine, and reference counting versus tracing remain OPEN rather than being selected automatically from Borg.

## 7. Cryptography and recovery key material

### 7.1 SSH and stored backup encryption are separate

SSH protects communication and authenticates peers. It does not provide the required confidentiality of stored backups from the backup server.

The client MUST compress and encrypt backup objects before transfer. Restore and mount MUST decrypt and verify on the client. The server MUST NOT receive data decryption keys.

There are separate roles for:

- transport authentication credentials;
- stable secret material for deduplication identifiers;
- secret material for deriving encryption keys;
- passphrase-derived keys used to protect stored secret material.

These roles MUST NOT be collapsed into one raw key used interchangeably. Independent keys may be generated or derived through an established KDF with explicit domain separation. Exact construction remains OPEN.

### 7.2 Authenticated encryption

Cryptographic primitives MUST come from established implementations; do not invent or implement cryptographic primitives ourselves. Use an established AEAD implementation rather than assembling encryption and authentication primitives ourselves. AES-256-OCB and ChaCha20-Poly1305 are candidates motivated by Borg 2; the final choice requires checking maintained Python libraries, platform availability, license compatibility, and performance.

An encrypted object MUST bind its ciphertext to the intended object identity, object role, format, and repository/key scope through an explicit authenticated context. Additional authenticated data (AAD) is suitable for fields that need authentication without encryption.

Define what is authenticated directly in the envelope and what is bound through references. Reject unexpected object types and unsupported formats. Authentication failure MUST NOT produce accepted restored data.

The exact envelope, authenticated fields, algorithm identifiers, parsing bounds, and decompression limits remain OPEN and must be specified before committing an on-disk format.

### 7.3 Session-derived encryption keys

Adopt session-derived keys as the intended approach for backup object encryption. A session here is a cryptographic writing session, not an SSH session and not necessarily an entire snapshot.

For each new writing session:

1. Generate a fresh session identifier using a cryptographically secure random source.
2. Derive a session encryption key from durable secret material, the session identifier, and explicit protocol/algorithm context using an established KDF.
3. Allocate nonces uniquely for every encryption under that key.
4. Store the public information needed to reconstruct the key and nonce with each object or in an unambiguous, recoverable envelope structure.

This avoids a single persistent encryption counter shared across all backup runs. It does not remove nonce uniqueness or algorithm usage limits.

Requirements:

- Never reuse a session-key/nonce pair for a new encryption.
- Independent writers MUST have independent sessions or another explicitly proven nonce allocation scheme.
- Restarted or retried writing MUST NOT resume an old session with a reset counter. A fresh session is the intended default.
- Define usage limits and start a new session before reaching them.
- Restore MUST work using durable recovery secrets and stored public session information; it MUST NOT require retaining a separate secret for every historical session.
- Do not claim forward secrecy: compromise of the durable derivation secret can expose historical data encrypted under keys derived from it.

OPEN: KDF, identifier length, key hierarchy, nonce layout, session lifetime, usage limits, and envelope overhead. Do not copy a cryptographic construction merely because Borg uses it; review it for our libraries and threat model.

### 7.4 Stable identifiers remain independent of sessions

Deduplication identifiers MUST remain stable within their configured host/set key domain across writing sessions. Derive them from plaintext using a secret identifier key. Keyed BLAKE3 is a candidate; the final selection remains OPEN.

Session-specific ciphertext MUST NOT define the logical deduplication identity. Existing objects with the same logical ID are reused rather than replaced by a new encryption of the same plaintext.

The server cannot verify a keyed plaintext ID against plaintext without the key. A malicious client can therefore submit bad new content; server authorization and immutability MUST still prevent it from replacing objects required by earlier retained snapshots.

Host/set isolation follows chapter 4. There is no cross-host deduplication, and independently keyed sets have independent identifier namespaces. Define identifier domain separation for object roles.

### 7.5 Passphrases and recovery secrets

Argon2id is the intended approach for deriving a key from a user passphrase to protect stored recovery/key material. It is not the per-chunk encryption algorithm and MUST NOT be run for every data chunk.

Keep durable backup secrets independent of the user's passphrase where practical, so a passphrase change can rewrap key material without re-encrypting all backup contents. Exact wrapping and recovery procedures remain OPEN.

Parameters, salt generation, resource bounds, migration, and unattended unlocking must be specified and validated. A passphrase change is not equivalent to revoking already disclosed backup keys.

## 8. Object processing, deduplication, and physical storage

### 8.1 Processing

The intended pipeline is:

```text
source bytes -> content-defined chunks -> keyed plaintext identifiers
            -> query existing identifiers -> compress missing chunks
            -> authenticated encryption -> transfer and durable storage
```

The server can identify existing objects without knowing plaintext. Stable IDs do not require deterministic encryption.

### 8.2 Small files and chunking

- Empty files require metadata but no data chunk.
- A non-empty file smaller than the chunking threshold can be one chunk, without padding.
- The final chunk may be smaller than the nominal minimum.
- Small objects may be physically packed together without coupling unrelated files into one logical plaintext chunk.

Chunking parameters and container/pack layout remain OPEN. Delta chains between successive chunk versions are not required for version 1.0.

Use established chunking implementations through Python modules. Chunker selection, parameters, and any optimizations must be evaluated rather than inferred from Borg compatibility. Performance targets require measurements.

### 8.3 Physical packs and location indexes

Packs are a strong candidate, not a mandatory initial format. Evaluate them before selecting the storage layout.

The intended separation is:

- logical layer: independently identified, compressed, authenticated, encrypted objects;
- physical layer: one or more such objects concatenated into a storage container;
- location index: object ID to container, offset, and length.

Packing MUST NOT merge unrelated files into one logical plaintext chunk or couple their encryption. It MUST NOT cross independent key scopes without an explicit later decision.

For our architecture, server-side assembly of already encrypted objects is a candidate: the server can manage physical layout without receiving plaintext or decryption keys. Client-side packing is not required merely because Borg uses it.

If packs are adopted:

- Prefer self-describing bounded object framing that allows location-index reconstruction from stored bytes.
- Distinguish a reconstructed location index from cryptographically verified content and authoritative snapshot membership.
- Consider a hash of stored container bytes for server-side corruption detection. Such a hash does not authenticate plaintext or protect against a hostile server rewriting both data and hash.
- Allow individual object reads without downloading an entire large container where the storage supports it.
- Compaction MUST preserve the encrypted bytes of live objects; the keyless server cannot decrypt or recompress their plaintext.
- Persist replacement containers and crash-safe index changes before removing old containers.
- Missing or corrupt indexes MUST NOT authorize speculative deletion.

OPEN: pack adoption for version 1.0 and the first increment, sizing, buffering, naming, index engine, recovery format, and concurrency.

## 9. Snapshots and authoritative server metadata

### 9.1 Independent roots and shared immutable objects

Each completed snapshot MUST have its own stable identity and an encrypted root object describing how to reach its complete recoverable file selection.

The encrypted snapshot root references file-metadata objects, which in turn reference data objects. The authoritative server record separately captures the complete required object membership. This describes logical relationships, not a prescribed serialization or tree shape.

Snapshots MAY share unchanged data and metadata objects within the permitted deduplication scope. Logical objects required by retained snapshots MUST NOT be changed in place by a client.

Restore MUST NOT depend on replaying earlier snapshots. Deleting the first snapshot cannot invalidate a retained later snapshot.

Avoid a design that requires replacing a single client-controlled global manifest on every backup. A database transaction may still be appropriate for server metadata; independent roots do not forbid databases or eliminate transactions.

OPEN: snapshot ID generation, root/content ID relationship, metadata chunking, serialization, and metadata reuse strategy.

### 9.2 File metadata and consistency

Capture and restore Linux file metadata with an explicit fidelity contract. Before implementation, decide support for permissions, owners, hardlinks, symlinks, timestamps, ACLs, xattrs, sparse files, special files, and filesystem boundaries. Unsupported attributes must not be silently claimed as preserved.

A backup of a changing filesystem is not automatically application-consistent. Application-specific orchestration and database integration are not baseline version 1.0 requirements.

### 9.3 Encrypted description and authoritative membership

| Responsibility | Required contents and authority |
| --- | --- |
| Client-encrypted snapshot description | File paths, file attributes, data references, and the configuration used for the run; decryptable on the recovery client |
| Authoritative server snapshot record | Authenticated host/set scope, snapshot/root identity, complete required object membership, server commit time, and server retention/protection state |

The server MUST be able to preserve and reclaim encrypted objects without decrypting file metadata. Its authoritative membership includes metadata objects and the root, not only file-data chunks.

Membership may be declared by a client, but the server MUST validate existence and scope before commit, then persist the accepted membership immutably. This does not prove that a malicious client's new snapshot is complete or truthful. It prevents that client from rewriting the accepted dependencies of an earlier snapshot.

Server-readable object membership reveals relationships among opaque objects. This is an intentional consequence of our server-enforced protection model; it is not a promise to hide the entire object graph from the server.

The authoritative snapshot catalogue and its committed dependencies MUST NOT be replaceable by a client. Define storage, checking, and recovery for this authority separately from rebuildable location indexes and caches.

## 10. Storage backend in version 1.0

### 10.1 Server definitions, client choice, and defaults

Version 1.0 MUST know and use the concept of a storage backend. The available backends are defined and configured on the server. The client determines which server-defined backend to use, subject to authorization and defaults. Exactly one implementation MUST be delivered in version 1.0: `file`, identified as `file`. The backend name and identifier are both `file`, matching the `file:` URI scheme. This naming decision does not fix the still-open configuration syntax.

The server owns backend definitions: supported types, configured instances/targets, filesystem paths or future remote endpoints, credentials, and access permissions. The client selects an existing authorized backend; it does not supply an arbitrary filesystem path, endpoint, credentials, or new backend definition.

Default behaviour:

- `file` is the default backend.
- Omitting a client selection uses defaults; version 1.0 therefore requires no client-side backend option, configuration field, or user action.
- Explicit client selection is a later extension when additional backend choices are implemented. Do not add a placeholder selector to the version 1.0 client merely to anticipate that extension.
- Later explicit selections MUST be validated against server-defined availability and authorization. Unknown or unauthorized selections fail; default resolution applies to omitted selection, not to hiding an invalid explicit request.

Exact server configuration syntax, future client selection syntax, selection scope (for example host or set), and any additional default hierarchy remain OPEN. Do not silently introduce per-run backend switching or repository migration semantics.

`file` means filesystem access through a server-visible path. It does not mean that the bytes must reside on a physically local disk. Allowed deployment examples include:

| Server-visible storage path | Backend used by 8472backup 1.0 |
| --- | --- |
| Directory on a local disk or attached medium | `file` |
| Directory on an NFS mount | `file` |
| Directory on another OS-provided network filesystem | `file` |
| Directory exposed by a third-party cloud mount | `file` |

Mount creation, network credentials, and third-party synchronization are outside 8472backup. A cloud mount does not become native cloud support merely because the configured directory is backed by a cloud service. A later synchronization of local files is likewise an external operation, not an 8472backup backend implementation or a remote durability acknowledgement.

The implementation MUST NOT reject a storage path solely because it is mounted or not on a physically local disk. Define and document the filesystem behaviour required for durable writes, publication, locking as applicable, and recovery. Mounted filesystems remain subject to this contract; the `file` name does not guarantee that every filesystem or mount provides identical semantics.

The architecture MUST separate:

- server policy and authoritative snapshot state: authentication, authorization, membership, protection periods, admission, and cleanup decisions;
- storage operations: writing, reading, locating/listing, and reclaiming stored objects or containers, with explicit durability and error semantics.

Use a small explicit backend interface from version 1.0. Route backup-object persistence through that boundary; do not scatter `file` path operations throughout policy code. This does not force authoritative metadata into the same storage engine or fix the open index/database choice.

Selecting a backend does not grant direct storage access or bypass snapshot protection. Backend methods that remove data MUST only be reachable through authorized server operations that enforce retained-object liveness and protection. The interface is an internal persistence boundary, not a general client-accessible storage API.

For current work:

- Implement server-side definitions and the backend boundary with `file` (`file`) as the sole implementation.
- Use `file` by default, without introducing client-side backend selection functionality in version 1.0.
- Keep basic backup, restore, protection, cleanup, and recovery usable without a cloud account or external storage service.
- Do not add dynamic plugin discovery, external-service SDKs, cloud credential handling, or speculative backend capability machinery.
- Preserve local durability requirements and define real failure behaviour at the backend boundary.

## 11. Durable commit and crash recovery

Use a final server-controlled snapshot publication step after all dependencies are durably available.

Required logical sequence:

1. Admit a run in the authenticated host/set scope.
2. Receive and persist missing encrypted objects, without replacing committed objects.
3. Receive the encrypted snapshot root and complete membership declaration.
4. Verify required object existence and scope, including objects reused from earlier runs.
5. Durably record membership and authoritative snapshot metadata.
6. Publish the completed snapshot together with the server-assigned commit time and applicable protection state through a crash-safe commit mechanism.
7. Acknowledge completion only after that commit is durable.

Steps describe required ordering and visibility, not a fixed number of filesystem writes. Define transaction boundaries, fsync ordering, index updates, and recovery before implementation.

A crash before commit may leave staging or unreferenced objects, but MUST NOT publish an incomplete snapshot. A crash after commit or a lost acknowledgement MUST have a defined recovery and retry outcome. Cleanup MUST protect active operations as well as committed snapshots.

The server commit time, never a client-supplied date, starts minimum protection. There is no agreed additional grace period, mandatory preservation of the last snapshot, or ordinary client-accessible protection override.

Define persistent states for staging, data and index persistence, accepted membership, commit publication, logical pruning, garbage collection, and pack compaction. A cache or location-index rebuild does not reconstruct authoritative historical protection state.

OPEN: serialization, snapshot/root identifiers, index/database engine, transaction mechanism, fsync and atomicity boundaries, data integrity checks for opaque ciphertext, and corruption recovery. Do not select SQLite, a pack format, or another engine merely because it is convenient.

## 12. Retention, minimum protection, and garbage collection

Each repository has a server-controlled minimum protection duration. Default: seven days.

For a completed snapshot:

```text
protected_by_server = server_now < server_commit_time + protection_period
keep_snapshot = retention_selects_snapshot OR protected_by_server
```

`server_commit_time` is assigned by the server when completion is committed. A client-provided historical timestamp may be preserved separately but cannot determine protection expiry.

Protection covers every object required for complete restoration, including data chunks first stored long before the snapshot. Object age does not determine whether an object can be deleted.

Example: a three-month-old snapshot A and yesterday's protected snapshot B reference chunk X. A may expire; X must remain because B still needs it.

Once a snapshot's protection period expires, client-specified retention may remove it. An older snapshot is not granted a fresh seven-day delay when a retention policy changes. No additional deletion-grace requirement has been agreed.

After a completed backup, the server automatically evaluates the set's retention rules, applies the protection override, and reclaims eligible space safely. Logical snapshot deletion and physical reclamation may be separate internal operations.

The server MUST enforce protection at every application-level deletion path, not merely during a scheduled prune job. No ordinary client or maintenance API may bypass it. A host administrator with direct filesystem control remains outside this guarantee.

OPEN: exact calendar/count retention semantics, timezone handling, changes to server protection duration, treatment of retired sets, and any explicit administrator override. Preserve the last completed snapshot automatically only if Thomas adopts that additional policy; it is not currently an agreed requirement.

### 12.1 Liveness and garbage collection

Logical snapshot removal and physical object reclamation may be separate operations. Neither operation may bypass server protection.

The server MUST derive liveness from its authoritative records for retained snapshots and active operations. It MUST NOT rely solely on traversing client-encrypted file metadata or on a client-replaceable reference cache.

Tracing and reference counting remain alternatives to evaluate. Borg 2's choice of garbage collection does not automatically settle our implementation.

Any rebuild of acceleration indexes MUST preserve the distinction between cached locations and indispensable server authority. Do not infer historical protection timestamps or overwrite committed membership from client-provided metadata during repair.

Soft deletion and an `undelete` command are optional future product decisions, not a new requirement. They do not replace minimum protection.

Protection must be enforced by server authority even when a compromised client still possesses valid transport and encryption credentials. An encryption authentication tag proves neither that a client is uncompromised nor that deletion of historical snapshots is authorized.

## 13. Communication, execution, and protocol

### 13.1 Backup modes and scheduled pull

The backup mode is configured on the backup host (client) per host: `mode = "pull"` by default, or `mode = "push"` when explicitly selected. The setting applies to the host's backup sets. The precise client configuration file location, schema, and wire representation are OPEN. Both modes use the same server-side authorization, snapshot commit, retention, minimum protection, and safe garbage-collection rules.

At initial registration the client transmits its current configuration file to the server. The server validates the host mode and records it as part of registration. A host registered with `mode = "push"` is never added to pull scheduling and is not contacted for pull backups; an omitted mode defaults to `pull` and the server schedules it. Once registered in push mode, switching to pull requires the dedicated client command described below. Registration must not expose backup decryption keys or passphrases to the server. Exact serialization, validation and configuration fields remain OPEN.

In pull mode the server schedules the host's backup sets. When a scheduled pull connects to the client, the client supplies its current validated host configuration. If it reports `mode = "push"`, the server removes that host from its pull scheduling list and records the push scheduling state persistently; it does not start the pull backup for that host. This changes scheduling only: the host remains registered and its repository, snapshots, retention, protection, and recovery access remain intact.

The server does not periodically pull or probe that host merely to discover a switch back. To return to pull, the operator changes the client configuration to `mode = "pull"` and proactively invokes a dedicated client command that informs the server over an authenticated connection. The server then adds the host back to its pull scheduling list. The exact command name and protocol are OPEN. Repeated requests should be safe, and failed requests must be reported so the operator can retry. Missing, invalid, or unreadable configuration must not silently be interpreted as a request to change mode. Mode is scheduling configuration, not an authorization boundary.

OPEN: exact persisted representation, mode acknowledgement, retry and reconciliation behaviour, and handling of already-running or overlapping operations.

The server schedules hosts and sets, connects to a client through SSH, and invokes a restricted client operation. The client reads local configuration, processes source data, and returns encrypted objects and protocol metadata.

The server's SSH private key remains on the server. Its public key is installed in the client's `authorized_keys` with a forced command and restrictions on unrelated SSH facilities.

The client need not run a permanent backup daemon. It can be invoked through SSH or by a local user.

### 13.2 Push backups and client-initiated operations

In push mode the backup client initiates a backup over SSH to the server and sends encrypted objects and the protocol metadata needed to create a snapshot. The operator arranges invocation through a client-side scheduler such as cron or a systemd timer. The setting alone does not install or configure a scheduler, and no permanent client daemon is required.

The server admits and authorizes the operation and remains responsible for its authoritative snapshot record, commit timestamp, minimum protection, retention and garbage collection. A push client can create snapshots only through the authorized backup protocol; it cannot overwrite committed history, shorten protection, or obtain unrestricted repository access. After a successful backup, retention is evaluated subject to server protection just as in pull mode. Apply the same host/set isolation, encryption, failure handling and durability guarantees in both modes.

OPEN: exact backup CLI and pull-reactivation command, scheduler integration, acknowledgement and retries, and handling overlapping invocations or in-flight mode changes. Do not silently grant additional deletion or maintenance privileges to push credentials.

The client can also connect to the server through SSH for init, set registration, restore, mount, and other explicitly authorized CLI operations.

A client SSH key is separately authorized on the server through a restricted entry. Having a client-to-server key must not grant an arbitrary repository write API or deletion authority.

Pull uses server-side scheduling and execution; push uses client-side scheduling and initiation. Both require server admission and the same protection rules. Whether a user can manually request backups in pull mode remains OPEN.

### 13.3 Client SSH access to the server service

The intended access path for client-initiated operations, including push, registration, restore, mount, and mode changes, is:

```text
backup client <-> SSH <-> server sshd <-> restricted 8472backup SSH command
                                      <-> local Unix-domain socket <-> 8472backup server service
```

The systemd-managed server service is a separate long-running process. On SSH authentication, `sshd` starts a restricted per-connection command; the command exchanges framed protocol bytes with the client over standard input/output and relays requests and responses over the local Unix-domain socket to the server service. SSH provides the network transport; a separate TCP listener or SSH TCP port-forwarding tunnel is not required by this design. Prevent PTY allocation and unneeded forwarding on restricted keys. Keep protocol output separate from diagnostics.

The server service owns authorization, repository state, snapshot publication, retention, and protection. It must bind every operation to the authenticated host and applicable permissions. The SSH gateway must convey the authenticated identity using a server-controlled, trustworthy mapping from the accepted key or equivalent credential. A client-asserted UUID, hostname, request field, or untrusted `SSH_ORIGINAL_COMMAND` is not proof of identity. Restrict and protect the socket and the gateway-to-service trust boundary; do not assume a Unix socket or its shared OS account automatically authenticates individual SSH keys. The detailed identity mapping, local peer authorization, framing, and deployment remain OPEN and must be reviewed before implementation. Do not grant clients direct general-purpose access to the socket or repository.

For pull, the running server service initiates SSH to the client and invokes the restricted client command. That direction also does not require an additional TCP tunnel. This section specifies the intended design, not a claim that it is implemented.

### 13.4 Protocol requirements

- Version the protocol and negotiate supported capabilities.
- Define explicit operations and permission checks in both directions.
- Bound message lengths, allocation, object sizes, and concurrency.
- Batch object-existence queries and transfers to avoid one network round trip per chunk.
- Avoid unsafe deserialization and arbitrary client-controlled shell command execution.
- Verify SSH peer identity; do not disable host-key checking as a convenience default.
- Specify reconnect, abort, duplicate request, and commit acknowledgement behaviour.

OPEN: framing, serialization, session admission, authenticated identity mapping, and detailed gateway/socket protocol. SSH and the restricted-command-to-local-socket path are the agreed transport direction; the exact RPC design is not selected.

## 14. Initialization, registration, and identity recovery

The intended user workflow is:

1. Install the Linux server, with minimal standard-case setup.
2. Install the client and invoke init with the server address and optional set name and encryption-passphrase input.
3. Generate a client authentication key without a passphrase for unattended operations where needed.
4. Display instructions for installing that public key on the server with the restricted command.
5. For server-initiated pull, install the server's public SSH key on the client with the corresponding restricted command. Push-only operation does not itself require a server-to-client SSH connection; bootstrap details remain OPEN.
6. Generate a stable host UUID and the selected encryption material.
7. Persist the client's recoverable identity/key state under `/var/lib/8472backup/state/` on Linux.
8. Register host UUID, hostname, address, initial set, and transmit the client configuration over an authenticated channel. Set initial server scheduling state from its validated mode: push hosts are not added to pull scheduling; omitted mode defaults to pull.
9. Clearly tell the user to back up the recovery state separately.

Registration must be bound to approved authentication credentials, not self-asserted UUID/hostname/IP values. The bootstrap trust exchange must be documented; do not silently auto-enrol arbitrary clients.

The user may supply a passphrase through the CLI. Exact secure input mechanisms remain OPEN; prevent accidental secret exposure in logs and avoid argv exposure as the only input mechanism.

The passphrase-protection and key-separation direction is defined in chapter 7. OPEN: exact key generation/wrapping and unattended unlocking, initial server key provisioning, address updates, idempotent init, host reinstallation, recovery/re-enrolment, and authentication-key rotation.

## 15. Backup descriptions and configuration

Each set has a local backup-description file, conceptually similar to a borgmatic configuration. It defines source selections, exclusions, and requested retention.

- Descriptions can change between runs.
- Read and validate one fixed configuration version per run.
- Changes during a run take effect on the next run.
- New includes affect future snapshots; exclusions do not rewrite previous snapshots.
- Retention changes may affect older snapshots, subject to server protection.
- Save the used description encrypted as part of the snapshot for reproducibility.
- Pass the server the validated operational retention metadata needed to evaluate policy without decrypting the description.
- An invalid description fails the run and cannot become an empty/default deletion policy.
- The host-level backup mode defaults to `pull` and can be set to `push`; exact file placement remains OPEN.

OPEN: syntax, file locations, schema, supported rule vocabulary, and scheduling configuration location. Do not assume existing `retentions` semantics without review.

## 16. Restore and read-only mounting

- Restore is initiated on a client with the required recovery keys.
- The server returns encrypted metadata and objects; the client decrypts and verifies them locally.
- Read-only FUSE mounting is a planned Linux 1.0 feature and runs on the client.
- Mount support is optional to the core backup/restore installation and not a universal future-platform requirement.
- Specify overwrite behaviour, path traversal protection, symlink handling, integrity failures, interruption, and metadata fidelity.
- Recovery must not require the original source host to remain available.
- Cached indexes and acceleration state SHOULD be rebuildable; distinguish them from indispensable recovery secrets.

Import of Borg history through a mount was discussed as feasible but is not committed to version 1.0. Import must distinguish original archive date from server commit time. Borg/restic format compatibility is not required.

## 17. Operations and observability

- Explicit CLI commands, useful exit codes, and logs suitable for systemd/journald and automation.
- Report completed, failed, partial, and protected/pruned states accurately.
- Never log passwords, key material, or unintended plaintext file contents.
- Bound client consumption of server resources; quotas or equivalent admission limits must not evict protected history.
- Handle unreachable hosts, overlapping jobs, retries, interruption, and disk-full conditions predictably.
- Keep normal configuration and deployment small. No web UI, SaaS dependency, or enterprise orchestration framework is required.

OPEN: privilege model, service accounts, source filesystem privileges, default concurrency, schedules, retries, quotas, metrics, and package formats.

## 18. Verification and release acceptance

Tests must exercise behaviour and invariants, not merely mirror implementation. Include at least:

| Scenario | Required outcome |
| --- | --- |
| Client initiates an authorized SSH operation | Restricted gateway relays to the running server service over the local socket without a TCP tunnel |
| Client claims another host identity | Gateway and server derive authority from authenticated credentials; cross-host access is denied |
| Client invokes an unauthorized operation | Server rejects it; protected snapshots and repository state remain unchanged |
| Client disconnects during a gateway session | Incomplete work is handled safely and committed history remains intact |
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
| Location index lost when packs are used | Documented rebuild without inventing authoritative protection state |
| Version 1.0 client supplies no backend selection | Default `file` works without a client selector |
| Server defines `file` with a chosen path | Backup and restore use the server-defined location |
| Storage path is on a suitable mounted filesystem | Same `file` backend, subject to the documented filesystem contract |

Use integration tests and targeted fault injection for durability claims. Compare restored bytes and agreed metadata, not only command exit codes.

Benchmark representative unchanged backups, local edits in large files, many small files, restore, mount, and cleanup. Record environment, storage, cache state, CPU, memory, throughput, and repository growth. Do not claim Borg-equivalent performance without comparable measurements.

Use purpose-written Python tests, temporary directories, representative data, and fault injection alongside each increment. Add actual process interruption and restart tests when persistent commit/recovery exists: simulated failures alone do not establish crash durability.

Include candidate ciphers, keyed hashes, chunkers, object overhead, and pack behaviour in these measurements. Tests for later explicit backend selection must eventually verify rejection of unknown or unauthorized choices; do not implement that client feature merely to test it in version 1.0.

## 19. Development workflow for Codex

- Work in small coherent increments Thomas can review completely.
- Explain consequential format, protocol, cryptographic, and durability decisions before implementing them. Resolve routine reversible choices without repeated permission requests.
- Keep comments, identifiers, tests, and project documentation in English.
- Keep modules explicit and comprehensible. Avoid speculative frameworks, clever metaprogramming, or hidden generated logic.
- Record accepted decisions and recovery procedures in Git; chat history must not be essential project documentation.
- Use established libraries through Python modules. Check license compatibility, platform availability, maintenance, and data-copy overhead before selecting them.
- Use appropriate automated tests, Ruff, and mypy; exact configuration and supported Python matrix remain to be chosen.
- Report what changed, why, what was checked, and what remains unresolved.
- Preserve existing work and public interfaces. Never silently change safety semantics to get tests passing.
- No implementation, build, tests, or release may require an AI service to operate.
- Do not expand into QuorumBD, non-Linux completeness, hosting, a web UI, or a migration suite without an explicit scope change.

Resolve routine reversible details autonomously while keeping consequential OPEN choices visible. The newer architectural directions in this consolidated specification are the baseline for review; do not resurrect superseded alternatives implicitly. Preserve the backend boundary in version 1.0 while leaving explicit client backend selection deferred.

## 20. Incremental implementation — archive design first

1. Start with archive/repository design: agree independent snapshot roots, object membership, key boundaries, durable commit invariants, and the small `file` backend boundary; prototype uncertain library choices.
2. Implement a local round trip: backup, second snapshot with reuse, verify, and restore.
3. Establish transactional repository behaviour, protected retention, GC, and failure tests.
4. Add authenticated SSH operations, registration, server-initiated pull and client-initiated push backup sessions, and set management.
5. Integrate configuration, scheduling, service operation, and operational failure handling.
6. Add read-only FUSE mounting and refine restore fidelity.
7. Benchmark, harden, package, document recovery, and validate in controlled real use.

This order may change by agreement. Early prototypes are not production backup tools. Safety boundaries must shape the initial model even when features arrive later.

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

## 21. Additional native backends — MUCH, MUCH LATER

Roadmap label: `Version 1+++ — much, much later` (Thomas's wording: "viel, viel später"). This is deliberately not a concrete release number or deadline. Additional backend implementations are not version 1.0 acceptance criteria; the backend concept itself is.

In a distant future version, the server is intended to support native external storage backends, including:

- SFTP servers;
- S3 and compatible object stores;
- other cloud storage services;
- other external storage targets where practical.

The server remains the control and policy authority. Clients select from server-defined, authorized backends, using defaults when no choice is supplied. They do not define backend configurations or receive direct backend credentials or unrestricted object-write/delete access. Client/server SSH transport remains separate from the server/backend storage connection.

Future backends MUST preserve client-side encryption, immutable retained history, minimum protection, and safe cleanup. Backend-specific consistency, durability, range reads, retries, incomplete uploads, and deletion semantics need explicit evaluation at that time. The version 1.0 interface may evolve when those concrete requirements are known; do not claim that it guarantees future backend or format compatibility.

External deletion by a provider or someone with direct backend administrator access is outside the client-compromise guarantee, just as direct filesystem administration is in the local model.

OPEN for that later phase: authoritative metadata placement, whether packs are mandatory for some targets, backend migration, caching, and recovery when external storage is unavailable. Replication, multi-target redundancy, and tiering are not implied requirements.

Other DEFERRED or uncommitted ideas include full non-Linux clients, Borg-history import, optional labels/protection tags, soft deletion/undelete, and additional orchestration. They do not enlarge version 1.0 by implication. A client-controlled protection tag is never a replacement for server-enforced minimum retention.

## 22. Licensing and project identity

All functionality, protection features, updates, and recovery capabilities must remain FOSS. No freemium tier or paid feature unlocks. Optional commercial support and services are compatible with this policy.

AGPLv3 has been recommended. The precise choice between `AGPL-3.0-only` and `AGPL-3.0-or-later` has not been explicitly confirmed; inspect the repository's actual LICENSE before setting package metadata.

Project name: 8472backup. The name may remain. Use an independent identity; do not assume Borg affiliation or repository compatibility.

Public project description: https://www.thk-systems.net/8472backup/

## 23. Borg reference material and reuse

Borg 2 is inspiration, not a dependency or compatibility target. Its development documentation changes over time; distinguish released behaviour from development snapshots.

- [Borg 2 overview](https://www.borgbackup.org/releases/borg-2.0.html)
- [Borg security and encryption design](https://borgbackup.readthedocs.io/en/master/internals/security.html)
- [Borg data structures](https://borgbackup.readthedocs.io/en/master/internals/data-structures.html)
- [Borg pack files and publication order](https://borgbackup.readthedocs.io/en/master/internals/packs.html)

Server-readable authoritative membership and server-enforced protection are project-specific requirements. They take precedence over Borg's different client-trust assumptions.

The adopted Borg 2 ideas guide the design as written here. New external development-documentation changes are reference material, not automatic amendments to this specification. Review concepts and code before reuse, and check licensing before incorporating source code.

## 24. First instruction to Codex

Read this specification and inspect the actual repository and its instructions. Begin with archive and repository design as described in chapter 20. Summarize established requirements, work through concrete snapshot and object examples, and identify the few decisions needed for the first local backup/restore increment. Propose a small initial design and dedicated behavioural tests, including durability and encryption boundaries. Do not scaffold the complete product or silently decide the unresolved storage and cryptographic formats. Proceed with implementation in reviewable increments as directed by Thomas.

Apply AEAD with session-derived encryption keys, stable keyed identifiers, independent snapshot roots, server-owned membership, late server publication, and the version 1.0 `file` backend boundary as the agreed direction. Argon2id is the intended passphrase KDF; cipher choice, keyed BLAKE3 selection, chunker choice, pack adoption, and storage formats remain subject to the explicitly OPEN decisions. Do not implement native remote backends or a dynamic plugin platform in the initial increment.
