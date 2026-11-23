# 8472backup — Snapshots, Commit, and Retention

Specification version: 0.58 — Report missing metadata on repository initialization retry, 2026-11-23

requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

## 9. Snapshots and authoritative server metadata

### 9.1 Independent roots and shared immutable objects

Each completed snapshot MUST have its own stable identity and an encrypted root object describing how to reach its complete recoverable file selection. The server generates the snapshot UUID with `uuid4()` for every new snapshot.

The immutable `Snapshot` dataclass in `server/snapshot.py` contains `snapshot_id` as a `UUID`, `root_id` as `ObjectId`, `committed_at` and `protected_until` as UTC `datetime` values, an `object_catalog` reference, and `version`, initially 1. `ObjectCatalogue` contains a `frozenset` of object IDs. The `Repository` supplies the client and backup-set IDs when the snapshot record is saved or loaded. Section 11 defines the persistent representation.

Snapshot annotations use the shared `ObjectId` alias from `archive/objects.py` for the root ID and catalogue members.

The encrypted snapshot root references the root directory's metadata object. Directory metadata references its direct children by name and object ID. File metadata references its ordered data chunks. The authoritative server record separately captures the complete required object membership. The client-side encoding of directory entries and filesystem metadata remains OPEN.

The root is a client-encrypted object of type `3` with an object ID and a pack location. Its referenced metadata and data objects may reside in other packs and be shared with other snapshots. The server snapshot JSON records the snapshot identity and root ID. The location index resolves the root ID to a pack and byte range. The client restores data by following decrypted references. The server preserves the accepted dependencies using the binary catalogue.

Snapshots MAY share unchanged data and metadata objects within the permitted deduplication scope. Logical objects required by retained snapshots MUST NOT be changed in place by a client.

Restore MUST NOT depend on replaying earlier snapshots. Deleting the first snapshot cannot invalidate a retained later snapshot.

The server owns the snapshot register and the per-snapshot records described in chapter 11. Independent roots allow each retained snapshot to restore independently. The server controls publication and updates the register under the repository lock.

OPEN: root/content ID relationship, metadata chunking, client metadata encoding, and metadata reuse strategy.

Each snapshot MUST also have its own snapshot-specific metadata, distinct from source file attributes. The client-encrypted snapshot description must retain the configuration used for the run; descriptive text, source-side timing, and summary statistics are possible additional fields whose schema remains OPEN. The snapshot root carries or references this metadata; inline placement versus separate metadata objects is not decided here. The server separately owns authoritative commit time, scope, membership, and protection state. Client-reported snapshot timestamps cannot determine protection expiry. No snapshot metadata implementation is authorized by this clarification.

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

The snapshot object count comes from the length of its binary catalogue. The separate pack header records the number of object records in that pack.

The per-snapshot object catalogue is logically a set of unique object IDs containing every dependency required for complete restoration, including the snapshot root, snapshot-description metadata, file metadata, and data objects. Reused objects are included even when first stored by an earlier snapshot. Once committed, this membership is immutable. It contains no physical pack locations. A separate repository location index, scoped to the applicable key domain, maps an object ID to a pack and byte range. Compaction changes that location index without rewriting snapshot membership.

During snapshot assembly, the server collects every required ID in a per-snapshot set. On the first occurrence of an ID, it consults the location index and forwards the object to the pack writer only if the ID has no recorded location. Successive packs consume the same filtered iterator, so an ID is forwarded at most once even when its pack location has yet to be recorded. Exhaust the iterator before freezing the membership for the catalogue. The input iterator must support skipping unread bodies.

The snapshot service connects the filter and pack writer under the repository lock. It assigns pack IDs and paths within the key domain. After each pack is durably published, the writer reports the locations it recorded while writing, and the service adds them to the in-memory index. If all required objects already exist, the service assembles the catalogue without creating a pack. It saves the index before publishing the authoritative snapshot.
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

1. Admit a run in the authenticated host/set scope and atomically acquire its exclusive backup-run lock.
2. Process the membership stream and persist missing encrypted objects while preserving previously stored objects.
3. Receive the type 3 snapshot root as the final record and take the root ID from its header. The complete header and all declared body bytes signal the end of the backup input, including when the root ID is already indexed. A connection closure before the complete record interrupts the run.
4. Verify required object existence and scope, including objects reused from earlier runs.
5. Sync the packs and location index. Durably write the snapshot metadata JSON and its binary object catalogue.
6. Publish the completed snapshot by atomically updating and syncing the server-owned snapshot register.
7. Acknowledge completion only after that commit is durable.

The durable register update is the commit point. Readers use the register to determine which snapshots are published.

A crash before commit may leave staging or unreferenced objects, but MUST NOT publish an incomplete snapshot. A crash after commit or a lost acknowledgement MUST have a defined recovery and retry outcome. Cleanup MUST protect active operations as well as committed snapshots.

The server commit time, never a client-supplied date, starts minimum protection. There is no agreed additional grace period, mandatory preservation of the last snapshot, or ordinary client-accessible protection override.

Define persistent states for staging, data and index persistence, accepted membership, commit publication, logical pruning, garbage collection, and pack compaction. A cache or location-index rebuild does not reconstruct authoritative historical protection state.

### Local repository initialization

The façade derives the repository path as `<storage_root>/<client_id>` from the verified client identity. Each backup set has a key-domain ID (`key_id`) recorded by the server. The agreed layout is:

```text
<repo-root>/
  repository.json
  snapshot-registry.json
  snapshots/<backup-set-id>/<snapshot-id>.json
  snapshots/<backup-set-id>/<snapshot-id>.catalog
  data/packs/<key-id>/<pack-id>.pack
  data/index/<key-id>
```

The repository object carries the host's backup `mode`, which defaults to `pull`. `Repository.set_mode()` changes it in memory. `RepositoryService.set_mode(path, mode)` persists the selected mode for the entire repository under its lock. It writes and syncs a temporary file, atomically replaces the metadata file, and syncs the repository directory. Section 13.1 defines how host mode changes are communicated for scheduling.

The client initiates repository setup in both modes. The server requires at least one valid backup set and registers all accepted sets as one initialization. Reserve the new path exclusively, prepare storage for the accepted sets and their key domains, and initialize an empty snapshot register. Publish `repository.json` after these dependencies are durable. If the repository directory already exists and `repository.json` is missing, report that initialization may have been interrupted. Propagate initialization errors to the caller and preserve any existing or partially created repository files. The backup administrator handles inspection and cleanup before retrying.

`repository.json` in the repository root is UTF-8 JSON and contains both repository and backup-set metadata. Repository metadata includes the client UUID and mode. Set metadata is keyed by set UUID and includes its registered key-domain ID and the operational fields needed by the server. The façade must check the stored client UUID against the verified identity and resolve the key domain from the registered set. The exact schema for the pull SSH target, set names, scheduling, and retention remains OPEN.

`ServerService.init_repository()` takes the verified client UUID, the initial set-to-key-domain mapping, and the mode. The repository code uses the layout above. The development implementation stores the direct UUID mapping in `repository.json` version 1 and initializes `snapshot-registry.json` with `version` set to `1` and an empty `snapshots` object. The final metadata schemas remain OPEN.

### Local snapshot record and commit

`server.snapshot_service.SnapshotService` accepts an initialized `Repository` for the authorized scope. It uses the repository's paths for the index, packs, and snapshot records.

The current development implementation of `create_snapshot()` acquires the exclusive repository lock before reading the input and validates the first record as the root. The switch to final-root ordering and removal of the optional `snapshot_id` parameter are pending. The service stores missing objects and saves the index, assigns the UTC commit time, and calculates the protection deadline using the configured minimum duration, which defaults to seven days. `get_snapshot(snapshot_id)` loads the committed record under the repository lock and verifies its identity and scope.

The agreed format stores two server-owned files in the backup set's snapshot directory. Both are stored without compression or encryption.

| File | Contents |
| --- | --- |
| `<snapshot-id>.json` | Format version, snapshot UUID, client UUID, backup-set UUID, `root_id`, server commit time, stored protection deadline, and a checksum of the binary catalogue. Store `root_id` as 64 hexadecimal characters representing the original 32-byte object ID. |
| `<snapshot-id>.catalog` | Consecutive, distinct 32-byte binary object IDs in unspecified order, including the root ID and all reused dependencies. The format contains stable object IDs and can be read in bounded blocks independently of the location index. |

On restore, decode the JSON root ID back into its original 32 bytes. IDs read from the binary catalogue are already in the representation used by the location index.

`snapshot-registry.json` lives at the repository root and records the published snapshot IDs and their backup sets. Initialize it explicitly for a new repository. Update it atomically and durably under the repository lock as the last step of commit. During prune, check retention and protection, then durably remove the eligible entry from the register before removing its files. A crash between the durable writes can leave unregistered files for later recovery or cleanup. Treat a missing or invalid register in an initialized repository as an error.

The current `Snapshot.save_to()` and `Snapshot.load_from()` use a single `.snapshot` development file with a 106-byte binary header followed by the membership IDs. The JSON, binary catalogue, and register described above are the agreed next design.

OPEN: exact JSON schemas, catalogue checksum encoding, registry integrity and recovery, handling existing development records, transport and authenticated admission, retry acknowledgement, data integrity checks for opaque ciphertext, and corruption recovery.

### 11.1 Backup-run locking

The server MUST enforce an exclusive backup-run lock keyed by the stable `(client_id, backup_set_id)` pair. Resolve both IDs through authenticated, authorized context, not names or untrusted claims. Push and pull use the same lock authority. At most one backup run for that pair may proceed at a time. This lock does not itself serialize different sets or hosts.

Acquire the lock during admission, before object queries or transfers. Hold it through durable snapshot publication or until an aborted run has stopped writing. Bind ownership to the individual admitted run; a delayed disconnect or retry must not release a newer run's lock. A conflicting request must not start a second run. Whether to reject, skip, or queue it, and the precise disconnect, timeout, and restart recovery rules remain OPEN. Expiry alone must not permit a replacement writer while the previous owner can still write.

The backup-run lock identifies the admitted run for one set. The exclusive repository lock described below governs access to the packs and location index shared by all sets in that repository. The concrete locking and persistence mechanisms remain OPEN.

### 11.2 Client repository locking and maintenance

Each backup client has its own repository and location index. A repository's index covers only its own packs, with separate checkpoints and identifier namespaces for independent key domains. The server MUST grant exclusive access to one operation at a time per client repository. This applies to push, pull, mounts, restores, and maintenance that accesses the repository. All sets and key domains of the client share this lock.

Acquire the repository lock before opening the index or accessing packs. Hold it through durable snapshot publication or safe abort for a backup, through unmount or session cleanup for a mount, and through completion or safe abort for restore and maintenance. An active mount can delay a scheduled backup. The server must report the lock conflict and its cause. Queueing, timeouts, lock ownership, and restart recovery remain OPEN.

Different client repositories may run independently within the shared server memory budget. Once maintenance owns a repository's lock, it uses the authoritative retained-snapshot catalogues and effective protection rules to determine liveness.

The file backend provides a `RepositoryLock` context manager for local filesystems. It requests an exclusive, nonblocking `flock` on the repository's `.repository.lock` file. Acquisition reports contention immediately. The lock file stays in place so callers lock the same file. Repository operations will hold the context for their full lifetime.

### 11.3 Backup-run states and provisional data

A backup run has one of three coarse states: `in_progress`, `failed`, or `committed`. `in_progress` receives objects and prepares the snapshot; `failed` means the run ended without successful commit; `committed` means its required packs, index state, and authoritative snapshot record are durably published. `completed` is not a separate success state. These are run states, not a requirement to expose unfinished snapshots in the snapshot catalogue.

Unfinished packs and unpublished catalogues are existing data structures being created during an `in_progress` run. The informal term staging describes this phase; it does not require a separate domain entity, catalogue, class, or dedicated directory. The run state and ownership identify the associated provisional work. A failed run may leave unfinished files or unreferenced stored objects; cleanup must preserve anything needed by retained snapshots or active operations. Technical choices such as temporary filenames for atomic publication, state serialization, and restart recovery remain OPEN.

The handling and reconciliation of a commit whose acknowledgement is lost is deferred to a later edge-case discussion. This does not change the durable publication and acknowledgement ordering in chapter 11. The coarse server-side storage model is agreed; this documentation does not authorize implementation of snapshots or run management.
## 12. Retention, minimum protection, and garbage collection

Each repository has a server-controlled minimum protection duration. Default: seven days.

For a completed snapshot:

```text
effective_protected_until = max(snapshot.protected_until,
                                snapshot.committed_at + current_server_protection_interval)
protected_by_server = server_now < effective_protected_until
keep_snapshot = retention_selects_snapshot OR protected_by_server
```

`server_commit_time` is assigned by the server when completion is committed. A client-provided historical timestamp may be preserved separately but cannot determine protection expiry.

The stored `snapshot.protected_until` is server-controlled. Evaluate the effective expiry using the currently configured server interval as shown above. A shorter current interval cannot undercut the stored protection deadline; a longer current interval can extend effective protection for existing snapshots. Deletion through retention is permitted only after effective protection has expired and retention no longer selects the snapshot.

Protection covers every object required for complete restoration, including data chunks first stored long before the snapshot. Object age does not determine whether an object can be deleted.

Example: a three-month-old snapshot A and yesterday's protected snapshot B reference chunk X. A may expire; X must remain because B still needs it.

Once a snapshot's protection period expires, client-specified retention may remove it. An older snapshot is not granted a fresh seven-day delay when a retention policy changes. No additional deletion-grace requirement has been agreed.

After a completed backup, the server automatically evaluates the set's retention rules and applies the protection override. The background maintenance jobs handle physical space reclamation during idle periods.

The server MUST enforce protection at every application-level deletion path, not merely during a scheduled prune job. No ordinary client or maintenance API may bypass it. A host administrator with direct filesystem control remains outside this guarantee.

`remove_backup_set` marks the backup set as `removed`. `remove_repository` marks the repository as `removed`, which applies to all its sets. Marking a scope stops new backups within it and preserves access to retained snapshots.

The asynchronous cleanup job checks retired scopes during idle periods. It calls the internal `RepositoryService.delete_backup_set()` or `RepositoryService.delete()` for final deletion. Under the affected repository's lock, verify the `removed` state, snapshot retention and effective protection, and that active operations no longer require the scope. Delete a set's records and files once all of its snapshots are eligible. The repository's compaction rules govern reclamation of its objects in packs, including objects shared with retained snapshots. Delete the entire repository once all its snapshots are eligible and its data is no longer in use.

OPEN: exact calendar/count retention semantics, timezone handling, the expiry rule for count-based retention after retirement, handling active jobs during retirement, removal permissions, recovery after an interrupted deletion, and any explicit administrator override. Preserve the last completed snapshot automatically only if Thomas adopts that additional policy.

### 12.1 Liveness and garbage collection

Logical snapshot removal and physical object reclamation may be separate operations. Neither operation may bypass server protection.

The server MUST derive liveness from its authoritative records for retained snapshots and active operations. It MUST NOT rely solely on traversing client-encrypted file metadata or on a client-replaceable reference cache.

For initial compaction, use the register to enumerate the expected retained snapshots. Before reclaiming any data, check that each expected snapshot JSON and binary catalogue is present and readable, the stored snapshot and scope match the expected entry, and the catalogue checksum matches. Stop reclamation for that repository on any failed check, including a missing or invalid register or a required object that cannot be located.

Read the retained catalogues in bounded blocks under the repository lock. For each key domain, use a temporary bitmap with one bit per occupied location-index record, initially clear. Resolve each binary object ID to its record number in the loaded index and set the corresponding bit. Repeated references leave the same bit set. The marks are valid only for that loaded index during the compaction run. A persistent reference index is a future option if measurements show that scanning the catalogues is too costly.

DEFERRED implementation of the maintenance workflow. Run prune after a successful backup and schedule physical compaction as an asynchronous server job during idle periods, using the same scheduling approach as retired-scope cleanup. The compaction job holds the exclusive repository lock, determines liveness from the catalogues, and uses a percentage threshold to decide whether to reclaim space. Define idle admission and job frequency later.

OPEN compaction criterion. Define the metric, denominator, threshold value and default, and whether the criterion applies per repository, per pack, or at both levels. One candidate metric is reclaimable pack bytes, including space left by earlier runs, relative to total pack bytes. Measuring actual reclaimable bytes requires determining liveness.

Compaction and retired-scope cleanup operate independently per repository. Each job holds the affected repository's exclusive `RepositoryLock` while it checks and reclaims that repository's data. The server accounts for active operations at admission and applies the shared resource limits defined in chapter 13.

OPEN maintenance admission. Define how the jobs handle repository lock contention and how admission interacts with backup-run locks and the server-wide repository operation limit. Preserve the protection of retained snapshots and active operations.

Pack compaction reclaims space occupied by objects that are no longer required by retained snapshots or active operations. Copy the still-live object records unchanged into replacement packs; do not decrypt, recompress, or change their object IDs. Persist the replacements and switch their location-index entries durably before removing old packs, accounting for active readers. A wholly unused pack can be removed without copying; a wholly live pack needs no reclamation. Snapshot roots, metadata, and data have the same liveness protection. Exact selection thresholds, concurrency, crash recovery, and publication mechanics remain OPEN; compaction is not implemented by the header increment.

Any rebuild of acceleration indexes MUST preserve the distinction between cached locations and indispensable server authority. Do not infer historical protection timestamps or overwrite committed membership from client-provided metadata during repair.

Soft deletion and an `undelete` command are optional future product decisions, not a new requirement. They do not replace minimum protection.

Protection must be enforced by server authority even when a compromised client still possesses valid transport and encryption credentials. An encryption authentication tag proves neither that a client is uncompromised nor that deletion of historical snapshots is authorized.
