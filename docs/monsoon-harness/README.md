# monsoon-harness 方案文档

monsoon-harness（ms）是规划中的本机常驻 agent：单独运行时是全天候 agent（CLI harness + 本机 Web 控制台），连接 Loom 时把需要中心能力的任务外包给 Loom，多台 ms 可同时向同一台 Loom 提交任务、互不踩踏。本目录收录其施工方案与调查记录，属规划文档，不含 Loom 源码变更。

| 文档 | 内容 |
|---|---|
| [monsoon-harness-方案书-v2.2.md](monsoon-harness-方案书-v2.2.md) | 规格真源。五项决策（Q1-Q5）、拓扑与五形态、数据与身份模型、16 动作跨机契约、六批次施工与 37 条验收判据；含 2026-11-01 修订前言（吸收路线六项拍板）与附录 D 候选功能清单 |
| [monsoon-harness-交接文档.md](monsoon-harness-交接文档.md) | 施工入口。三库一手事实（file:line）、已定决策、16 动作契约速查、已知陷阱清单、批次顺序 |
| [调查报告-2026-11-01.md](调查报告-2026-11-01.md) | 三库（rakazo/RV/Loom 0.23.0）盘点、41 个 file:line 锚点核对、"吸收改造 vs 整仓 fork"路线分析、rakazo 可优化点清单（20 项） |
| [对照调研-2026-11-01.md](对照调研-2026-11-01.md) | DeepSeek 双 agent 架构核验（9 条全成立）与吸收/不吸收清单、dsh 产品仓 52 包功能点、同类产品（Codex/Claude Code/Devin/Manus/Goose/OpenHands/Cursor）扫描 |

## 阅读顺序

了解方案：方案书修订前言 → 第一章决策记录 → 第 4.4 节跨机契约 → 第十章批次。动手施工：交接文档（五分钟上手路径）→ 方案书对应批次 → 附录验收判据。

## 与本仓库的关系

- Loom 是 ms 的编排中心；批次三（Loom 侧改造：事件游标分页、副作用登记端点、关闭机器批准路径、角色能力集调整）将作为本仓库的后续实施 PR 逐项推进。
- 方案书 v2.2 修订前言记录的六项拍板（2026-11-01）：主栈路线 A（概念吸收 + Python 重写）、Loom 式认证、dsh/Codex 式 CLI harness 客户端形态、Pi 不进 Phase 1、批次三先行、rakazo 优化清单采纳为设计约束。
- 涉及的只读基线快照：rakazo `d13ef7a`（Apache-2.0）、Realtime-Venus `e53ae8d`、dsh-superagent `6337fc8`（MIT）、deepseek-harness `ce7073d`。
