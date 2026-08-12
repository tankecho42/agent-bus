# Agent Team Maturity Model (ATMM)
> Inspired by CMMI — adapted for AI agent teams managed by a coordinator agent

## The Five Levels

### Level 1: Reactive（被动响应）
> *"你叫我做什么，我就做什么"*

- Agents wait for explicit human instructions
- No task queue, no prioritization, no inter-agent communication
- Every task starts from zero — no shared context or memory
- Failures are silent — nobody notices until the human checks
- **Signals**: `echo "do X" → agent does X → done, no follow-up`

### Level 2: Managed（受控）
> *"有规矩，按流程来"*

- Shared communication bus (AgentBus) operational
- Task queue with status tracking (pending → in_progress → done)
- Named agents with registration, API keys, known capabilities
- Operational handbook defines roles and protocols
- Basic audit trail (message history, task transitions)
- **Signals**: Agents can send messages to each other; tasks have lifecycle

### Level 3: Defined（标准）
> *"能力可复制，流程可重放"*

- Standardized task templates for common work types (code review, deploy, monitor)
- Agents can self-activate on a schedule (cron-based wake)
- Cross-session memory — agents recall past work via shared knowledge base
- Documented playbooks for incident response, deployment, QA
- New agents can be onboarded by reading the handbook
- **Signals**: A new agent reads the handbook and starts producing within minutes

### Level 4: Measured（量化）
> *"数据说话"*

- Metrics dashboard: task throughput, completion rate, latency, error rate
- Heartbeat monitoring — dead agents detected automatically
- SLA tracking — tasks have deadlines, overdue alerts fire
- Code quality tracked over time (review findings, fix rate)
- Historical baselines enable anomaly detection
- **Signals**: "Agent X completed 23 tasks this week, avg 45s each, 0 failures"

### Level 5: Optimizing（持续改进）
> *"系统自己变好"*

- Agents identify improvement opportunities from metrics (auto-retro)
- Failed tasks trigger root-cause analysis and playbook updates
- New tooling auto-proposed and auto-integrated (agent suggests a new MCP tool)
- Capacity auto-scales — new agents spawned when queue depth exceeds threshold
- Knowledge base self-organizes — lessons learned feed back into playbooks
- **Signals**: "Queue backed up → system spawned a helper agent → cleared in 3 min → updated playbook to prevent recurrence"

---

## Current Assessment (2026-08-12)

### Our Team: **Level 2 → Level 3 transition (≈2.6/5)**

| Criterion | Level 1 | Level 2 | Level 3 | Level 4 | Level 5 | Our State |
|-----------|---------|---------|---------|---------|---------|-----------|
| Communication | ❌ | ✅ AgentBus | — | — | — | **L2** ✅ |
| Task Management | ❌ | ✅ Task entity | — | — | — | **L2** ✅ |
| Agent Registry | ❌ | ✅ Named agents | — | — | — | **L2** ✅ |
| Audit Trail | ❌ | ✅ Messages + audit_events | — | — | — | **L2** ✅ |
| Self-Activation | — | ❌ | ✅ Cron wake | — | — | **L3** ✅ |
| Cross-Session Memory | — | ❌ | ✅ Obsidian | — | — | **L2.5** ◑️ |
| Playbooks | — | ❌ | ✅ Ops handbook | — | — | **L2.5** ◑️ |
| Heartbeat/Monitoring | — | — | ✅ Code ready | — | — | **L2.3** ◑️ (not deployed) |
| Metrics Dashboard | — | — | ❌ | ✅ Needed | — | **L1** ❌ |
| SLA Tracking | — | — | ❌ | ✅ Needed | — | **L1** ❌ |
| Auto-Retro | — | — | — | ❌ | ✅ | **L1** ❌ |
| Auto-Scale | — | — | — | ❌ | ✅ | **L1** ❌ |

### Strongest signals of L2:
- AgentBus deployed with agents, messages, tasks, audit tables
- CC-macmini can read inbox, do work, report results autonomously
- Operational handbook v1.0 written

### Strongest signals of L3 (partial):
- Cron-based self-activation working (cc-macmini-wake.sh)
- Obsidian knowledge base started but not deeply integrated
- Playbooks exist but are not yet machine-actionable

### Biggest gaps blocking L3:
1. **HK server down** — AgentBus can't be fully deployed → heartbeat untested
2. **CC-HK not activated** — only 2 of 3 planned agents operational
3. **No standardized task templates** — each task is ad-hoc
4. **Obsidian not queryable by agents** — knowledge is write-only

---

## Path to Level 3 (Target: Next 2-3 sessions)

### 3.1 Deploy AgentBus v2 to HK (blocking everything)
- [ ] SSH recovery
- [ ] Run deploy-agentbus-v2.sh
- [ ] Verify heartbeat endpoint
- [ ] Activate CC-HK poll loop

### 3.2 Standardize Task Templates
Create JSON schemas for recurring task types:
```
code_review: { repo, branch, focus_areas, output_format }
deploy:      { service, target, verify_steps, rollback_plan }
monitor:     { service, metrics, alert_threshold, duration }
research:    { topic, depth, output_format, deadline }
```

### 3.3 Agent Knowledge Integration
- Make Obsidian vault readable by agents via CLI (read → context injection)
- Build a `lookup_experience` function that agents call before starting work
- Store every completed task's learnings in structured format

### 3.4 Full Agent Mesh
- Echo (coordinator) ↔ CC-macmini ↔ CC-HK all online
- Cross-agent task delegation tested end-to-end
- Heartbeat-driven liveness alerts

---

## Path to Level 4 (Target: 1-2 weeks)

### 4.1 Metrics Pipeline
- AgentBus `/metrics` endpoint → time-series store (SQLite or Prometheus)
- Weekly digest: tasks completed, avg latency, error rate per agent
- Visual dashboard (even a simple HTML page served from HK)

### 4.2 SLA Framework
- Tasks carry `due_at` with monitoring
- Overdue task → broadcast alert to public channel
- Auto-escalation: if assignee doesn't respond in N minutes, reassign

### 4.3 Quality Trend Tracking
- Code review findings per file over time
- Bug fix rate vs new bug introduction rate
- Deploy success rate

---

## Path to Level 5 (Aspirational)

### 5.1 Auto-Retrospective
After each significant task, an agent writes:
- What went well
- What went wrong
- What playbook update would prevent recurrence
→ Automatically patches the relevant playbook

### 5.2 Capacity Auto-Scale
- Monitor queue depth → if > threshold for > N min → spawn helper agent
- Helper agents are ephemeral: boot, work, commit results, shut down

### 5.3 Self-Proposed Tooling
- Agent notices it's doing a repetitive task → proposes automation
- Agent encounters a gap → suggests a new MCP tool or skill
- Human approves → agent implements → system improves

---

## Summary

```
Level 1 ████████████████████░░░░░░░░░░░░░░░░ (past)
Level 2 ██████████████████████████████████░░░░ (current — solid)
Level 3 ████████████████░░░░░░░░░░░░░░░░░░░░░░ (transitioning)
Level 4 ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ (not started)
Level 5 ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ (aspiration)
```

**One-line verdict**: We have the bones of a Level 2 team with early Level 3 features (self-activation). The HK deploy is the critical path — without it, we can't prove the heartbeat/monitoring layer that distinguishes a real system from a prototype.

**Next concrete action**: Deploy to HK → activate CC-HK → standardize task templates → then start measuring.
