# Ballista Push-Based Scheduling (Implementation Spec)

## Scope
Switch the Spice runtime cluster executor from Ballista pull-based task polling to push-based task dispatch, using Ballista's existing Executor gRPC service and scheduler push policy.

This spec is based on the current integration points in this repo and the local `datafusion-ballista` dependency.

## Current Integration (Observed)
- Executor uses pull loop:
  - `crates/runtime/src/cluster/mod.rs` starts `execution_loop::poll_loop(...)`.
  - `ExecutorRegistration.grpc_port` is set to `0` (comment notes push-only).
- Scheduler uses default scheduling policy:
  - `crates/runtime/src/cluster/mod.rs` builds `SchedulerConfig` with `..Default::default()`.
- Executor discovery uses `DescribeExecutor`:
  - `crates/runtime/src/cluster/service.rs` returns `executor.metadata.grpc_port` in `DescribeExecutorResponse`.
  - `crates/runtime/src/cluster/discovery.rs` registers executors using that response.
- Ballista push infrastructure is already present in dependency:
  - Executor gRPC server and push handling in `datafusion-ballista/ballista/executor/src/executor_server.rs`.
  - Scheduler push dispatch via `ExecutorGrpcClient` in `datafusion-ballista/ballista/scheduler/src/state/executor_manager.rs`.
  - Scheduling policy is `TaskSchedulingPolicy::PushStaged` in `datafusion-ballista/ballista/scheduler/src/config.rs`.

## Required Changes in Spice Runtime

### 1) Force push-staged scheduling (no config)
Files:
- `crates/runtime/src/cluster/mod.rs`

Rationale:
- Scheduling policy is fixed to push-staged.

### 2) Set scheduler policy to push unconditionally
Files:
- `crates/runtime/src/cluster/mod.rs` (`create_scheduler_server`)

Changes:
- Populate `SchedulerConfig.scheduling_policy` as `TaskSchedulingPolicy::PushStaged`.

### 3) Start the executor gRPC server on the existing executor cluster service
Files:
- `crates/runtime/src/cluster/mod.rs` (`initialize_cluster_executor`)
- `crates/runtime/src/cluster/servers.rs` (`start_executor_flight_server`)

Changes:
- Remove `execution_loop::poll_loop(...)` and use Ballista executor gRPC startup unconditionally.
- Bind the Executor gRPC service on the executor's existing cluster service listener (the same bind address used for `ExecutorService`), not a separate port.
- Extend the executor cluster service server to also host `ExecutorGrpcServer` (from `ballista_core::serde::protobuf::executor_grpc_server::ExecutorGrpcServer`).

### 4) Advertise executor gRPC on the existing cluster service port
Files:
- `crates/runtime/src/cluster/mod.rs`

Changes:
- Set `ExecutorRegistration.grpc_port` to the executor cluster service port (same as `node_bind_address`).
- Continue to set `host` to `node_advertise_address` and `port` to the Flight port.

### 5) Keep `DescribeExecutor` returning the gRPC port
Files:
- `crates/runtime/src/cluster/service.rs`

Changes:
- No schema changes required; ensure executor metadata includes the non-zero `grpc_port` so discovery supplies it to the scheduler.

### 6) Readiness/Health synchronization
Files:
- `crates/runtime/src/cluster/mod.rs`

Changes:
- Replace `poll_loop` readiness with a push-ready signal that fires after:
  - Executor gRPC server is listening on the cluster service port, and
  - Scheduler registration succeeds.

### 7) Scheduler-polled executor health and task status
Files:
- `crates/runtime/src/cluster/discovery.rs`
- `crates/runtime/src/cluster/service.rs`

Changes:
- Scheduler actively polls executor health instead of relying on executor heartbeats.
- Add a scheduler-driven poll loop to fetch task status from executors (do not rely on `UpdateTaskStatus` from executors).
- Reuse or extend discovery to periodically re-validate executors by calling `DescribeExecutor` (or a dedicated health RPC if added to `spice.proto`).
- Mark executors as unhealthy if they fail N consecutive polls, and trigger rescheduling.

### 8) Keep Flight server unchanged
Files:
- `crates/runtime/src/cluster/servers.rs`

Notes:
- Flight service remains for data exchange.
- `ExecutorService` for discovery stays on the Flight server, but now reports the executor gRPC port for push dispatch.

## Required Changes in datafusion-ballista (minimal-path)

Goal: avoid Ballista proto changes by using a Spice-specific gRPC service, while still inverting health/status flow.

Minimum changes still required in Ballista (executor + scheduler):

- `ballista/executor/src/executor_server.rs`
  - Add an internal status queue/registry that records task status updates and executor health snapshots.
  - Expose this queue via a small hook/trait so `spiced` can read it (no gRPC in Ballista).
  - Stop relying on outbound `UpdateTaskStatus`/`HeartBeatFromExecutor` for correctness in Spice mode (keep calls optional/legacy).

- `ballista/scheduler/src/state/executor_manager.rs`
  - Add a polling loop that calls the Spice-specific executor service to fetch status/health.
  - Feed those polled `TaskStatus` updates into the existing scheduler state update path.
  - Mark executors dead on consecutive poll failures and reschedule.

Notes:
- This keeps Ballista’s public proto stable.
- Ballista still needs to change to produce polled status and consume it in the scheduler.

## Compatibility and Migration
- Behavior is push-staged only.
- Executors and schedulers must be updated together because the executor gRPC server becomes mandatory.

## Testing Plan
- Integration:
  - Cluster start with push mode: scheduler + executor join, register, and run a simple query.
  - DNS SRV discovery still registers executors with correct `grpc_port`.
- Failure:
  - Executor gRPC server down: scheduler marks executor dead and reschedules.
  - Executor restart: re-registration and reconnection under push policy.
