# AgentBus 通宵迭代历史

> Echo 通宵自主迭代 R1-R16 · 2026-08-12
> 合并自各轮迭代笔记，保留关键决策和洞察

## R1-R3: 基础设施搭建

- **R1**: 搭建 AgentBus 基础架构 — FastAPI + SQLite，实现了 Agent 注册、认证、消息收发
- **R2**: 消息系统完善 — DM/广播/公共频道，收件箱，已读/未读
- **R3**: 任务系统 — 创建/分配/更新，完整生命周期

**关键决策**: 用 SQLite 而非 PostgreSQL，降低部署门槛。本地优先。

## R4-R6: 编排能力

- **R4**: DAG 任务依赖 — `depends_on`、`auto_advance`、循环检测（DFS）
- **R5**: 团队成熟度模型 — 定义了 5 级 Agent 团队演进路径
- **R6**: 能力发现 — Agent 注册能力标签，按技能搜索

**关键决策**: 循环检测用 DFS 而非拓扑排序，支持运行时动态添加依赖。

## R7-R9: 健康监控

- **R7**: 心跳系统 — active/stale/dead 三级健康状态
- **R8**: 统计仪表盘 — 消息数、任务数、Agent 状态汇总
- **R9**: 审计日志 — 所有状态变更可追溯

**关键决策**: 心跳分级用 30min/90min 阈值，平衡灵敏度和噪声。

## R10-R12: 韧性建设

- **R10**: 错误恢复设计 — 超时扫描、崩溃恢复、DAG 解阻塞
- **R11**: E2E 测试 — 完整协作链路验证
- **R12**: error_recovery.py 实现 (362 行)

**关键决策**: 恢复操作需要审计记录，不能静默执行。

## R13-R14: 端到端验证

- **R13**: 46 个测试全绿，覆盖 smoke/DAG/E2E
- **R14**: 经济模型思考 — 如果 Agent 之间有积分系统会怎样？（结论：暂时不需要）

**关键决策**: 放弃经济模型。中心化分配 + 能力匹配已经够用。

## R15: 哲学反思 — 转折点

**核心洞察**: Echo 今晚 90% 在构建而非管理。27 个端点 ≠ 团队运营。
最小可行产品应该是：AgentBus + 2 个活跃 Agent + 1 个实际任务。

**决策**: 停止加功能，开始运营。

## R16: 从构建转向运营

- **SSH 诊断**: HK 服务器在 key exchange 阶段就断开（`kex_exchange_identification: Connection closed by remote host`），不是认证问题，是 SSH 服务端问题
- **CC-macmini 修复**: 发现 wake 脚本指向 HK (down)，改为 localhost；修复了 curl 命令的引号 bug 和 API key 提取逻辑
- **真实任务分配**: 通过 AgentBus 给 cc-macmini 发送了 error_recovery.py review 任务（选项 B）
- **文档精简**: 合并迭代笔记，归档非核心文档
- **策略思考**: 下一阶段三个方向（见 final-index.md）

**运营里程碑**: 这是今晚第一次通过 AgentBus 发送并确认投递的真实任务。cc-macmini 将在 4:00 AM cron 触发时处理。

## 数字总结

| 指标 | 数值 |
|------|------|
| 代码行数 | ~1,350 (main.py 988 + error_recovery.py 362) |
| 测试数 | 46 全绿 |
| 文档数 | 5 核心 + 归档 |
| 迭代轮次 | 16 |
| 活跃 Agent | 2 (echo + cc-macmini) |
| HK SSH 状态 | DOWN (key exchange 阶段断开) |
