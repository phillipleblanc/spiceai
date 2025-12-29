# Executor Lease-Based Task Slot Management (Implementation Spec)

## Summary

This spec describes a lease-based mechanism for executor task slot management that enables **stateless schedulers** in a multi-scheduler cluster. Instead of schedulers tracking executor capacity centrally, each executor owns and manages its own task slots. Schedulers must acquire leases from executors before dispatching tasks.

## Problem Statement

Current Ballista architecture assumes a single scheduler that tracks all executor capacity state in-memory:

```
┌─────────────────────────────────────────────────────────────┐
│                        Scheduler                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │         InMemoryClusterState                         │    │
│  │  ┌───────────────────────────────────────────────┐  │    │
│  │  │  task_slots: HashMap<ExecutorId, AvailableSlots>│  │    │
│  │  │  heartbeats: DashMap<ExecutorId, Heartbeat>    │  │    │
│  │  └───────────────────────────────────────────────┘  │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                           │
                           │ LaunchMultiTask (push)
                           ▼
         ┌─────────────────────────────────────┐
         │            Executor                  │
         │  - Executes tasks                    │
         │  - No capacity tracking              │
         │  - Semaphore for local concurrency   │
         └─────────────────────────────────────┘
```

**Problems with centralized state:**

1. **Single point of failure**: Scheduler crash loses all capacity state
2. **No horizontal scaling**: Can't run multiple schedulers - they'd oversubscribe executors
3. **State reconstruction complexity**: Recovering scheduler must query all executors to rebuild state
4. **Stale state**: Executor crashes can leave phantom reservations in scheduler

## Solution: Executor-Owned Leases

Invert the ownership model: executors own their task slot state, schedulers request leases.

```
                    ┌─────────────┐  ┌─────────────┐
                    │ Scheduler A │  │ Scheduler B │
                    │ (stateless) │  │ (stateless) │
                    └──────┬──────┘  └──────┬──────┘
                           │                │
          ReserveSlots()   │                │  ReserveSlots()
          RenewLease()     │                │  RenewLease()
          ReleaseLease()   │                │  ReleaseLease()
                           │                │
                           ▼                ▼
         ┌─────────────────────────────────────────────────┐
         │                    Executor                      │
         │  ┌───────────────────────────────────────────┐  │
         │  │            LeaseManager                    │  │
         │  │  - total_slots: 8                          │  │
         │  │  - leases: HashMap<LeaseId, Lease>        │  │
         │  │  - pending_requests: HashMap<ReqId, ...>  │  │
         │  └───────────────────────────────────────────┘  │
         │  ┌───────────────────────────────────────────┐  │
         │  │         TaskExecutionSemaphore            │  │
         │  │  - Enforces lease validity on task run    │  │
         │  └───────────────────────────────────────────┘  │
         └─────────────────────────────────────────────────┘
```

### Key Properties

| Property | Description |
|----------|-------------|
| **Executor-owned state** | Executors are the source of truth for their capacity |
| **Lease TTL** | Leases expire automatically if not renewed (handles scheduler failures) |
| **Idempotent reservation** | `request_id` prevents duplicate reservations on retry |
| **Enforcement** | Executor rejects task execution without valid lease |
| **Stateless schedulers** | No scheduler-side capacity bookkeeping required |

## Detailed Design

### 1. Lease Data Model

```rust
/// Unique identifier for a lease
pub type LeaseId = Uuid;

/// Unique identifier for a reservation request (for idempotency)
pub type RequestId = String;

/// A lease represents reserved task slots on an executor
#[derive(Debug, Clone)]
pub struct Lease {
    /// Unique identifier for this lease
    pub id: LeaseId,
    
    /// Scheduler that owns this lease
    pub scheduler_id: String,
    
    /// Original request ID (for idempotency checking)
    pub request_id: RequestId,
    
    /// Number of task slots reserved
    pub slots: u32,
    
    /// When this lease expires (absolute timestamp)
    pub expires_at: SystemTime,
    
    /// When this lease was created
    pub created_at: SystemTime,
    
    /// Number of slots currently in use (running tasks)
    pub slots_in_use: u32,
}

/// Executor-side lease manager
pub struct LeaseManager {
    /// Total task slots available on this executor
    total_slots: u32,
    
    /// Active leases by lease ID
    leases: RwLock<HashMap<LeaseId, Lease>>,
    
    /// Request ID -> Lease ID mapping for idempotency
    request_cache: RwLock<HashMap<RequestId, LeaseId>>,
    
    /// Background task handle for expiration cleanup
    expiration_task: JoinHandle<()>,
}
```

### 2. gRPC Service Definition

Add a new service to the executor (or extend `ExecutorGrpc`). **Prefer adding to Spice's executor service** to avoid Ballista proto changes.

```protobuf
// In crates/runtime-proto/proto/spice.proto

service ExecutorLeaseService {
    // Reserve task slots on this executor
    rpc ReserveSlots(ReserveSlotsRequest) returns (ReserveSlotsResponse);
    
    // Renew an existing lease
    rpc RenewLease(RenewLeaseRequest) returns (RenewLeaseResponse);
    
    // Release a lease (free reserved slots)
    rpc ReleaseLease(ReleaseLeaseRequest) returns (ReleaseLeaseResponse);
    
    // Get current executor capacity and lease state
    rpc GetCapacity(GetCapacityRequest) returns (GetCapacityResponse);
}

message ReserveSlotsRequest {
    // Scheduler requesting the reservation
    string scheduler_id = 1;
    
    // Idempotency key - same request_id returns same lease
    string request_id = 2;
    
    // Number of slots requested
    uint32 slots = 3;
    
    // Requested TTL in milliseconds
    uint64 ttl_ms = 4;
}

message ReserveSlotsResponse {
    // Empty if reservation failed
    optional string lease_id = 1;
    
    // Number of slots actually granted (may be < requested)
    uint32 granted = 2;
    
    // When the lease expires (Unix timestamp ms)
    uint64 expires_at_ms = 3;
    
    // Reason if reservation failed or partial
    optional string reason = 4;
}

message RenewLeaseRequest {
    string lease_id = 1;
    uint64 ttl_ms = 2;
}

message RenewLeaseResponse {
    // New expiration time (Unix timestamp ms)
    uint64 expires_at_ms = 1;
    
    // False if lease not found or already expired
    bool success = 2;
}

message ReleaseLeaseRequest {
    string lease_id = 1;
}

message ReleaseLeaseResponse {
    bool success = 1;
}

message GetCapacityRequest {}

message GetCapacityResponse {
    // Total slots on this executor
    uint32 total_slots = 1;
    
    // Currently free (unreserved) slots
    uint32 free_slots = 2;
    
    // Slots currently executing tasks
    uint32 in_use = 3;
    
    // Active leases
    repeated LeaseInfo leases = 4;
}

message LeaseInfo {
    string lease_id = 1;
    string scheduler_id = 2;
    uint32 slots = 3;
    uint32 slots_in_use = 4;
    uint64 expires_at_ms = 5;
}
```

### 3. Executor Implementation

#### 3.1 LeaseManager

Location: `crates/runtime/src/cluster/lease.rs` (new file)

```rust
// Pseudocode - actual implementation will follow project patterns

/// Fixed TTL for request_id -> lease_id idempotency cache
const REQUEST_ID_CACHE_TTL: Duration = Duration::from_secs(60);

/// Entry in the request ID cache with its own expiration
struct RequestCacheEntry {
    lease_id: LeaseId,
    expires_at: Instant,
}

/// Callback for task cancellation when leases expire
pub trait LeaseExpirationHandler: Send + Sync {
    /// Called when a lease expires. Implementation should cancel all tasks
    /// associated with this lease.
    fn on_lease_expired(&self, lease_id: LeaseId, scheduler_id: &str);
}

impl LeaseManager {
    pub fn new(
        total_slots: u32,
        expiration_handler: Arc<dyn LeaseExpirationHandler>,
    ) -> Self {
        let manager = Self {
            total_slots,
            leases: RwLock::new(HashMap::new()),
            request_cache: RwLock::new(HashMap::new()),
            expiration_handler,
            expiration_task: spawn_expiration_loop(),
        };
        manager
    }

    /// Reserve slots. Returns existing lease if request_id matches (within 60s cache TTL).
    pub async fn reserve_slots(
        &self,
        scheduler_id: &str,
        request_id: &str,
        slots_requested: u32,
        ttl: Duration,
    ) -> ReserveSlotsResult {
        let now = Instant::now();
        
        // Check idempotency cache first (with its own 60s TTL)
        {
            let request_cache = self.request_cache.read().await;
            if let Some(entry) = request_cache.get(request_id) {
                if entry.expires_at > now {
                    // Cache entry still valid
                    if let Some(lease) = self.leases.read().await.get(&entry.lease_id) {
                        return ReserveSlotsResult {
                            lease_id: Some(entry.lease_id),
                            granted: lease.slots,
                            expires_at: lease.expires_at,
                            reason: None,
                        };
                    }
                }
            }
        }

        // Calculate available slots
        let mut leases = self.leases.write().await;
        let reserved: u32 = leases.values().map(|l| l.slots).sum();
        let available = self.total_slots.saturating_sub(reserved);
        
        if available == 0 {
            return ReserveSlotsResult {
                lease_id: None,
                granted: 0,
                expires_at: SystemTime::UNIX_EPOCH,
                reason: Some("No slots available".into()),
            };
        }

        // Grant up to available slots
        let granted = slots_requested.min(available);
        let lease = Lease {
            id: LeaseId::new_v4(),
            scheduler_id: scheduler_id.to_string(),
            request_id: request_id.to_string(),
            slots: granted,
            expires_at: SystemTime::now() + ttl,
            created_at: SystemTime::now(),
            slots_in_use: 0,
        };

        let lease_id = lease.id;
        let expires_at = lease.expires_at;
        
        leases.insert(lease_id, lease);
        
        // Cache with fixed 60-second TTL (independent of lease TTL)
        self.request_cache.write().await.insert(
            request_id.to_string(),
            RequestCacheEntry {
                lease_id,
                expires_at: now + REQUEST_ID_CACHE_TTL,
            },
        );

        ReserveSlotsResult {
            lease_id: Some(lease_id),
            granted,
            expires_at,
            reason: None,
        }
    }

    /// Renew a lease's TTL
    pub async fn renew_lease(&self, lease_id: LeaseId, ttl: Duration) -> Option<SystemTime> {
        let mut leases = self.leases.write().await;
        if let Some(lease) = leases.get_mut(&lease_id) {
            let now = SystemTime::now();
            if lease.expires_at > now {
                lease.expires_at = now + ttl;
                return Some(lease.expires_at);
            }
        }
        None
    }

    /// Release a lease, freeing its slots
    pub async fn release_lease(&self, lease_id: LeaseId) -> bool {
        let mut leases = self.leases.write().await;
        leases.remove(&lease_id).is_some()
        // Note: request_cache entries expire independently via their own TTL
    }

    /// Check if a lease is valid and has available slots for a task
    pub async fn try_use_slot(&self, lease_id: LeaseId) -> bool {
        let mut leases = self.leases.write().await;
        if let Some(lease) = leases.get_mut(&lease_id) {
            let now = SystemTime::now();
            if lease.expires_at > now && lease.slots_in_use < lease.slots {
                lease.slots_in_use += 1;
                return true;
            }
        }
        false
    }

    /// Return a slot when task completes
    pub async fn return_slot(&self, lease_id: LeaseId) {
        let mut leases = self.leases.write().await;
        if let Some(lease) = leases.get_mut(&lease_id) {
            lease.slots_in_use = lease.slots_in_use.saturating_sub(1);
        }
    }

    /// Background task to expire leases and cancel associated tasks
    async fn expiration_loop(self: Arc<Self>) {
        let mut interval = tokio::time::interval(Duration::from_secs(1));
        loop {
            interval.tick().await;
            let now_system = SystemTime::now();
            let now_instant = Instant::now();
            
            // Expire leases and cancel their tasks
            let expired_leases: Vec<(LeaseId, String)> = {
                let mut leases = self.leases.write().await;
                let expired: Vec<(LeaseId, String)> = leases
                    .iter()
                    .filter(|(_, l)| l.expires_at <= now_system)
                    .map(|(id, l)| (*id, l.scheduler_id.clone()))
                    .collect();
                
                for (lease_id, _) in &expired {
                    leases.remove(lease_id);
                }
                expired
            };
            
            // Cancel tasks for expired leases
            for (lease_id, scheduler_id) in expired_leases {
                tracing::info!(
                    lease_id = %lease_id,
                    scheduler_id = %scheduler_id,
                    "Lease expired, cancelling associated tasks"
                );
                self.expiration_handler.on_lease_expired(lease_id, &scheduler_id);
            }
            
            // Clean up expired request cache entries
            {
                let mut request_cache = self.request_cache.write().await;
                request_cache.retain(|_, entry| entry.expires_at > now_instant);
            }
        }
    }
}
```

#### 3.2 Task Execution Enforcement

Modify task launch to require a valid lease:

```rust
// In executor task handling

impl ExecutorServer {
    async fn launch_task_with_lease(
        &self,
        lease_id: LeaseId,
        task: TaskDefinition,
    ) -> Result<(), BallistaError> {
        // Enforce lease validity
        if !self.lease_manager.try_use_slot(lease_id).await {
            return Err(BallistaError::General(
                "Invalid or expired lease, or no available slots".into()
            ));
        }

        // Execute task (existing logic)
        let result = self.run_task(task).await;

        // Return slot when done
        self.lease_manager.return_slot(lease_id).await;

        result
    }
}
```

### 4. Scheduler Integration

#### 4.1 Lease-Aware Task Binding

The scheduler's `bind_schedulable_tasks` flow needs modification:

```rust
// Current flow (ClusterState::bind_schedulable_tasks):
// 1. Get available slots from in-memory HashMap
// 2. Bind tasks to slots
// 3. Decrement slot counts in memory
// 4. Return bound tasks for dispatch

// New flow (LeaseAwareScheduler):
// 1. Get capacity from executors via GetCapacity RPC
// 2. For each executor with free slots:
//    a. ReserveSlots(scheduler_id, request_id, needed_slots, ttl)
//    b. Track granted lease
// 3. Bind tasks to acquired leases
// 4. LaunchMultiTask with lease_id in request
// 5. Start lease renewal background task
// 6. On task completion/failure, ReleaseLease or adjust slots_in_use
```

#### 4.2 Lease Renewal Loop

Each scheduler runs a background loop to renew its active leases:

```rust
async fn lease_renewal_loop(
    leases: Arc<RwLock<HashMap<LeaseId, ActiveLease>>>,
    executors: Arc<ExecutorRegistry>,
    renewal_interval: Duration,
    lease_ttl: Duration,
) {
    let mut interval = tokio::time::interval(renewal_interval);
    loop {
        interval.tick().await;
        
        let active_leases = leases.read().await.clone();
        for (lease_id, lease_info) in active_leases {
            let executor = executors.get(&lease_info.executor_id);
            match executor.renew_lease(lease_id, lease_ttl).await {
                Ok(new_expiry) => {
                    leases.write().await
                        .get_mut(&lease_id)
                        .map(|l| l.expires_at = new_expiry);
                }
                Err(e) => {
                    tracing::warn!(
                        lease_id = %lease_id,
                        executor = %lease_info.executor_id,
                        "Failed to renew lease: {}", e
                    );
                    // Lease will expire, executor will clean up
                }
            }
        }
    }
}
```

#### 4.3 Stage Planning with Lease Accumulation

Before executing a stage, the scheduler must accumulate enough leases using an all-or-nothing approach:

```rust
async fn plan_stage_execution(
    &self,
    stage: &ExecutionStage,
    executors: &[ExecutorInfo],
) -> Result<StagePlan, SchedulerError> {
    let required_tasks = stage.partitions.len();
    
    loop {
        // Step 1: Pre-check capacity across all executors
        let mut total_available = 0;
        let mut executor_capacities: Vec<(ExecutorInfo, u32)> = Vec::new();
        
        for executor in executors {
            match executor.get_capacity().await {
                Ok(capacity) => {
                    total_available += capacity.free_slots;
                    executor_capacities.push((executor.clone(), capacity.free_slots));
                }
                Err(e) => {
                    tracing::warn!(executor = %executor.id, "Failed to get capacity: {}", e);
                }
            }
        }
        
        // Step 2: Abort early if insufficient capacity
        if (total_available as usize) < required_tasks {
            tracing::debug!(
                required = required_tasks,
                available = total_available,
                "Insufficient cluster capacity, waiting..."
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
            continue;
        }
        
        // Step 3: Attempt to acquire leases
        let mut accumulated_leases: Vec<AcquiredLease> = Vec::new();
        let mut total_slots = 0;
        
        for (executor, _free_slots) in &executor_capacities {
            if total_slots >= required_tasks {
                break;
            }
            
            let needed = required_tasks - total_slots;
            let request_id = format!(
                "{}-{}-{}-{}",
                self.scheduler_id, stage.job_id, stage.id, executor.id
            );
            
            match executor.reserve_slots(
                &self.scheduler_id,
                &request_id,
                needed as u32,
                self.lease_ttl,
            ).await {
                Ok(lease) if lease.granted > 0 => {
                    total_slots += lease.granted as usize;
                    accumulated_leases.push(AcquiredLease {
                        lease_id: lease.lease_id,
                        executor_id: executor.id.clone(),
                        slots: lease.granted,
                    });
                }
                Ok(_) => continue,
                Err(e) => {
                    tracing::warn!(executor = %executor.id, "Failed to reserve slots: {}", e);
                }
            }
        }
        
        // Step 4: Check if we got enough slots
        if total_slots >= required_tasks {
            return Ok(StagePlan {
                stage_id: stage.id,
                leases: accumulated_leases,
            });
        }
        
        // Step 5: Release ALL leases and retry (all-or-nothing)
        tracing::debug!(
            required = required_tasks,
            acquired = total_slots,
            "Failed to acquire sufficient leases, releasing all and retrying"
        );
        
        for lease in &accumulated_leases {
            if let Some(executor) = executors.iter().find(|e| e.id == lease.executor_id) {
                let _ = executor.release_lease(lease.lease_id).await;
            }
        }
        
        // Brief backoff before retry
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}
```

### 5. Failure Handling

#### 5.1 Scheduler Failure

When a scheduler dies:
1. Lease renewals stop
2. Leases expire after TTL (e.g., 30 seconds)
3. Executor's expiration loop detects expired leases
4. Executor cancels all running tasks associated with expired leases
5. Slots become available for other schedulers
6. Associated jobs fail (expected behavior)

#### 5.2 Executor Failure

When an executor dies:
1. Scheduler's health check detects executor unreachable
2. Scheduler marks leases for that executor as invalid
3. Scheduler fails affected jobs or reschedules on other executors

#### 5.3 Network Partition

Temporary network issues:
1. Lease renewals may fail
2. If partition resolves before TTL expires, renewals resume
3. If TTL expires, executor cleans up, scheduler must re-acquire leases

### 6. Configuration

```yaml
# spicepod.yaml cluster configuration

runtime:
  cluster:
    # Lease TTL - how long leases last without renewal
    lease_ttl_ms: 30000  # 30 seconds
    
    # How often schedulers renew their leases
    lease_renewal_interval_ms: 10000  # 10 seconds
    
    # Request ID idempotency cache TTL (fixed, not configurable)
    # request_id_cache_ttl_ms: 60000  # 60 seconds (hardcoded)
```

## Implementation Plan

### Phase 1: Executor Lease Service (Spice)

**Files to create:**
- `crates/runtime/src/cluster/lease.rs` - `LeaseManager` implementation
- `crates/runtime-proto/proto/spice.proto` - Add `ExecutorLeaseService`

**Files to modify:**
- `crates/runtime/src/cluster/service.rs` - Add `ExecutorLeaseService` to gRPC server
- `crates/runtime/src/cluster/mod.rs` - Initialize `LeaseManager` in executor

### Phase 2: Scheduler Lease Client (Spice)

**Files to create:**
- `crates/runtime/src/cluster/scheduler_lease.rs` - Scheduler-side lease management

**Files to modify:**
- `crates/runtime/src/cluster/mod.rs` - Lease renewal loop in scheduler

### Phase 3: Ballista Integration Hooks

**Goal:** Transport `lease_id` via task metadata to avoid Ballista proto changes.

**Files to modify in datafusion-ballista (minimal):**
- `ballista/executor/src/executor_server.rs` - Read `lease_id` from task properties in `run_task()`

**Approach:**
1. Scheduler adds `lease_id` to task properties (existing `props` field in `TaskDefinition`)
2. Executor's Spice wrapper extracts `lease_id` from task properties
3. Spice wrapper validates lease before delegating to Ballista task execution
4. No changes to `ballista.proto` required

```rust
// Scheduler side: Add lease_id to task properties
fn create_task_definition(task: &TaskDescription, lease_id: &LeaseId) -> TaskDefinition {
    let mut props = task.session_config.options().entries();
    props.push(ConfigEntry {
        key: "spice.cluster.lease_id".to_string(),
        value: Some(lease_id.to_string()),
    });
    
    TaskDefinition {
        // ... other fields
        props,
        // ...
    }
}

// Executor side: Extract and validate lease_id
async fn handle_task(&self, task: TaskDefinition) -> Result<(), BallistaError> {
    // Extract lease_id from task properties
    let lease_id = task.props.iter()
        .find(|p| p.key == "spice.cluster.lease_id")
        .and_then(|p| p.value.as_ref())
        .and_then(|v| LeaseId::parse_str(v).ok())
        .ok_or_else(|| BallistaError::General("Missing lease_id in task".into()))?;
    
    // Validate lease before execution
    if !self.lease_manager.try_use_slot(lease_id).await {
        return Err(BallistaError::General(
            "Invalid or expired lease".into()
        ));
    }
    
    // Execute task via Ballista
    let result = self.run_task_inner(task).await;
    
    // Return slot
    self.lease_manager.return_slot(lease_id).await;
    
    result
}
```

### Phase 4: Task Dispatch Integration

**Files to modify:**
- `crates/runtime/src/cluster/mod.rs` - Replace scheduler's `InMemoryClusterState` usage with lease-aware client

## Design Decisions

The following decisions have been made for this implementation:

### 1. Lease Granularity

**Decision:** Per-batch/stage with internal slot tracking.

Leases reserve N slots that can be used for multiple tasks within a stage. The `LeaseManager` tracks `slots_in_use` within each lease.

### 2. Lease ID Transport

**Decision:** Use task metadata/properties to transport `lease_id` (Option b).

The `lease_id` is passed as a task property in the existing task metadata mechanism. This avoids modifying Ballista's `LaunchTaskParams.proto`. The executor's Spice wrapper layer validates the lease before delegating task execution to Ballista.

**Rationale:** Minimizes Ballista changes while maintaining enforcement.

### 3. Partial Grants and Scheduler Behavior

**Decision:** Best-effort pre-check with all-or-nothing retry.

1. Before requesting leases, scheduler queries `GetCapacity()` from all executors
2. Scheduler only proceeds if total available slots >= required tasks
3. Scheduler requests leases from executors in order
4. If accumulated slots fall short (race condition), scheduler:
   - Releases ALL acquired leases
   - Waits briefly (backoff)
   - Retries from step 1
5. Executors may still return partial grants; scheduler accumulates across executors

**Rationale:** Prevents slot fragmentation and reduces wasted lease churn.

### 4. Request ID Caching

**Decision:** Fixed 60-second TTL for `request_id` -> `lease_id` mapping.

The idempotency cache is independent of lease TTL. After 60 seconds, the same `request_id` can create a new lease (the original lease may or may not still exist).

**Rationale:** Bounds memory usage for idempotency cache while providing sufficient retry window.

### 5. Task Cancellation on Lease Expiry

**Decision:** Cancel running tasks when lease expires.

When a lease expires:
1. Executor immediately cancels all tasks associated with that lease
2. Tasks are aborted (not allowed to complete)
3. Slots are freed for other schedulers

**Rationale:** Clean failure semantics. If a scheduler dies, its work should not continue indefinitely. The scheduler's job will fail, which is the expected behavior.

### 6. Mixed Clusters

**Decision:** Out of scope.

All executors in a cluster must support the lease service. Backward compatibility with non-lease executors is not supported in this implementation.

## Open Questions

### 1. Metrics and Observability

**Question:** What metrics should be exposed?

**Required metrics:**
- `executor_lease_total` - Total leases created
- `executor_lease_active` - Currently active leases
- `executor_lease_expired` - Leases that expired without release
- `executor_slots_reserved` - Currently reserved slots
- `executor_slots_in_use` - Slots actively executing tasks
- `scheduler_lease_renewals` - Lease renewal attempts (success/failure)

### 8. Testing Strategy

**Question:** How to test lease behavior in CI?

**Approaches:**
- Unit tests for `LeaseManager` logic
- Integration tests with multi-scheduler scenarios
- Chaos testing for failure modes (scheduler crash, network partition)

## Appendix: Current Task Slot Flow

### Ballista's Current Implementation

1. **Executor Registration** (`executor_server.rs`):
   - Executor sends `RegisterExecutorParams` with `ExecutorSpecification.task_slots`
   - Scheduler stores in `InMemoryClusterState.task_slots`

2. **Task Binding** (`cluster/memory.rs`):
   - `bind_schedulable_tasks()` reads `task_slots` mutex
   - Decrements available slots for each bound task
   - Returns `Vec<BoundTask>` with executor assignments

3. **Task Launch** (`state/mod.rs`, `executor_manager.rs`):
   - `launch_multi_task()` sends tasks to executor via gRPC
   - Executor uses `Semaphore` for local concurrency control

4. **Slot Return** (`executor_manager.rs`):
   - `unbind_tasks()` increments slots when tasks complete

### Key Files in datafusion-ballista

| File | Purpose |
|------|---------|
| `scheduler/src/cluster/mod.rs` | `ClusterState` trait, `bind_task_*` functions |
| `scheduler/src/cluster/memory.rs` | `InMemoryClusterState` implementation |
| `scheduler/src/state/executor_manager.rs` | Executor tracking, task launch |
| `scheduler/src/state/mod.rs` | `revive_offers()`, task dispatch orchestration |
| `executor/src/executor_server.rs` | Task execution, gRPC handlers |
| `executor/src/execution_loop.rs` | Poll-based task execution (not used in push mode) |
| `core/proto/ballista.proto` | gRPC service definitions |

### Key Files in spiceai

| File | Purpose |
|------|---------|
| `crates/runtime/src/cluster/mod.rs` | Scheduler/executor initialization |
| `crates/runtime/src/cluster/service.rs` | Spice cluster gRPC service |
| `crates/runtime-proto/proto/spice.proto` | Spice-specific proto definitions |
