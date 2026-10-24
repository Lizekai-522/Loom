# Loom 0.12 验证记录

2026-10-25，0.12.0，development-suite-api-alpha。已开放开发套件精确预览、具名operator冻结和套件读取API；未绑定新执行合同，生成用例仍未执行。[注册表历史](validation-0.11.md)

| 完整阶段 | 主测试 | 子测试 | 失败 | 跳过 |
|---|---:|---:|---:|---:|
| unit-and-local | 702 | 382 | 0 | 0 |
| real-infrastructure | 44 | 14 | 0 | 0 |
| 合计 | 746 | 396 | 0 | 0 |

新增20项审批API测试包含在完整集合中。118项API/身份/注册表兼容专项和100项权限专项范围重叠，不相加；模型为替身，未新增付费。真实基础设施继续验证Docker、repair、NATS/重投、PG等。

独立数据库副本中完成39项实际HTTP检查：真实旧候选来源、精确摘要/选择、operator专权、Worker/viewer/legacy/未认证/跨租户拒绝、服务端actor、幂等、只追加事件、计划/任务/费用不变、暂停后拒绝新冻结及历史stale。原主库事件链不变，不是主库人工审批，也不证明独立人员。

17项实际Chromium检查通过，覆盖现有工作台角色、候选来源/未执行标签、viewer强制提交无写入、移动布局和凭据不持久化；模型和沙箱为公开fixture替身。控制台资产字节未变，原16项数字精度检查按相同JS复用，未计为本轮新断言。当前没有suite冻结UI。

初次专项和完整回归的失败来自两处旧测试精确权限快照尚未包括新增suite.approve；更新operator白名单断言并验证Worker/viewer/legacy继续无此权限。原完整失败JSON/JUnit保留，基础设施当时未执行而非skip。实际HTTP脚本还误用/pause，已改现存/control/pause，重建副本后39项通过；旧副本保留。

本机服务更新后另做33项旧真实四角色只读对账。原任务submitted、review pending、Goal未成功，第二模型待配置。新注册表在主库仅初始化空表，不自动批准套件或生成执行任务。

下一步新草案明确引用suite/独立host acceptance，在批准计划及首个开发模型发起前重验来源、compiler、目标与预算，再接真实development/repair和工作台。旧批准计划不改，input grant和suite.freeze不授予执行权。
