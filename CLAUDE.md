# CLAUDE.md — AgentBus Project Guide

## Project Overview
AgentBus is a lightweight async messaging bus for multi-agent communication.
Built with FastAPI + SQLite. Running at http://43.132.211.23:7700 (production).

## Quick Start
```bash
# Start server (development)
python3 -m uvicorn main:app --host 0.0.0.0 --port 7700

# Run tests
python3 -m pytest tests/ -v

# Error recovery scan
python3 error_recovery.py --scan
```

## Architecture
- **main.py** — FastAPI app with all API endpoints (agents, messages, tasks, recovery)
- **error_recovery.py** — Stale task detection, dead agent recovery, DAG unblocking
- **agent_bus.db** — SQLite database (WAL mode)
- **dashboard/** — Team dashboard web UI (static HTML)

## Key Design Decisions

### Per-Agent Read Tracking (R17 Fix)
Messages use a `read_by` JSON array to track which agents have read them.
This is NOT a global `read_at` flag — each agent independently marks messages read.
- `POST /messages/{id}/read` — marks a message read for the calling agent
- `GET /messages/inbox?mark_read=true` — marks returned messages read (merges into read_by, not overwrites)

### Circuit Breaker in Error Recovery
If an agent's tasks are recovered >3 times in 24h, the circuit breaker trips and requires manual intervention (`--force` flag).

### File Lock for Recovery
`recovery_lock()` uses fcntl.flock to prevent concurrent recovery cycles.

## API Authentication
All endpoints require `X-API-Key` header. Keys are per-agent, stored in:
- Production: agent_bus.db on HK server
- Credentials file: ~/.hermes/data/agent_bus_credentials.md (local)

## Testing
Tests use a temporary database and auto-registered test agents.
46 tests covering agents, messages, tasks, DAG dependencies, and error recovery.

## Deployment
1. Push to GitHub (origin: https://github.com/tankecho42/agent-bus.git)
2. SSH to HK: `cd ~/agent-bus && git pull && sudo systemctl restart agent-bus`
3. Verify: `curl http://localhost:7700/health`

## Known Issues
- HK SSH intermittently blocked (fail2ban suspected)
- Local localhost:7700 instance is dev-only, not synced with production
- Dashboard needs CORS (added to main.py)
