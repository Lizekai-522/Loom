# Loom 0.17.2 验证记录

2026-10-26，unified-node-alpha。[使用与首轮测试](unified-nodes.md) · [0.17.1历史](validation-0.17.1.md)

| 完整回归阶段 | 主测试 | 子测试 | 失败 | 跳过 |
|---|---:|---:|---:|---:|
| unit-and-local | 906 | 382 | 0 | 0 |
| real-infrastructure | 57 | 14 | 0 | 0 |
| 合计 | 963 | 396 | 0 | 0 |

本轮新增35本地与2真实Docker主测试已含总数。最后只读指引收紧“缺冻结宿主合同不得准备执行”后，相关35专项与额外2项缺判据断言通过，重复专项不再加进963。执行/预算/认领代码在完整回归后未变。完整门槛的模型HTTP均替身；真实Docker、JetStream与独立PostgreSQL检查，不是PG Store生产验收。真实模型调用另记。

## 功能验收

统一CLI按冻结合同执行analyst/architect/tester/developer/reviewer/fixer，不建重复Goal。预览不读私有配置、模型或Docker；显式执行核对具名operator/worker、旧预览、硬依赖、effect、预留、预算与容量，精确输入在认领同事务为epoch1重批。结构化角色不接收宿主用例，validator明确待配置。未知不自动重发、审计失败回滚、并发一个赢家，旧CLI继续兼容。

四条Docker路径覆盖普通developer有/无辅助数据、单次fixer成功/失败转人工；提供方为替身。七角色推进指引只读同一权威快照，显示真实节点数量/阻塞/输入/路由与无凭据执行准备，不触发执行、预算或成功。

Chromium40：新指引/跳转/批准返回/迟到响应/只读19，主实例真实修复证据12，旧真实来源9。旧浏览器第一次读取切换前的旧DOM，补run_id与加载完成等待后9项通过；无产品错误，原失败输出保留。主实例109HTTP：新指引与真实修复49、旧审阅执行27、旧四角色来源33，零新增模型请求。OpenAPI32路径，服务资源与源码摘要一致。

## 真实审查与修复

新受控来源run3ad501ae-9edd-4676-93db-c1beeeecd2ef，初始代码明确由HTTP替身提供、真实Docker运行。原3项普通宿主终验通过，但3项额外布尔边界诊断失败，暴露isinstance(bool,int)导致的已知需求缺陷；没有声称初始开发是真实模型生成。

真实审查/修复runebf28162-e3d6-45fb-91ec-415e56be85e8：真实Flash reviewer发现1项问题，具名本机自动operator核对精确意见后批准一次fixer；真实Flash fixer通过原3项宿主终验与额外3项边界诊断，原判据摘要不变。两次真实响应3313tokens、费用记账上界1436µUSD、预留0，非供应商账单。来源替身35tokens/20µUSD仅为测试账本记录，不计真实消费。未重复任何旧付费请求，无自动重试。

reviewer/fixer仍submitted，validator仍pending且两条硬依赖未done；无独立签名、Goal成功或生产验收。额外诊断是受控缺陷前后检查，有限覆盖不代表通用修复效果；隐藏宿主/诊断正文没有进入审查/修复prompt。

## 数据兼容与发现的问题

主库升级前38runs/573events/1suite/1decision，升级后40runs/621events/1suite/2decisions；新增2个受控测试run及48事件。所有旧runs/tasks/effects/reservations/candidates/grants/suites/decisions/events/receipts行摘要保持不变，备份与数据库完整性ok。

首轮1失败为测试public-checks多填不允许的summary；2失败为测试库漏建review_decisions；后2失败暴露统一CLI简化回执漏带verified字段，已兼容并复测Docker两路径。额外修正指引返回后可能缓存旧数据、无冻结宿主摘要时不应显示可准备；边界专项通过。旧服务进程已退出，本轮恢复为仅回环隐藏后台进程。全部原失败另报，不改为skip；TestClient弃用提示仍存在，未扩大依赖升级范围。

## 继续推进

0.17.x优先工作台与本机执行器的受控调度衔接及完整人工分支，再推进新非受控需求的真实纵向链路。第二模型、独立认证、工具/配额/上下文、生产身份/恢复和部署仍按0.18–0.20追踪。原62项不删，FR-051/052仍partial；0.20计划上线候选，门槛通过1.0。11/15部署与11/20前提交目标不变。
