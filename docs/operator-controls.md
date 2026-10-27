# 本机人工控制与主体授权

记录日期：2026-10-23。人工操作使用具名本地主体，提供持久暂停、恢复、终止、未知模型调用核对和只读证据查看。它没有人工“强制完成任务”入口，也不会给待配置的独立模型环节制造签名。

## 身份与权限

| 主体角色 | 本轮权限 |
|---|---|
| operator | 查看、创建目标、保存/批准计划、暂停/恢复/终止和人工核对；不能认领或提交 Worker 任务 |
| worker | 查看、认领和提交任务；worker_id 必须等于认证 subject_id，不能冒名或行使人工控制 |
| viewer | 只读查看，包括待办、证据、事件和回放 |
| 旧 developer token | 保留旧本地开发接口；没有新增人工控制权限 |

本地主体配置由 `loom.auth.load_identities` 读取源码和公开 outputs 以外的私有文件；错误与 repr 不显示 token，不把模型 API key 用作本地身份。operator 与 worker 不能同时配置在一个主体上。`GET /v1/me` 返回当前 tenant、subject、角色与可用能力；业务接口仍隔离 tenant。

这些是本地 Bearer 凭据和软件角色，不是 OIDC、真实人员身份认证、独立双人审批或生产治理。拥有本机全部凭据的人仍能控制多个主体。不能把角色名称当作已经满足生产职责分离或高风险双人审批。

## 实际接口

| 用途 | 接口 |
|---|---|
| 人工队列 | `GET /v1/interventions` |
| 安全产物/费用摘要 | `GET /v1/runs/{run_id}/review` |
| 暂停、恢复、终止 | `POST /v1/runs/{run_id}/control/pause`、`.../resume`、`.../terminate` |
| 核对未知效果 | `POST /v1/runs/{run_id}/effects/{call_id}/reconcile` |

控制命令需要 `Idempotency-Key` 和 `{"reason":"有依据的操作说明"}`。actor 来自认证 subject，不能通过请求体伪造。同键、同参数、同主体返回原回执；换主体或参数会冲突。业务状态、完整事件、outbox 与命令回执在同一 SQLite 事务提交。

人工队列包含 paused、awaiting_human、失去 owner 的未知调用和 submitted 待独立验证，健康运行中的普通 intent 不自动算成异常。submitted 代表待复核，不能显示为成功交付。review 会区分受信执行器产生的 `execution.evaluated` 与 Worker 自报的 `tests.passed`；后者不会变成受信测试证据。

## 暂停、恢复与终止

暂停阻止之后的新动作，不清空已经发出的请求、不退回预留、不声称外部效果没有发生。已经开始的模型计算或沙箱线程不能因 HTTP 暂停命令而保证立即停止；模型取消不等于供应商免单，容器仍依赖资源/时间上限和清理机制。操作器暂停或取消也不会被迟到的 Worker 回调重新改成 open。

恢复必须单独、有审计地执行，只接受 paused/awaiting_human。草案、取消和成功等终态不能被恢复。任一 intent/indeterminate/partial 或未结 reservation 都会阻止恢复，包括“似乎还没发出”但没有足够证据的预留；不自动退款或再次发起。

核对完的待重试任务只会恢复为 pending，由正常调度取得新的 epoch；旧 claim 仍无提交权。恢复检查批准计划、逻辑任务累计费用/token 与 max_attempts，不能放宽原合同。示例真实代码任务的 max_attempts=1，失败后通常已无下一次尝试额度；应保留原记录并新建经审批的 Goal，不能通过恢复重置计数。已 submitted 的任务维持验证屏障，恢复不把它改成 done。

终止把 run 置 cancelled，并撤销非完成任务的执行权；保留已提交内容、费用与未知外部效果。未知调用仍需核对。cancelled 不计普通成功，迟到的超支结算不会把它复活。

## 未知模型调用人工核对

本轮只支持具有确定发起标识及对应 Python 预算 reservation 的模型生成调用；其他业务外部写没有兼容核对合同，不能套用模型逻辑。有效活 owner 存在时拒绝抢先核对，尤其不能把仍在途的请求认作未发送。

核对请求示意如下，字段是占位示意，不包含任何实际凭据：

```json
{
  "decision": "completed",
  "evidence": {
    "reference": "operator-ledger://reviewed-provider-record",
    "summary": "说明已核对的提供方记录、范围和依据，不粘贴密钥或密码。"
  },
  "actual_cost_microusd": 10,
  "actual_tokens": 20
}
```

decision 为 completed 或 not_sent。reference 最多 500 字符、summary 最多 2000 字符且均非空，额外字段拒绝。completed 按人工核对的实际用量结算；not_sent 必须有充分的人工证据并提交 0 费用、0 tokens。单凭超时、进程退出、没有响应或本地缺少完成记录，不足以得出 not_sent。

服务保存原始部分响应，另追加人工证据事件；不把人工说明替换成模型输出、源码、测试结果或验证签名。已结算费用不可改写，幂等重放不重复扣款/退款。实际用量超出预留如实记录为 overrun，不能通过恢复绕过 run/任务预算上限。

核对只处理未知状态与账务，不立即派发。即使调度器之前让 run 仍处 open，核对后也会要求显式恢复。not_sent 保留明确终态，原发起 grant 不会因“未发”说明而自动重新放行。上述证据由具名本地操作器负责判断，平台没有自动连接提供方账单证明其陈述为真。

## 无模型费用的 HTTP 检查

先启动配置了 operator、worker、viewer 三个同租户主体的本地服务，再从项目根目录执行：

```powershell
& '..\..\work\loom-venv\Scripts\python.exe' scripts/check_operator_api.py `
  --identities-file '..\..\work\loom-private\local-identities.json' `
  --base-url 'http://127.0.0.1:8796' `
  --report 'artifacts/operator-api-check.json'
```

私有文件名以实际启动配置为准。检查每次新建 UUID 前缀的测试 Goal，验证实际身份权限、默认七角色计划审批、拒绝冒名、submitted/下游屏障、伪测试不能自证、暂停/恢复/终止幂等、终态及事件 actor/回放。它不调用模型、不执行候选代码、不种入未知业务效果。旧 developer 控制拒绝由 API 专项覆盖，该脚本没有旧 token 输入时明确不检查，不能计成通过。

脚本只接受回环 HTTP origin，禁用代理和跨地址重定向，拒绝报告覆盖私有身份文件或写入其目录，不打印原始 HTTP 异常、token 或私有路径内容。报告只有固定检查标签和结果；任何身份 token 出现在响应或报告中都会使检查失败。
