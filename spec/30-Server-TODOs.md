# Server TODOs

See the [project specification](10-8472backup-Spec.md) and the [client backlog](31-Client-TODOs.md).

## 1. Repository maintenance

Run final deletion and compaction together during idle periods under the repository lock. Finish deleting retired sets and repositories once their snapshots are eligible. Define restart behavior for interrupted deletion, the compaction threshold, and scheduling under lock contention. Use the [retention and compaction rules](10c-SnapshotsCommitAndRetention.md#12-retention-minimum-protection-and-garbage-collection).

## 2. Server configuration and deployment

Load server settings and pass them to the façade and scheduler. Add the systemd unit with journal integration, deployment version stamping with the full Git commit ID, and the server-side SSH setup from the [initialization spec](10d-CommunicationConfigurationAndOperations.md#14-initialization-registration-and-identity-recovery).

## 3. Scheduling and resource limits

Connect pull job discovery and execution to the scheduler, including mode changes, retired sets, and retries. Apply shared admission limits and the memory budget to server operations. Define pull queueing and push wait responses. See the [configuration roadmap](70-ConfigurationRoadmap.md).

## 4. Client transport

Connect the façade to the Unix socket and SSH paths. Implement the identity and permission checks, request framing, batched object-ID exchange, and durable commit acknowledgement in the [communication spec](10d-CommunicationConfigurationAndOperations.md#134-protocol-requirements). Make the reserved snapshot UUID mandatory in the internal `create_snapshot()` call and test retries after a lost acknowledgement.
