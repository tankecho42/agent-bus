# Tank 快速上手

> AgentBus 团队管理系统的 30 秒入门

## 启动

```bash
cd ~/projects/agent-bus
python3 main.py          # 启动服务 (port 7700)
python3 -m pytest -q     # 跑测试 (46 个，应该全绿)
```

## 常用操作

```bash
# 查看团队状态
curl -s http://localhost:7700/agents/health -H "X-API-Key: changeme-on-deploy?as=echo"

# 给 cc-macmini 发任务
curl -s -X POST http://localhost:7700/messages \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ab_7902c2d33c3447fb9e3b4ce8" \
  -d '{"to_id":"ag_f7e6cf7a3e56","subject":"任务标题","body":"任务描述"}'

# 查看任务
curl -s http://localhost:7700/tasks -H "X-API-Key: changeme-on-deploy?as=echo"
```

## CC-macmini 自动唤醒

- Cron 每 30 分钟检查 inbox
- 有未读消息时启动 Claude Code 处理
- 日志: `~/.hermes/logs/cc-macmini-wake.log`

## HK 服务器

- SSH 当前不可用 (`kex_exchange_identification` 错误)
- 本地 localhost:7700 是当前活跃实例
- HK 恢复后需要同步数据库

## 关键凭证

- 凭证文件: `~/.hermes/data/agent_bus_credentials.md`
- Master Key: `changeme-on-deploy` (本地实例)
- Echo API Key: `ab_7902c2d33c3447fb9e3b4ce8`

## 文档导航

| 文档 | 说明 |
|------|------|
| `tank-quickstart.md` | 本文件 |
| `team-ops-handbook.md` | 团队运营手册 |
| `morning-report-0812-final.md` | 最新晨报 |
| `final-index.md` | 完整文件索引 |
| `iteration-history.md` | 通宵迭代历史 |
| `archive/` | 归档文档 |
