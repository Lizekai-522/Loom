# 结构化候选、辅助输入批准与预览

本模块提供需求、设计和公开开发检查的候选注册、历史查询、operator 批准及只读预览。来源固定标记为 `authenticated-worker-submission`，表示由经过认证的具名 Worker 提交；`producer_verified=false`、`counts_as_success=false`。结构校验和人工辅助读取批准不证明候选内容正确，也不证明真实模型已执行。

0.6 交付候选注册、明确批准与只读预览；0.7 在此基础上增加获准 Python 执行器的辅助上下文消费。`GET context-preview` 本身仍返回 `execution_connected=false`，不调用模型、网络或工具，不取得 claim、不预留预算；该字段描述预览接口，不代表执行器没有消费能力。原 `depends` 硬依赖保持有效，上游只有 `submitted` 时仍不能放行下游；批准候选不会将任务改为 `done`，也不改变 Gate-V。

## 三类候选

候选 body 必须符合对应版本的严格 schema，拒绝未知字段、隐式类型转换、非有限数字、重复条目名称及过深/过大的结构。来源任务的冻结角色必须匹配，并具备 `artifacts.write`。

| kind | schema_version | 来源角色 |
|---|---|---|
| `requirements` | `loom.requirements/v1` | `analyst` |
| `design` | `loom.design/v1` | `architect` |
| `public-checks` | `loom.public-checks/v1` | `tester` |

需求示例：

```json
{
  "schema_version": "loom.requirements/v1",
  "summary": "对合法 JSON 输入返回计算结果。",
  "items": [{"id": "req-one", "text": "空列表的结果为零。"}],
  "uncertainties": ["异常输入的业务规则尚待确认。"]
}
```

设计示例：

```json
{
  "schema_version": "loom.design/v1",
  "overview": "将输入解析与计算分离。",
  "components": [{"id": "calculator", "responsibility": "计算已校验的数据。"}],
  "steps": ["读取 JSON，再处理合法列表。"],
  "risks": ["输入规模需要限制。"]
}
```

公开开发检查示例：

```json
{
  "schema_version": "loom.public-checks/v1",
  "visibility": "development",
  "cases": [{"name": "empty", "input": {"values": []}, "expected": 0}]
}
```

`public-checks.visibility` 只能为 `development`。这些检查仍是候选数据，不覆盖冻结 `evaluation_digest`、已批准 `repair.development_digest` 或任何宿主验收用例，也不会自动成为执行器的测试输入。

需求条目和设计组件分别最多 64 项；公开检查最多 32 项。条目 ID/用例名须使用以小写字母开头的小写字母、数字、下划线或连字符，最长 48 字符；正文文本另有字段长度限制。候选 body 在补齐 schema 默认字段后的规范化 JSON UTF-8 编码也须不超过 **32 KiB（32768 字节）**。这不是整个 HTTP 请求的大小限额。

## 冻结的辅助读取声明

目标节点可在审批前声明 `input_artifacts`，最多 8 项。以下声明允许读取 `spec` 节点的需求候选：

```json
{"input_artifacts": [{
  "source_node": "spec",
  "kind": "requirements",
  "schema_version": "loom.requirements/v1",
  "required": true
}]}
```

来源必须是同一工作流中的其他节点，角色与类型相符，不允许重复选择器。声明纳入计划摘要和冻结合同，批准后不可修改；目标还必须具有 `artifacts.read`。声明只允许后续显式辅助读取，不自动批准候选或添加执行边。未批准的必需项使预览 `ready=false`；0.7 执行器也会在生成前拒绝缺失的必需输入，不会改变调度器的 hard depends 规则。省略 `required` 时默认为 false。

未设置或为 `null` 的新 `input_artifacts` 字段不进入序列化；旧 `evaluation_digest:null` 保留，因此没有新增声明的旧版计划摘要和合同继续可读。[完整示例计划](../configs/workflow.artifacts.example.json) 保留 `spec → design` 硬依赖，可验证辅助批准不会绕过它。

## HTTP 接口与权限

以下路径均位于 `/v1/runs/{run_id}` 下，租户来自 Bearer 身份，不能由请求指定。

| 动作与路径 | 权限 |
|---|---|
| `GET /artifacts`，可选 `task_id` | 有读取权限的同租户主体；列表只返回元数据 |
| `GET /artifacts/{artifact_id}` | 有读取权限的同租户主体；返回精确版本 body 和 stale 信息 |
| `POST /tasks/{task_id}/artifacts` | 具名 worker；须持有该任务当前有效 running claim，且 `worker_id` 等于认证 subject |
| `POST /tasks/{task_id}/auxiliary-inputs` | 具名 operator；目标冻结声明必须允许该来源、类型与 schema |
| `GET /tasks/{task_id}/context-preview` | 有读取权限的同租户主体；只读预览 |

operator 不能作为 worker 发布候选；worker、viewer 不能批准辅助输入。legacy developer 保留读取兼容权限，但不能发布候选或获得 operator 批准权限。跨租户或跨 run 的候选/目标引用拒绝。新两个写接口均要求 `Idempotency-Key`。

保存候选的请求体如下，`body` 替换为上方对应示例：

```json
{
  "worker_id": "worker-one",
  "epoch": 1,
  "kind": "requirements",
  "body": {
    "schema_version": "loom.requirements/v1",
    "summary": "保持授权范围。",
    "items": [{"id": "req-one", "text": "只处理合法 JSON 输入。"}]
  }
}
```

示例主体和 epoch 应替换为实际认领结果。actor 由服务端填写为 `subject:<认证主体>`；客户端不能填写来源、验证状态或任意可信元数据。

批准请求必须精确匹配审阅时的版本：

```json
{
  "artifact_id": "替换为候选ID",
  "expected_plan_digest": "替换为64位小写十六进制计划摘要",
  "expected_source_epoch": 1,
  "expected_target_epoch": 0,
  "expected_revision": 1,
  "expected_content_hash": "替换为64位小写十六进制内容摘要"
}
```

目标尚未被领取时 epoch 通常为 0；领取后会变化，旧批准随之失效。新批准必须重新读取并提交当前目标 epoch。这里批准的是辅助数据读取，不是独立验证、任务执行、签名或最终验收。

## 版本、来源与事务

每次新保存创建不可变候选和递增 revision，不覆盖旧内容。候选绑定租户、run、来源任务/节点、来源 epoch、批准计划摘要、schema、内容哈希和 `artifact.candidate_saved` 源事件。事件同时记录完整 typed body 与认证 actor；读取时重新核对内容和事件绑定，不读取任意 task output 或模型自报可信标记。

辅助 grant 精确绑定候选 ID、revision、content hash、来源 epoch、目标 epoch、plan digest、源事件和 `artifact.input_approved` 批准事件。新 revision 不会自动代替旧版本；来源或目标换代、来源失效、候选/事件绑定不一致时拒绝使用。历史候选仍可查阅并标 `stale`，但不能据此重建有效预览。旧 grant 失效后需要 operator 显式重新批准当前版本。

候选或批准的投影、完整事件、outbox 和幂等回执在同一 SQLite 命令事务内提交，中途失败全部回滚。相同键、相同参数重试返回原回执；改参数、actor 或操作复用同一键返回冲突。回执是原命令结果，判断当前可用性应重新读取候选或预览。

## 预览与验证

预览只重建当前有效 grant 指定的白名单 typed body，返回 `items`、`missing_inputs`、`ready`、绑定摘要及未验证标记。已有 grant 变旧时返回冲突，不能静默换入最新候选。正文中的 URL、文件路径或指令只是数据，不自动解引用，也不能覆盖系统授权。

最终规范化 JSON 响应（含 `bytes` 和 `context_digest`）不超过 **64 KiB（65536 字节）**。`bytes` 是该最终响应的 UTF-8 字节数；`context_digest` 对排除这两个派生字段后的 context 求摘要。超过上限直接拒绝，不静默截断。这只是静态字节与结构约束，没有 tokenizer、语义相关性裁剪或 token 节省效果承诺。

[check_artifact_api.py](../scripts/check_artifact_api.py) 对回环 API 执行候选、权限、精确批准、版本失效及预览检查，读取本机私有具名身份文件，不调用模型。它会创建独立测试 Goal 和候选数据，应使用本机开发/测试实例；具体命令参数可用 `--help` 查看。该检查与后端专项、浏览器检查及完整版本门槛分别记录，不用接口存在或类型通过代替真实角色协作验收。

## 0.7 执行期辅助上下文

以下 Python 接口只供可信本机执行器使用，没有新增普通 HTTP 执行或模型密钥上传路由：

| 接口 | 行为 |
|---|---|
| `capture_execution_context(claim)` | 在有效 running claim 下只读重建精确 `loom.execution-context/v1` 快照；无 selector 返回 None，缺必需批准时拒绝 |
| `assert_execution_context(claim, snapshot)` | 只读重建当前绑定，与完整快照比较 |
| `assert_execution_context_in(db, claim, snapshot)` | 在调用者当前事务内执行同样复核，供结果持久化使用 |
| `admit_execution_effect(claim, snapshot, ...)` | 同事务复核当前快照、预留原预算、登记稳定 effect intent，并记录绑定摘要 |
| `submit_execution_output(claim, key, output, snapshot)` | 同事务复核快照并执行标准 `submitted` 提交、事件/outbox/回执 |

快照绑定目标 worker/epoch、当前计划和每项精确候选/grant/事件，包含白名单 typed body 及未验证标记，最终规范化大小仍不超过 64 KiB。目标续租不改变快照；来源换 revision、同候选新 grant 或任一 epoch 换代都会使旧快照不可沿用。比较覆盖全部字段，调用者自行重算一个被篡改快照的哈希也不能通过。

执行器只把 kind、schema 和 body 放入模型 `user` 消息的 `auxiliary_inputs` 数据位；本机主体、grant 和事件引用不发送给提供方。任务说明和候选文本不能改写 system 指令或工具权限。真实序列化请求还需通过网关大小检查；字节限制不等于精确 token 预算，金额/token 仍由现有预留和用量结算约束。候选中的公开开发检查可以作为数据被读取，但不会覆盖冻结测试或自动成为测试入口。

发起前、模型返回后、沙箱执行边界及结果持久化时重新复核绑定；最终复核与输出提交在同一事务，避免复核后换版本再提交旧结果。输入失效转人工处理并释放目标租约；已发生的响应和费用照常记账，不能因数据失效退款或宣称调用未发生。

`execution.context_bound` 记录 `context_digest`、`context_bytes`、`context_item_count`、`prompt_digest`、call/reservation ID 和精确 refs，不重复候选 body。该事件与预算/intent 同事务，是**本地准入记录**，不是供应商收到请求或已经消费上下文的收据。事务不跨网络持锁；未知效果继续阻止自动重发。

必须先领取目标，再由 operator 按实际目标 epoch 批准。pending epoch0 的批准不能用于首次 running epoch1；Rebind 的新 epoch 也不会自动继承原 grant，需要新的明确批准。没有 selector 时保留原模型请求、提交 shape 和旧回执，不额外写上下文事件。没有独立验证时，通过冻结用例仍只 `submitted`，不能放行原 hard depends 或计完整成功。

### 本机启动入口

[run_context_task.py](../scripts/run_context_task.py) 和 [示例任务](../configs/python-context-task.example.json) 提供单次受限 Python JSON 任务的预览/显式执行入口，参数见：

```powershell
python scripts/run_context_task.py --help
```

默认预览只读取任务与 Pack，不联网、不读身份或模型密钥、不启动 Docker；即使必须提供后续执行参数，未加 `--approve` 也不会加载那些私有文件。显式 `--approve` 会创建本机测试 Goal，通过具名回环 HTTP 发布需求候选、提交、领取目标并批准精确输入，再使用同一 SQLite 执行一次模型生成及 Docker 测试，可能计费。脚本先检查已有 DB 与 Store 兼容，新建 draft 后立即对账同库；不匹配时不继续审批或 claim，失败报告保留已核验的安全 run ID。

示例计划显式将 spec 作为辅助来源，implement 没有 spec 的 hard depends；这是预先批准的输入声明，不是运行时绕过依赖。来源仍为认证 Worker 提交的本地需求，未伪装真实 analyst 模型执行；independent_review 仍 hard depends implement。`model_dispatch_admissions` 只说明本地准入，收到已记录响应才标 `model_response_received`；未知状态不代表零费用。此入口不自动配置第二模型，不伪造签名或最终验收。
