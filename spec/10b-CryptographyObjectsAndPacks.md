# 8472backup — Cryptography, Objects, and Packs

Specification version: 0.62 — Align retention configuration with approved rules, 2026-11-24

requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

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

Cryptographic primitives MUST come from established implementations; do not invent or implement cryptographic primitives ourselves. Use AES-256 through an established AEAD implementation rather than assembling encryption and authentication primitives ourselves. GCM versus OCB remains OPEN for later review, including the packaged Python libraries, platform availability, license compatibility, usage limits, and performance.

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

Deduplication identifiers MUST remain stable within their configured host/set key domain across writing sessions. During development, use HMAC-SHA-256 over plaintext with a secret identifier key, through Python's standard-library `hmac` and `hashlib` modules. The ID width is fixed at 32 binary bytes, displayed as 64 hexadecimal characters when needed. The object-ID algorithm is not configurable on either client or server. The development choice may be revisited before release through an explicit code change, with keyed BLAKE3 as a possible candidate. Migration design and implementation are DEFERRED; no migration machinery is requested now. Changing code must not silently reinterpret existing repository identities. Key derivation, role separation, and exact authenticated encoding remain subject to explicit design review.

This development choice adds no runtime dependency. See the [Python HMAC documentation](https://docs.python.org/3.12/library/hmac.html). Other algorithms are not alternative configuration values for object IDs; a pre-release code change is a separate decision.

Session-specific ciphertext MUST NOT define the logical deduplication identity. Existing objects with the same logical ID are reused rather than replaced by a new encryption of the same plaintext.

The server cannot verify a keyed plaintext ID against plaintext without the key. A malicious client can therefore submit bad new content; server authorization and immutability MUST still prevent it from replacing objects required by earlier retained snapshots.

Host/set isolation follows chapter 4. There is no cross-host deduplication, and independently keyed sets have independent identifier namespaces. Define identifier domain separation for object roles.

The Python implementation defines `ObjectId` as a type alias for `bytes` and `OBJECT_ID_SIZE = 32` in `archive/objects.py`. Use this common alias for object references, including snapshot root IDs and catalogue entries. Header encoding and implemented ID-length validation use the same constant. The alias names the role of the bytes; it does not enforce their length by itself.

### 7.5 Passphrases and recovery secrets

Argon2id MUST be used to derive the protection key from the client's master passphrase for stored recovery/key material. It is not the per-chunk encryption algorithm and MUST NOT be run for every data chunk.

Keep durable backup secrets independent of the user's passphrase where practical, so a passphrase change can rewrap key material without re-encrypting all backup contents. Exact wrapping and recovery procedures remain OPEN.

Parameters, salt generation, resource bounds, migration, and unattended unlocking must be specified and validated. A passphrase change is not equivalent to revoking already disclosed backup keys.

### 7.6 Quantum-security review

Evaluate quantum resistance for each cryptographic role, using established primitives available under the Debian Trixie dependency policy. Distinguish symmetric encryption, keyed identifiers, and derivation from high-entropy secrets from asymmetric key exchange and signatures. Assess key-search, forgery, and collision requirements separately; an algorithm name or digest length alone is not a complete security claim.

HKDF-SHA256 is a candidate for deriving session keys from high-entropy secret material; the final KDF/hash selection remains OPEN. Argon2id remains mandatory for the master passphrase. Evaluate the system OpenSSH's hybrid post-quantum key exchange for both push and pull; supported algorithms, enforcement/fallback policy, and authentication-signature coverage remain OPEN.
## 8. Object processing, deduplication, and physical storage

A chunk is a logical piece of file content. Each file-content chunk corresponds to one data object; metadata and snapshot roots are objects as well. An object is the general independently identified, immutable unit referenced by snapshots and stored in packs. For a data object, chunk ID and object ID denote the same logical identifier, not two separate identifiers. Compression and encryption produce its stored representation without changing that identity. A pack groups object records physically. Use `chunk` when discussing file-content boundaries and `object` when discussing storage and references that also apply to metadata.

### 8.1 Processing

The intended pipeline is:

```text
source bytes -> content-defined chunks -> keyed plaintext identifiers
            -> query existing identifiers -> compress missing chunks
            -> authenticated encryption -> transfer and durable storage
```

The server can identify existing objects without knowing plaintext. Stable IDs do not require deterministic encryption.

Type 1 supports LZ4 and Zstandard via the native Debian Trixie packages `python3-lz4` and `python3-zstandard`. The default algorithm, selectable levels, and handling of incompressible chunks remain OPEN.

### 8.2 Small files and chunking

- Apply content-defined chunking to each source file separately, following Borg 2's file-content model. A data chunk contains the whole content of one small file or a contiguous part of one larger file; it never combines content from different source files.
- Empty files require metadata but no data chunk.
- A non-empty file smaller than the chunking threshold can be one chunk, without padding.
- The final chunk may be smaller than the nominal minimum.
- File paths and attributes are stored in encrypted file metadata together with the ordered data-chunk references. Identical chunks may be reused across files and snapshots within the permitted key scope.
- Objects from different source files may share a physical pack while remaining separate logical objects.

Adopt initial data-chunk defaults of 512 KiB minimum, approximately 2 MiB target, and 8 MiB maximum, with the small-file and final-chunk exceptions above. In a later increment, these sizes are to become configurable on the server and communicated to the source client before chunking. Configuration scope, validation limits, and parameter exchange remain OPEN. Changes affect future chunking and may reduce deduplication across the transition; they do not rewrite earlier snapshots.

Chunker-specific parameters, metadata chunk sizes, and container/pack layout remain OPEN. Delta chains between successive chunk versions are not required for version 1.0.

Use established chunking implementations through Python modules. Chunker selection, parameters, and any optimizations must be evaluated rather than inferred from Borg compatibility. Performance targets require measurements.

### 8.3 Physical packs and location indexes

Packs of independently encrypted objects are the adopted physical-storage direction for version 1.0. Follow Borg 2's separation between file-content chunks and their physical grouping without assuming Borg format compatibility or importing its exact pack format.

The intended separation is:

- logical layer: independently identified, compressed, authenticated, encrypted objects;
- physical layer: one or more such objects concatenated into a storage container;
- location index: object ID to container, offset, and length.

`LocationIndex.put(object_id, location)` takes a 32-byte key and an immutable `ObjectLocation` value. The value contains the three pack location fields below. The repository and key domain determine the scope of the index.

| Field | Python type | Intended encoded width | Meaning |
| --- | --- | --- | --- |
| `object_id` | `ObjectId` | 32 bytes | Logical object ID; the index key within its key domain |
| `pack_id` | `int` | 8 bytes, unsigned | Numeric pack identity within the client repository |
| `offset` | `int` | 8 bytes, unsigned | Absolute byte offset of the object's common header within the pack file |
| `length` | `int` | 8 bytes, unsigned | Complete stored record length, including the 44-byte object header |

These fields total 56 raw bytes, with a 24-byte location value after the 32-byte key. This is not the memory footprint of a Python instance. A maximum-sized object needs a record length of 16 GiB plus 44 bytes; 32-bit offsets or lengths are insufficient. The agreed pack path is `data/packs/<key-domain-id>/<pack-id>.pack`, with decimal pack IDs. The development implementation assigns new IDs under the repository lock starting one above the highest existing pack filename in that key domain, or at 1 for an empty directory. Pack integrity checks still require review.

The development location index checkpoint contains an 18-byte header, the packed 56-byte records, and a 32-byte SHA-256 digest over the header and records. The header layout is `<8sHQ`: the eight-byte marker `8472IDX\x00`, version 1 as an unsigned 16-bit integer, and the record count as an unsigned 64-bit integer. Header integers and the three unsigned 64-bit location fields are little endian. The loader checks the header, count, exact file size, checksum, and uniqueness of the IDs. It rebuilds the in-memory bucket array from the records. The digest detects accidental corruption.

`LocationIndex.save_to(path, lock)` writes a temporary file in the destination directory, syncs it, atomically replaces the checkpoint, and syncs the directory. `LocationIndex.load_from(path, lock)` reports a missing checkpoint as an error. Both require a held repository lock covering the path. Callers choose a separate checkpoint path for each key domain. The index tracks pending changes internally. A new index requires an initial save, including when empty. Inserting a new ID marks it as changed; duplicate IDs keep the first location. After validating the held lock and path, `save_to` returns `False` without writing when there are no pending changes. It returns `True` after a successful durable save and then clears the pending-change flag. A failed save raises an exception and preserves pending changes for retry. A loaded index starts with no pending changes. Repository orchestration and index reconstruction remain separate work.

Packing MUST NOT merge unrelated files into one logical plaintext chunk or couple their encryption. It MUST NOT cross independent key scopes without an explicit later decision.

For our architecture, server-side assembly of already encrypted objects is a candidate: the server can manage physical layout without receiving plaintext or decryption keys. Client-side packing is not required merely because Borg uses it.

Pack requirements:

- Prefer self-describing bounded object framing that allows location-index reconstruction from stored bytes.
- Distinguish a reconstructed location index from cryptographically verified content and authoritative snapshot membership.
- Consider a hash of stored container bytes for server-side corruption detection. Such a hash does not authenticate plaintext or protect against a hostile server rewriting both data and hash.
- Allow individual object reads without downloading an entire large container where the storage supports it.
- Compaction MUST preserve the encrypted bytes of live objects; the keyless server cannot decrypt or recompress their plaintext.
- Persist replacement containers and crash-safe index changes before removing old containers.
- Missing or corrupt indexes MUST NOT authorize speculative deletion.

The first snapshot increment MUST use real chunks and physical packs. An individual-file-per-object placeholder is not the agreed starting point. OPEN: buffering, naming, index engine, recovery format, and concurrency.

The agreed common header below identifies an object and declares its stored body length. Additional per-object boundary markers are excluded. Within a sequence of object records, the next record starts at the current record offset plus 44 bytes plus its declared body length. A marker byte sequence could also occur in a body and would not by itself prove a valid boundary. A sequential reader must validate the header and available body bytes; a detected invalid or truncated record stops that walk rather than triggering heuristic searches inside the body. A corrupted length that still appears plausible may require integrity verification to detect. Integrity checks, recovery using independently known locations, interrupted-write handling, and the complete pack layout remain OPEN. A structural index reconstruction does not authenticate plaintext, restore authoritative snapshot membership, or make incomplete records committed.

The server-configurable pack target defaults to 50 MB (50,000,000 bytes). After appending each complete object record, close the pack if its body length has reached or exceeded the target. This includes the record that crosses the threshold, even when that record alone exceeds the target.

#### Pack header (agreed development format)

The approved header uses a 2-byte unsigned pack-format version, a 4-byte unsigned `object_count`, and an 8-byte unsigned `body_length`, totaling 14 bytes without padding, with little-endian integers (`<HIQ`). Version is initially `1`, independent of the object-format version. `object_count` counts complete records; `body_length` counts bytes in all records, including their 44-byte object headers. These fields use different units and need different capacities.

The immutable `Pack` class in `src/p8472backup/storage/file/packs.py` implements `to_header()` and `from_header()`, returning bytes and a `Pack` instance respectively. The header describes a non-empty pack: the count is between 1 and 2^32 - 1, and body length must fit an unsigned 64-bit integer. Its body must be large enough for all object headers and no larger than the combined maximum possible record sizes. The complete file length is 14 plus `body_length`.

`Pack.write_to()` writes one pack to a caller-supplied path. It copies each declared object body in bounded blocks and leaves later records in the caller's iterator once the pack reaches its target. The writer finalizes and syncs the header and records in a temporary file, then publishes the file without replacing an existing destination and syncs the directory. A truncated body fails before publication. If the caller supplies `on_stored`, the writer collects object IDs, offsets, and record lengths while writing. It buffers up to 256 KiB of these fixed-size entries in memory before spilling to a temporary file. The writer reports the locations through `on_stored` after the directory sync succeeds.

`Pack.load_from()` reads the pack header and validates the file size. `Pack.read_object()` also validates the requested record range, then copies the body to the caller's output stream and returns the common object header. The reader and writer use the common framing for every object type. Storage integrity checks remain open.

#### Common object header (agreed development format)

All integer fields are unsigned and little endian, with no alignment padding. The common object-format version remains `1` until the 1.0 release. The plaintext layouts for types 1, 2, and 3 are specified below.

| Offset | Field | Encoding | Meaning |
| --- | --- | --- | --- |
| 0 | `type` | 2-byte unsigned integer (`uint16`) | `1`: data object containing one file chunk; `2`: metadata object describing files, attributes, and chunk references; `3`: snapshot root. Other values are unsupported. |
| 2 | `version` | 2-byte unsigned integer (`uint16`) | Common object-format version, currently `1`. Other versions are unsupported. |
| 4 | `id` | 32 binary bytes | HMAC-SHA-256 plaintext identifier during development. The algorithm is not user-configurable; there is no ID-length field. |
| 36 | `length` | 8-byte unsigned integer (`uint64`) | Number of stored body bytes following the header, including any subheader and the entire cryptographic envelope. Maximum: 16 GiB (17,179,869,184 bytes). |

The common header is exactly 44 bytes (`<HH32sQ` in Python's `struct` notation). The header codec rejects unsupported types and versions, IDs of an incorrect length, negative body lengths, and lengths above 16 GiB. Decoding requires exactly 44 input bytes. At this layer, a declared body length of zero is representable; validity of the actual body and cryptographic envelope requires later validation. The codec does not read or allocate the body and does not authenticate it or recompute its ID. A future pack reader must also check that the declared bytes are available. This framing ceiling does not change the chunk-size defaults in chapter 8.2.

The current proposal retains one stored-body length. A separate encrypted per-object metadata slot is not required by the agreed model and is not selected here. In the newer [Borg 2 pack format](https://borgbackup.readthedocs.io/en/master/internals/packs.html), the metadata slot contains object bookkeeping such as type, compression settings, and original size. Separately encrypting it permits metadata-only authenticated reads without reading/decrypting the data slot. This is distinct from file-metadata objects containing paths, attributes, and chunk references, which already have their own object identities in our model. Reconsider separate slots only for a concrete requirement. Borg's pair of 4-byte length fields cannot represent our permitted 16 GiB body sizes; a future split must preserve suitable widths and explicit bounds.

Object IDs identify content across object roles within the applicable key domain; identical content of the same role intentionally reuses an ID. Preserve role separation and stable plaintext-based identity. Do not introduce a separate chunk identifier or a ciphertext-based replacement ID.

Keep type-specific subheaders conditional on concrete requirements. Corruption detection and safe recovery remain open. Authenticate the cleartext header through the object's AEAD context as required by chapter 7; the plaintext-derived object ID is not itself authentication of that header. Approval of the common header does not select the object body, encryption envelope, or complete pack format.

#### Type 1 data object

The client encrypts and authenticates the 13-byte subheader, chunk data, and padding together. The server uses the common object header to locate and store the encrypted body.

| Plaintext offset | Field | Encoding |
| --- | --- | --- |
| 0 | `compression` | Unsigned byte. 0: uncompressed, 1: LZ4 frame, 2: Zstandard frame. |
| 1 | `chunk_size` | Little-endian unsigned 64-bit length of the original chunk. |
| 9 | `padding_length` | Little-endian unsigned 32-bit length of the trailing padding. |
| 13 | Chunk data | Bytes up to the trailing padding. For uncompressed data the length must equal `chunk_size`. |
| After chunk data | Padding | Exactly `padding_length` bytes. |

The client rejects unknown compression identifiers and checks that decoding produces exactly `chunk_size` bytes. A compressed chunk contains one complete frame. A Zstandard frame includes its content size for a nonempty chunk and omits it for an empty chunk to work around a decoder limitation in Debian Trixie. The common header's size limit applies to the entire stored body, including cryptographic overhead.

The provisional code constants for encryption are 1 (AES-256-GCM) and 2 (AES-256-OCB). The AEAD choice and envelope encoding remain open.

#### Type 2 metadata object

Each type 2 object describes one filesystem entry. The client encrypts and authenticates the 12-byte subheader, metadata block, chunk references, and padding together.

| Plaintext offset | Field | Encoding |
| --- | --- | --- |
| 0 | `metadata_length` | Little-endian unsigned 64-bit byte length of the metadata block. |
| 8 | `padding_length` | Little-endian unsigned 32-bit length of the trailing padding. |
| 12 | Metadata block | Exactly `metadata_length` bytes supplied by the client. The block carries the filesystem entry type, path, and attributes. Its internal encoding remains open. |
| 12 + `metadata_length` | Chunk references | Ordered 32-byte type 1 object IDs up to the trailing padding, including repeated IDs. |
| After chunk references | Padding | Exactly `padding_length` bytes. |

The client interprets the entry type and validates which types may carry chunk references. The reference region may be empty and must contain a whole number of IDs.

#### Type 3 snapshot root

The client encrypts and authenticates the 12-byte subheader, snapshot description, metadata references, and padding together.

| Plaintext offset | Field | Encoding |
| --- | --- | --- |
| 0 | `description_length` | Little-endian unsigned 64-bit byte length of the snapshot description. |
| 8 | `padding_length` | Little-endian unsigned 32-bit length of the trailing padding. |
| 12 | Snapshot description | Exactly `description_length` bytes supplied by the client, including the backup configuration used for the run. The internal encoding remains open. |
| 12 + `description_length` | Metadata references | 32-byte type 2 object IDs up to the trailing padding. |
| After metadata references | Padding | Exactly `padding_length` bytes. |

The current development codec allows a flat sequence of metadata references. The agreed directory tree uses a reference to the root directory as described in chapter 9. The exact client-side encoding remains OPEN. The server's snapshot JSON and binary object catalogue record the authoritative commit time, protection deadline, and membership independently of this object.

For all three content types, reject a padding length that exceeds the available plaintext. Padding is removed before decoding references or decompressing chunk data. The padding strategy is open; padding and its length must not affect the logical object ID. The common object body limit includes padding and cryptographic overhead.
