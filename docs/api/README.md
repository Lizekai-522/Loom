# 本地 API 契约

此目录的 `openapi.json` 由当前 FastAPI 应用生成，描述实际可运行的 HTTP 子集。当前版本 **0.19.2**；本机 [控制台](http://127.0.0.1:8796/console) 与 [接口文档](http://127.0.0.1:8796/docs) 同源提供。设计方案附录中的其余接口仍属后续目标，未因列入规范而自动实现。

`/health` 返回 version，stage 仍为 `S1-local`，表示 HTTP 内核继续使用单实例 SQLite；`external_writes_enabled=false` 表示此 API 没有执行真实外部写的路由。本机脚本的真实模型调用另见 [本机执行说明](../local-execution.md)。`/integrity` 的 `external_checkpoint: "not_configured"` 表示该 HTTP 实例尚未配置独立检查点；离线 Ed25519 组件不在普通查询时自动签名，使用方法见 [检查点说明](../checkpoints.md)。

| 动作 | 实际接口 |
|---|---|
| 当前认证主体与动作权限 | `GET /v1/me` |
| 本租户执行器最近心跳/固定工具目录 | `GET /v1/executor-status`、`GET /v1/tools`；只读，不等于健康或执行授权 |
| 本租户持久配额与计数 | `GET /v1/quotas`；只读，实际模型及已登记 Python 工具准入在相应 effect 事务核对，工具不新增模型费用预留 |
| 本机工具未知态核对 | `POST /v1/runs/{run_id}/tool-effects/{call_id}/reconcile`；具名操作员，公网禁止，不改费用或自动重跑 |
| 独立验证能力 | `GET /v1/verification-capabilities`；鉴权只读，区分签名核心与真实配置/完成入口，不接受HTTP密钥登记 |
| 创建 Goal 草案 | `POST /v1/goals` |
| 列出本租户目标/读取目标 | `GET /v1/goals`、`GET /v1/goals/{run_id}` |
| 保存/读取版本化计划 | `PUT /v1/goals/{run_id}/plan`、`GET /v1/goals/{run_id}/plan` |
| 任务可选上下文上限 | 节点 `limits.context_limit_bytes`，256–262144 整数，随计划批准冻结；省略保持旧合同兼容 |
| 批准当前审阅计划并创建任务 | `POST /v1/goals/{run_id}/plan/approve` |
| 创建/查询 run | `POST /v1/runs`、`GET /v1/runs/{run_id}` |
| 为旧 fixture run 创建任务 | `POST /v1/runs/{run_id}/tasks`；拒绝受管 Goal 的增任务请求 |
| 认领就绪任务 | `POST /v1/runs/{run_id}/claim` |
| Worker 提交待验证输出 | `POST /v1/runs/{run_id}/tasks/{task_id}/submit` |
| 候选列表/精确版本正文 | `GET /v1/runs/{run_id}/artifacts`、`.../artifacts/{artifact_id}` |
| 具名 Worker 保存结构化候选 | `POST /v1/runs/{run_id}/tasks/{task_id}/artifacts` |
| operator 批准精确版本为辅助输入 | `POST /v1/runs/{run_id}/tasks/{task_id}/auxiliary-inputs` |
| 只读辅助上下文预览 | `GET /v1/runs/{run_id}/tasks/{task_id}/context-preview` |
| 人工待办 | `GET /v1/interventions` |
| 当前任务/调用/预留审阅摘要 | `GET /v1/runs/{run_id}/review` |
| 执行尝试与修复安全摘要 | `GET /v1/runs/{run_id}/attempts`，亦包含在review.attempts中；不返回源码/输入/答案/stdout |
| 暂停/恢复/终止 | `POST /v1/runs/{run_id}/control/{action}`，action 为 pause、resume、terminate |
| 核对未知模型调用 | `POST /v1/runs/{run_id}/effects/{call_id}/reconcile` |
| 事件/历史回放/完整性 | `GET /v1/runs/{run_id}/events`、`.../replay`、`.../integrity` |

设计中的独立 `/v1/tasks` 入口、SSE、分页、最终验收裁决及模型/业务副作用 HTTP 执行接口尚未开放。人工控制和未知调用核对已开放给具名 operator，但不授予最终成功或模型签名权限。本地任务路径包含 run，避免隐含跨 run 归属。真实 Flash 网关与 Docker 适配器仍通过 Python/独立脚本接入，没有新增普通 HTTP 执行或密钥上传路由。

业务接口从 Bearer token 映射租户、具名主体和角色。operator 可以创建/审批计划和人工控制，不能认领/提交任务；worker 可读取、认领与提交，不能批准或控制；viewer 只读。具名 Worker 请求的 worker_id 必须等于认证 subject_id，不能借用同租户其他 Worker 的身份。旧租户 key 映射 legacy developer，保留 fixture 兼容动作但没有 control 权限；它不代表具名职责分离，生产应移除该兼容面。[本机身份说明](../local-auth.md)

创建 Goal、保存/批准计划、控制/核对、旧 run/task 创建和产物提交均需 `Idempotency-Key`。同键同参返回原回执，同键异参或异操作返回 409。所有 Goal、计划、run、任务、核对和事件访问受租户归属限制，跨租户资源 ID 返回 404；目标列表和人工待办只包含本租户数据。缺认证返回 401，无动作权限返回 403。

结构化候选保存及辅助输入批准也需幂等键。仅具名worker可保存且worker_id必须等于认证subject；operator/viewer/legacy不能冒充来源。仅具名operator可批准辅助输入，worker/viewer/legacy均拒绝。批准绑定artifact_id、plan_digest、source_epoch、target_epoch、revision和content_hash，须符合冻结input_artifacts选择器；旧版本或换代不自动沿用。候选body限32768字节，最终preview限65536字节，均按规范化UTF-8计算。旧普通submit的256KiB限制保持原语义。[完整候选合同](../artifacts.md)

preview中的ready只表示必需辅助数据已有有效批准；producer_verified=false、counts_as_success=false、execution_connected=false。它不表示任务可执行、依赖完成或通过验收，不读取通用输出、隐藏终验报告、宿主文件或远程URL，该预览GET本身不发起模型；0.7本机Python执行器可另按当前claim与精确grant消费辅助数据，不增加HTTP执行入口。

创建 Goal 的请求包含 title、description、acceptance，以及可选的 cost_limit_microusd/token_limit。acceptance 只能引用当前三个已注册判据：schema.valid、tests.pass、report.traceable。目标描述是任务数据，不能授予工具、系统权限或验收成功。

```json
{"title":"修复本地程序","description":"实现并验证已授权的变更","acceptance":["schema.valid","tests.pass","report.traceable"],"cost_limit_microusd":1000000,"token_limit":10000}
```

创建返回 run_id，run 状态为 draft，plan_revision=0。草案没有可领取任务。保存计划时提交 `{"definition":<工作流定义>,"expected_revision":0}`；服务校验代码登记的七角色、差异化技能/工具、local-workspace 范围、DAG 依赖和资源上限，成功后 revision 增加并返回执行字段 digest。实际模板是 `packs/software-dev/workflow-v1.json`；它默认没有冻结执行用例摘要，不能直接充当真实执行授权。

审批时提交当前 `expected_revision` 与 `expected_digest`。只有与数据库内当前定义完全一致的版本才获批，审批在单一事务中核对任务预算总和、冻结七角色合同、创建全部命名空间任务并把 run 改为 open。旧 revision/digest、审批后编辑、从旧 tasks 路由注入任务以及预算超配都会被拒绝。合同包含角色工具/技能、模型别名、指令、依赖、预算、anchor、plan_digest 和可选 evaluation_digest；真实执行还需绑定宿主冻结测试的摘要。

输出提交只产生 `submitted`，不因候选自报通过放行后续依赖；受信验证引擎的 `complete` 与验收引擎的 `finish_run` 不暴露给 HTTP Worker。0.3.0 真实探针通过 4 个黄金用例后仍为 submitted、awaiting_independent_models。用户确认本阶段双签待配置，本轮人工操作也不能将其读作完整 Goal 成功或双模型已验收。

## 人工控制、核对和证据

control 请求体为 `{"reason":"可追查的操作理由"}`。暂停只阻止新动作，不把在途请求当作未发送或退款。恢复会检查未知调用、在途 owner、冻结计划及预算/尝试上限；不能恢复 draft/终态、自动增加尝试次数或把 submitted 改为 done。终止撤销旧执行权并保留未知效果与未核对预留，run 为 cancelled，不计成功。

核对请求体如下，仅在提供方记录等证据确实可核实时填写：

```json
{"decision":"completed","evidence":{"reference":"本机核对记录编号","summary":"提供方记录与实际用量核对说明"},"actual_cost_microusd":0,"actual_tokens":0}
```

decision 为 completed 或 not_sent；上面零值只展示结构，不能代替实际核实用量。not_sent 必须同时为零费用、零 tokens。接口只接受当前未知模型生成调用，并拒绝仍有 live owner 或不匹配的任务/预留；一般业务副作用不因此获得自动恢复能力。核对以事务原子记录状态、预算结算、回执和完整审计事件；相同幂等命令不会双次退款或结算。核对不自动恢复任务，不生成可信签名，也不算验收成功。

人工待办列出 status、task_counts、unknown_effects 和 awaiting_independent_verification。review 返回任务、调用、reservation 的安全摘要；产物上的“通过”字段不直接作为可信证据，执行证据必须对应真实执行事件、当前 task、epoch 及内容摘要。历史 attempt 或未绑定的 Worker 自报字段不能冒充当前已验证产物。事件链哈希验证本身也不证明模型结论正确。

这些能力属于本机受控开发。已实现本机角色动作授权，仍未完成生产 OIDC、真实人员认证、独立验证服务身份或双人高风险审批；同一操作系统用户管理多个 token 不证明多个独立人员。计划批准、人工核对和 Gate-V 验收各自保留边界。旧 fixture run 使用 local-fixture，受管 Goal 使用 local-workspace；二者都不授予任意宿主目录访问。金额为整数微美元，1 USD = 1,000,000 microusd。

接口拒绝未知字段、不严格的预算类型、非有限 JSON 产物和超过 256 KiB 的产物字段；这不是通用生产请求限流/反 DoS 网关。事件整链读取是本地限制，生产分页与流式传输另行实现。

CLI 通过 `loom serve --identities-file <源码和公开outputs之外的私有JSON文件>` 加载具名主体。现有启动器保留首轮旧身份，并增加独立 operator/worker/viewer token；不要打印、截图或复制这些凭据到源码。控制台只把登录凭据留在页面内存，无 CDN、外部资源或内联脚本；重载/退出后幂等键不持久化，须先核对列表再决定是否重发。[控制台说明](../console.md)

0.6历史为469主/302子、100HTTP/46浏览器；本轮0.7为530主测试、326子测试，零失败/跳过；另116项实际HTTP、29项新增真实浏览器断言通过。完整门槛与单独真实模型证据见[验证记录](../validation.md)，候选/绑定摘要仍不等于独立验收。

`attempts`/`review.attempts`可包含可信`execution.context_bound`摘要：task/epoch/plan、context_digest、context_bytes、context_item_count及固定未验证标记；不返回refs、body、worker身份或隐藏测试。该事件仅表示本机准入，不能证明供应商收到。

## 0.8 角色来源

计划可显式冻结`role_output`：analyst→requirements或architect→design，不能混代码验收/repair。HTTP路由数仍24，没有新增付费执行或密钥上传入口。`review`返回冻结role_output；仅在核心来源及当前task/epoch/plan/body hash/schema/call完全匹配后返回`artifact.model_source_recorded=true`和白名单标量`model_provenance`。Worker自报/旧epoch/替代引用均不取得该标签。`attempts`新增`role.candidate_generated`，只展示与已核验候选真实source_event_hash匹配的安全摘要，不复制body/提示或provider request_id。来源记录不授予trusted_evaluation或依赖完成权限。候选列表/详情的model_provenance为核心验证过的完整白名单，字段形状与review标量摘要不同。


## 0.9 tester来源

路径仍24个；显式role_output新增tester→public-checks/loom.public-checks/v1，visibility仅development。review/attempts来源白名单允许tester并投影public_checks_executed=false；trusted_evaluation=false/cases=[]/hard_criteria_passed=null保持，Worker自报不晋升。没有新增HTTP自动模型触发入口，本机CLI仍需原权限、预算与精确epoch批准。


## 0.12审批API补记（2026-10-25）

公开检查套件新增精确预览GET、具名operator冻结POST和suite读取GET，OpenAPI27路径。suite.approve仅operator，不能由Worker/viewer/legacy或正文actor获取；冻结绑定精确review digest/选择及幂等头，计划/任务/费用不变。新增20项API测试，完整746主/396子零失败/跳过；独立副本39项实际HTTP和现有UI17项Chromium通过，无新付费。旧权限快照/HTTP路径夹具失败保留并已复测。

下一步新草案suite来源引用与执行前核对，再真实development/repair和工作台；当前冻结不授执行权，生成case未执行、第二模型待配置、Goal未成功。原62项及部署日期继续追踪。


## 0.13审阅套件执行补记（2026-10-25）

新增可省略的development_suite精确引用，旧计划/合同hash保持。存草案、具名operator批准、执行准入/返回/沙箱/提交与Rebind前验证同tenant来源、epoch/revision、suite事件/编译与摘要。零辅助items也绑定suite上下文；引用不复制模型来源、不改旧计划，未知intent预留保留。

产品CLI支持新引用任务，默认离线preview，显式approve须现有DB及私有operator/worker配置。自动载入注册表用例，拒绝混用raw development_tests；host验收单独冻结且失败不回流修复。GET suite给出当前execution_reference；API路线仍27条。

完整782主/396子，0失败/跳过，含48真实基础设施。真实run7bf5b872-fae6-4c4d-a154-f2de9470846b，1Flash、3生成+4host用例通过，552tokens/342µUSD上界、预留0、未核对账单，目标submitted/独立review pending。27新+33旧HTTP和17Chromium通过；源码/包/凭据另核。suite图形表单和完整七角色/生产/双签仍未完成。
