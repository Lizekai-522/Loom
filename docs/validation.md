# Loom 0.9 分步验证记录

2026-10-25，0.9.0，role-handoff-alpha，**开发检查点，完整验收未通过**。API/控制台已恢复，Docker Desktop当前不可用；四角色真实执行和Docker回归待验收。原0.8完整证据见[历史记录](validation-0.8.md)。

| 当前门槛 | 主测试 | 子测试 | 失败/跳过 | 状态 |
|---|---:|---:|---|---|
| 全部unit-and-local | 617 | 382 | 0/0 | 通过 |
| 真实NATS/重启/PostgreSQL | 23 | 14 | 0/0 | 通过 |
| Docker原17项及新增交接3项 | 20项待执行 | — | 未运行 | 环境阻塞 |

实际HTTP共163项：API9、Goal12、人工控制31、旧真实任务17、候选31、旧上下文16、旧角色来源24、新tester来源23。浏览器17项通过，实际health=0.9.0/S1-local，资产hash与主API核对；其模型/沙箱为测试替身，不作为真实Docker或模型证据。专项与完整单元集合重叠，不相加。

新增tester核心14项、runner18项、来源API7项、四角色CLI15项均包含于617项。非CSV任务误套CSV指令的P2问题已修，新增及旧CLI12项通过。严格development-only JSON、原子来源/费用/回执、心跳/未知态停止、六次精确grant、1/2/3项上下文及失败停止均已测试；生成检查不会覆盖evaluation_digest或被标为已执行。

## 新真实tester

run 9dc5a8ad-3335-4a59-b142-abb00f248998：具名HTTP批准tester合同，1次Flash响应，469输入+154输出=623tokens，326µUSD为上界记账，预留0，未核对账单。候选未执行/未独立验证，任务submitted、review pending、Goal未完成；没有发送宿主验收用例。23项只读HTTP核对来源、费用与事件。

上一轮0.8当前业务复测run 972addd4-906d-4c59-b649-ea7692e1cc00，3Flash/5Docker用例、3670tokens/2091µUSD上界，保留原证据，本次未重复，不能当成0.9四角色容器验收。

## 阻塞及后续门槛

Docker/run/dockerInference残留socket启动失败；保留重建运行目录后，转到docker-secrets-engine/engine.sock。其父目录Move-Item、.NET改名、Rename-Item均访问被拒绝；单个socket删除操作被自动审批拒绝，未执行。未清镜像/卷、重置Desktop、注销WSL或升级系统。20项Docker测试和4次真实完整交接未运行，没有转skip或计通过。

恢复Docker后先运行scripts/check_s2.py完整门槛，确认原固定runner镜像及43项基础设施测试无跳过；再预览/显式批准四角色CLI，最多4Flash/8host冻结case。核对来源/费用/依赖后才形成完整源码包。原0.8包保留；本检查点不作为完整验收发行。

第二模型依用户决定待配置，不伪造签名/done。原62项FR、七角色/生产身份/恢复/PG Store及效果对照缺口保留；11月15日部署、11月20日前提交目标不变。[实施计划](implementation-plan.md)
