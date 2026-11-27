# Server TODOs

This backlog tracks implementation gaps and open design decisions for the server façade and the client features it depends on. The [project specification](10-8472backup-Spec.md) defines the requirements.

## 1. Façade and repository lookup

Define the inputs, results, errors, and permissions for `init_repository`, `add_backup_set`, `remove_backup_set`, `remove_repository`, `backup`, `prune`, `list`, `restore`, `recover`, `mount`, `set_mode`, `cleanup`, `check`, and `version`. `restore` covers a complete snapshot. `recover` selects a file or directory tree on the client. Both use the shared object read path.

- **Caller identity.** The Unix-socket handler and the pull scheduler call the same façade. The scheduler supplies the client identity from the registered job. Define how the socket verifies its local caller and how the later SSH helper carries the client UUID bound to the approved SSH credential. Define how the systemd service accesses the key generated during server installation for outgoing pull connections.
- **Repository lookup.** Derive `<storage_root>/<client_id>` from the verified identity. Check the stored `client_id` before operating on the repository, resolve the requested backup-set UUID, and take its `key_id` from the registered set. Define the error responses for a missing repository, an identity mismatch, and an unavailable set.
- **Initialization.** The local façade initializes all accepted sets for a verified client and requires at least one set. Initialization errors propagate to the caller. The backup server administrator handles any cleanup before retrying. Connect client-initiated `init_repository` in both modes. The mode applies to the entire client, and each backup set has its own client-side TOML configuration. Report invalid set configurations individually.
- **Server metadata.** The current version 1 `repository.json` stores the deleted state at repository and set scope, the key-domain ID, and an optional display name for each set. The per-set JSON records the latest accepted prune rule. Define the fields for the pull SSH target and scheduling. The complete per-set TOML stays on the client and is preserved in the encrypted snapshot description.
- **Set and repository removal.** Local `remove_backup_set()` and `remove_repository()` mark the affected scopes deleted and remove snapshots past their effective protection deadline. Connect both operations to authorized clients and scheduling. Add internal `RepositoryAdminService.delete_backup_set()` and `RepositoryAdminService.delete()` for the asynchronous cleanup job. Check the deletion flags, effective protection, and active use under the repository lock before final deletion. Reclaim pack storage for retired sets through compaction. Define removal permissions, handling of scheduled jobs, and recovery after interrupted final deletion.
- **Check and version.** Connect the local `check()` and `version()` façade methods to the client commands. Stamp the version string with the full Git commit ID during deployment.

## 2. Push, pull, and scheduling

Route incoming push jobs and scheduled pull jobs through the same backup operation. Local `set_mode()` persists the mode per client, with pull as the registration default. Connect mode changes to the pull scheduler. Define how to record the SSH endpoint, retry failed jobs, and apply mode changes to queued and active jobs.

Enforce the configurable server-wide limit for concurrent backup sets, default 3. Queue pull jobs when full and return a wait response to push clients. Define queue ordering and cancellation.

## 3. Server process and wire protocol

Implement the systemd service and Unix-socket handler as a caller of the façade. Add the restricted SSH relay in a later increment. Push uses one SSH session for requests and replies through the relay and socket. Pull starts an SSH connection from the server to the restricted client command. Implement the batched object-ID exchange and request correlation specified in chapter 13.4. Define authentication and authorization, the exact message encoding, size and pipelining limits, and timeouts. Define how asynchronous connections interact with blocking file I/O and repository locks. See the [communication spec](10d-CommunicationConfigurationAndOperations.md#134-protocol-requirements).

## 4. Backup completion and snapshot commit

Use the complete final snapshot root object as the upload completion signal. `SnapshotService.create_snapshot()` currently expects the root first and requires its ID up front. Remove its optional `snapshot_id` parameter and always generate the snapshot UUID on the server. The local snapshot service saves the JSON and binary catalogue and durably publishes them through the server-owned register after saving the packs and index. Connect backup acknowledgement to that commit point.

## 5. Snapshot listing

Connect the local `list` façade method to the authorized client command. The local implementation reads the register and snapshot JSON, supports an optional backup-set filter, and provides the text output defined in chapter 13.5.

## 6. Shared object read path

Serve objects for restore, recover, and mount. Check requested IDs against the selected snapshot's catalogue, find them through the location index, and verify that the record at the indexed pack offset matches the requested object ID and expected length. The current `get_snapshot()` operation returns the snapshot record.

## 7. Directory tree and snapshot description

Replace the current flat metadata-ID list in the client-encrypted snapshot root content with a reference to the root directory. Directory metadata links direct child names to their object IDs. Capture the backup configuration used for the run in the encrypted snapshot description. Define the directory encoding, filename representation, and which filesystem metadata a restore preserves.

## 8. Restore

Support `restore` for a full snapshot and `recover` for a selected file or directory tree. The client resolves the requested path through the decrypted directory tree. Define what happens when a destination file already exists or an interrupted restore leaves a partially written file.

## 9. Mount

Provide read-only access on the client, with metadata and data loaded as needed. Define how to locate the chunks covering a file offset, bound caches, and release the repository lock on unmount or disconnect.

## 10. Client data path and object encryption

Connect source traversal and content-defined chunking to the existing object and compression code. Implement authenticated object encryption and client-side decryption. Settle AES-GCM versus AES-OCB, the fields to authenticate, and the key hierarchy. Argon2id is required for deriving the protection key from the client's master passphrase. Key storage and unattended unlocking remain open. See the [cryptography spec](10b-CryptographyObjectsAndPacks.md).

## 11. Locks and shared memory budget

Keep the existing exclusive repository lock and enforce one active backup run per `(client_id, backup_set_id)`. Enforce server-wide capacity through admission accounting for active operations. The planned default is 10 concurrent repository operations. Define admission and waiting behavior for capacity limits and lock contention. The 64–128 MiB target applies across active server operations, including index and catalogue memory and any server-side caching for mount readers. See the [configuration roadmap](70-ConfigurationRoadmap.md).

## 12. Interrupted runs and recovery

Connect the local `cleanup()` façade method to the regular client CLI and forward each set's `auto-cleanup` option to `backup()`. The local service applies the repository-locked cleanup rules in chapter 11.3. Define recovery and acknowledgement on the transport. Determine reclaimability of orphaned objects in finalized packs from published snapshots and active operations. Implement `LocationIndex.rebuild_from_packs()`, which is currently a stub.

## 13. Prune and compact

The local `backup()` persists the prune rule and applies it after commit. The local `prune()` applies the stored rule to the selected set. Connect both operations to the authorized client commands. Schedule compact and retired-scope cleanup as asynchronous jobs during idle periods. Use the affected repository's exclusive `RepositoryLock` and its own location index. Define the idle criteria, job frequency, contention handling, and how job admission interacts with backup-run locks and the server-wide limit. Define the exact compaction criterion, including its threshold, default, metric, denominator, and scope. Validate the register and all expected catalogues before compaction, then stream ID lists and mark one bit per occupied index record. Preserve objects referenced by retained snapshots or active operations. Make replacement packs and the updated index durable before removing the old packs. See the [snapshot and retention spec](10c-SnapshotsCommitAndRetention.md).

## 14. End-to-end verification

Extend the existing local write-path coverage with two encrypted backups that reuse objects and a direct single-file restore. Test interruption immediately before the final root byte and after durable commit but before acknowledgement. Verify the outcome after reopening the repository.

## 15. Object encryption session keys

For each writing session, derive an AES key on the client from its durable backup secret and a fresh public session identifier. Allocate unique nonces within the session and start a fresh session after a restart. Store the public information needed to derive the historical key and decrypt the object during restore. Keep object IDs stable across sessions for deduplication. Define the KDF, nonce layout, usage limits, and where to store the session identifier. SSH manages its own transport keys. See [session-derived encryption keys](10b-CryptographyObjectsAndPacks.md#73-session-derived-encryption-keys).

## 16. Initial server and client setup

Keep the standard setup short. On the server, configure the storage path and start the systemd service. At client setup, supply the server address, mode, and at least one valid backup-set configuration. The client generates its UUID, SSH key, and backup secrets and offers a recovery export. The administrator approves the client's SSH public key once. For pull, the client also authorizes the server's public SSH key with a restricted command. The client requests repository creation and registration of all valid sets in both modes. Backup secrets stay with the client. Define how the first SSH contact establishes trust and how scheduled backups unlock client key material. See [initialization and registration](10d-CommunicationConfigurationAndOperations.md#14-initialization-registration-and-identity-recovery).

During server installation, generate an Ed25519 SSH key pair for the user running the installation. Use the account name and the server's fully qualified domain name in the key comment `8472backup-<user>@<fqdn>`.

Plan the switch to a post-quantum SSH authentication key once the supported OpenSSH versions can use it on both peers. Track Kyber-based ML-KEM separately for the SSH key exchange.

## 17. Logging

The shared `Log` class reads `P8472BACKUP_ENV` at server startup. Set it to `dev` to write `INFO` to stdout and `WARNING` and `ERROR` to stderr with readable level names. In `intg` and `prod`, the class writes to stderr with syslog severity prefixes. The default is `prod`. Configure the systemd unit to collect stderr in the journal and interpret the prefixes.

Backup admission, commit, and failures include the client and set IDs and a snapshot ID once assigned. The server logs repository changes, snapshot removal, cleanup, check results, scheduled dispatches, and trusted time outcomes. Apply the project logging rules to the transport when it is connected and reserve its stdout for protocol frames.
