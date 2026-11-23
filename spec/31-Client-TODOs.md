# Client TODOs

This backlog tracks implementation gaps and open design decisions for the client. The [project specification](10-8472backup-Spec.md) defines the requirements. See the [server backlog](30-Server-TODOs.md) for the corresponding server work.

## 1. Initial setup and recovery

Prompt for the server address and client mode. Generate the client UUID, SSH credentials, and backup key material and establish SSH trust. Validate the per-set TOML files, report invalid sets individually, and send the required operational fields for all valid sets in one client-initiated repository creation request. Require at least one valid set. Support exporting recovery material and importing it on another machine. See the [setup spec](10d-CommunicationConfigurationAndOperations.md#14-initialization-registration-and-identity-recovery).

## 2. Backup-set configuration

Define the per-set TOML schema for source paths, exclusions, and requested retention. Capture and validate one configuration version per run. Send the required operational fields to the server before uploading objects. Include the complete configuration in the encrypted snapshot description. See the [configuration spec](10d-CommunicationConfigurationAndOperations.md#15-backup-descriptions-and-configuration).

## 3. CLI and SSH

Provide commands for setup, backup, listing, restore, mounting, and changing the backup mode. In push mode, the client starts SSH to the server. In pull mode, the server invokes the restricted client command over SSH. Use the same client backup workflow in both modes. Provide clear exit codes and useful diagnostics for automation.

When the server returns a wait response because the backup-set concurrency limit is reached, report the wait and retry after the configurable client interval, default 60 seconds. Retry after subsequent wait responses as well.

## 4. Source filesystem traversal

Walk source paths, apply exclusions, and collect filesystem metadata. Define how to handle symlinks, hard links, read failures, and files that change while being read. Decide which conditions make a run fail or count as partial.

## 5. Content-defined chunking

Select a chunker available in Debian Trixie and apply it to each source file. Use the agreed initial sizes of 512 KiB minimum, approximately 2 MiB target, and 8 MiB maximum. Handle empty files, small files, and final chunks according to the [chunking spec](10b-CryptographyObjectsAndPacks.md#82-small-files-and-chunking).

## 6. Object IDs and deduplication

Derive 32-byte object IDs from plaintext using HMAC-SHA-256. Batch object-ID existence queries, send only missing object bodies, and report reused IDs as part of the snapshot membership.

## 7. Compression

Use the existing LZ4 and Zstandard support. Decide the default algorithm, compression levels, and when to store a chunk uncompressed.

## 8. Encryption and key management

Use Argon2id to derive a protection key from the client's master passphrase for stored key material. Define the AES-256 AEAD construction, session-key derivation, nonce handling, and the encrypted object envelope needed for restore. Define how scheduled backups unlock the local key material. See the [cryptography spec](10b-CryptographyObjectsAndPacks.md#7-cryptography-and-recovery-key-material).

## 9. Directory tree and metadata format

File entries reference chunks in order. Directory objects map child names to their object IDs. Change `SnapshotRootContent` from a flat list of metadata IDs to a reference to the root directory. Define the metadata schema, how to represent Linux filenames, and which attributes can be restored.

## 10. Backup completion

Transfer objects with bounded buffers and send the complete snapshot root as the final object. Report success once the server acknowledges the durable commit. Define how the client can query the run's outcome if it loses the acknowledgement.

## 11. Snapshot selection and direct restore

List published snapshots. Use `restore` for a complete snapshot and `recover` for a selected file or directory tree. Resolve selected paths in the decrypted directory tree, then fetch and verify the required objects. Define overwrite behavior, safe handling of paths and symlinks, and cleanup after an interrupted restore. See the [restore spec](10d-CommunicationConfigurationAndOperations.md#16-restore-and-read-only-mounting).

## 12. Read-only mount

Add Linux FUSE support and load metadata on demand. Include chunk lengths or offsets in file metadata so the client can find the chunks covering a read. Define cache limits and behavior on disconnect.

## 13. End-to-end tests

Back up real files twice, change part of the source data between runs, and verify that unchanged objects are reused. Restore a single file on a fresh client using the recovery export. Exercise the same backup workflow over push and pull.

## 14. Size obfuscation

Choose an optional padding strategy for data, metadata, and snapshot root content. Evaluate the protection against size-based fingerprinting and the storage overhead. Generate padding before encryption, keep object IDs independent of padding, and account for the object body size limit.

## 15. Logging

Report backup, restore, and mount failures with enough context for diagnosis. Include the backup set and run or snapshot ID where available. Report push waits and retries. Send diagnostics to stderr and reserve stdout for protocol messages during SSH sessions. Keep credentials and file contents out of logs.
