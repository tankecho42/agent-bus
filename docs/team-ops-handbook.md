# 团队运营手册

> AgentBus 多 Agent 团队管理 SOP

## Agent 角色

| Agent | 运行环境 | 职责 | 唤醒方式 |
|-------|---------|------|---------|
| Echo | Mac mini Hermes | 协调、规划、分配任务 | 120min cron |
| CC-macmini | Mac mini Claude Code | 代码审查、测试、部署 | 30min cron (智能空转) |
| CC-HK | 港服 Claude Code | (待恢复) | SSH down |
| CC-Pi | 树莓派 Claude Code | (待激活) | SSH |

## 任务分配原则

1. **能力匹配**: 通过 `GET /agents/capabilities?skill=code_review` 找到合适的 Agent
2. **优先级**: priority 0=正常, 1=高, 2=紧急
3. **DAG 编排**: 有依赖的任务用 `depends_on` 声明，`auto_advance=1` 自动推进
4. **审计**: 所有操作自动记录到 `audit_events` 表

## 健康监控

- Agent 心跳: 30min active / 90min stale / >90min dead
- 超时任务: in_progress > 2h 自动标记 stale
- 崩溃恢复: dead agent 的任务自动重置为 pending
- HK SSH Monitor: 每 10 分钟检查一次

## 故障处理

### CC-macmini 不响应
1. 检查日志: `tail -50 ~/.hermes/logs/cc-macmini-wake.log`
2. 确认 AgentBus 在运行: `curl localhost:7700/health`
3. 确认 cron 在执行: `hermes cron list | grep CC-macmini`
4. 手动触发: `bash ~/.hermes/scripts/cc-macmini-wake.sh`

### HK SSH 不通
1. 检查监控: `tail ~/.hermes/logs/hk-ssh-monitor.log`
2. 如果是 `kex_exchange_identification` 错误 → SSH 服务端问题，需要重启 sshd
3. 腾讯云控制台可以 VNC 登录重启

## 数据备份

- 每日 3:00 AM: 核心备份到飞书
- 每周一 4:00 AM: 完整备份
- 数据库: `~/projects/agent-bus/agent_bus.db`
