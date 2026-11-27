# 8472backup — Future Work and References

Specification version: 0.71 — Prefer portable Python over peak performance, 2026-11-27

requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

## 21. Additional native backends — MUCH, MUCH LATER

Roadmap label: `Version 1+++ — much, much later` (meaning "much, much later"). This is deliberately not a concrete release number or deadline. Additional backend implementations are not version 1.0 acceptance criteria; the backend concept itself is.

In a distant future version, the server is intended to support native external storage backends, including:

- SFTP servers;
- S3 and compatible object stores;
- other cloud storage services;
- other external storage targets where practical.

The server remains the control and policy authority. Clients select from server-defined, authorized backends, using defaults when no choice is supplied. They do not define backend configurations or receive direct backend credentials or unrestricted object-write/delete access. Client/server SSH transport remains separate from the server/backend storage connection.

Future backends MUST preserve client-side encryption, immutable retained history, minimum protection, and safe cleanup. Backend-specific consistency, durability, range reads, retries, incomplete uploads, and deletion semantics need explicit evaluation at that time. The version 1.0 interface may evolve when those concrete requirements are known; do not claim that it guarantees future backend or format compatibility.

External deletion by a provider or someone with direct backend administrator access is outside the client-compromise guarantee, just as direct filesystem administration is in the local model.

OPEN for that later phase: authoritative metadata placement, backend-specific pack requirements, backend migration, caching, and recovery when external storage is unavailable. Replication, multi-target redundancy, and tiering are not implied requirements.

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

When Thomas refers to Borg 2, use the current upstream development state and identify the inspected version or revision. The distribution-packaged Borg version is not the conceptual reference. Runtime dependency availability remains a separate concern under chapter 3.

The adopted Borg 2 ideas guide the design as written here. New external development-documentation changes are reference material, not automatic amendments to this specification. Review concepts and code before reuse, and check licensing before incorporating source code.
## 24. First instruction to Codex

Read the complete specification set and inspect the actual repository and its instructions. Begin with archive and repository design as described in chapter 20. Summarize established requirements, work through concrete snapshot and object examples, and identify the few decisions needed for the first local backup/restore increment. Propose a small initial design and dedicated behavioural tests, including durability and encryption boundaries. Do not scaffold the complete product or silently decide the unresolved storage and cryptographic formats. Proceed with implementation in reviewable increments as directed by Thomas.

Apply AES-256 AEAD with session-derived encryption keys, HMAC-SHA-256 object IDs of 32 bytes during development, independent snapshot roots, server-owned membership, late server publication, per-file content-defined chunking with the agreed initial sizes, physical packs, and the version 1.0 `file` backend boundary as the agreed direction. Argon2id is mandatory for the client's master passphrase. GCM versus OCB, identifier-key derivation and role separation, session KDF, chunker choice, compression choice, pack format and parameters, and other storage formats remain subject to the explicitly OPEN decisions and the quantum-security review. The object-ID algorithm is not user-configurable; a pre-release code change remains possible, and migration is deferred. The common header is agreed; object-body and pack formats require further review. Do not implement native remote backends or a dynamic plugin platform in the initial increment.
