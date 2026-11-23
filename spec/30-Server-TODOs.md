# Server TODOs

This backlog tracks implementation gaps and open design decisions for the server façade and the client features it depends on. The [project specification](10-8472backup-Spec.md) defines the requirements.

## 1. Façade and repository lookup

Define the inputs, results, and errors for `init_repository`, `backup`, `list`, `restore`, `mount`, and `set_mode`. Resolve the repository from the authenticated `client_id` and authorize access to the requested backup set and its key domain. `RepositoryService` currently initializes and opens repositories by path. Define how approved SSH credentials map to client UUIDs and how those UUIDs map to server-side paths.

## 2. Push, pull, and scheduling

Route incoming push jobs and scheduled pull jobs through the same backup operation. The mode is already stored per client and defaults to pull. Implement pull scheduling and define how to record the SSH endpoint, retry failed jobs, and apply mode changes to queued and active jobs.

Enforce the configurable server-wide limit for concurrent backup sets, default 3. Queue pull jobs when full and return a wait response to push clients. Define queue ordering and cancellation.

## 3. Server process and wire protocol

Implement the systemd service and the restricted SSH entry points. Push goes through the SSH helper and the server's Unix socket. Pull starts an SSH connection from the server to the restricted client command. Define authentication and authorization, message framing, size limits, timeouts, and the object-ID exchange for sending only missing object bodies. Define how asynchronous connections interact with blocking file I/O and repository locks. See the [communication spec](10d-CommunicationConfigurationAndOperations.md#13-communication-execution-and-protocol).

## 4. Backup completion and snapshot commit

Use the complete final snapshot root object as the upload completion signal. `SnapshotService.create_snapshot()` currently expects the root first and requires its ID up front. Include both new and reused object IDs in the snapshot catalogue. Acknowledge the backup after the packs and index are durable and the snapshot record has been durably published.

## 5. Snapshot listing

Implement `list` for published snapshots that the authenticated client is authorized to see, with an optional backup-set filter. Read the header information needed for listing without loading every snapshot's object catalogue.

## 6. Shared object read path

Serve objects for restore and mount. Check requested IDs against the selected snapshot's catalogue, find them through the location index, and verify that the record at the indexed pack offset matches the requested object ID and expected length. The current `get_snapshot()` operation returns the snapshot record.

## 7. Directory tree and snapshot description

Replace the current flat metadata-ID list in the client-encrypted snapshot root content with a reference to the root directory. Directory metadata links direct child names to their object IDs. Capture the backup configuration used for the run in the encrypted snapshot description. Define the directory encoding, filename representation, and which filesystem metadata a restore preserves.

## 8. Restore

Support restoring a full snapshot or a single file directly. The client resolves the requested path through the decrypted directory tree and reads the file's referenced chunks. Define what happens when a destination file already exists or an interrupted restore leaves a partially written file.

## 9. Mount

Provide read-only access on the client, with metadata and data loaded as needed. Define how to locate the chunks covering a file offset, bound caches, and release the repository lock on unmount or disconnect.

## 10. Client data path and object encryption

Connect source traversal and content-defined chunking to the existing object and compression code. Implement authenticated object encryption and client-side decryption. Settle AES-GCM versus AES-OCB, the fields to authenticate, and the key hierarchy. Argon2id is required for deriving the protection key from the client's master passphrase. Key storage and unattended unlocking remain open. See the [cryptography spec](10b-CryptographyObjectsAndPacks.md).

## 11. Locks and shared memory budget

Keep the existing exclusive repository lock and enforce one active backup run per `(client_id, backup_set_id)`. Define admission and waiting behavior when a lock or server-wide slot is unavailable. The planned default is 10 simultaneous repository locks. The 64–128 MiB target applies across active server operations, including index and catalogue memory and any server-side caching for mount readers. See the [configuration roadmap](70-ConfigurationRoadmap.md).

## 12. Interrupted runs and recovery

On the next backup, clean up temporary files from an interrupted run under the repository lock. Finalized packs can contain orphaned objects. Determine reclaimability from published snapshots and active operations. Implement `LocationIndex.rebuild_from_packs()`, which is currently a stub. A durably committed snapshot remains valid if its acknowledgement is lost.

## 13. Prune and compact

Run prune after a successful backup and compact only when the reclamation threshold is reached. Define the retention rules and threshold metric, denominator, and default. Enforce minimum snapshot protection and preserve objects referenced by retained snapshots or active operations. Make replacement packs and the updated index durable before removing the old packs. See the [snapshot and retention spec](10c-SnapshotsCommitAndRetention.md).

## 14. End-to-end verification

Extend the existing local write-path coverage with two encrypted backups that reuse objects and a direct single-file restore. Test interruption immediately before the final root byte and after durable commit but before acknowledgement. Verify the outcome after reopening the repository.

## 15. Object encryption session keys

For each writing session, derive an AES key on the client from its durable backup secret and a fresh public session identifier. Allocate unique nonces within the session and start a fresh session after a restart. Store the public information needed to derive the historical key and decrypt the object during restore. Keep object IDs stable across sessions for deduplication. Define the KDF, nonce layout, usage limits, and where to store the session identifier. SSH manages its own transport keys. See [session-derived encryption keys](10b-CryptographyObjectsAndPacks.md#73-session-derived-encryption-keys).

## 16. Initial server and client setup

Keep the standard setup short. On the server, configure the storage path and start the systemd service. At client setup, supply the server address and first backup set. The client generates its UUID, SSH key, and backup secrets and offers a recovery export. The administrator approves the client's SSH public key once. For pull, the client also authorizes the server's public SSH key with a restricted command. After authenticated registration, use the same create-once repository operation in both modes. Backup secrets stay with the client. Define how the first SSH contact establishes trust and how scheduled backups unlock client key material. See [initialization and registration](10d-CommunicationConfigurationAndOperations.md#14-initialization-registration-and-identity-recovery).
