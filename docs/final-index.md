# AgentBus 最终索引

> Echo 通宵迭代 R1-R16 完整产出 · 2026-08-12

## 核心数字

| 指标 | 数值 |
|------|------|
| 代码行数 | 1,350 (main.py 988 + error_recovery.py 362) |
| 测试 | **46 全绿** |
| 文档 | 5 核心 + 3 归档 |
| 活跃 Agent | 2 (echo + cc-macmini) |
| 通宵迭代轮次 | 16 |

## 核心文档

| 文档 | 说明 |
|------|------|
| `tank-quickstart.md` | 30 秒上手指南 |
| `team-ops-handbook.md` | 团队运营 SOP |
| `morning-report-0812-final.md` | 今晚最终晨报 |
| `agent-growth-profiles.md` | Agent 成长档案 |
| `iteration-history.md` | R1-R16 迭代历史 |

## 归档文档 (`archive/`)

| 文档 | 说明 |
|------|------|
| `agent-economy-model.md` | 经济模型思考（结论：暂不需要） |
| `agent-team-maturity-model.md` | 团队成熟度 5 级模型 |
| `error-recovery-design.md` | 错误恢复设计文档 |

## 代码模块

| 文件 | 行数 | 说明 |
|------|------|------|
| `main.py` | 988 | AgentBus 主服务 |
| `error_recovery.py` | 362 | 错误恢复模块 |

### main.py 端点清单

- 消息: register, inbox, public, sent, send, read, delete
- 任务: create, list, get, update, my/active
- DAG: depends_on, auto_advance, 循环检测
- 能力: register capabilities, search by skill
- 健康: heartbeat, health dashboard, stats
- 恢复: scan stale, crash recovery, DAG unblock
- 审计: audit_events 全表

## 系统能力

### ✅ 已完成
1. 消息系统 (DM/广播/公共频道)
2. 任务系统 (完整生命周期)
3. DAG 编排 (依赖+循环检测+自动推进)
4. 能力发现
5. 健康监控
6. 错误恢复
7. 审计日志
8. E2E 测试验证

### 🔄 进行中
- CC-macmini 运营验证 (R16 分配了第一个真实任务)
- HK SSH 恢复 (需要 Tank 介入)

### 📋 下一阶段方向
1. **任务自动化** — 能力匹配自动分配 + 负载均衡
2. **可观测性** — Web Dashboard + DAG 可视化
3. **多节点部署** — HK 恢复 + Pi 激活 + 跨节点协作
