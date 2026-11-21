# Glossary

Status: project terminology, 2026-11-13. These definitions explain the model in the [main specification](10-8472backup-Spec.md). They do not select an on-disk format, algorithm, or serialization. Unresolved design choices remain unresolved.

## Chunks, objects, and packs

| Term | Meaning |
| --- | --- |
| **Chunk** | A logical piece of file content: the complete content of a small non-empty file or a contiguous part of a larger file. Chunk boundaries are determined separately for each file. A chunk never combines content from different files. An empty file needs metadata but no data chunk. |
| **Chunker** | The algorithm that divides file content into chunks. The intended approach is content-defined chunking; the concrete implementation remains open. |
| **Object** | The general independently identified, immutable unit of backup content. Objects can hold file data or metadata, including a snapshot root. Each object has its own compressed/encrypted representation, subject to the compression policy. Objects can be referenced and reused without regard to their physical pack location. |
| **Data object** | An object representing one file-content chunk. Each chunk corresponds to one logical data object. Reusing an existing chunk reuses that object; it does not create a new logical object for each reference. |
| **Metadata object** | An object whose plaintext describes backup structure, such as file paths, attributes, or references to other objects. Metadata is client-encrypted. Its serialization, grouping, and chunking remain open. |
| **Per-object metadata** | Bookkeeping about one object, such as its compression settings and original size. It is distinct from metadata objects describing files and snapshot structure. Borg 2 can store this bookkeeping in a separately encrypted slot; that split is not selected for 8472backup's current proposal. |
| **Object ID** | The stable logical identifier used to reference and deduplicate an object within its key domain. During development, it is derived from plaintext using HMAC-SHA-256, with separation between object roles. The width is 32 raw binary bytes, displayed as 64 hexadecimal characters when needed. The algorithm is not user-configurable; an explicit pre-release code change, potentially to keyed BLAKE3, remains possible. Migration is deferred. Compression, encryption sessions, and physical relocation do not change the identity. |
| **Chunk ID** | The object ID of a data object. In this context, chunk ID and object ID refer to the same logical identity. The terminology does not introduce a second hash or identifier layer. Use object ID when the statement also covers metadata objects. |
| **Object envelope** | The representation needed to store and later decrypt and authenticate an object, including the required public cryptographic information. Its exact fields and relationship to pack framing remain open. An envelope does not give an object a new logical identity. |
| **Object header** | The agreed 44-byte common header: type (2 bytes), format version (2 bytes), object ID (32 bytes), and stored body length (8 bytes). Integer fields are unsigned little endian. Version starts at 1; types are 1 for data, 2 for metadata, and 3 for a snapshot root. The ID has no length field. Body encoding, optional type-specific subheaders, and authentication binding remain under discussion. The header codec alone does not validate or authenticate the body. |
| **Pack record** | One entry consisting of the 44-byte object header and its declared stored body. There is no additional boundary marker. Sequential traversal uses the header size and body length; integrity checks and recovery beyond damaged records remain open. |
| **Pack** | A physical storage container grouping one or more object records. A pack can contain objects from different files within the permitted key scope. Packing does not combine their plaintext into one chunk or couple their encryption. The configurable size target applies to the stored object records. |
| **Pack header** | The agreed 14-byte prefix: format version (2 bytes), object count (4 bytes), and body length (8 bytes), all unsigned little endian. Body length includes every object header and stored body. The current `Pack` class represents and validates these fields; it does not yet read or write pack records. |
| **Compaction** | Physical space reclamation by copying still-live object records unchanged from partly unused packs into replacements, durably updating their locations, and removing old packs when safe. It preserves object IDs and snapshot contents. |
| **Location index** | The repository-level mapping for one backup client from an object ID to its pack, offset, and stored length, interpreted within the applicable key domain. Access is serialized under that client's repository lock. It locates stored bytes; it does not describe files or establish snapshot membership. Index reconstruction is distinct from cryptographic verification. |
| **Object location** | The immutable `ObjectLocation` value holds the numeric pack ID, absolute offset of the object header, and complete stored record length including that header. Each integer occupies 8 unsigned bytes. Together with the separate 32-byte object ID key, an index entry occupies 56 raw bytes. Python instance memory and the persistent index representation are separate concerns. Repository and key-domain context is external. |

### Usage example

A file divided into two chunks contributes two data objects, unless those objects already exist and can be reused. Its path, attributes, and ordered chunk references belong to metadata objects. A pack can contain those data and metadata objects together with other objects.

Use **chunk** when discussing file-content boundaries and chunking sizes. Use **object** when discussing identifiers, storage, encryption, or references that also apply to metadata. Use **pack** for the physical grouping.

## Snapshots and server authority

| Term | Meaning |
| --- | --- |
| **Snapshot** | A complete recoverable file selection for one backup set at a particular backup run. Its objects may be shared with other snapshots. Restoration does not require replaying earlier snapshots. A snapshot is not a pack and is not confined to one pack. |
| **Snapshot root** | The client-encrypted type-3 metadata object serving as the entry point into a snapshot's file and metadata references. It has an object ID and is stored as a record in a pack. Its references can lead to objects in other packs. The server catalogue records the root ID separately from its physical location. A snapshot's identity and its root object's ID are different concepts; their concrete representation remains open. |
| **Snapshot metadata** | Information about the snapshot itself, including the used configuration and potentially descriptive text, source-side timing, and summary statistics. It belongs to the client-encrypted snapshot description, with its schema and placement still open. The server separately maintains authoritative commit and protection information. |
| **Snapshot membership** | The complete set of object IDs required by a snapshot, including its root, metadata objects, and data objects. The server validates existence and scope before accepting and preserving this membership. It cannot validate the encrypted description's truth without the client keys. |
| **Snapshot object catalogue** | The per-snapshot set of unique object IDs recording its complete membership, including reused objects from earlier backups. Committed membership is immutable and has no pack locations. The separate repository location index resolves IDs to stored byte ranges. This is distinct from the server snapshot catalogue, which lists snapshots. |
| **Server snapshot record** | The authoritative server-readable record of a snapshot's scope, identity, root, accepted membership, server commit time, and protection state. The local format stores one versioned record per committed snapshot. |
| **Snapshot catalogue** | The server-owned collection of authoritative snapshot records. It governs which snapshots exist and which dependencies must be preserved. Rebuilding a location index does not reconstruct this authority. |
| **Backup run** | The server-managed operation creating a snapshot, with coarse states `in_progress`, `failed`, and `committed`. Successful durable publication produces a visible snapshot. Run state and snapshot contents are distinct. |
| **Staging** | Informal name for the phase in which a run creates packs and catalogues before publication. It does not introduce a separate domain structure, class, catalogue, or required directory. Provisional work belongs to an admitted run; cleanup must preserve data needed by retained snapshots or active operations. |
| **Backup-run lock** | The server-enforced exclusive lock for one stable `(client_id, backup_set_id)` pair, shared by push and pull. It is owned by an admitted run and prevents overlapping backups of the same set. Its scope is distinct from synchronization of shared repository storage and protection of active object dependencies. |
| **Repository lock** | The server-owned exclusive lock for access to one backup client's repository. All of that client's backup sets share it. It covers the lifetime of a backup, mount, restore, or maintenance operation. |
| **Key domain** | The scope in which objects use compatible identifier and encryption key material and may be deduplicated. There is no cross-host deduplication; independently keyed backup sets have separate domains. |

## Terminology boundaries

- **Archive** is used for the broader backup model or archive-processing code. It does not name an additional persistent container between snapshot and pack.
- Use **pack** consistently for the physical container; **package** is reserved for software or distribution packages.
- Use **metadata object** for client-encrypted backup descriptions and **server snapshot record** for server-readable authority. The unqualified word metadata can refer to either and should be qualified when the distinction matters.
