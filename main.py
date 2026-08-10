"""
Agent Communication Bus — Agent 之间异步消息交换服务
支持：Agent注册、私聊、广播、公共频道、消息查询
"""
import os
import sqlite3
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Query
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
                id          TEXT PRIMARY KEY,
                name        TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                api_key     TEXT NOT NULL,
                created_at  REAL NOT NULL,
                last_seen   REAL
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
        """)

init_db()

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
                "INSERT INTO agents (id, name, description, api_key, created_at) VALUES (?, ?, ?, ?, ?)",
                (agent_id, payload.name, payload.description, api_key, time.time())
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Agent '{payload.name}' already exists")
    return {"agent_id": agent_id, "name": payload.name, "api_key": api_key}

@app.get("/agents")
def list_agents(agent: dict = Depends(resolve_agent)):
    """列出所有已注册的 Agent。"""
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, description, created_at, last_seen FROM agents ORDER BY name").fetchall()
    return {"agents": [dict(r) for r in rows]}

@app.get("/agents/me")
def whoami(agent: dict = Depends(resolve_agent)):
    """查看自己的身份。"""
    return {"id": agent["id"], "name": agent["name"], "description": agent["description"]}

@app.delete("/agents/{name}")
def delete_agent(name: str, _=Depends(require_master)):
    with get_db() as conn:
        row = conn.execute("SELECT id FROM agents WHERE name = ?", (name,)).fetchone()
        if not row:
            raise HTTPException(404, "Agent not found")
        conn.execute("DELETE FROM messages WHERE from_id = ? OR to_id = ?", (row["id"], row["id"]))
        conn.execute("DELETE FROM agents WHERE id = ?", (row["id"],))
    return {"deleted": name}

# ── 消息收发 ────────────────────────────────────────────
@app.post("/messages")
def send_message(payload: MessageSend, agent: dict = Depends(resolve_agent)):
    """发送消息。to 为空=广播给所有人；channel=public=发到公共频道。"""
    import json

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
    import json
    with get_db() as conn:
        query = """
            SELECT m.*, a.name AS from_name
            FROM messages m JOIN agents a ON m.from_id = a.id
            WHERE (m.to_id = ? OR m.channel = 'broadcast')
        """
        params = [agent["id"]]
        if unread_only:
            query += " AND m.read_at IS NULL"
        query += " ORDER BY m.priority DESC, m.created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn.execute(query, params).fetchall()

        if mark_read and rows:
            now = time.time()
            for r in rows:
                conn.execute(
                    "UPDATE messages SET read_at = COALESCE(read_at, ?), read_by = ? WHERE id = ?",
                    (now, json.dumps([agent["id"]]), r["id"])
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
    import json
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
            "SELECT COUNT(*) AS c FROM messages WHERE (to_id = ? OR channel = 'broadcast') AND read_at IS NULL",
            (agent["id"],)
        ).fetchone()["c"]
    return {
        "agents": agents,
        "messages_total": total,
        "dm": dm,
        "broadcast": broadcast,
        "public": public,
        "my_unread": my_unread,
    }

# ── Run ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT)
