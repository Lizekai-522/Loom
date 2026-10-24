# Loom 0.8 验证记录

日期2026-10-24，版本0.8.0，role-candidates-alpha。**602主测试、356子测试，零失败/跳过；另140项实际HTTP和41项新增浏览器断言通过。** 专项与完整回归重叠，不累加；完整套件模型均为测试替身，真实调用另列。

起点为0.7提交e9e2e8b及逐文件核验的157文件源码包，沿用530主/326子、116HTTP、29浏览器基线。分阶段门槛：A核心及旧合同117主/90子；B角色及旧执行109主/14子；C来源摘要及旧API48主（新增15主）、CLI11主；然后完整基础设施、浏览器、本地升级和真实探针。测试夹具问题和交叉审查见单独问题报告。

| 完整阶段 | 主测试 | 子测试 | 失败 | 跳过 |
|---|---:|---:|---:|---:|
| unit-and-local | 562 | 342 | 0 | 0 |
| real-infrastructure | 40 | 14 | 0 | 0 |

真实基础设施仍40主/14子：JetStream8、原生重启1、独立PostgreSQL14、原Docker9、Rebind Docker4、上下文Docker4。角色新增路径没有Docker或代码工具；原Python隔离与预算/lease/控制回归保留。API/Store仍单实例SQLite，PG检查不等于PG Store已接通。

实际HTTP合计140：原API9、Goal12、人工控制31、旧真实任务17、候选31、旧上下文16、新角色来源24。旧0.4与0.7真实任务只读对账，合同、用例、费用和事件不变；没有重跑旧付费请求。浏览器41项为本轮新断言，完整阶段与真实health/assets观测见独立JSON，非沿用旧计数。

## 两次真实Flash响应

run `1d3a9c8c-df0e-43f3-93d6-b386658e5584`：具名HTTP身份创建并批准显式role_output，分析角色产生requirements；operator按当前design claim/epoch批准精确需求版本，设计角色消费typed辅助数据后产生design。最多两次请求，各一次，无自动重试，未执行代码/文件/导出或网络工具。两份候选均绑定gateway done effect、usage、settled预留、冻结合同、内容摘要和来源事件；候选/来源/submitted/receipt同事务。

供应商响应报告总3005 tokens，费用上界记账2120µUSD，剩余预留0；billed_currency_verified=false，未核对独立账单。schema_validated和model_source_recorded不证明内容正确。两个任务均submitted/producer_verified=false/verified=false，独立review仍被hard depends阻挡，Goal未完成，第二模型按用户决定待配置。

## 拒绝与来源展示

未显式批准角色合同、混用代码验收/repair、非法JSON/重复键/非有限数/截断响应、超预算、旧epoch或失效上下文均拒绝。有效已知响应先保留effect与费用，再检查控制/版本；未知调用保留预留并转人工，不重发。旧Worker候选digest与事件shape保留。

review只在核心重建完整来源且task/epoch/plan/内容/类型/call均匹配时显示来源已记录。attempts匹配真实候选的source_event_hash，伪造或重放事件不能加一条来源。来源摘要不含正文/原始响应/请求ID/提示/身份；候选正文仍可按授权单独查看。旧候选、篡改引用、stale或Worker自报不获得可信标签。独立窄探针和业务全表读取无变化另有审查报告，不冒充第二模型验证。

SQLite升级前备份及完整性、editable安装/pip check、OpenAPI24路径、方案81引用/10ADR/24映射、wheel资产与源码交付校验通过。原方案摘要不变，凭据仍在公开outputs之外，未发布公网。历史见[0.7](validation-0.7.md)，下一步见[实施计划](implementation-plan.md)。
