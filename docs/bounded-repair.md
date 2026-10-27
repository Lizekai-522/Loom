# 公开开发检查与有界修复

0.5.0增加同一逻辑节点的Rebind执行。它是software-dev领域的受限真实执行路径，不代表七角色完整协作、完整图RepairPlan或六种原语全部完成。第二模型按用户决定保持待配置。

## 执行顺序

批准计划时，把冻结验收摘要放在原evaluation_digest，把公开开发用例摘要和最多1–2次修复写入repair。总尝试次数继续受limits.max_attempts约束。没有repair字段的旧合同仍使用原run_python；新合同只能通过run_python_cycle，不能跳过开发检查。

每轮先通过原模型网关生成源码，预算预留、稳定模型call_id、usage结算和Docker隔离规则继续生效。宿主先比较公开开发用例；只有这些用例失败，才允许在原目标、规则、权限与预算内生成新输入版本。修复上下文包括上轮源码和有界公开失败输出，它们始终处于不可信user数据位，不构成系统授权。

模型预算预留和发起意图现由reserve_and_begin_effect在同一事务落盘；两条执行路径都使用该入口，暂停不能在两者之间留下无effect的孤立预留。隐藏终验失败的受信报告、失败分类和挂起投影/事件也同事务保存，避免失败证据已落盘而停止状态未提交的缝隙。

公开开发检查通过后才执行冻结验收。冻结验收的input、expected及失败输出不进入修复提示。终验失败立即转人工，不用隐藏答案驱动下一轮。终验报告、候选事件引用及失败状态仍保留审计证据。这里的冻结用例由本机操作器提供，不是已经完成独立标注与统计效力验收的held-out基准。

通过两层检查仍只提交submitted/verified=false，独立验证节点继续等待；没有伪造双模型签名，也没有让修复后的父节点提前放行依赖。

## 冻结合同与旧版本兼容

TaskNode可选字段示例：

```json
{
  "schema_version": "loom.rebind/v1",
  "development_digest": "<64位公开开发用例摘要>",
  "max_repairs": 1,
  "stop_on_duplicate_source": true
}
```

以上对象作为节点repair字段，需与冻结evaluation_digest及limits同时批准。修复不能关掉重复源码停止规则，不能把max_repairs设为超过2或超过max_attempts允许的剩余次数。默认不启用；CLI需要显式--max-repairs 1或2。

新增repair=None只在序列化时省略该新字段，原evaluation_digest:null继续保留；旧0.4已批准计划的摘要和合同不重写。检查合同仍从批准定义重建，与缓存逐字段核对。

## 原子交接、停止与费用

RebindService只接受可信候选事件和公开评测事件的hash引用。它重新检查tenant/run/task/epoch、角色、plan/development/source摘要、已知模型返回及已结预算，然后在同一SQLite事务中直接递增running任务的epoch，保留同一容量槽、更新租约，追加failure.classified、repair.applied、task.claimed、回执和outbox。没有pending到重新claim之间的抢占窗口。旧epoch提交被拒，心跳切到新claim。

源码重复、尝试耗尽、预算不足或不可信证据会停止。超时、HTTP错误、未知/部分效果不会自动重发或退款；保留预留并转人工。暂停/终止/失租后不能在catch中重新取得发起权。已启动的容器仍由原限时和自身清理约束处理，不能据此承诺即时撤回所有在途操作。

failure.classified使用静态错误码/HTTP状态分类为quota、network、scope_denied、canceled、crash、auth、other；不解析或保存提供方错误文案。network不计模型能力错误。该分类和有界Rebind不是通用自动工具替代、全图回退或完整故障恢复实现。

结果usage、usage_cost_upper_microusd汇总本次cycle已收到有效响应的用量上界；usage_complete=false表示还有未知计费，已知部分为0不表示提供方没有计费。billed_currency_verified保持false。attempts_completed和repairs_applied分别记录有效响应次数与已授权修复次数；停止不映射到普通成功。

## 入口与证据

- CLI：scripts/run_local_task.py，可选development_tests任务字段和--max-repairs；先不带approve预览，再按预算明确启用付费请求。
- 本机可信Python接口：LocalTaskRunner.run_python_cycle(claim, acceptance_tests, development_tests, max_output_tokens=512)。没有新增模型执行HTTP入口。
- 安全摘要：GET /v1/runs/{run_id}/attempts及review.attempts，按租户鉴权，只返回事件/epoch/原语/分类/摘要等白名单标量，不返回源码、stdout、输入或答案。工作台待办页展示该摘要。
- 无付费验证：scripts/check_repair.py --image <本机已批准镜像ID>。模型HTTP使用MockTransport，候选在真实Docker执行；明确与真实模型探针分别记录。

完整验证数量见 [当前验证记录](validation.md)。0.4历史见 [0.4记录](validation-0.4.md)。
