# Loom 0.6 验证记录

日期：2026-10-23（Asia/Shanghai）；版本0.6.0，artifact-preview-alpha。

**469主测试、302子测试通过，0失败、0跳过；另100项实际HTTP和46项真实浏览器断言通过。** 新增模型请求0，生成候选的HTTP测试数据不冒充模型结果。

## 逐步门槛

先核对干净0.5提交52e2100、对应已通过报告及主API健康，再实现0.6-A核心候选合同。核心专项连同旧workflow/execution/Rebind为111主/58子通过；B新API及旧control为39主通过；C控制台与故障反例通过后才运行下述完整门槛。这些专项与完整套件重叠，不相加。

| 阶段 | 主测试 | 子测试 | 失败 | 跳过 |
|---|---:|---:|---:|---:|
| unit-and-local | 433 | 288 | 0 | 0 |
| real-infrastructure | 36 | 14 | 0 | 0 |

真实基础设施36主/14子：JetStream8、原生broker重启1、PostgreSQL DDL/RLS14、原Docker隔离9、有界修复Docker4。模型HTTP使用测试替身；API/内核仍SQLite，独立PG检查不代表运行时PG Store已实现。

## HTTP与浏览器

实际8796检查为原API9、Goal/Plan12、人工控制31、旧真实任务兼容17，另候选交接31。候选演示run为`77978d67-e408-43a2-9756-34f9dd0dd1c1`，来源明确为认证HTTP测试Worker，无模型、无代码执行。source仍submitted，design仍pending且硬依赖不可领取；旧真实任务452 tokens/262µUSD上界、5用例、合同/事件均保留。

真实浏览器使用独立8797服务和公开sentinel身份：46断言覆盖候选/目标/运行切换、慢响应、503、旧revision、目标epoch变化、纯文本注入、响应丢失同键重试、viewer及原控制流程，并检查移动端布局。未使用8796真实身份或API key；测试服务和会话已关闭。

## 修复与独立反例

目标epoch变化时原界面误报解析错误并仍可提出必败旧CAS，已改为明确刷新提示、锁定批准，forced submit为0写，完整刷新后恢复。核心在接入前补齐target_epoch绑定和最终响应字节计数：65536字节允许、65537拒绝。独立审查还验证同键并发单写、receipt故障全部回滚、跨租户/run、来源与双向epoch、revision/content/plan/event绑定、GET无写入及硬depends不变；这些探针不另计主测试。

## 安装与交付

标准隔离editable构建/安装及pip check通过，未更改依赖版本；Starlette TestClient/httpx弃用警告仍为非失败提示。OpenAPI按0.6生成24个路径，原方案81数字引用/10ADR及24项修订映射检查通过。主API升级前已备份SQLite并检查完整性，沿用旧身份和私有具名配置，未发布公网。

机器报告位于源码之外outputs，均以Loom_0.6开头；独立审查单独记录。历史见[0.5](validation-0.5.md)、[0.4](validation-0.4.md)。本轮只验收候选注册、辅助读取批准和只读preview；未接prompt/派发/Gate-V。独立双模型按用户决定待配置，FR-051/052仍部分实现。生产身份、完整七角色/修复、任意仓库、PG Store/多主机和恢复/业务效果仍待验收。
