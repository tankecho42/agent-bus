# Error Recovery & Resilience Design

> AgentBus Phase 4 Design Document — Round 14
> Author: Echo (autonomous overnight iteration)
> Date: 2026-08-12

## 1. Problem Statement

In a multi-agent system, things go wrong:
- **Agent crash**: An agent process dies while working on a task, leaving it stuck in `in_progress` forever.
- **Task timeout**: An agent starts a task but stalls (infinite loop, stuck on a prompt, API outage).
- **Network partition**: The HK server becomes unreachable; agents can't report status.
- **Cascading DAG stall**: An upstream task is stuck → all downstream tasks in the DAG are permanently blocked.

Currently, recovery requires Echo to manually notice and intervene. This is fine for 3 agents but doesn't scale.

## 2. Design: Three-Layer Error Recovery

### Layer 1: Task Timeout Detection (Stale Task Scanner)

**Concept**: A background scanner runs every N minutes. For any task in `in_progress` state whose `started_at` is older than the configured timeout, it marks the task as `stale`.

```
POST /tasks/{id}/stale   (Echo or auto-scanner)
  → Sets status = "stale"
  → Records stale_since timestamp
  → Sends a notification message to the assignee
  → Logs to audit_events
```

**New status added**: `stale` — a sub-state of `in_progress` indicating the task is suspected abandoned.

**Configuration**:
```python
TASK_TIMEOUT_HOURS = float(os.getenv("TASK_TIMEOUT_HOURS", "2"))  # Default: 2 hours
STALE_SCAN_INTERVAL = int(os.getenv("STALE_SCAN_INTERVAL", "300"))  # 5 minutes
```

### Layer 2: Agent Crash Recovery (Heartbeat Watcher)

**Concept**: The `/agents/health` endpoint already classifies agents as `active`/`stale`/`dead` based on heartbeat. We add a recovery action:

```
POST /agents/{name}/recover
  → Find all tasks where assignee = agent_id AND status IN ('assigned', 'in_progress', 'stale')
  → Reset them to status = "pending", assignee = NULL
  → Log to audit_events: "Agent crash recovery: N tasks reverted"
  → Return list of recovered task IDs
```

**Recovery triggers**:
- Manual: Echo calls this when an agent is confirmed dead.
- Semi-auto: A cron/watchdog calls this when an agent has been `dead` for > 1 hour.

### Layer 3: DAG Re-trigger After Recovery

**Concept**: When a task is recovered (reset to pending), its downstream tasks might also be stuck. The system should:

1. Check if the recovered task's upstream deps are all `done`.
2. If so, auto-advance the recovered task to `assigned` (if a new assignee is available).
3. If not, leave it `pending` — it will naturally advance when deps complete.

This reuses the existing `advance_ready_tasks()` function.

## 3. Implementation: Error Recovery Module

The recovery logic lives in a new module `error_recovery.py` that can be run as:
1. A standalone cron script: `python3 error_recovery.py --scan`
2. An API endpoint: `POST /recovery/scan` (Echo-only)
3. A background thread within main.py (if `ENABLE_RECOVERY=1`)

### API Additions to main.py

```python
# New status
VALID_STATUSES = {"pending", "assigned", "in_progress", "stale", "review", "done", "failed"}

# POST /recovery/scan-stale-tasks
#   → Find all in_progress tasks older than TASK_TIMEOUT_HOURS
#   → Mark them stale
#   → Return count + task list

# POST /recovery/agent-crash/{agent_name}
#   → Reset all tasks for a dead agent to pending
#   → Return recovered task list

# POST /recovery/dag-unblock
#   → Find pending tasks whose deps are all done but haven't been auto-advanced
#   → Force-advance them
```

## 4. Decision Tree

```
Task stuck?
├── Status = in_progress, > 2h old?
│   ├── Mark stale → notify assignee
│   └── If assignee is dead → recover tasks → reassign
├── Status = assigned but assignee dead?
│   └── Crash recovery → reset to pending
├── Status = pending, deps all done, not advancing?
│   └── DAG unblock → force advance
└── Status = pending, deps blocked?
    └── Root cause: find the actual stuck upstream task → recurse
```

## 5. Safety Guarantees

1. **No silent data loss**: Every status change is logged to `audit_events`.
2. **No duplicate execution**: `in_progress` → `stale` → `pending` transition is atomic.
3. **Echo override**: Echo can always manually reassign before the auto-scanner acts.
4. **Configurable**: Timeouts are env-var configurable per deployment.
5. **Bounded blast radius**: Recovery only affects the specific agent's tasks, not the entire system.

## 6. Implementation Status

### ✅ Implemented (this round)

```python
# error_recovery.py — standalone module, 180 lines
class ErrorRecoveryManager:
    def scan_stale_tasks(timeout_hours=2)     # → list of stale task IDs
    def recover_dead_agent(agent_name)         # → list of recovered task IDs
    def dag_unblock_scan()                     # → list of unblocked task IDs
    def full_recovery_cycle()                  # → combined report
```

### ⏳ Future Work

- Background thread integration in main.py (behind feature flag)
- Slack/notification integration for critical recoveries
- Auto-reassignment using capability matching after recovery
- ML-based anomaly detection (abnormally long task = likely stuck)
- Circuit breaker pattern (auto-pause new task assignment to chronically failing agents)
