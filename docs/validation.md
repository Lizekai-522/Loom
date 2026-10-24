# Loom 0.11 验证记录

2026-10-25，0.11.0，development-suite-registry-alpha。完成独立开发套件审阅注册表核心；API授权入口、执行计划引用和生成用例执行仍待接入。[0.10纯编译记录](validation-0.10.md)，[0.9真实四角色记录](validation-0.9.md)。

| 完整阶段 | 主测试 | 子测试 | 失败 | 跳过 |
|---|---:|---:|---:|---:|
| unit-and-local | 682 | 382 | 0 | 0 |
| real-infrastructure | 44 | 14 | 0 | 0 |
| 合计 | 726 | 396 | 0 | 0 |

本轮新增22项注册表主测试和1项真实NATS测试，已包含在上述总数。66项注册表/编译/CLI/NATS专项与完整集合重叠，不叠加；模型均为替身，无新付费请求。NATS新增真实suite_frozen事件投递、PubAck、确认丢失重投、inbox去重和Store重启读取；原43项Docker/NATS/PG门槛继续通过。

注册表验证精确候选内容/模型来源、当前epoch/revision/计划/事件、编译版本、按源顺序的选定case和独立review digest。suite/事件/回执/outbox同事务，只追加，插入失败整体回滚；同请求并发只创建一份，同键异参数/actor拒绝。源换代、superseded、过期租约或暂停时不能新增冻结，旧记录保留并标stale。修改记录或来源事件不被接受。

两份既有0.9真实模型候选在隔离DB副本各完成一次冻结和幂等/重启核对；主库仅只读备份和指纹核对，没有新增suite表、事件、批准、任务状态或费用。它们不是实际主API的人工审批，也不证明独立人员身份。

首次单元夹具将Store.snapshot误当含run键的嵌套对象，修正为整个快照对比。首次NATS夹具重复ack同一已确认Msg而报MsgAlreadyAckd；改为真实丢ack后从broker重投，不忽略异常、不改生产bridge，重投去重验收通过。原失败XML保留。

当前API、权限和UI没有新增suite入口。服务更新后重新做33项四角色只读HTTP对账；原196HTTP/33浏览器按不变业务代码/资产复用，不相加或当作新suite UI验收。原任务仍submitted，独立review pending、Goal不成功、第二模型待配置。

下一步依次接入具名operator权限API/准确预览、新草案suite引用和预算合同、首个开发调用前重新核对、Docker开发检查/repair及工作台；不得改旧批准计划或用辅助input grant替代执行许可。
