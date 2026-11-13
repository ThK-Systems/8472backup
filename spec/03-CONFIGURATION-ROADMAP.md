# Configuration Roadmap

Status: medium-term inventory, 2026-11-13. The [main specification](01-8472backup-SPEC.md) remains authoritative. This document records settings to design incrementally; it does not define a configuration schema or request implementation.

**Planned** means configurability is already agreed. **Candidate** means a possible setting derived from an unresolved design or operations topic; it is not yet a requirement. Names below describe concepts, not final configuration keys. Configuration file locations and syntax remain open.

## Client configuration

| Setting | Status | Scope and current direction |
| --- | --- | --- |
| Backup mode | Planned | Per host/client, uniformly for every set: `pull` by default or `push`. No per-set override. Detailed switching commands and acknowledgement remain open. |
| Backup sets and source selection | Planned | Per set: selected paths and exclusions. Capture and validate one description per run; later edits affect subsequent runs. |
| Requested retention | Planned | Per set: client requests a retention policy; server minimum protection always applies. Calendar/count rules and timezone semantics remain open. |
| Server connection | Candidate | Client connection settings for the server address, SSH identity, and host-key verification. Exact options and their interaction with system SSH configuration remain open. Host-key verification stays mandatory. |
| Key-domain scope | Planned | During host/set setup: one domain per host or independent domains per set. Register the scope with the server. Changing an existing scope needs a reviewed key/history transition. No cross-host deduplication. |
| Unattended unlocking and Argon2id parameters | Candidate | Local client key protection. Secure input, resource parameters, and unattended operation need review. Passphrases and decryption keys must not enter the operational configuration sent to the server. |
| Restore and mount options | Candidate | Local operation options such as destination, overwrite policy, and read-only mount settings. Exact supported options and metadata-fidelity rules remain open. |
| Logging verbosity | Candidate | Local diagnostics suitable for CLI use and automation. Never include secrets or unintended file contents. |

## Server configuration

| Setting | Status | Scope and current direction |
| --- | --- | --- |
| Minimum protection duration | Planned | Per host repository, server-controlled; default seven days from server commit. Changes must preserve the agreed protection guarantees. Rules for applying duration changes to existing snapshots remain open. |
| Chunk sizes | Planned | Server-defined minimum, target, and maximum, communicated to the source before chunking. Initial defaults: 512 KiB, approximately 2 MiB, and 8 MiB. Larger configurations are possible within supported implementation and stored-object limits. Host/set configuration scope and validation remain open. |
| Pull schedule | Planned | Server schedules pull backups for hosts/sets. Push hosts use an external client-side scheduler and are not scheduled for pull. Scheduling syntax, configuration ownership, and overlap handling remain open. |
| Repository and service locations | Candidate | Server repository roots and Unix-socket location, with explicit service-account and access rules. Exact deployment options remain open. |
| Pack sizing and buffering | Candidate | Physical storage settings, such as a target pack size and buffering limits. Defaults and handling of objects larger than a target pack remain open. Pack targets must not silently become chunk-size limits. |
| Admission and resource limits | Candidate | Host/repository quotas and server concurrency/resource bounds. Reaching a limit must reject or delay new work rather than evict protected history. |
| Timeouts and retries | Candidate | Pull connection attempts and operation failure handling. Define bounded retries and commit acknowledgement semantics before choosing options. |
| Maintenance timing | Candidate | Scheduling or thresholds for eligible garbage collection and pack compaction. Maintenance stays automatic and cannot bypass retention, protection, or active-operation liveness. |
| Logging verbosity | Candidate | Service diagnostics suitable for systemd/journald. Report completion, failure, and maintenance outcomes accurately. |

## Settings requiring a shared client/server decision

| Setting | Status | Scope and current direction |
| --- | --- | --- |
| Compression algorithm and level | Candidate | LZ4 and Zstandard are candidates. The source performs compression. Selection ownership, supported levels, defaults, and the policy for incompressible content remain open. Stored objects must remain decodable after settings change. |

Do not turn every implementation constant into an option. Add a setting when its use case, authority, validation, default, and effect on existing backups are understood.

## Current decisions and change rules

- **During development, object IDs use HMAC-SHA-256 with 32-byte binary output. The algorithm is not configurable on either client or server.** An explicit code change before release, potentially to keyed BLAKE3, remains possible. Migration design and implementation are deferred. There is no user-facing hash-algorithm selection or ID-length field.
- The maximum stored object body is **16 GiB**, including any subheader and cryptographic envelope. This is a fixed upper bound, not a request for a size option.
- Snapshot membership, server commit time, and protection authority are server-owned state, not client configuration values.
- Capture the applicable configuration once per run and exchange validated operational configuration before object queries or data transfer. Preserve the used backup description encrypted with the snapshot.
- A configuration change must not mutate committed objects or silently reinterpret historical bytes. Review compatibility and recovery implications before making persistent or cryptographic choices configurable.
- Authentication, client-side encryption, server protection, and input validation are mandatory properties, not optional switches.

## Outside this medium-term list

Version 1.0 uses the server's `file` backend by default. Client selection among future backends, cloud credentials, S3/HTTPS settings, and other native remote storage backends remain deferred to much later, as described in the main specification.
