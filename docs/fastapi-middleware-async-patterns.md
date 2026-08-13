# AgentBus FastAPI 中间件与异步通信模式深度解析

> 团队学习材料 | 2026-08-13 | 基于 `main.py` v1.4 (1056 行) + `bus-worker.sh` 60s 轮询架构

---

## 一、FastAPI 特性盘点：当前用到了什么，缺了什么

通读 `main.py` 后，AgentBus 使用了以下 FastAPI 能力：

| 特性 | 用法 | 评价 |
|------|------|------|
| **CORSMiddleware** | `allow_origins=["*"]` 全开 | 生产环境应收窄白名单 |
| **StaticFiles** | `app.mount("/dashboard", ...)` 同源部署 | 巧妙规避 CORS，仪表盘零依赖 |
| **Pydantic Models** | `AgentRegister`, `MessageSend`, `TaskCreate` 等 | ✅ 输入验证做得好 |
| **Depends 依赖注入** | `resolve_agent()`, `require_master()` | ✅ 认证逻辑复用清晰 |
| **Header / Query / Body** | `X-API-Key` 头、分页参数、能力过滤 | ✅ 声明式参数绑定 |
| **HTTPException** | 401/403/404/409 全覆盖 | ✅ 错误语义完整 |

**关键缺失：所有端点都是 `def`，没有 `async def`。** FastAPI 对同步函数会丢入 threadpool 执行——功能正确但浪费了异步框架的核心优势：

```python
# ❌ 当前：同步阻塞，FastAPI 委托给 threadpool
@app.get("/messages/inbox")
def get_inbox(agent: dict = Depends(resolve_agent)):
    with get_db() as conn:  # 阻塞 I/O
        rows = conn.execute(...).fetchall()
    return {"messages": [dict(r) for r in rows]}

# ✅ 理想：异步 + 连接池
@app.get("/messages/inbox")
async def get_inbox(agent: dict = Depends(resolve_agent)):
    async with db_pool.acquire() as conn:
        rows = await conn.execute(...).fetchall()
    return {"messages": [dict(r) for r in rows]}
```

另外，`get_db()` 每次请求都新建 + 关闭 SQLite 连接（含 `PRAGMA journal_mode=WAL`），在高并发下会成为瓶颈。应做连接复用或迁移 `aiosqlite`。

---

## 二、轮询 vs WebSocket 推送：场景驱动的权衡

### 当前架构：bus-worker.sh 每 60s 轮询

```
Agent (bus-worker.sh)
  └── loop:
        GET /messages/inbox?unread_only=true   ← 轻量探测
        if unread > 0:
          POST /messages/{id}/read              ← 防重复处理
          claude -p "处理这些消息..."           ← 重 LLM 调用
          re-poll (连续模式)
        else:
          sleep 60s
```

**这套设计在当前阶段是合理的。** 理由：

1. **Agent 通信本质异步**——Echo 和 CC 之间不需要亚秒级延迟，60s 完全可接受
2. **容错天然**——worker 挂了重启即可，消息在 SQLite 里不会丢
3. **零基础设施**——不需要 WebSocket 连接管理、心跳保活、断线重连
4. **LLM 处理本身就是分钟级**——即使消息 0s 到达，`claude -p` 也要 30-120s

### WebSocket 推送能解决什么？

| 痛点 | 轮询 | WebSocket |
|------|------|-----------|
| 消息延迟 | 最坏 60s | ~0ms |
| 空闲资源消耗 | 每 agent 每 60s 一次 HTTP | 仅 TCP 保活心跳 |
| Agent 突发响应 | 排队等下一轮 | 即时唤醒 |
| 实现复杂度 | 低（bash 脚本） | 高（连接池 + 状态机） |
| 断线容错 | 天然（重试即可） | 需重连逻辑 + 消息补偿 |
| SQLite 兼容性 | 完美 | 需额外事件通知机制 |

**结论：除非 AgentBus 要支持实时协作（如多 Agent 同时编辑、即时对话），否则 60s 轮询的 ROI 远高于 WebSocket。**

---

## 三、如果要改 WebSocket Push：设计方案

假设确实需要低延迟推送（比如 Tank 要求 Agent 在 5s 内响应），以下是改造方案：

### 3.1 连接管理器

```python
from fastapi import WebSocket, WebSocketDisconnect
import asyncio

class ConnectionManager:
    """维护每个 agent 的 WebSocket 连接。"""
    def __init__(self):
        self.active: dict[str, set[WebSocket]] = {}  # agent_id → connections

    async def connect(self, websocket: WebSocket, agent_id: str):
        await websocket.accept()
        self.active.setdefault(agent_id, set()).add(websocket)

    def disconnect(self, websocket: WebSocket, agent_id: str):
        self.active.get(agent_id, set()).discard(websocket)

    async def notify(self, agent_id: str, message: dict):
        """向指定 agent 的所有连接推送消息。"""
        for ws in self.active.get(agent_id, set()):
            await ws.send_json(message)

manager = ConnectionManager()
```

### 3.2 WebSocket 端点

```python
@app.websocket("/ws/inbox")
async def ws_inbox(websocket: WebSocket, x_api_key: str):
    """Agent 通过 WebSocket 订阅自己的收件箱推送。"""
    agent = resolve_agent(x_api_key)  # 复用现有认证
    await manager.connect(websocket, agent["id"])
    try:
        while True:
            # 保活：等待客户端心跳（30s 超时）
            await asyncio.wait_for(websocket.receive_text(), timeout=30)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        manager.disconnect(websocket, agent["id"])
```

### 3.3 在消息发送时触发推送

```python
@app.post("/messages")
async def send_message(payload: MessageSend, agent: dict = Depends(resolve_agent)):
    # ... 现有入库逻辑 ...
    msg_id = f"msg_{uuid.uuid4().hex[:16]}"

    # 异步推送：不阻塞 HTTP 响应
    if to_id:
        asyncio.create_task(manager.notify(to_id, {
            "type": "new_message", "id": msg_id, "from": agent["name"]
        }))
    elif channel == "broadcast":
        # 广播给所有在线 agent
        for aid in manager.active:
            asyncio.create_task(manager.notify(aid, {
                "type": "broadcast", "id": msg_id, "from": agent["name"]
            }))

    return {"id": msg_id, "status": "sent"}
```

### 3.4 需要同步改造的点

| 改动项 | 工作量 | 风险 |
|--------|--------|------|
| `def` → `async def` 全量端点 | 中 | SQLite 同步阻塞需换 `aiosqlite` |
| `bus-worker.sh` 加 WebSocket 客户端 | 高 | bash 不支持 WS，需 Python 重写 worker |
| 消息补偿机制（断线期间丢失的消息） | 中 | 需 sequence number + gap 检测 |
| 多 worker 进程下的连接共享 | 高 | 需要 Redis Pub/Sub 做跨进程广播 |

---

## 四、其他优化路径：比 WebSocket 更务实的方案

### 方案 A：SSE（Server-Sent Events）——推荐首选

SSE 是 HTTP 上的单向推送，**不需要 WebSocket 的复杂度**，FastAPI 原生支持：

```python
from fastapi.responses import StreamingResponse

@app.get("/events/inbox")
async def sse_inbox(agent: dict = Depends(resolve_agent)):
    async def event_stream():
        last_check = time.time()
        while True:
            # 查询新消息
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE to_id = ? AND created_at > ?",
                    (agent["id"], last_check)
                ).fetchall()
            for r in rows:
                yield f"data: {json.dumps(dict(r))}\n\n"
                last_check = max(last_check, r["created_at"])
            await asyncio.sleep(5)  # 5s 轮询数据库（比 60s 快 12 倍）

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**优势**：worker 端只需 `curl -N` 即可消费，不需要 WebSocket 客户端库。断线自动重连是 SSE 协议内置行为。

### 方案 B：长轮询（Long Polling）——最小改动

把当前 60s 固定 sleep 改为服务端 hold 住请求直到有新消息或 30s 超时：

```python
@app.get("/messages/poll")
async def long_poll(agent: dict = Depends(resolve_agent), timeout: int = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE to_id = ? AND created_at > ?",
                (agent["id"], agent.get("last_poll", 0))
            ).fetchall()
        if rows:
            return {"messages": [dict(r) for r in rows]}
        await asyncio.sleep(2)  # 2s 间隔查库
    return {"messages": [], "timeout": True}
```

### 方案 C：PostgreSQL LISTEN/NOTIFY——根治推送

如果未来迁移到 PostgreSQL，可用原生推送，彻底消除轮询：

```python
# 消息写入时
conn.execute("NOTIFY agent_channel, %s", (json.dumps({"to": to_id}),))

# Agent 端长连接监听
conn.execute("LISTEN agent_channel")
# 有消息时 PostgreSQL 主动推送，零延迟
```

---

## 五、总结与建议

| 方案 | 延迟 | 改动量 | 适合阶段 |
|------|------|--------|----------|
| **现状：60s 轮询** | ≤60s | 0 | ✅ 当前 4 个 Agent，够用 |
| **SSE + 5s 间隔** | ≤5s | 小 | 短期优化首选 |
| **长轮询 30s** | ≤2s | 小 | 需要低延迟但不想加复杂度 |
| **WebSocket** | ~0ms | 大 | 未来多 Agent 实时协作 |
| **PG LISTEN/NOTIFY** | ~0ms | 大（含 DB 迁移） | 长期架构演进 |

**务实的下一步**：把 `bus-worker.sh` 的轮询间隔从 60s 降到 15s（一行改动），然后在服务端加一个 SSE 端点作为 PoC。如果 SSE 稳定且有明显体验提升，再考虑全面 `async def` 改造。不要为了"现代化"而引入 WebSocket——AgentBus 的核心价值是简单可靠，不是技术先进性。
