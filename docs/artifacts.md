# 结构化候选、辅助输入批准与预览

本模块提供需求、设计、公开开发检查和代码审查意见的候选注册、历史查询、operator 批准及只读预览。普通 Worker 提交的来源为 `authenticated-worker-submission`；0.8 新增受限 analyst / architect 网关生成，0.9 合同扩展到 tester，来源为 `model-gateway-response`，由本机可信执行器核对响应与已结 usage 后保存。两类候选均保持 `producer_verified=false`、`counts_as_success=false`。结构校验、来源记录和辅助读取批准都不证明内容正确。

0.6 交付候选注册、明确批准与只读预览；0.7 增加获准 Python 执行器的辅助上下文消费；0.8 将同一精确输入绑定用于只读分析/设计生成。`GET context-preview` 本身仍返回 `execution_connected=false`，不调用模型、网络或工具，不取得 claim、不预留预算；该字段描述预览接口，不代表执行器没有消费能力。原 `depends` 硬依赖保持有效，上游只有 `submitted` 时仍不能放行下游；批准候选不会将任务改为 `done`，也不改变 Gate-V。

## 四类候选

候选 body 必须符合对应版本的严格 schema，拒绝未知字段、隐式类型转换、非有限数字、重复条目名称及过深/过大的结构。来源任务的冻结角色必须匹配，并具备 `artifacts.write`。

| kind | schema_version | 来源角色 |
|---|---|---|
| `requirements` | `loom.requirements/v1` | `analyst` |
| `design` | `loom.design/v1` | `architect` |
| `public-checks` | `loom.public-checks/v1` | `tester` |
| `review-findings` | `loom.review-findings/v1` | `reviewer` |

0.17新增审查意见：必须显式冻结review_subject；默认只读校验与模型生成/保存分开。操作员只能将明确findings批准给绑定同一原实现的fixer，no_findings不能触发修复。[执行协议](reviewer-execution.md)

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

每次新保存创建不可变候选和递增 revision，不覆盖旧内容。候选绑定租户、run、来源任务/节点、来源 epoch、批准计划摘要、schema、内容哈希和源事件。Worker 保存使用 `artifact.candidate_saved`，actor 为认证 subject；0.8 网关生成使用 `role.candidate_generated`，actor 为 `local-executor`。事件记录完整 typed body 和对应来源绑定；读取时重新核对内容及事件，网关候选另复核调用/响应与已结 usage，不把任意 task output 或模型自报可信标记当作来源证明。

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

## 0.8 只读角色生成与原子来源提交

新增可选 `role_output`，在计划批准时冻结，不能仅根据角色名推断授权：

```json
{"role": "analyst", "role_output": {
  "kind": "requirements", "schema_version": "loom.requirements/v1"
}}
```

architect 对应 `design` / `loom.design/v1`。0.8 范围不包含 tester 网关生成，该类型当时只可由认证 Worker 保存为开发检查候选；0.9 扩展见下节。来源角色须有 `artifacts.write`，`role_output` 不能与代码 `evaluation_digest` 或 `repair` 混用。省略或为 null 时单独省略新字段，旧计划摘要继续保留原有 `evaluation_digest:null`。

`RoleTaskRunner.run_role(claim, max_output_tokens=...)` 只发起一次获准 Flash JSON 响应，不提供沙箱、解释器、文件、浏览器或网络工具接口，不运行候选代码。辅助输入沿用 0.7 的精确 snapshot、原子准入和返回/提交复核；有必需输入时，先取得目标当前 claim，再按当前 epoch 由 operator 批准。来源换版本、target epoch 换代或 Rebind 都不能继承旧批准。

模型正文使用共享严格 decoder：拒绝重复 JSON 字段、非有限数、过深结构、额外字段、schema 不符、UTF-8/规范化 body 超过 32 KiB，以及截断响应；不会从代码块或任意文本中猜测 JSON。schema 通过只表示格式有效。

可信提交入口为 `submit_role_output(claim, key, *, call_id, reservation_id, request, snapshot)`，不接受调用者另传正文。它在同一命令事务中核对当前冻结合同、完整上下文、完成的 effect、匹配的网关 intent/done 事件和 settled reservation，从记录的响应提取 typed body，然后原子保存候选、`role.candidate_generated`、标准 `submitted` 输出、outbox 与回执。回执绑定完整 request 和完整 snapshot 摘要；同键异参拒绝。候选/事件/回执写入失败全部回滚，但此前已保存的模型响应与已知费用不会回滚或被当作零费用。

网关候选的 `model_provenance` 白名单记录 call/reservation、task/epoch/plan、role/output、内容及请求摘要、route/provider/model、usage、`usage_cost_upper_microusd`、网关事件哈希等；不公开原始请求、原始响应包装、提供方 request ID 或凭据。`model_source_recorded=true` 表示本机已核对来源记录，不是供应商收据、账单认证或独立正确性证明。旧候选 nullable 来源迁移保留原 body、源事件键和摘要；普通 HTTP 保存入口不能指定新的 origin/provenance。

任务输出以 `kind=role-artifact` 绑定 artifact ID、schema/content hash、plan 和 model call；辅助消费仍需显式精确批准。API/review 的来源摘要从核心候选及其当前 task/epoch/plan 关联重新核对，不能只信 Worker 输出自报的 `model_source_recorded`。没有第二模型时保持 `submitted`，`verified=false`、`producer_verified=false`、`counts_as_success=false`，不释放原 hard depends，也不更改 Gate-V。

### 本机角色入口与阶段证据

[run_role_tasks.py](../scripts/run_role_tasks.py) 与 [示例任务](../configs/role-task.example.json) 提供受限分析→精确批准需求→设计的本机入口，参数可用 `python scripts/run_role_tasks.py --help` 查看。默认预览不联网、不加载身份/模型密钥、不创建数据库或启动 Docker；显式 `--approve` 授权本机具名 operator/worker 流程，最多两次模型请求、无自动重试，可能计费。使用这些凭据不代表两名真实人员独立审阅。

示例中 spec 与 design 预先声明互无 hard depends，design 必需读取精确批准的 spec 辅助候选；independent_review 仍 hard depends 两个父任务。这是批准计划中的辅助数据交接，不能把 `submitted` 父任务伪装成已验证。脚本不执行代码，也不是七角色完整链。

本阶段已记录核心 **117 主/90 子**、角色执行及旧执行组 **109 主/14 子**、相关 API **48 项（新增15项）**、CLI **11项**通过。这些门槛范围重叠，不能相加作为版本总数；完整回归、实际 HTTP/浏览器和真实模型探针另行登记于[验证记录](validation.md)。下一步优先受限 tester / `public-checks` 及分析设计到开发的真实辅助交接，仍需保持开发检查与冻结验收分离，不让候选改写规则或硬依赖。

## 0.9 tester 候选与开发辅助交接合同

本轮先复核原 0.8 提交、166 文件源码包及现有服务/真实任务记录，再逐步扩展合同与执行接入。旧版证据保留，不将只读复核或各专项相加为新版本总数。

新增显式冻结输出如下；仍需匹配的 tester 角色和 `artifacts.write`，不按角色名称自动授予生成权：

```json
{"role": "tester", "role_output": {
  "kind": "public-checks", "schema_version": "loom.public-checks/v1"
}}
```

body 继续使用上文的 `PublicChecksBody`：`visibility` 只能为 `development`，最多32个不重名case，拒绝重复 JSON 字段、NaN/Infinity、额外验证字段、hidden/acceptance visibility、过深结构和超过32KiB的正文。生成结果只是未执行候选，不是受信测试报告。冻结角色配置中即使含 `tests.run`，这一只读角色入口也不调用测试工具或沙箱。

来源与原子提交协议沿用0.8：从已完成网关effect和settled usage提取正文；候选、来源事件、submitted、outbox/回执同事务；完整request和snapshot绑定幂等。`ModelProvenance.role`可为tester，但来源与schema有效仍不使`producer_verified`、`verified`或`counts_as_success`变为true。requirements/design、旧Worker来源、nullable迁移和省略None的旧摘要保持兼容。

同一批准计划内，developer可冻结声明requirements、design、public-checks为辅助输入，领取当前目标后由operator按精确源/目标epoch、revision、内容、plan及事件批准。三类body只进入模型user数据位；source/grant失效或换代必须停止，不静默换入最新候选。已有0.8 run不能追加未冻结的developer节点，也不通过本接口跨run导入来源。

**辅助输入grant不晋升测试。** 候选case可作为开发参考，但不自动成为执行器的development/acceptance用例，不覆盖`evaluation_digest`或`repair.development_digest`。本轮单run交接入口的宿主黄金验收先于首次模型请求冻结；最多4次Flash请求、8个宿主冻结Docker用例，无自动重试或repair。这里的Docker执行属于developer的原受限代码路径，tester生成阶段本身不执行case；通过仍只submitted，独立review的硬依赖和Gate-V保持不变。

下一独立步骤拟设计operator单独审阅生成检查、再冻结development digest的协议；必须明确批准的内容/来源、版本及执行权限，不能把现有输入grant解释为该授权。不得使用生成者的公开检查代替宿主冻结验收或第二模型独立复核。

已通过A核心及旧候选/合同/Store门槛 **131主/125子**，其中新增tester14主；与旧0.8阶段数量重叠，不累计。B执行接入、CLI/UI和完整/实际HTTP/浏览器/真实模型证据按完成顺序另记，未完成的门槛不据合同存在而预填通过。


2026-10-25阶段结果：合同/来源API与四角色CLI包含于617主/382子通过集合；真实原生23主/14子、HTTP163、浏览器17通过。单独真实tester1次通过，Docker20项与4角色真实全链受本机运行目录访问拒绝阻塞，未执行。生成检查仍是未运行辅助候选，完整交接未验收。


2026-10-25最终补记：Docker已用本机进程专用缓存恢复，0.9四角色辅助交接完整660主/396子、HTTP196、浏览器33及实际4Flash/5host用例通过。此前阶段阻塞记录保留，不能读作当前仍阻塞。生成公开检查依然未执行；单独审阅/冻结development是下一步骤，input grant不晋升为执行许可。


## 0.10纯编译子步验收（2026-10-25）

严格public-checks schema与freeze_tests双重校验、不可变编译文本/全新JSON树、source body/development/compiler摘要及只读CLI完成。43项新增测试、103主/71子兼容专项、完整703主/396子通过，零失败/跳过；两个既有真实tester候选各3case免费核对，不修改原候选/任务或执行生成用例。首次pytest长ID夹具错误保留并已修复。

接下来直接实现独立审阅/冻结注册表核心及反例，按docs/public-checks.md核对精确来源、编译版本、单独具名operator、幂等和事件/回执/outbox原子性。再实现新草案合同引用、API权限和启动重新核对；未实现前不接通执行，不改任何已批准计划。原62项FR、FR-051/052 partial与部署日期不变。


## 0.11独立审阅注册表核心验收（2026-10-25）

完成DevelopmentSuiteService预览、精确review_digest冻结和核对读取；来源版本/事件/编译/选择绑定、只追加、幂等并发、全事务回滚、stale历史均有反例。新增22本地/1真实NATS，完整726主/396子、零失败/跳过。两个0.9真实模型候选仅在隔离副本冻结，主库/任务/费用未改；没有新增模型或执行用例。

下一步直接接具名operator权限API/预览，再新草案suite引用与首次模型发起前核对。内部freeze依赖适配层认证，现有主API未开放；生产身份、执行授权和跨run来源引用均不能据内部记录存在而视为完成。旧计划/input grant/独立验证边界不变，FR-051/052及原日程继续追踪。


## 0.12审批API补记（2026-10-25）

公开检查套件新增精确预览GET、具名operator冻结POST和suite读取GET，OpenAPI27路径。suite.approve仅operator，不能由Worker/viewer/legacy或正文actor获取；冻结绑定精确review digest/选择及幂等头，计划/任务/费用不变。新增20项API测试，完整746主/396子零失败/跳过；独立副本39项实际HTTP和现有UI17项Chromium通过，无新付费。旧权限快照/HTTP路径夹具失败保留并已复测。

下一步新草案suite来源引用与执行前核对，再真实development/repair和工作台；当前冻结不授执行权，生成case未执行、第二模型待配置、Goal未成功。原62项及部署日期继续追踪。


## 0.13审阅套件执行补记（2026-10-25）

新增可省略的development_suite精确引用，旧计划/合同hash保持。存草案、具名operator批准、执行准入/返回/沙箱/提交与Rebind前验证同tenant来源、epoch/revision、suite事件/编译与摘要。零辅助items也绑定suite上下文；引用不复制模型来源、不改旧计划，未知intent预留保留。

产品CLI支持新引用任务，默认离线preview，显式approve须现有DB及私有operator/worker配置。自动载入注册表用例，拒绝混用raw development_tests；host验收单独冻结且失败不回流修复。GET suite给出当前execution_reference；API路线仍27条。

完整782主/396子，0失败/跳过，含48真实基础设施。真实run7bf5b872-fae6-4c4d-a154-f2de9470846b，1Flash、3生成+4host用例通过，552tokens/342µUSD上界、预留0、未核对账单，目标submitted/独立review pending。27新+33旧HTTP和17Chromium通过；源码/包/凭据另核。suite图形表单和完整七角色/生产/双签仍未完成。
