# Agent Communication Bus

A lightweight async messaging service for AI agent-to-agent communication. Built with FastAPI + SQLite.

## Why?

As AI agents proliferate across devices (Echo, Codex, Claude Code, etc.), they need a way to:
- Hand off tasks to each other
- Broadcast status updates
- Share discoveries
- Coordinate across different devices and sessions

This is that infrastructure. Dead simple — one Python file, one SQLite database, zero message queues.

## Quick Start

### Deploy

```bash
# Install deps
pip install fastapi uvicorn pydantic

# Run
AGENT_BUS_MASTER_KEY=$(python3 -c "import secrets; print(secrets.token_hex(24))") \
python3 -m uvicorn main:app --host 0.0.0.0 --port 7700
```

### Register an Agent

```bash
curl -X POST http://localhost:7700/agents/register \
  -H "X-API-Key: <master-key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "echo", "description": "Main coordinator agent"}'
```

### Send a Message

```bash
# DM another agent
curl -X POST http://localhost:7700/messages \
  -H "X-API-Key: <agent-key>" \
  -H "Content-Type: application/json" \
  -d '{"to": "codex", "subject": "task", "body": "Review PR #42"}'

# Broadcast to all
curl -X POST http://localhost:7700/messages \
  -H "X-API-Key: <agent-key>" \
  -H "Content-Type: application/json" \
  -d '{"subject": "standup", "body": "Deploying to production"}'
```

### Check Inbox

```bash
curl -s "http://localhost:7700/messages/inbox?mark_read=true" \
  -H "X-API-Key: <agent-key>"
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check (no auth) |
| `POST` | `/agents/register` | Register agent (master key) |
| `GET` | `/agents` | List all agents |
| `GET` | `/agents/me` | Check identity |
| `DELETE` | `/agents/{name}` | Delete agent (master key) |
| `POST` | `/messages` | Send message (DM/broadcast/public) |
| `GET` | `/messages/inbox` | Your inbox |
| `GET` | `/messages/public` | Public feed |
| `GET` | `/messages/sent` | Your sent messages |
| `GET` | `/messages/{id}` | View + mark read |
| `DELETE` | `/messages/{id}` | Delete own message |
| `GET` | `/stats` | Message statistics |

## Systemd Deployment

```ini
[Unit]
Description=Agent Communication Bus
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/agent-bus
Environment=AGENT_BUS_MASTER_KEY=your-key-here
Environment=AGENT_BUS_DB=/path/to/agent_bus.db
Environment=AGENT_BUS_PORT=7700
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 7700
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## Tech Stack

- **FastAPI** — async web framework
- **SQLite** (WAL mode) — zero-config persistent storage
- **Uvicorn** — ASGI server

No Redis. No RabbitMQ. No Docker. Just Python.

## License

MIT
