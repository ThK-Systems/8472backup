# 8472backup — Communication, Configuration, and Operations

Specification version: 0.70 — Publish and list snapshot metadata, 2026-11-27

requirements and agreed architectural direction; early design phase. No production-ready implementation is implied.

## 13. Communication, execution, and protocol

### 13.1 Backup modes and scheduled pull

The backup mode is configured on the backup host (client) per host: `mode = "pull"` by default, or `mode = "push"` when explicitly selected. The setting applies uniformly to all of the host's backup sets; per-set mode selection or overrides are not supported. The precise client configuration file location, schema, and wire representation are OPEN. Both modes use the same server-side authorization, snapshot commit, retention, minimum protection, and safe garbage-collection rules.

At initial registration the client transmits the required operational configuration to the server, including identity/set associations, mode, and applicable scheduling and retention inputs. The complete backup description, including source paths and exclusions, stays client-side and is stored client-encrypted with each snapshot. The server validates the host mode and records it as part of registration. A host registered with `mode = "push"` is never added to pull scheduling and is not contacted for pull backups; an omitted mode defaults to `pull` and the server schedules it. Once registered in push mode, switching to pull requires the dedicated client command described below. Registration must not expose backup decryption keys or passphrases to the server. Exact serialization, validation and operational fields remain OPEN.

In pull mode the server schedules the host's backup sets. When a scheduled pull connects to the client, the client supplies its current validated host configuration. If it reports `mode = "push"`, the server removes that host from its pull scheduling list and records the push scheduling state persistently; it does not start the pull backup for that host. This changes scheduling only: the host remains registered and its repository, snapshots, retention, protection, and recovery access remain intact.

The server does not periodically pull or probe that host merely to discover a switch back. To return to pull, the operator changes the client configuration to `mode = "pull"` and proactively invokes a dedicated client command that informs the server over an authenticated connection. The server then adds the host back to its pull scheduling list. The exact command name and protocol are OPEN. Repeated requests should be safe, and failed requests must be reported so the operator can retry. Missing, invalid, or unreadable configuration must not silently be interpreted as a request to change mode. Mode is scheduling configuration, not an authorization boundary.

The local server service façade persists the requested mode for the whole client under the repository lock. When pull scheduling is connected, the façade will trigger reevaluation of that client's pull jobs after persistence and lock release. Admission checks the current mode before starting a new pull job.

Section 11 defines the local server record for the backup mode. OPEN: mode acknowledgement, retry and reconciliation behaviour, and handling of already-running or overlapping operations. Detailed mode-switching workflows and commands will be designed later; switching always applies to the whole client.

The server schedules hosts and sets, connects to a client through SSH, and invokes a restricted client operation. The client reads local configuration, processes source data, and returns encrypted objects and protocol metadata.

The server's SSH private key remains on the server. Its public key is installed in the client's `authorized_keys` with a forced command and restrictions on unrelated SSH facilities.

The client need not run a permanent backup daemon. It can be invoked through SSH or by a local user.

### 13.2 Push backups and client-initiated operations

In push mode the backup client initiates a backup over SSH to the server and sends encrypted objects and the protocol metadata needed to create a snapshot. The operator arranges invocation through a client-side scheduler such as cron or a systemd timer. The setting alone does not install or configure a scheduler, and no permanent client daemon is required.

The server admits and authorizes the operation and remains responsible for its authoritative snapshot record, commit timestamp, minimum protection, retention and garbage collection. A push client can create snapshots only through the authorized backup protocol; it cannot overwrite committed history, shorten protection, or obtain unrestricted repository access. After a successful backup, retention is evaluated subject to server protection just as in pull mode. Apply the same host/set isolation, encryption, failure handling and durability guarantees in both modes.

OPEN: exact backup CLI and pull-reactivation command, scheduler integration, acknowledgement and retries, and handling overlapping invocations or in-flight mode changes. Do not silently grant additional deletion or maintenance privileges to push credentials.

The client can also connect to the server through SSH for init, set registration, restore, mount, and other explicitly authorized CLI operations.

A client SSH key is separately authorized on the server through a restricted entry. Having a client-to-server key must not grant an arbitrary repository write API or deletion authority.

Pull uses server-side scheduling and execution; push uses client-side scheduling and initiation. Both require server admission and the same protection rules. Whether a user can manually request backups in pull mode remains OPEN.

The server limits the number of concurrently running backup-set operations across push and pull. The limit is configurable and defaults to 3. Scheduled pull jobs that cannot start because the limit is reached wait in a server-side queue. A push request that cannot start because the limit is reached receives a wait response; the client reports that it is waiting and retries after its configured interval, which defaults to 60 seconds. Repeated wait responses result in further retries. The server also limits concurrently active repository operations across all clients, with a configurable default of 10. Both limits are enforced at operation admission by accounting for active operations. The per-repository and per-backup-set locks protect access to their respective scopes.

Queue ordering, retry limits, cancellation, and the wire format of the wait response remain OPEN.

### 13.3 Client SSH access to the server service

The intended access path for client-initiated operations, including push, registration, restore, mount, and mode changes, is:

```text
backup client <-> SSH <-> server sshd <-> restricted 8472backup SSH command
                                      <-> local Unix-domain socket <-> 8472backup server service
```

The systemd-managed server service is a separate long-running process. On SSH authentication, `sshd` starts a restricted per-connection command; the command exchanges the common framed binary protocol with the client over standard input/output and relays requests and responses over the local Unix-domain socket to the server service. SSH provides the network transport; a separate TCP listener or SSH TCP port-forwarding tunnel is not required by this design. Prevent PTY allocation and unneeded forwarding on restricted keys. Keep protocol output separate from diagnostics.

The server service owns authorization, repository state, snapshot publication, retention, and protection. It must bind every operation to the authenticated host and applicable permissions. The SSH gateway must convey the authenticated identity using a server-controlled, trustworthy mapping from the accepted key or equivalent credential. A client-asserted UUID, hostname, request field, or untrusted `SSH_ORIGINAL_COMMAND` is not proof of identity. Restrict and protect the socket and the gateway-to-service trust boundary; do not assume a Unix socket or its shared OS account automatically authenticates individual SSH keys. The detailed identity mapping, local peer authorization, framing, and deployment remain OPEN and must be reviewed before implementation. Do not grant clients direct general-purpose access to the socket or repository.

For pull, the running server service initiates SSH to the client and invokes the restricted client command. That direction also does not require an additional TCP tunnel. This section specifies the intended design, not a claim that it is implemented.

### 13.4 Protocol requirements

Use a common framed binary request/response protocol for client/server operations over SSH standard input/output, following the Borg 1.4 transport approach. HTTP is not the client/server application protocol. This adopts the transport principle, not Borg protocol compatibility or a particular serialization library; MessagePack and the exact wire format remain unselected. Later HTTP(S) connections to an S3 or similar backend belong to the separate storage-backend path in the server process.

- Version the protocol and negotiate supported capabilities.
- Define explicit operations and permission checks in both directions.
- Bound message lengths, allocation, object sizes, and concurrency.
- Batch object-existence queries and transfers to avoid one network round trip per chunk.
- Avoid unsafe deserialization and arbitrary client-controlled shell command execution.
- Verify SSH peer identity; do not disable host-key checking as a convenience default.
- Specify reconnect, abort, duplicate request, and commit acknowledgement behaviour.

For a remote backup, the full exchange within one façade `backup()` call uses a single bidirectional SSH session. Each protocol message has a type and a payload length.

1. The client announces a block of object IDs for the backup set.
2. The server checks the set's key-domain location index and replies with the missing IDs. Both the existing and missing IDs are part of the snapshot membership.
3. The client sends the requested objects. After satisfying the outstanding requests, it sends the complete snapshot root as the final object, even if the root ID is already indexed. Chapter 11 defines publication and acknowledgement.

To support pipelining, an object-ID query carries a request ID that the server echoes in its reply. Request IDs are unique among outstanding queries within the session. Several ID blocks can be outstanding on the same channel. Bound the number of outstanding queries and the bytes buffered for them.

OPEN: exact framing fields and serialization, pipelining limits and detailed sequencing, session admission, authenticated identity mapping, and detailed gateway/socket protocol.

### 13.5 Push/pull boundary and common backup sessions

The server service façade is the common entry point for operations between the server and its clients. It addresses each client by its stable `client_id` UUID and resolves the repository from the authorized server context. The Unix-socket handler and pull scheduler both call the façade. The socket handler is a separate entry point, and the SSH relay to that socket is a later increment. The scheduler supplies the registered client and set for a due pull job. Both callers use the common backup session and snapshot commit path.

The façade covers `init_repository`, `add_backup_set`, `remove_backup_set`, `remove_repository`, `backup`, `list`, `restore`, `recover`, `mount`, `set_mode`, `cleanup`, `check`, and `version`. Adding a set registers it in the existing repository under that client's mode. Chapter 12 defines retirement and reclamation.

`ServerFacade.list(client_id, *, backup_set_id=None)` returns a `SnapshotListing` for the verified client's repository. Under the repository lock it reads the published register entries and snapshot JSON for the requested sets. It reads trusted server time once and applies the effective protection rule from chapter 12 to each row. The façade requires a configured `server_fqdn` at construction. The result uses the address `<fqdn>:8472backup/<client_id>`.

The text form prints the repository address once and one block per selected set. Print the set name or its UUID when a name is absent. Within a block, sort snapshots by UTC commit time with the newest first and include `SNAPSHOT ID`, `COMMENT`, `COMMITTED AT (UTC)`, and `PROTECTED` in that order. Append the three-letter English weekday in parentheses to each UTC commit timestamp, such as `(Thu)`. Show the comment column for a set with at least one comment. The exact remote permissions and wire encoding remain OPEN.

- `check` is a separate administrator command backed by the façade. Acquire the regular repository lock and report consistency findings for the snapshot register, the expected snapshot metadata and catalogues, and their location-index references into packs. The check is read-only.
- `version` is a separate command backed by `ServerFacade.version()`. It returns `VERSION` from `server/main.py` and requires neither a repository lookup nor a repository lock. The current value is `0.1-dev`. Deployment will stamp the string with the full Git commit ID of the built source, for example `0.1-dev (git <commit-id>)`.
- `cleanup` is a command in the regular client CLI for the client administrator. It calls the façade for an authorized backup set in either mode, following chapter 11.3.

Push/pull MUST have its own small, explicit boundary for mode-specific scheduling and invocation, connection establishment, and handoff to the common backup session:

- Push: the source CLI starts SSH to the server. The restricted server-side helper relays the common protocol in both directions over the local Unix-domain socket to the running server service. The server requests data over the same SSH session.
- Pull (default): the running server service starts SSH to the source host and invokes the restricted source CLI. The service exchanges the common protocol directly over the SSH subprocess's standard input/output.
- Both paths provide a bidirectional byte stream and a server-validated host/set authorization context to the same backup protocol and archive logic. The detailed authorization handoff remains OPEN.
- At the start of the common backup session, the client sends the required operational fields extracted from the run's captured, validated configuration to the server. This exchange and server validation precede object-existence queries and source-data transfer in both push and pull. The complete backup description, including source paths and exclusions, stays client-side and is archived client-encrypted with the snapshot. The run uses the same fixed configuration version as specified in chapter 15.
- Pass the selected backup set's prune rules as an explicit argument to the façade's `backup()` call in both modes. The server validates the supplied rules and uses them for that run's post-commit prune.
- Send the selected backup set's `auto-cleanup` setting with the other operational fields at backup start. The server applies the interrupted-run cleanup rules in chapter 11.3.
- The source client always reads, chunks, compresses, and encrypts source data. The server always controls admission, repository state, snapshot publication, retention, and cleanup. Starting the SSH connection does not exchange those application roles or grant additional permissions.
- Object-existence queries, transfer, membership submission, commit, and failure handling MUST use the same semantics and common implementation in both modes. Archive and repository code must not branch on which side started SSH.

Keep this boundary explicit in Python and limited to the concrete push/pull paths. The storage-backend boundary is separate: server-side `file` operations and any future backend HTTP(S) calls do not depend on who initiated the backup connection.

### 13.6 Asynchronous communication architecture

Use Python's standard-library `asyncio` for asynchronous communication and connection concurrency. Connection handling MUST NOT use threads or thread pools. Use lightweight `asyncio.Task` instances, preferably managed through `asyncio.TaskGroup`, and `asyncio.Queue`, `Lock`, and `Semaphore` where appropriate. Bound concurrency and apply backpressure.

- Pull: the server initiates OpenSSH to the client, which starts the restricted client program. Use `asyncio.create_subprocess_exec()` with asynchronous stdin/stdout communication. A permanent client daemon is not required.
- Push: the client initiates OpenSSH to the server, which starts the restricted gateway/helper process. The gateway communicates with the running server service through a Unix-domain socket using `asyncio.open_unix_connection()`; the service accepts connections using `asyncio.start_unix_server()`. Keep the gateway lightweight; authorization and repository operations belong to the server service.
- Do not add a TCP listener or SSH tunneling.
- Do not block the event loop with synchronous I/O or CPU-intensive work. Evaluate blocking filesystem operations and CPU-intensive processing separately; do not introduce thread pools as an implicit workaround.
- Preserve existing SSH authentication, authorization, retention, and transaction guarantees.

These requirements define the concurrency architecture. Implementation details remain subject to architectural review.
## 14. Initialization, registration, and identity recovery

The intended user workflow is:

1. Install the Linux server. During installation, generate an Ed25519 SSH key pair for the user running the installation. Set the SSH key comment to `8472backup-<user>@<fqdn>`, using that account's username and the server's fully qualified domain name.
2. Install the client and run client setup with the server address, the client mode, at least one valid backup-set configuration, and any encryption-passphrase input.
3. Generate a client authentication key without a passphrase for unattended operations where needed.
4. Display instructions for installing that public key on the server with the restricted command.
5. For server-initiated pull, install the server's public SSH key on the client with the corresponding restricted command. Push-only operation does not itself require a server-to-client SSH connection; bootstrap details remain OPEN.
6. Generate a stable host UUID and the selected encryption material.
7. Persist the client's recoverable identity/key state under `/var/lib/8472backup/state/` on Linux.
8. Authenticate to the server and request repository initialization with the client mode and the required operational fields for all valid sets. The client reports invalid set configurations individually. The server validates the submitted operational fields and registers all accepted sets together. Pull scheduling starts after successful registration for a client in pull mode. Omitted mode defaults to pull.
9. Clearly tell the user to back up the recovery state separately.

Registration must be bound to approved authentication credentials, not self-asserted UUID/hostname/IP values. The bootstrap trust exchange must be documented; do not silently auto-enrol arbitrary clients.

The client starts `init_repository` in both modes. Repository creation is a create-once operation on server storage. An init request for an existing initialized repository fails. Chapter 11 defines local initialization.

The user may supply a passphrase through the CLI. Exact secure input mechanisms remain OPEN; prevent accidental secret exposure in logs and avoid argv exposure as the only input mechanism.

The passphrase-protection and key-separation direction is defined in chapter 7. OPEN: exact backup key generation/wrapping and unattended unlocking, server SSH key storage, address updates, host reinstallation, recovery/re-enrolment, and authentication-key rotation.

Switch the server's SSH authentication key to a post-quantum signature key once a suitable algorithm is supported by the OpenSSH versions on both peers. An ML-DSA-based key is a candidate. Hybrid SSH key exchange with ML-KEM, the standard derived from Kyber, is evaluated separately under [the quantum security review](10b-CryptographyObjectsAndPacks.md#76-quantum-security-review).
## 15. Backup descriptions and configuration

Each backup set has its own local TOML file defining source selections, exclusions, and requested retention. The exact schema remains OPEN.

- Descriptions can change between runs.
- Read and validate one fixed configuration version per run.
- Changes during a run take effect on the next run.
- New includes affect future snapshots; exclusions do not rewrite previous snapshots.
- Retention changes may affect older snapshots, subject to server protection.
- Save the used description encrypted as part of the snapshot for reproducibility.
- The set identity groups runs whose configuration may differ. Each snapshot preserves its own captured configuration inside the client-encrypted snapshot description; later edits do not replace that historical configuration.
- Pass the server the validated operational retention metadata needed to evaluate policy without decrypting the description.
- An invalid description fails the run and cannot become an empty/default deletion policy.
- The host-level backup mode defaults to `pull` and can be set to `push`; exact file placement remains OPEN.

The supported retention rules and their calendar semantics are defined in chapter 12. OPEN: file locations, the TOML schema, and the scheduling configuration location.

The server does not need a separate readable copy of the complete backup description. The pre-run exchange exposes only required operational fields; source paths and exclusions are evaluated locally and retained in the client-encrypted snapshot description. The exact operational field schema remains OPEN.

See the [configuration roadmap](70-ConfigurationRoadmap.md) for planned settings, their authority and scope, and candidates that still need a decision.
## 16. Restore and read-only mounting

- Restore is initiated on a client with the required recovery keys.
- `restore` restores a complete snapshot to a client-side destination. `recover` restores a selected file or a complete directory tree without mounting. The client resolves the selected path using decrypted metadata.
- Restore, recover, and mount use the same server-side object read path. Check membership in the selected snapshot and match the stored record's ID and length to the requested object before sending its body.
- The server returns encrypted metadata and objects; the client decrypts and verifies them locally.
- Read-only FUSE mounting is a planned Linux 1.0 feature and runs on the client.
- Mount support is optional to the core backup/restore installation and not a universal future-platform requirement.
- Specify overwrite behaviour, path traversal protection, symlink handling, integrity failures, interruption, and metadata fidelity.
- Recovery must not require the original source host to remain available.
- Cached indexes and acceleration state SHOULD be rebuildable; distinguish them from indispensable recovery secrets.

Import of Borg history through a mount was discussed as feasible but is not committed to version 1.0. Import must distinguish original archive date from server commit time. Borg/restic format compatibility is not required.
## 17. Operations and observability

- Explicit CLI commands, useful exit codes, and logs suitable for systemd/journald and automation.
- Report completed, failed, partial, and protected/pruned states accurately.
- Never log passwords, key material, or unintended plaintext file contents.
- Bound client consumption of server resources; quotas or equivalent admission limits must not evict protected history.
- Handle unreachable hosts, overlapping jobs, retries, interruption, and disk-full conditions predictably.
- Keep normal configuration and deployment small. No web UI, SaaS dependency, or enterprise orchestration framework is required.

OPEN: privilege model, service accounts, source filesystem privileges, schedules, retry limits, quotas, metrics, and package formats.
