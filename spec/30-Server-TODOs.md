# Server TODOs

See the [project specification](10-8472backup-Spec.md) and the [client backlog](31-Client-TODOs.md).

## 1. Server configuration and integration

Load server settings from TOML and pass them to the façade and scheduler. Add the systemd unit with journal integration and deployment version stamping with the full Git commit ID.

## 2. Scheduling and resource limits

Connect pull job discovery and execution to the scheduler, including mode changes, retired sets, and retries. Apply shared admission limits and the memory budget to server and [maintenance operations](10c-SnapshotsCommitAndRetention.md#12-retention-minimum-protection-and-garbage-collection). Define pull queueing and push wait responses. See the [configuration roadmap](70-ConfigurationRoadmap.md).

## 3. Client transport

Connect the façade to the Unix socket and SSH paths. Include server-side SSH setup from the [initialization spec](10d-CommunicationConfigurationAndOperations.md#14-initialization-registration-and-identity-recovery). Implement the identity and permission checks, request framing, batched object-ID exchange, and durable commit acknowledgement in the [communication spec](10d-CommunicationConfigurationAndOperations.md#134-protocol-requirements). Make the reserved snapshot UUID mandatory in the internal `create_snapshot()` call and test retries after a lost acknowledgement.
