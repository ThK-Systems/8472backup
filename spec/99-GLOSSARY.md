# Glossary

Status: project terminology, 2026-11-13. These definitions explain the model in the [main specification](01-8472backup-SPEC.md). They do not select an on-disk format, algorithm, or serialization. Unresolved design choices remain unresolved.

## Chunks, objects, and packs

| Term | Meaning |
| --- | --- |
| **Chunk** | A logical piece of file content: the complete content of a small non-empty file or a contiguous part of a larger file. Chunk boundaries are determined separately for each file. A chunk never combines content from different files. An empty file needs metadata but no data chunk. |
| **Chunker** | The algorithm that divides file content into chunks. The intended approach is content-defined chunking; the concrete implementation remains open. |
| **Object** | The general independently identified, immutable unit of backup content. Objects can hold file data or metadata, including a snapshot root. Each object has its own compressed/encrypted representation, subject to the compression policy. Objects can be referenced and reused without regard to their physical pack location. |
| **Data object** | An object representing one file-content chunk. Each chunk corresponds to one logical data object. Reusing an existing chunk reuses that object; it does not create a new logical object for each reference. |
| **Metadata object** | An object whose plaintext describes backup structure, such as file paths, attributes, or references to other objects. Metadata is client-encrypted. Its serialization, grouping, and chunking remain open. |
| **Object ID** | The stable logical identifier used to reference and deduplicate an object within its key domain. It is derived from plaintext using a keyed cryptographic construction, with separation between object roles. Its final algorithm and output length remain open. Compression, encryption sessions, and physical relocation do not change it. |
| **Chunk ID** | The object ID of a data object. In this context, chunk ID and object ID refer to the same logical identity. The terminology does not introduce a second hash or identifier layer. Use object ID when the statement also covers metadata objects. |
| **Object envelope** | The representation needed to store and later decrypt and authenticate an object, including the required public cryptographic information. Its exact fields and relationship to pack framing remain open. An envelope does not give an object a new logical identity. |
| **Pack record** | One framed entry for an object's stored representation within a pack. A server-readable header followed by opaque encrypted object bytes is the current proposal; exact framing and integrity checks remain open. |
| **Pack** | A physical storage container grouping one or more object records. A pack can contain objects from different files within the permitted key scope. Packing does not combine their plaintext into one chunk or couple their encryption. Physical pack size and layout remain open. |
| **Location index** | The mapping from an object ID to its pack, offset, and stored length, interpreted within the applicable key domain. It locates stored bytes; it does not describe files or establish snapshot membership. Index reconstruction is distinct from cryptographic verification. |

### Usage example

A file divided into two chunks contributes two data objects, unless those objects already exist and can be reused. Its path, attributes, and ordered chunk references belong to metadata objects. A pack can contain those data and metadata objects together with other objects.

Use **chunk** when discussing file-content boundaries and chunking sizes. Use **object** when discussing identifiers, storage, encryption, or references that also apply to metadata. Use **pack** for the physical grouping.

## Snapshots and server authority

| Term | Meaning |
| --- | --- |
| **Snapshot** | A complete recoverable file selection for one backup set at a particular backup run. Its objects may be shared with other snapshots. Restoration does not require replaying earlier snapshots. A snapshot is not a pack and is not confined to one pack. |
| **Snapshot root** | The client-encrypted metadata object serving as the entry point into a snapshot's file and metadata references. A snapshot's identity and its root object's ID are different concepts; their concrete representation remains open. |
| **Snapshot membership** | The complete set of object IDs required by a snapshot, including its root, metadata objects, and data objects. The server validates existence and scope before accepting and preserving this membership. It cannot validate the encrypted description's truth without the client keys. |
| **Server snapshot record** | The authoritative server-readable record of a snapshot's scope, identity, root, accepted membership, server commit time, and protection/retention state. It is distinct from the client-encrypted metadata objects. Its persistent format remains open. |
| **Snapshot catalogue** | The server-owned collection of authoritative snapshot records. It governs which snapshots exist and which dependencies must be preserved. Rebuilding a location index does not reconstruct this authority. |
| **Key domain** | The scope in which objects use compatible identifier and encryption key material and may be deduplicated. There is no cross-host deduplication; independently keyed backup sets have separate domains. |

## Terminology boundaries

- **Archive** is used for the broader backup model or archive-processing code. It does not name an additional persistent container between snapshot and pack.
- Use **pack** consistently for the physical container; **package** is reserved for software or distribution packages.
- Use **metadata object** for client-encrypted backup descriptions and **server snapshot record** for server-readable authority. The unqualified word metadata can refer to either and should be qualified when the distinction matters.
