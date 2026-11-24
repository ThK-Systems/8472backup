# Configuration Roadmap

Status: medium-term inventory, 2026-11-13. The [main specification](10-8472backup-Spec.md) remains authoritative. This document records settings to design incrementally; it does not define a configuration schema or request implementation.

**Planned** means configurability is already agreed. **Candidate** means a possible setting derived from an unresolved design or operations topic; it is not yet a requirement. Names below describe concepts, not final configuration keys. Configuration file locations and syntax remain open.

## Client configuration

| Setting | Status | Scope and current direction |
| --- | --- | --- |
| Backup mode | Planned | Per host/client, uniformly for every set: `pull` by default or `push`. No per-set override. Detailed switching commands and acknowledgement remain open. |
| Push retry interval | Planned | Client waits before retrying a push request when the server's concurrent backup-set limit is reached. Default: 60 seconds. Retry limits and cancellation remain open. |
| Backup sets and source selection | Planned | One TOML file per set for selected paths, exclusions, and other set settings. Capture and validate one description per run. Later edits affect subsequent runs. |
| Requested retention | Planned | Per set. Select UTC calendar periods from hours through years under [chapter 12](10c-SnapshotsCommitAndRetention.md#12-retention-minimum-protection-and-garbage-collection). Server minimum protection always applies. |
| `auto-cleanup` | Planned | Per set, default `false`. The client administrator uses an explicit `cleanup` request by default and can enable automatic cleanup of interrupted backup runs. |
| Server connection | Candidate | Client connection settings for the server address, SSH identity, and host-key verification. Exact options and their interaction with system SSH configuration remain open. Host-key verification stays mandatory. |
| Key-domain scope | Planned | During host/set setup: one domain per host or independent domains per set. Register the scope with the server. Changing an existing scope needs a reviewed key/history transition. No cross-host deduplication. |
| Unattended unlocking and Argon2id parameters | Candidate | Local client key protection. Secure input, resource parameters, and unattended operation need review. Passphrases and decryption keys must not enter the operational configuration sent to the server. |
| Restore and mount options | Candidate | Local operation options such as destination, overwrite policy, and read-only mount settings. Exact supported options and metadata-fidelity rules remain open. |
| Logging verbosity | Candidate | Local diagnostics suitable for CLI use and automation. Never include secrets or unintended file contents. |

## Server configuration

| Setting | Status | Scope and current direction |
| --- | --- | --- |
| Minimum protection duration | Planned | Per host repository, server-controlled; default seven days from server commit. Effective expiry is the maximum of the stored snapshot protection deadline and the snapshot commit time plus the currently configured interval. Retention may delete a snapshot only after this effective deadline. |
| Chunk sizes | Planned | Server-defined minimum, target, and maximum, communicated to the source before chunking. Initial defaults: 512 KiB, approximately 2 MiB, and 8 MiB. Larger configurations are possible within supported implementation and stored-object limits. Host/set configuration scope and validation remain open. |
| Pull schedule | Planned | Server schedules pull backups for hosts/sets. Push hosts use an external client-side scheduler and are not scheduled for pull. Scheduling syntax, configuration ownership, and overlap handling remain open. |
| Concurrent backup-set operations | Planned | Server-wide admission limit shared by push and pull. The default is 3 active backup-set operations. Queue scheduled pull jobs when full and return a wait response to push clients so they can retry. |
| Repository and service locations | Candidate | Server repository roots and Unix-socket location, with explicit service-account and access rules. Exact deployment options remain open. |
| Target pack size | Planned | The server controls the target size of new packs. The default is 50 MB (50,000,000 bytes). Close the pack after the complete record that reaches or exceeds the target. The target is independent of chunk-size limits. |
| Pack buffering | Candidate | Buffering limits remain open within the shared memory budget. |
| Admission and resource limits | Candidate | Host/repository quotas and server concurrency/resource bounds. Reaching a limit must reject or delay new work rather than evict protected history. |
| Concurrent repository operations | Planned | Server-wide admission limit for active repository operations. The default is 10. The shared memory budget applies across all active operations. Queueing, rejection, and validation rules remain open. |
| Shared memory budget | Planned | Target 64–128 MiB across all active server operations combined, including concurrent push clients and one or two mount readers. Serial pull work may reuse the budget. Exact configuration, accounting, enforcement, and admission rules remain open; no per-client multiplication of the budget. |
| Timeouts and retries | Candidate | Pull connection attempts and operation failure handling. Define bounded retries and commit acknowledgement semantics before choosing options. |
| Compaction threshold | Planned, implementation deferred | The background job reclaims space when the percentage threshold is reached. Value, default, metric, denominator, and scope remain open. One candidate metric is accumulated reclaimable pack bytes relative to total pack bytes. |
| Maintenance scheduling | Planned, implementation deferred | Compaction and retired-scope cleanup run asynchronously during idle periods. Define what counts as idle, job frequency, and how background work competes for repository locks and server operation slots. |
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
