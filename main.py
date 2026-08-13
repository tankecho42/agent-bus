"""
Agent Communication Bus — Agent 之间异步消息交换服务
支持：Agent注册、私聊、广播、公共频道、消息查询
"""
import os
import json
import sqlite3
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ── Config ──────────────────────────────────────────────
DB_PATH = os.getenv("AGENT_BUS_DB", "agent_bus.db")
MASTER_KEY = os.getenv("AGENT_BUS_MASTER_KEY", "changeme-on-deploy")
DEFAULT_PORT = int(os.getenv("AGENT_BUS_PORT", "7700"))

# ── Database ────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                id           TEXT PRIMARY KEY,
                name         TEXT UNIQUE NOT NULL,
                description  TEXT DEFAULT '',
                api_key      TEXT NOT NULL,
                created_at   REAL NOT NULL,
                last_seen    REAL,
                capabilities TEXT DEFAULT '[]'    -- JSON array of skill tags, e.g. ["code_review","deploy"]
            );

            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                from_id     TEXT NOT NULL,
                to_id       TEXT,          -- NULL = broadcast to all
                channel     TEXT DEFAULT 'dm',  -- 'dm' | 'broadcast' | 'public'
                subject     TEXT DEFAULT '',
                body        TEXT NOT NULL,
                priority    INTEGER DEFAULT 0,  -- 0=normal, 1=high, 2=urgent
                created_at  REAL NOT NULL,
                read_at     REAL,          -- first read timestamp
                read_by     TEXT,          -- JSON array of agent IDs that read it
                reply_to    TEXT,          -- message ID this replies to
                FOREIGN KEY (from_id) REFERENCES agents(id),
                FOREIGN KEY (to_id) REFERENCES agents(id)
            );

            CREATE INDEX IF NOT EXISTS idx_msg_to      ON messages(to_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_msg_channel ON messages(channel, created_at);
            CREATE INDEX IF NOT EXISTS idx_msg_from    ON messages(from_id, created_at);

            -- ── Tasks table (v2 Phase 2 + DAG Phase 3) ────────
            CREATE TABLE IF NOT EXISTS tasks (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                description TEXT DEFAULT '',
                status      TEXT DEFAULT 'pending',   -- pending|assigned|in_progress|review|done|failed
                priority    INTEGER DEFAULT 0,        -- 0=normal, 1=high, 2=urgent
                assignee    TEXT,                     -- agent name or id
                created_by  TEXT NOT NULL,            -- agent id of creator
                created_at  REAL NOT NULL,
                updated_at  REAL,
                assigned_at REAL,
                started_at  REAL,
                completed_at REAL,
                due_at      REAL,                     -- optional deadline
                tags        TEXT DEFAULT '[]',        -- JSON array of tags
                result      TEXT DEFAULT '',          -- completion notes / output
                depends_on  TEXT DEFAULT '[]',        -- JSON array of task IDs (DAG dependencies)
                auto_advance INTEGER DEFAULT 0,       -- 1=auto-activate when deps are done
                FOREIGN KEY (assignee) REFERENCES agents(id),
                FOREIGN KEY (created_by) REFERENCES agents(id)
            );

            CREATE INDEX IF NOT EXISTS idx_task_status   ON tasks(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_task_assignee ON tasks(assignee, status);

            -- ── Audit events table (v2 Phase 1) ──────────────
            CREATE TABLE IF NOT EXISTS audit_events (
                id           TEXT PRIMARY KEY,
                timestamp    REAL NOT NULL,
                actor_id     TEXT,
                actor_name   TEXT,
                action       TEXT NOT NULL,
                entity_type  TEXT,
                entity_id    TEXT,
                changes      TEXT DEFAULT '{}',
                context      TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_events(actor_id);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);
            """)

init_db()

# ── Migrations (additive columns for existing DBs) ──────
def _migrate():
    with get_db() as conn:
        # agents.capabilities
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
        if "capabilities" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN capabilities TEXT DEFAULT '[]'")

        # tasks.depends_on, tasks.auto_advance
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "depends_on" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN depends_on TEXT DEFAULT '[]'")
        if "auto_advance" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN auto_advance INTEGER DEFAULT 0")

_migrate()

# ── Auth ────────────────────────────────────────────────
def resolve_agent(x_api_key: Optional[str] = Header(None)) -> dict:
    """从 API key 解析当前 agent。master key 可模拟任意 agent（用 ?as=agent_id）。"""
    if not x_api_key:
        raise HTTPException(401, "Missing X-API-Key header")
    if x_api_key == MASTER_KEY:
        # master key — 需要配合 ?as= 参数
        raise HTTPException(401, "Master key requires ?as=<agent_id>")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agents WHERE api_key = ?", (x_api_key,)).fetchone()
    if not row:
        raise HTTPException(401, "Invalid API key")
    # update last_seen
    with get_db() as conn:
        conn.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (time.time(), row["id"]))
    return dict(row)

def require_master(x_api_key: Optional[str] = Header(None)):
    """仅允许 master key 调用的管理接口。"""
    if x_api_key != MASTER_KEY:
        raise HTTPException(403, "Master key required")
    return True

# ── Models ──────────────────────────────────────────────
class AgentRegister(BaseModel):
    name: str = Field(..., description="Agent 唯一名称，如 echo / codex / claude-code")
    description: str = ""
    capabilities: list = Field(default=[], description='技能标签列表，如 ["code_review","deploy","patrol"]')

class AgentInfo(BaseModel):
    id: str
    name: str
    description: str
    created_at: float
    last_seen: Optional[float] = None

class MessageSend(BaseModel):
    to: Optional[str] = Field(None, description="目标 agent 的 name 或 id。留空=广播")
    channel: str = Field("dm", description="dm | broadcast | public")
    subject: str = ""
    body: str
    priority: int = 0
    reply_to: Optional[str] = None

# ── App ─────────────────────────────────────────────────
app = FastAPI(title="Agent Communication Bus", version="1.0.0")

# CORS — allow dashboard and other frontend apps
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount dashboard as static files at /dashboard — same origin, no CORS issues
from fastapi.staticfiles import StaticFiles
_dashboard_dir = os.path.join(os.path.dirname(__file__), "dashboard")
if os.path.isdir(_dashboard_dir):
    app.mount("/dashboard", StaticFiles(directory=_dashboard_dir, html=True), name="dashboard")

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

# ── Agent 管理 ──────────────────────────────────────────
@app.post("/agents/register")
def register_agent(payload: AgentRegister, _=Depends(require_master)):
    """注册新 Agent（仅 master key）。返回生成的 api_key。"""
    api_key = f"ab_{uuid.uuid4().hex[:24]}"
    agent_id = f"ag_{uuid.uuid4().hex[:12]}"
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO agents (id, name, description, api_key, created_at, capabilities) VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, payload.name, payload.description, api_key, time.time(), json.dumps(payload.capabilities))
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Agent '{payload.name}' already exists")
    return {"agent_id": agent_id, "name": payload.name, "api_key": api_key}

@app.get("/agents")
def list_agents(agent: dict = Depends(resolve_agent)):
    """列出所有已注册的 Agent（含能力声明）。"""
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, description, created_at, last_seen, capabilities FROM agents ORDER BY name").fetchall()
    return {"agents": [dict(r) for r in rows]}

@app.get("/agents/capabilities")
def find_by_capability(
    skill: Optional[str] = Query(None, description="按技能过滤，如 code_review"),
    agent: dict = Depends(resolve_agent),
):
    """能力发现端点：查找拥有指定技能的 Agent。

    GET /agents/capabilities              → 所有 Agent 及其能力
    GET /agents/capabilities?skill=deploy → 返回拥有 deploy 能力的 Agent
    """
    with get_db() as conn:
        if skill:
            # LIKE-based filter on JSON array — works for SQLite without JSON1 extension
            rows = conn.execute(
                """SELECT id, name, description, capabilities, last_seen
                   FROM agents WHERE capabilities LIKE ? ORDER BY name""",
                (f'%"{skill}"%',)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, description, capabilities, last_seen FROM agents ORDER BY name"
            ).fetchall()
    return {"skill_filter": skill, "agents": [dict(r) for r in rows], "count": len(rows)}

@app.get("/agents/me")
def whoami(agent: dict = Depends(resolve_agent)):
    """查看自己的身份。"""
    return {"id": agent["id"], "name": agent["name"], "description": agent["description"], "capabilities": json.loads(agent.get("capabilities") or "[]")}

@app.patch("/agents/me/capabilities")
def update_capabilities(payload: dict = Body(...), agent: dict = Depends(resolve_agent)):
    """Agent 自主声明或更新自己的能力列表。"""
    skills = payload.get("skills", [])
    if not isinstance(skills, list):
        raise HTTPException(400, "skills must be a list of strings")
    with get_db() as conn:
        conn.execute(
            "UPDATE agents SET capabilities = ? WHERE id = ?",
            (json.dumps(skills), agent["id"])
        )
    return {"agent": agent["name"], "capabilities": skills}

@app.delete("/agents/{name}")
def delete_agent(name: str, _=Depends(require_master)):
    with get_db() as conn:
        row = conn.execute("SELECT id FROM agents WHERE name = ?", (name,)).fetchone()
        if not row:
            raise HTTPException(404, "Agent not found")
        # cascade: clean up messages, tasks, and audit events referencing this agent
        conn.execute("DELETE FROM messages WHERE from_id = ? OR to_id = ?", (row["id"], row["id"]))
        conn.execute("UPDATE tasks SET assignee = NULL WHERE assignee = ?", (row["id"],))
        conn.execute("DELETE FROM agents WHERE id = ?", (row["id"],))
    return {"deleted": name}

# ── Agent 心跳 (v2 Phase 1) ─────────────────────────────
@app.post("/agents/me/heartbeat")
def heartbeat(payload: dict = Body(default={}), agent: dict = Depends(resolve_agent)):
    """Agent 心跳上报。更新 last_seen，记录系统信息。"""
    now = time.time()
    hostname = payload.get("hostname", "")
    load_avg = payload.get("load_avg")
    current_task = payload.get("current_task_id")
    
    with get_db() as conn:
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE id = ?",
            (now, agent["id"])
        )
        # Log heartbeat to metrics
        conn.execute(
            "INSERT INTO audit_events (id, timestamp, actor_id, actor_name, action, entity_type, entity_id, changes, context) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"ae_{uuid.uuid4().hex[:16]}",
                now,
                agent["id"],
                agent["name"],
                "heartbeat",
                "agent",
                agent["id"],
                json.dumps({"hostname": hostname, "load_avg": load_avg, "current_task": current_task}),
                json.dumps({})
            )
        )
    return {"status": "ok", "timestamp": now}

@app.get("/agents/health")
def agents_health(agent: dict = Depends(resolve_agent)):
    """所有 Agent 健康状态概览。"""
    now = time.time()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, description, created_at, last_seen FROM agents ORDER BY name"
        ).fetchall()
    
    result = []
    for r in rows:
        last = r["last_seen"] or 0
        elapsed = now - last
        if elapsed < 1800:      # < 30min
            status = "active"
        elif elapsed < 5400:    # < 90min
            status = "stale"
        elif last == 0:
            status = "unknown"
        else:
            status = "dead"
        
        result.append({
            "id": r["id"],
            "name": r["name"],
            "status": status,
            "last_seen": last,
            "last_seen_ago_sec": int(elapsed) if last else None
        })
    return {"agents": result, "checked_at": now}

# ── 消息收发 ────────────────────────────────────────────
@app.post("/messages")
def send_message(payload: MessageSend, agent: dict = Depends(resolve_agent)):
    """发送消息。to 为空=广播给所有人；channel=public=发到公共频道。"""
    to_id = None
    channel = payload.channel

    if payload.to:
        # 解析目标：可能是 name 或 id
        with get_db() as conn:
            target = conn.execute(
                "SELECT id FROM agents WHERE name = ? OR id = ?", (payload.to, payload.to)
            ).fetchone()
        if not target:
            raise HTTPException(404, f"Recipient '{payload.to}' not found")
        to_id = target["id"]
        channel = "dm"
    elif channel == "public":
        to_id = None
        channel = "public"
    else:
        # 没 to + 没指定 public → broadcast
        channel = "broadcast"
        to_id = None

    msg_id = f"msg_{uuid.uuid4().hex[:16]}"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO messages
               (id, from_id, to_id, channel, subject, body, priority, created_at, read_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, agent["id"], to_id, channel, payload.subject, payload.body,
             payload.priority, time.time(), json.dumps([]))
        )
    return {"id": msg_id, "status": "sent", "channel": channel}

@app.get("/messages/inbox")
def get_inbox(
    agent: dict = Depends(resolve_agent),
    limit: int = Query(50, le=200),
    offset: int = 0,
    unread_only: bool = False,
    mark_read: bool = False,
):
    """获取发给我的消息（DM + 广播）。不含公共频道。"""
    with get_db() as conn:
        query = """
            SELECT m.*, a.name AS from_name
            FROM messages m JOIN agents a ON m.from_id = a.id
            WHERE (m.to_id = ? OR m.channel = 'broadcast')
        """
        params = [agent["id"]]
        if unread_only:
            # Per-agent unread: a message is unread for THIS agent if its id
            # is NOT in read_by JSON array for this agent. Also exclude messages
            # the agent itself sent (don't show own broadcasts as unread).
            query += """ AND m.from_id != ?
                AND (
                    m.read_by IS NULL
                    OR m.read_by = '[]'
                    OR json_extract(m.read_by, '$') NOT LIKE ?
                )"""
            agent_pattern = f'%"{agent["id"]}"%'
            params = [agent["id"], agent["id"], agent_pattern]
        query += " ORDER BY m.priority DESC, m.created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn.execute(query, params).fetchall()

        if mark_read and rows:
            now = time.time()
            msg_ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(msg_ids))
            # Per-agent read tracking: merge current agent into each message's read_by array
            # instead of overwriting. This fixes the bug where multi-agent reads clobber each other.
            for mid in msg_ids:
                row = conn.execute("SELECT read_by FROM messages WHERE id = ?", (mid,)).fetchone()
                if row:
                    try:
                        readers = json.loads(row["read_by"] or "[]")
                    except (json.JSONDecodeError, TypeError):
                        readers = []
                    if agent["id"] not in readers:
                        readers.append(agent["id"])
                    conn.execute(
                        "UPDATE messages SET read_at = COALESCE(read_at, ?), read_by = ? WHERE id = ?",
                        (now, json.dumps(readers), mid)
                    )

    return {"messages": [dict(r) for r in rows], "count": len(rows)}

@app.get("/messages/public")
def get_public_feed(
    agent: dict = Depends(resolve_agent),
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """公共频道——所有人可见的帖子流。"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.*, a.name AS from_name
               FROM messages m JOIN agents a ON m.from_id = a.id
               WHERE m.channel = 'public'
               ORDER BY m.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
    return {"messages": [dict(r) for r in rows], "count": len(rows)}

@app.get("/messages/sent")
def get_sent(
    agent: dict = Depends(resolve_agent),
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """查看我发出去的消息。"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.*,
               sf.name AS from_name,
               st.name AS to_name
               FROM messages m
               JOIN agents sf ON m.from_id = sf.id
               LEFT JOIN agents st ON m.to_id = st.id
               WHERE m.from_id = ?
               ORDER BY m.created_at DESC LIMIT ? OFFSET ?""",
            (agent["id"], limit, offset)
        ).fetchall()
    return {"messages": [dict(r) for r in rows], "count": len(rows)}

@app.get("/messages/{msg_id}")
def get_message(msg_id: str, agent: dict = Depends(resolve_agent)):
    """查看单条消息详情。"""
    with get_db() as conn:
        row = conn.execute(
            """SELECT m.*, sf.name AS from_name, st.name AS to_name
               FROM messages m
               JOIN agents sf ON m.from_id = sf.id
               LEFT JOIN agents st ON m.to_id = st.id
               WHERE m.id = ?""",
            (msg_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Message not found")

    # 权限检查：只能看自己发的、发给自己的、广播、或公共频道
    d = dict(row)
    if d["from_id"] != agent["id"] and d["to_id"] != agent["id"] and d["channel"] not in ("broadcast", "public"):
        raise HTTPException(403, "Not authorized to read this message")

    # mark read
    read_list = json.loads(d.get("read_by") or "[]")
    if agent["id"] not in read_list:
        read_list.append(agent["id"])
        with get_db() as conn:
            conn.execute(
                "UPDATE messages SET read_at = COALESCE(read_at, ?), read_by = ? WHERE id = ?",
                (time.time(), json.dumps(read_list), msg_id)
            )
        d["read_at"] = d.get("read_at") or time.time()
        d["read_by"] = read_list
    return d

@app.post("/messages/{msg_id}/read")
def mark_message_read(msg_id: str, agent: dict = Depends(resolve_agent)):
    """标记单条消息为已读（per-agent）。修复 R17：CC agents 调这个端点不再 404。"""
    with get_db() as conn:
        row = conn.execute("SELECT id, read_at, read_by FROM messages WHERE id = ?", (msg_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Message not found")

        try:
            readers = json.loads(row["read_by"] or "[]")
        except (json.JSONDecodeError, TypeError):
            readers = []

        if agent["id"] not in readers:
            readers.append(agent["id"])
            now = time.time()
            conn.execute(
                "UPDATE messages SET read_at = COALESCE(read_at, ?), read_by = ? WHERE id = ?",
                (now, json.dumps(readers), msg_id)
            )
        else:
            now = row["read_at"] or time.time()

    return {"id": msg_id, "read": True, "read_at": now, "read_by": readers}

@app.delete("/messages/{msg_id}")
def delete_message(msg_id: str, agent: dict = Depends(resolve_agent)):
    """删除消息（仅发送者可删）。"""
    with get_db() as conn:
        row = conn.execute("SELECT from_id FROM messages WHERE id = ?", (msg_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Message not found")
        if row["from_id"] != agent["id"]:
            raise HTTPException(403, "Only the sender can delete a message")
        conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    return {"deleted": msg_id}

# ── Stats ───────────────────────────────────────────────
@app.get("/stats")
def get_stats(agent: dict = Depends(resolve_agent)):
    """查看消息统计。"""
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        dm = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE channel = 'dm'").fetchone()["c"]
        broadcast = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE channel = 'broadcast'").fetchone()["c"]
        public = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE channel = 'public'").fetchone()["c"]
        agents = conn.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
        my_unread = conn.execute(
            """SELECT COUNT(*) AS c FROM messages
               WHERE (to_id = ? OR channel = 'broadcast')
                 AND from_id != ?
                 AND (
                     read_by IS NULL OR read_by = '[]'
                     OR json_extract(read_by, '$') NOT LIKE ?
                 )""",
            (agent["id"], agent["id"], f'%"{agent["id"]}"%')
        ).fetchone()["c"]
    return {
        "agents": agents,
        "messages_total": total,
        "dm": dm,
        "broadcast": broadcast,
        "public": public,
        "my_unread": my_unread,
    }

@app.get("/metrics")
def get_metrics(agent: dict = Depends(resolve_agent)):
    """Dashboard 指标端点 — 供 /dashboard 前端 renderStats 消费。

    返回字段对齐 index.html 的 renderStats() 期望: total / last_24h / unread_total。
    之前 dashboard 调 /metrics 但后端无此端点 → 404 → Promise.all 整体失败 →
    前端抛出含 "CORS headers" 字样的错误, 被误判为跨域问题 (实为端点缺失)。
    """
    cutoff = time.time() - 86400  # 24h ago (messages.created_at 是 Unix epoch 秒)
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        last_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE created_at >= ?", (cutoff,)
        ).fetchone()["c"]
        unread_total = conn.execute(
            """SELECT COUNT(*) AS c FROM messages
               WHERE (to_id = ? OR channel = 'broadcast')
                 AND from_id != ?
                 AND (
                     read_by IS NULL OR read_by = '[]'
                     OR json_extract(read_by, '$') NOT LIKE ?
                 )""",
            (agent["id"], agent["id"], f'%"{agent["id"]}"%'),
        ).fetchone()["c"]
    return {
        "total": total,
        "last_24h": last_24h,
        "unread_total": unread_total,
    }

@app.get("/stats/timeline")
def stats_timeline(agent: dict = Depends(resolve_agent)):
    """过去 24 小时每小时消息数 — 供 dashboard 趋势图。

    返回 24 个整点桶(含 count=0 的空桶), 方便前端直接画柱状/折线。
    """
    now = time.time()
    start = now - 24 * 3600
    first_hour = int(start - (start % 3600))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT created_at FROM messages WHERE created_at >= ?", (start,)
        ).fetchall()
    buckets = {}
    for r in rows:
        hour = int(r["created_at"] - (r["created_at"] % 3600))
        buckets[hour] = buckets.get(hour, 0) + 1
    timeline = [
        {"hour": first_hour + i * 3600, "count": buckets.get(first_hour + i * 3600, 0)}
        for i in range(24)
    ]
    return {"timeline": timeline, "total_24h": len(rows), "bucket_size": 3600}


@app.get("/stats/activity")
def stats_activity(
    rng: str = Query("24h", alias="range", description="24h | 7d | 30d"),
    agent: Optional[str] = Query(None, description="按发件人 agent name/id 过滤; 留空=全部"),
    tz: float = Query(8, description="查看者时区偏移(小时), 用于按本地日历切天/切小时"),
    requester: dict = Depends(resolve_agent),
):
    """Dashboard 活动统计 — 时间维度柱状图 + GitHub 风格热力图。

    返回:
      - volume: 时间维度桶 (24h→每小时 24 桶, 7d/30d→每天), 供柱状图
      - heatmap: 每天单元格 (至少 7 天; 24h 时展示最近 7 天), dow=周一0..周日6, 供 Mon-Sun 热力图
      - total: 所选 range 内的消息数 (volume 桶之和)

    所有桶按 tz 时区切分 (前端传浏览器偏移), 与服务器进程时区解耦。
    统计口径=发送者 (from_id), 与 GitHub commit 语义一致。
    注: URL 参数名仍为 range (Query alias), Python 变量名用 rng 以免遮蔽内置 range()。
    """
    now = time.time()
    RANGES = {
        "24h": {"span": 24 * 3600, "gran": "hour", "heat_days": 7},
        "7d":  {"span": 7 * 86400,  "gran": "day",  "heat_days": 7},
        "30d": {"span": 30 * 86400, "gran": "day",  "heat_days": 30},
    }
    cfg = RANGES.get(rng, RANGES["24h"])
    span, gran, heat_days = cfg["span"], cfg["gran"], cfg["heat_days"]
    off = tz * 3600  # 时区秒偏移

    vol_start = now - span
    heat_start = now - heat_days * 86400
    fetch_start = min(vol_start, heat_start)

    # 解析发件人过滤
    agent_id, agent_name = None, None
    if agent:
        with get_db() as conn:
            a = conn.execute(
                "SELECT id, name FROM agents WHERE name = ? OR id = ?", (agent, agent)
            ).fetchone()
        if not a:
            raise HTTPException(404, f"Agent '{agent}' not found")
        agent_id, agent_name = a["id"], a["name"]

    with get_db() as conn:
        rows = conn.execute(
            "SELECT created_at, from_id FROM messages WHERE created_at >= ?", (fetch_start,)
        ).fetchall()
    if agent_id:
        rows = [r for r in rows if r["from_id"] == agent_id]

    def day_floor(ts):
        local = ts + off
        return local - (local % 86400) - off
    def hour_floor(ts):
        local = ts + off
        return local - (local % 3600) - off
    def fmt(ts, f):
        return time.strftime(f, time.gmtime(ts + off))

    # Volume series (按所选 range)
    volume = []
    if gran == "hour":
        first = hour_floor(vol_start)
        buckets = {}
        for r in rows:
            if r["created_at"] >= vol_start:
                h = hour_floor(r["created_at"])
                buckets[h] = buckets.get(h, 0) + 1
        for i in range(24):
            ts = first + i * 3600
            volume.append({"ts": ts, "label": fmt(ts, "%H:%M"), "count": buckets.get(ts, 0)})
    else:
        buckets = {}
        for r in rows:
            if r["created_at"] >= vol_start:
                d = day_floor(r["created_at"])
                buckets[d] = buckets.get(d, 0) + 1
        n_days = int(round(span / 86400))
        d0 = day_floor(vol_start)
        for i in range(n_days):
            ts = d0 + i * 86400
            volume.append({"ts": ts, "label": fmt(ts, "%m-%d"), "count": buckets.get(ts, 0)})

    # Heatmap cells (每日, heat_days 天)
    hbuckets = {}
    for r in rows:
        d = day_floor(r["created_at"])
        hbuckets[d] = hbuckets.get(d, 0) + 1
    cells = []
    hd0 = day_floor(heat_start)
    for i in range(heat_days):
        ts = hd0 + i * 86400
        lt = time.gmtime(ts + off)
        cells.append({
            "ts": ts,
            "date": time.strftime("%Y-%m-%d", lt),
            "dow": lt.tm_wday,   # 周一=0 .. 周日=6
            "count": hbuckets.get(ts, 0),
        })

    return {
        "range": rng,
        "agent": agent_name,
        "agent_id": agent_id,
        "granularity": gran,
        "volume": volume,
        "heatmap": {"days": heat_days, "cells": cells},
        "total": sum(v["count"] for v in volume),
        "now": now,
    }


@app.post("/messages/read-all")
def read_all_messages(agent: dict = Depends(resolve_agent)):
    """标记当前 agent 的所有未读消息为已读 (per-agent read_by, 不影响他人)。"""
    now = time.time()
    pattern = f'%"{agent["id"]}"%'
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, read_by FROM messages
               WHERE (to_id = ? OR channel = 'broadcast')
                 AND from_id != ?
                 AND (read_by IS NULL OR read_by = '[]'
                      OR json_extract(read_by, '$') NOT LIKE ?)""",
            (agent["id"], agent["id"], pattern),
        ).fetchall()
        for r in rows:
            try:
                readers = json.loads(r["read_by"] or "[]")
            except (json.JSONDecodeError, TypeError):
                readers = []
            if agent["id"] not in readers:
                readers.append(agent["id"])
            conn.execute(
                "UPDATE messages SET read_at = COALESCE(read_at, ?), read_by = ? WHERE id = ?",
                (now, json.dumps(readers), r["id"]),
            )
    return {"marked_read": len(rows)}


# ── Task Models ─────────────────────────────────────────
class TaskCreate(BaseModel):
    title: str
    description: str = ""
    assignee: Optional[str] = None  # agent name or id
    priority: int = 0
    due_at: Optional[float] = None
    tags: list = []
    depends_on: list = Field(default=[], description="上游依赖的 task IDs")
    auto_advance: bool = Field(False, description="上游全部 done 时自动从 pending 变为 assigned")

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None  # pending|assigned|in_progress|review|done|failed
    assignee: Optional[str] = None
    priority: Optional[int] = None
    due_at: Optional[float] = None
    tags: Optional[list] = None
    result: Optional[str] = None
    depends_on: Optional[list] = None
    auto_advance: Optional[bool] = None

VALID_STATUSES = {"pending", "assigned", "in_progress", "review", "done", "failed"}

def _resolve_assignee(conn, assignee: Optional[str]) -> Optional[str]:
    """Resolve agent name/id to agent id. Returns None if unassigned."""
    if not assignee:
        return None
    row = conn.execute("SELECT id FROM agents WHERE name = ? OR id = ?", (assignee, assignee)).fetchone()
    if not row:
        raise HTTPException(404, f"Agent '{assignee}' not found")
    return row["id"]

def advance_ready_tasks(conn, completed_task_id: str):
    """DAG engine: when a task is completed, auto-advance downstream tasks whose dependencies are all met.

    Called after any task transitions to 'done'. For each task that:
      1. Has auto_advance=1
      2. Is currently 'pending'
      3. Has the completed task in its depends_on list
      4. All other dependencies are also 'done'
    → transition it to 'assigned' (if it has an assignee) or keep pending with deps_met=true.

    Returns list of newly advanced task IDs.
    """
    now = time.time()
    advanced = []

    # Find all tasks that depend on the completed task
    # Since depends_on is JSON text, use LIKE to find candidates, then verify precisely
    candidates = conn.execute(
        """SELECT id, depends_on, auto_advance, status, assignee
           FROM tasks WHERE depends_on LIKE ? AND auto_advance = 1 AND status = 'pending'""",
        (f'%{completed_task_id}%',)
    ).fetchall()

    for task in candidates:
        deps = json.loads(task["depends_on"] or "[]")
        if completed_task_id not in deps:
            continue

        # Check ALL dependencies are done
        all_met = True
        for dep_id in deps:
            dep = conn.execute("SELECT status FROM tasks WHERE id = ?", (dep_id,)).fetchone()
            if not dep or dep["status"] != "done":
                all_met = False
                break

        if all_met and task["assignee"]:
            conn.execute(
                "UPDATE tasks SET status = 'assigned', assigned_at = ?, updated_at = ? WHERE id = ?",
                (now, now, task["id"])
            )
            advanced.append(task["id"])

    return advanced

def detect_cycle(conn, task_id: str, depends_on: list) -> tuple:
    """Check whether setting task_id.depends_on = depends_on would create a cycle in the DAG.

    A cycle exists if task_id is reachable by following the dependency chain
    starting from any task in depends_on.  More precisely: if any dep in
    depends_on transitively depends on task_id, we'd have a loop.

    Returns (has_cycle: bool, cycle_path: list[str] | None).
    """
    if not depends_on:
        return (False, None)

    # Build adjacency map: task → its depends_on list
    # We need to check if adding edges (task_id → dep) for each dep creates a cycle.
    # A cycle occurs if task_id is already reachable from any dep by following
    # reverse edges (i.e., dep depends on ... depends on task_id).
    # Equivalently: starting from task_id, follow depends_on edges; if we reach
    # task_id itself, there's a cycle.
    #
    # But task_id might not exist yet (create). So we simulate:
    # Build graph from existing tasks + the proposed edges, then DFS from task_id.

    # Load all existing task→depends_on edges
    all_tasks = conn.execute("SELECT id, depends_on FROM tasks").fetchall()
    graph = {}
    for t in all_tasks:
        graph[t["id"]] = json.loads(t["depends_on"] or "[]")

    # Add/update the proposed edges
    graph[task_id] = list(depends_on)

    # DFS from task_id — if we revisit task_id, there's a cycle
    visited = set()
    rec_stack = set()

    def dfs(node, path):
        if node in rec_stack:
            # Found cycle — build path from first occurrence
            idx = path.index(node)
            return path[idx:] + [node]
        if node in visited:
            return None
        visited.add(node)
        rec_stack.add(node)
        path.append(node)
        for neighbor in graph.get(node, []):
            result = dfs(neighbor, list(path))
            if result is not None:
                return result
        rec_stack.discard(node)
        return None

    cycle_path = dfs(task_id, [])
    if cycle_path:
        return (True, cycle_path)
    return (False, None)


# ── Task Endpoints ──────────────────────────────────────
@app.post("/tasks")
def create_task(payload: TaskCreate, agent: dict = Depends(resolve_agent)):
    """Create a new task. Requires authentication."""
    task_id = f"task_{uuid.uuid4().hex[:16]}"
    now = time.time()
    with get_db() as conn:
        assignee_id = _resolve_assignee(conn, payload.assignee)

        # Cycle detection: reject if depends_on would create a circular dependency
        if payload.depends_on:
            has_cycle, cycle_path = detect_cycle(conn, task_id, payload.depends_on)
            if has_cycle:
                raise HTTPException(400, f"Circular dependency detected: {' → '.join(cycle_path)}")

        # If task has unmet dependencies, it starts as pending regardless of assignee
        deps_met = True
        if payload.depends_on:
            for dep_id in payload.depends_on:
                dep = conn.execute("SELECT status FROM tasks WHERE id = ?", (dep_id,)).fetchone()
                if not dep or dep["status"] != "done":
                    deps_met = False
                    break
        status = "assigned" if (assignee_id and deps_met) else "pending"
        conn.execute(
            """INSERT INTO tasks
               (id, title, description, status, priority, assignee, created_by,
                created_at, updated_at, assigned_at, due_at, tags, result, depends_on, auto_advance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, payload.title, payload.description, status, payload.priority,
             assignee_id, agent["id"], now, now,
             now if (assignee_id and deps_met) else None, payload.due_at,
             json.dumps(payload.tags), "", json.dumps(payload.depends_on),
             1 if payload.auto_advance else 0)
        )
    return {"id": task_id, "status": status, "title": payload.title}

@app.get("/tasks")
def list_tasks(
    agent: dict = Depends(resolve_agent),
    status: Optional[str] = Query(None),
    assignee: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """List tasks. Filter by status and/or assignee."""
    query = """
        SELECT t.*, a.name AS assignee_name, c.name AS created_by_name
        FROM tasks t
        LEFT JOIN agents a ON t.assignee = a.id
        JOIN agents c ON t.created_by = c.id
        WHERE 1=1
    """
    params = []
    if status:
        query += " AND t.status = ?"
        params.append(status)
    if assignee:
        query += " AND (t.assignee = ? OR a.name = ?)"
        params += [assignee, assignee]
    query += " ORDER BY t.priority DESC, t.created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"tasks": [dict(r) for r in rows], "count": len(rows)}

@app.get("/tasks/{task_id}")
def get_task(task_id: str, agent: dict = Depends(resolve_agent)):
    """Get a single task by ID."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT t.*, a.name AS assignee_name, c.name AS created_by_name
               FROM tasks t
               LEFT JOIN agents a ON t.assignee = a.id
               JOIN agents c ON t.created_by = c.id
               WHERE t.id = ?""",
            (task_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Task not found")
    return dict(row)

@app.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: TaskUpdate, agent: dict = Depends(resolve_agent)):
    """Update a task. Any agent can update any task (cooperative model)."""
    now = time.time()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Task not found")

        updates = {}
        if payload.status is not None:
            if payload.status not in VALID_STATUSES:
                raise HTTPException(400, f"Invalid status. Must be one of: {VALID_STATUSES}")
            updates["status"] = payload.status
            if payload.status == "in_progress" and not row["started_at"]:
                updates["started_at"] = now
            elif payload.status in ("done", "failed") and not row["completed_at"]:
                updates["completed_at"] = now
        if payload.assignee is not None:
            assignee_id = _resolve_assignee(conn, payload.assignee)
            updates["assignee"] = assignee_id
            if assignee_id and row["status"] == "pending":
                updates["status"] = "assigned"
                updates["assigned_at"] = now
        if payload.title is not None:
            updates["title"] = payload.title
        if payload.description is not None:
            updates["description"] = payload.description
        if payload.priority is not None:
            updates["priority"] = payload.priority
        if payload.due_at is not None:
            updates["due_at"] = payload.due_at
        if payload.tags is not None:
            updates["tags"] = json.dumps(payload.tags)
        if payload.result is not None:
            updates["result"] = payload.result
        if payload.depends_on is not None:
            # Cycle detection on update
            has_cycle, cycle_path = detect_cycle(conn, task_id, payload.depends_on)
            if has_cycle:
                raise HTTPException(400, f"Circular dependency detected: {' → '.join(cycle_path)}")
            updates["depends_on"] = json.dumps(payload.depends_on)
        if payload.auto_advance is not None:
            updates["auto_advance"] = 1 if payload.auto_advance else 0

        updates["updated_at"] = now
        # Defense-in-depth: whitelist allowed column names
        ALLOWED_FIELDS = {"title", "description", "priority", "due_at", "tags",
                         "result", "status", "assignee", "updated_at",
                         "depends_on", "auto_advance", "started_at", "completed_at", "assigned_at"}
        safe_updates = {k: v for k, v in updates.items() if k in ALLOWED_FIELDS}
        set_clause = ", ".join(f"\"{k}\" = ?" for k in safe_updates)
        values = list(safe_updates.values()) + [task_id]
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)

        # DAG engine: if task just became 'done', auto-advance downstream tasks
        dag_result = []
        if payload.status == "done":
            dag_result = advance_ready_tasks(conn, task_id)

    return {"id": task_id, "updated_fields": list(updates.keys()), "updated_at": now, "dag_advanced": dag_result}

@app.get("/tasks/my/active")
def my_active_tasks(agent: dict = Depends(resolve_agent)):
    """Get all tasks assigned to me that are not done/failed."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT t.*, c.name AS created_by_name
               FROM tasks t
               JOIN agents c ON t.created_by = c.id
               WHERE t.assignee = ? AND t.status NOT IN ('done', 'failed')
               ORDER BY t.priority DESC, t.created_at""",
            (agent["id"],)
        ).fetchall()
    return {"tasks": [dict(r) for r in rows], "count": len(rows)}

# ── Error Recovery Endpoints (master key only) ──────────

TASK_TIMEOUT_HOURS = float(os.getenv("TASK_TIMEOUT_HOURS", "2"))
HEARTBEAT_DEAD_THRESHOLD = int(os.getenv("HEARTBEAT_DEAD_THRESHOLD", "5400"))  # 90 min

@app.post("/recovery/scan")
def recovery_scan(
    dry_run: bool = Query(True, description="If true, report only without making changes"),
    _=Depends(require_master),
):
    """Scan for stale tasks and dead agents. Master key required.

    - dry_run=true (default): Report only, no state changes.
    - dry_run=false: Execute full recovery cycle (mark stale, recover dead agents, unblock DAGs).
    """
    now = time.time()
    stale_threshold = now - (TASK_TIMEOUT_HOURS * 3600)
    dead_threshold = now - HEARTBEAT_DEAD_THRESHOLD

    result = {
        "timestamp": now,
        "dry_run": dry_run,
        "stale_tasks": [],
        "dead_agents": [],
        "dag_unblocked": [],
        "actions_taken": [],
    }

    with get_db() as conn:
        # 1. Scan stale tasks (in_progress longer than TASK_TIMEOUT_HOURS)
        stale_rows = conn.execute(
            """SELECT t.id, t.title, t.assignee, t.started_at,
                      a.name AS assignee_name
               FROM tasks t
               LEFT JOIN agents a ON t.assignee = a.id
               WHERE t.status = 'in_progress'
                 AND t.started_at IS NOT NULL
                 AND t.started_at < ?""",
            (stale_threshold,)
        ).fetchall()

        for r in stale_rows:
            age_h = (now - r["started_at"]) / 3600
            result["stale_tasks"].append({
                "task_id": r["id"],
                "title": r["title"],
                "assignee": r["assignee_name"] or "unassigned",
                "age_hours": round(age_h, 1),
            })

        # 2. Scan dead agents (heartbeat older than HEARTBEAT_DEAD_THRESHOLD)
        dead_rows = conn.execute(
            """SELECT id, name, last_seen
               FROM agents
               WHERE last_seen IS NOT NULL AND last_seen < ?
               ORDER BY last_seen""",
            (dead_threshold,)
        ).fetchall()

        for r in dead_rows:
            result["dead_agents"].append({
                "agent_id": r["id"],
                "name": r["name"],
                "last_seen": r["last_seen"],
                "dead_seconds": int(now - (r["last_seen"] or 0)),
            })

        # 3. Scan blocked DAG nodes
        pending = conn.execute(
            "SELECT id, title, depends_on, auto_advance, assignee FROM tasks WHERE status = 'pending'"
        ).fetchall()
        for task in pending:
            deps = json.loads(task["depends_on"] or "[]")
            if not deps:
                continue
            all_done = True
            for dep_id in deps:
                dep = conn.execute("SELECT status FROM tasks WHERE id = ?", (dep_id,)).fetchone()
                if not dep or dep["status"] != "done":
                    all_done = False
                    break
            if all_done and task["assignee"]:
                result["dag_unblocked"].append({
                    "task_id": task["id"],
                    "title": task["title"],
                    "reason": "deps_met_but_not_advanced",
                })

    # 4. Execute recovery if not dry_run
    if not dry_run:
        # Mark stale tasks
        if result["stale_tasks"]:
            stale_ids = [s["task_id"] for s in result["stale_tasks"]]
            with get_db() as conn:
                for tid in stale_ids:
                    conn.execute(
                        "UPDATE tasks SET status = 'stale', updated_at = ? WHERE id = ? AND status = 'in_progress'",
                        (now, tid)
                    )
            result["actions_taken"].append(f"Marked {len(stale_ids)} stale tasks")

        # Recover dead agents
        for d in result["dead_agents"]:
            with get_db() as conn:
                agent_row = conn.execute(
                    "SELECT id FROM agents WHERE name = ?", (d["name"],)
                ).fetchone()
                if not agent_row:
                    continue
                tasks = conn.execute(
                    """SELECT id FROM tasks
                       WHERE assignee = ? AND status IN ('assigned', 'in_progress', 'stale')""",
                    (agent_row["id"],)
                ).fetchall()
                recovered_count = 0
                for t in tasks:
                    conn.execute(
                        """UPDATE tasks SET status = 'pending', assignee = NULL,
                           assigned_at = NULL, updated_at = ? WHERE id = ?""",
                        (now, t["id"])
                    )
                    recovered_count += 1
                if recovered_count:
                    result["actions_taken"].append(
                        f"Recovered {recovered_count} tasks from dead agent {d['name']}"
                    )

        # Force-advance blocked DAG nodes
        if result["dag_unblocked"]:
            block_ids = [b["task_id"] for b in result["dag_unblocked"]]
            with get_db() as conn:
                for tid in block_ids:
                    conn.execute(
                        "UPDATE tasks SET status = 'assigned', assigned_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, tid)
                    )
            result["actions_taken"].append(f"Force-advanced {len(block_ids)} blocked DAG tasks")

    return result


@app.post("/recovery/agent-crash/{agent_name}")
def recovery_agent_crash(
    agent_name: str,
    _=Depends(require_master),
):
    """Manually mark an agent as crashed and release all its tasks back to pending.

    Use this when an agent is known to be down (e.g., host reboot, process killed)
    but the heartbeat hasn't expired yet. Master key required.
    """
    now = time.time()
    with get_db() as conn:
        agent_row = conn.execute(
            "SELECT id, name FROM agents WHERE name = ?", (agent_name,)
        ).fetchone()
        if not agent_row:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        tasks = conn.execute(
            """SELECT id, title, status FROM tasks
               WHERE assignee = ? AND status IN ('assigned', 'in_progress', 'stale')""",
            (agent_row["id"],)
        ).fetchall()

        recovered = []
        for t in tasks:
            conn.execute(
                """UPDATE tasks SET status = 'pending', assignee = NULL,
                   assigned_at = NULL, updated_at = ? WHERE id = ?""",
                (now, t["id"])
            )
            recovered.append({
                "task_id": t["id"],
                "title": t["title"],
                "previous_status": t["status"],
            })

        # Log to audit
        if recovered:
            conn.execute(
                """INSERT INTO audit_events (id, timestamp, actor_id, actor_name, action, entity_type, entity_id, changes, context)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"ae_{int(now)}_crash", now, "system", "recovery_api",
                 "agent_crash_recovery", "agent", agent_row["id"],
                 json.dumps({"recovered_tasks": len(recovered)}),
                 json.dumps({"agent_name": agent_name, "triggered_by": "api"}))
            )

    return {
        "agent": agent_name,
        "recovered_count": len(recovered),
        "recovered_tasks": recovered,
    }


# ── Run ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT)
