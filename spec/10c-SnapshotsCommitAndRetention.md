# 8472backup — Snapshots, Commit, and Retention

Specification version: 0.71 — Prefer portable Python over peak performance, 2026-11-27

requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

## 9. Snapshots and authoritative server metadata

### 9.1 Independent roots and shared immutable objects

Each completed snapshot MUST have its own stable identity and an encrypted root object describing how to reach its complete recoverable file selection. The server generates the snapshot UUID with `uuid4()` for every new snapshot.

The immutable `Snapshot` dataclass in `server/snapshot.py` contains `snapshot_id` and `backup_set_id` as UUIDs, `root_id` as `ObjectId`, `committed_at` and `protected_until` as UTC `datetime` values, an optional `comment`, and `version`, initially 1. Its `object_catalog` property returns an `ObjectCatalogue` containing a `frozenset` of object IDs. `SnapshotService` uses the repository client ID and the selected backup-set ID when saving or loading a record. Section 11 defines the persistent representation.

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

The repository object carries the host's backup `mode`, which defaults to `pull`. `Repository.set_mode()` changes it in memory. `ServerFacade.set_mode(client_id, mode)` opens the verified client's repository under its lock and passes the repository and held lock to `RepositoryService.set_mode()`. The repository service rejects mode changes for retired repositories. A change applies to all sets of the client. To persist a changed mode, the service writes and syncs a temporary file, atomically replaces the metadata file, and syncs the repository directory. If the requested mode is already set, the operation returns successfully without rewriting metadata. Section 13.1 defines how host mode changes will be communicated for scheduling.

The client initiates repository setup in both modes. The server requires at least one valid backup set and registers all accepted sets as one initialization. Reserve the new path exclusively, prepare storage for the accepted sets and their key domains, and initialize an empty snapshot register. Publish `repository.json` after these dependencies are durable. If the repository directory already exists and `repository.json` is missing, report that initialization may have been interrupted. Propagate initialization errors to the caller and preserve any existing or partially created repository files. The backup server administrator handles inspection and cleanup before retrying.

`repository.json` in the repository root is UTF-8 JSON and contains repository metadata and the registered backup sets. Repository metadata includes the client UUID and mode. Registered sets are keyed by set UUID and include their key-domain IDs. The façade must check the stored client UUID against the verified identity and resolve the key domain from the registered set. The server stores the latest accepted prune rule in `backup-sets/<set-id>.json` at the repository root. This UTF-8 file contains a string field `prune`, for example `{"prune": "7d4w3M1y"}`. The exact schema for the pull SSH target and scheduling remains OPEN.

`Repository` represents the whole client repository. It holds the path, client UUID, mode, boolean `deleted` flag, and all registered `BackupSet` records by set UUID. Each `BackupSet` holds its set UUID, key-domain UUID, boolean `deleted` flag, and optional display name. `Repository.snapshots` is the immutable set of published `Snapshot` objects as of the lookup. `RepositoryService` reads and writes the repository and backup-set metadata using this model. It obtains the snapshots through `SnapshotService`.

`ServerFacade.init_repository()` takes the verified client UUID, the initial set-to-key-domain mapping, the mode, and optional `backup_set_names` by set UUID. `add_backup_set()` accepts an optional `name` for the new set. Initialization returns the repository with all its initial sets and an empty snapshot collection. The current `repository.json` format uses version 1 and requires a repository-level boolean `deleted`. Each entry in `backup_sets` is keyed by set UUID and contains a UUID string `key_id`, a boolean `deleted`, and a printable single-line `name` when supplied. Initialization also creates `snapshot-registry.json` with `version` set to `1` and an empty `snapshots` object. Keep the metadata format version at 1 during pre-release development.

`ServerFacade.add_backup_set()` opens the repository through `RepositoryService.locked_repository()` using the verified client UUID. This context acquires the repository lock, reads the current metadata, and checks the stored client identity. The façade passes the resulting `Repository` and the held lock to `RepositoryService.add_backup_set()` along with the new set and key-domain UUIDs. The service rejects an already registered set, creates the set's snapshot directory, and prepares an empty index and pack directory for a new key domain. A registered key domain reuses its existing index and packs. After the new storage is durable, the service updates the repo's set mapping in `repository.json`. Existing unregistered storage is reported as an inconsistent state for the backup server administrator to inspect.

### Local snapshot record and commit

`server.snapshot_service.SnapshotService` is stateless. Its create and read operations accept a `Repository` and an explicit backup-set UUID. The service uses the selected set's key domain for index and pack access. It owns snapshot reads, writes, and listing.

The caller passes `create_snapshot()` the repository and exclusive lock from `RepositoryService.locked_repository()` and supplies the client-generated object headers and opaque bodies. The service rejects deleted sets before reading the input. It takes the root ID from the final type 3 header and finishes the input after reading the declared root body, including for a reused root. Ending the input before the complete root aborts the backup. The service stores missing objects and saves the index, assigns the UTC commit time, and calculates the protection deadline using the configured minimum duration, which defaults to seven days. `get_snapshot()` loads the committed record under the repository lock and verifies its identity and scope.

Before writing packs, the service checks that the key domain contains every pack named by the location index. For a reused snapshot root it validates the stored header, ID, and type. Before publication, the service syncs the index directory even when it reuses the existing checkpoint.

`list_snapshots()` reads the published snapshot metadata and defers catalogue reads. The first access to `Snapshot.object_catalog` loads and verifies the catalogue under the same repository lock and caches it for the lifetime of the instance. Snapshot equality and hashing use its metadata fields. `find_by_client_id()` returns after releasing its lock. Use the returned metadata to select a snapshot and call `get_snapshot()` to load its catalogue under a new lock.

The agreed format stores two server-owned files in the backup set's snapshot directory. Both are stored without compression or encryption.

| File | Contents |
| --- | --- |
| `<snapshot-id>.json` | Format version 1, `snapshot_id`, `client_id`, `backup_set_id`, `root_id` as 64 hexadecimal characters, `committed_at` and `protected_until` as ISO 8601 UTC strings, `catalogue_sha256` as a hexadecimal SHA-256 checksum of the catalogue bytes, and an optional server-readable `comment` of at most 40 characters. |
| `<snapshot-id>.catalog` | Consecutive, distinct 32-byte binary object IDs in unspecified order, including the root ID and all reused dependencies. The format contains stable object IDs and can be read in bounded blocks independently of the location index. |

On restore, decode the JSON root ID back into its original 32 bytes. IDs read from the binary catalogue are already in the representation used by the location index.

`snapshot-registry.json` lives at the repository root. Its version 1 `snapshots` object maps each published snapshot UUID to its backup-set UUID. Initialize it explicitly for a new repository. Update it atomically and durably under the repository lock as the last step of commit, after the packs, location index, catalogue, and JSON are durable. To remove a snapshot, atomically move its entry from `snapshots` to the optional `pending_deletions` object after verifying its eligibility. Both objects use the same UUID mapping and their IDs must be disjoint. After the register update is durable, remove the snapshot files, sync the set directory, and clear those pending entries. Retry file removal for the recorded pending entries of the same set, allowing for a file that was already removed. Treat a missing or invalid register in an initialized repository as an error.

The local `SnapshotService` uses the register for publication, lookup, listing, and snapshot removal. A repository with an empty register and old `.snapshot` records requires conversion before those operations can proceed.

The server stores an additional recovery copy under `/var/lib/8472backup/recovery/<client-id>/<repository-incarnation>/` by default. The `.metadata-recovery` record binds the copy to the repository instance. The copy holds snapshot JSON, catalogues, their verification data, and the register generation including pending deletions. The server stores these files on its local filesystem. Availability of the packs remains a prerequisite for restoration.

Before replacing the primary register, the server durably writes the proposed generation to `prepared.json`. After primary publication is durable, it writes the verified generation to `confirmed.json` and clears the prepared file. Backup acknowledgement and reclaiming deleted data follow confirmation. During a retry, a readable primary register selects a matching generation. If neither generation can be established from the surviving files, recovery stops. A repository with intact pre-existing metadata can initialize its copy on the next write or reclamation.

`ServerFacade.repair_snapshot_metadata()` holds the exclusive repository lock. It validates the selected recovery generation, snapshot identity and timestamps, catalogue checksums and membership, and the required index and pack records. It then atomically replaces affected primary files and returns their count. Final cleanup removes the copy for the detached repository incarnation. `check()` remains a read-only validation operation.

OPEN questions include conversion of existing development records, transport and authenticated admission, retry acknowledgement, and data integrity checks for opaque ciphertext.

### 11.1 Backup-run locking

The server MUST enforce an exclusive backup-run lock keyed by the stable `(client_id, backup_set_id)` pair. Resolve both IDs through authenticated, authorized context, not names or untrusted claims. Push and pull use the same lock authority. At most one backup run for that pair may proceed at a time. This lock does not itself serialize different sets or hosts.

Acquire the lock during admission, before object queries or transfers. Hold it through durable snapshot publication or until an aborted run has stopped writing. Bind ownership to the individual admitted run; a delayed disconnect or retry must not release a newer run's lock. A conflicting request must not start a second run. Whether to reject, skip, or queue it, and the precise disconnect, timeout, and restart recovery rules remain OPEN. Expiry alone must not permit a replacement writer while the previous owner can still write.

The backup-run lock identifies the admitted run for one set. The exclusive repository lock described below governs access to the packs and location index shared by all sets in that repository. The concrete locking and persistence mechanisms remain OPEN.

### 11.2 Client repository locking and maintenance

Each backup client has its own repository and location index. A repository's index covers only its own packs, with separate checkpoints and identifier namespaces for independent key domains. The server MUST grant exclusive access to one operation at a time per client repository. This applies to push, pull, mounts, restores, and maintenance that accesses the repository. All sets and key domains of the client share this lock.

Acquire the repository lock before opening the index or accessing packs. Hold it through durable snapshot publication or safe abort for a backup, through unmount or session cleanup for a mount, and through completion or safe abort for restore and maintenance. An active mount can delay a scheduled backup. The server must report the lock conflict and its cause. Queueing, timeouts, lock ownership, and restart recovery remain OPEN.

Different client repositories may run independently within the shared server memory budget. Once maintenance owns a repository's lock, it uses the authoritative retained-snapshot catalogues and effective protection rules to determine liveness.

The server provides `RepositoryLock` in `server/lock.py`. It requests an exclusive, nonblocking `flock` on the repository's `.repository.lock` file. Acquisition reports contention immediately. The lock file stays in place so callers lock the same file. Repository operations hold the context for their full lifetime.

If a lock-holding process hangs, the backup server administrator can terminate it with `kill -TERM <PID>`. Terminating a shared server process interrupts every job it owns. Cleanup can acquire the lock once the last file descriptor holding it closes.

### 11.3 Backup-run states and provisional data

A backup run has one of three coarse states: `in_progress`, `failed`, or `committed`. `in_progress` receives objects and prepares the snapshot; `failed` means the run ended without successful commit; `committed` means its required packs, index state, and authoritative snapshot record are durably published. `completed` is not a separate success state. These are run states, not a requirement to expose unfinished snapshots in the snapshot catalogue.

Staging describes the provisional phase of a backup run. The local backup operation reserves a server-generated snapshot UUID under the repository lock. Before writing backup files it creates and syncs an empty `.in-progress-<snapshot-id>` marker in the selected set's snapshot directory. Named temporary files for that run use the prefix `.backup-<snapshot-id>-`. This identifies the owner even for files in a directory shared by several backup sets. After durable publication in the snapshot register, the server removes the marker.

The regular client CLI will expose `cleanup` to the client administrator for an authorized backup set. The local `ServerFacade.cleanup(client_id, backup_set_id)` acquires the repository lock and reports contention immediately. It validates the snapshot register before removing any files. For each marker in the selected set, remove the attributed temporary files. Also remove the snapshot JSON and catalogue when the UUID is absent from both `snapshots` and `pending_deletions`. Sync the affected directories before removing the markers and then sync the set directory. This ordering allows a retry to complete interrupted cleanup. Return the number of run markers cleared. Compaction handles finalized packs.

The per-set `auto-cleanup` client setting maps to the local backup operation's `auto_cleanup` argument and defaults to `false`. An outstanding marker requires an explicit cleanup before the next backup can start with the default setting. With `auto-cleanup = true`, the server uses the same cleanup operation after an aborted run stops writing and before starting a new run when an outstanding marker exists. Automatic cleanup uses the lock already held by the backup operation.

The handling and reconciliation of a commit whose acknowledgement is lost is deferred to a later edge-case discussion. This does not change the durable publication and acknowledgement ordering in chapter 11. The coarse server-side storage model is agreed; this documentation does not authorize implementation of snapshots or run management.
### Location index recovery

`ServerFacade.rebuild_index(client_id, backup_set_id)` selects the registered set's key domain and holds the exclusive repository lock throughout recovery. It reads the headers of finalized packs in ascending numeric pack ID order and reconstructs object locations in memory. For repeated object IDs, the first record in that order supplies the location.

The server validates the metadata and catalogue of every published snapshot in the key domain against the rebuilt index, including snapshots in retired sets. It also verifies the packed root headers. Once validation succeeds, it syncs the pack directory and atomically replaces the selected index checkpoint through the regular durable save path.

## 12. Retention, minimum protection, and garbage collection

Each repository has a server-controlled minimum protection duration. Default: seven days.

The server MUST obtain current UTC time through `TrustedTime.now()` on the shared clock that the daemon synchronizes with its HTTPS quorum. Snapshot commit timestamps, retention, and expiry decisions use this clock. If the clock cannot provide a valid reading, the operation raises `TrustedTimeError` before publishing or deleting a snapshot.

For a completed snapshot:

```text
effective_protected_until = max(snapshot.protected_until,
                                snapshot.committed_at + current_server_protection_interval)
protected_by_server = server_now <= effective_protected_until
keep_snapshot = retention_selects_snapshot OR protected_by_server
```

`server_commit_time` is assigned by the server when completion is committed. A client-provided historical timestamp may be preserved separately but cannot determine protection expiry.

The stored `snapshot.protected_until` is server-controlled. Evaluate the effective expiry using the currently configured server interval as shown above. A shorter current interval cannot undercut the stored protection deadline; a longer current interval can extend effective protection for existing snapshots. Deletion through retention is permitted only after effective protection has expired and retention no longer selects the snapshot.

Protection covers every object required for complete restoration, including data chunks first stored long before the snapshot. Object age does not determine whether an object can be deleted.

Example: a three-month-old snapshot A and yesterday's protected snapshot B reference chunk X. A may expire; X must remain because B still needs it.

Once a snapshot's protection period expires, client-specified retention may remove it. An older snapshot is not granted a fresh seven-day delay when a retention policy changes. No additional deletion-grace requirement has been agreed.

`backup()` validates the supplied comment before validating and durably saving the prune rule under the repository lock. The rule takes effect before the server reads the objects and continues to apply if the upload fails later. After a successful snapshot commit, the server applies the same rule under the held lock. A failure during this prune is reported with the committed snapshot ID. `prune(client_id, backup_set_id)` acquires the repository lock and applies the stored rule to the selected active set. If the rule is missing or invalid, report the error before changing the register. Return the UUIDs whose deletion completed in this call.

Use the hierarchical calendar selection from [retentions](https://github.com/tkn777/retentions/blob/master/retentions.py#L795-L919). The rule contains adjacent positive counts followed by case-sensitive unit codes. Each unit can occur once. Evaluate enabled units in the order `h` (hours), `d` (days), `w` (ISO weeks), `M` (months), `q` (calendar quarters), `W` (13-week blocks within the ISO week-year), and `y` (years). Use the server-assigned `committed_at` and UTC calendar boundaries. For each rule, keep the newest snapshot in each of the requested number of occupied periods, starting with the most recent eligible period. Break ties by snapshot UUID in descending order. A coarser rule considers only periods strictly before the period containing the oldest snapshot selected so far. Empty periods consume no slots. An empty rule keeps every published snapshot. Include protected snapshots in the calendar selection and apply the server protection override before removing any snapshot.

Before unpublishing an eligible snapshot, verify that each snapshot being retained in its set still has a catalogue file. A missing catalogue stops the deletion. Prune removes the eligible snapshot's register entry, JSON, and catalogue in the order defined in chapter 11. Physical object reclamation remains the responsibility of asynchronous compaction.

The server MUST enforce protection at every application-level deletion path, not merely during a scheduled prune job. No ordinary client or maintenance API may bypass it. A host administrator with direct filesystem control remains outside this guarantee.

`ServerFacade.remove_backup_set()` opens the verified client's repository under its exclusive lock and resolves the requested `BackupSet`. `RepositoryService` durably sets `deleted` to true, blocking new backups in that set. Under the same lock, `SnapshotService` validates the deleted set's snapshot records and removes those strictly beyond their effective protection deadline. Calendar retention applies to normal prune. Set removal applies the protection rule to every snapshot in the set. Protected snapshots remain available for recovery.

The scheduler and local façade share a seven-day protection default. Supply the same server value to both when constructing them with an explicit duration. Set and repository removal use the same file removal sequence as prune. A retry completes pending deletions in the selected set and evaluates remaining published snapshots for protection expiry. The index and packs remain available for restore and subsequent compaction.

`ServerFacade.remove_repository()` acquires the repository lock for the verified client. `RepositoryService` durably sets `deleted` to true for the repository and all its sets in a single metadata write. This blocks new backups and backup-set registration in that repository. Under the same lock, the service uses the existing snapshot removal logic for each set. It removes snapshots strictly past their effective protection deadline and keeps protected snapshots available for recovery. The caller can retry after a cleanup error or to remove snapshots whose protection has since expired.

The scheduler checks retired scopes in its maintenance window. Under the exclusive repository lock, it reads `TrustedTime.now()` and removes published snapshots strictly after their effective protection deadline. Final deletion requires a retired scope with zero published snapshots and zero pending snapshot deletions. A failed trusted time reading stops maintenance.

`RepositoryService.delete_backup_set()` clears temporary files attributed to stopped runs before removing the snapshot directory and set policy. It also removes the key domain's index and packs when this was its last registered set. It syncs the affected directories before unregistering the set. A retry can finish an empty retired set whose storage was already removed. Shared key domains remain available to their other sets and are eligible for compaction.

`RepositoryService.delete()` moves an eligible repository atomically to `.deleting-<client-id>` under the same server storage root, then syncs that parent directory. The detached path is reserved for final deletion. The scheduler resumes recursive removal from this name after a restart, including when some files have already been removed. It acquires the detached repository lock and confirms the rename by syncing the parent before removing files, then syncs the parent again after removal.

The remote removal permissions remain open.

### 12.1 Liveness and garbage collection

Logical snapshot removal and physical object reclamation may be separate operations. Neither operation may bypass server protection.

The server MUST derive liveness from its authoritative records for retained snapshots and active operations. It MUST NOT rely solely on traversing client-encrypted file metadata or on a client-replaceable reference cache.

For initial compaction, use the register to enumerate the expected retained snapshots. Before reclaiming any data, check that each expected snapshot JSON and binary catalogue is present and readable, the stored snapshot and scope match the expected entry, and the catalogue checksum matches. Stop reclamation for that repository on any failed check, including a missing or invalid register or a required object that cannot be located.

Read the retained catalogues in bounded blocks under the repository lock. For each key domain, use a temporary bitmap with one bit per occupied location-index record, initially clear. Resolve each binary object ID to its record number in the loaded index and set the corresponding bit. Repeated references leave the same bit set. The marks are valid only for that loaded index during the compaction run. A persistent reference index is a future option if measurements show that scanning the catalogues is too costly.

The read-only compaction analysis validates the published catalogues and scans finalized pack headers under the repository lock. It reports total, live, and reclaimable bytes per pack. A referenced object contributes live bytes at its indexed location. An unindexed copy contributes reclaimable bytes. The pack header counts as live if the pack holds any live records.

Scheduled maintenance requires a storage root. Local callers pass it to the scheduler. The planned server TOML configuration will supply it in deployments. The scheduler starts maintenance 30 minutes after the daily pull batch by default, with an optional fixed UTC start. It dispatches repositories serially while operation capacity is available and no backups are active. `RepositoryService.maintain_repository()` runs retired-scope cleanup and due compaction under the same exclusive `RepositoryLock`. Detached repository removal uses repository capacity during its own lock scope. Busy or invalid repositories are logged and can retry in a later window. Maintenance allocations share the server work budget.

The server durably creates an empty `.compaction-pending` file in the repository before unpublishing snapshots or writing new backup packs. A successful backup clears a marker that it created for its own pack writes after publishing the snapshot. An interrupted write leaves the marker for maintenance, including after client cleanup. Maintenance clears the marker after completing a pass, including a pass that finds no eligible pack. A later removal or interrupted write triggers a new analysis. Retired scopes are checked in each maintenance window for newly expired protection.

A pack qualifies when it contains reclaimable bytes and their share of its total size reaches the configured threshold. The default is 25 percent. The scheduler accepts a whole percentage from 0 through 100. A wholly dead pack qualifies at any supported threshold. A wholly live pack is left intact. Changes to the threshold take effect the next time a repository is analyzed.

After validating the repository, copy live object records from selected packs unchanged into durable replacement packs. Update their indexed locations and discard remaining mappings to the old packs. Recheck published catalogue coverage and sync the index checkpoint before unlinking superseded packs. Sync the pack directory after removal. Snapshot roots, metadata, and data have the same liveness protection. A partially live pack needs temporary space for its replacements and the checkpoint.

Before a checkpoint switch, the old index and packs remain usable and unindexed replacement copies can be recovered as dead space. After a switch, the new index locates the live copies and the old pack can be reclaimed. A retry confirms the checkpoint directory sync before reclaiming old packs, removes any incomplete compaction files, and resumes pending snapshot deletions from the register.

Any rebuild of acceleration indexes MUST preserve the distinction between cached locations and indispensable server authority. Do not infer historical protection timestamps or overwrite committed membership from client-provided metadata during repair.

Soft deletion and an `undelete` command are optional future product decisions, not a new requirement. They do not replace minimum protection.

Protection must be enforced by server authority even when a compromised client still possesses valid transport and encryption credentials. An encryption authentication tag proves neither that a client is uncompromised nor that deletion of historical snapshots is authorized.
