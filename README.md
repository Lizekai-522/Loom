# Loom

基于Loom v2.1及[第四轮复审后项目策划书](docs/project-plan-2026-10-28.md)的Agent编排项目。**0.18.6**增加可冻结在批准任务中的模型请求体字节上限，超限先停止；0.18.5已将 Python 隔离测试接入固定工具注册表。此前提供私有副本恢复、执行器心跳及源码导出，第二模型仍待配置。[本轮验证](docs/validation-0.18.6.md) · [上下文上限](docs/context-budget.md) · [公网体验](docs/public-preview.md) · [本机启动](docs/local-dispatch.md)

0.17.4历史重测967主/382子、Chromium63项，三份源码下载与权威原始字节一致；0.17.5测试与真实基础设施证据单独登记。[历史验证](docs/validation.md)

10月29日18:00左右需要公网可体验，按用户选择采用本机临时Cloudflare HTTPS隧道；主机须保持在线。原62项及独立验证/生产门槛继续追踪。[交付路线](docs/release-roadmap.md)

## 当前可以做什么

- 将 Goal / Intent / Task、完整事件信封、幂等回执与 outbox 持久化到 SQLite。
- 从 HTTP 创建 Goal 草案，保存带 revision/digest 的计划，按审阅版本批准后在单一事务中创建任务；草案不可领取，批准后旧增任务入口和计划修改均被拒绝。
- 冻结 analyst、architect、tester、developer、reviewer、fixer、validator 的不同技能/工具权限、模型路由、依赖、指令、预算与判据摘要；七角色规划存在不表示七角色真实执行已经完成。
- 按依赖和优先级派发任务，原子预占共享容量，保留验证通道，拒绝旧 epoch 提交。
- Gate-P 使用受信范围、Anchor 和判据注册表；Gate-V 校验明确的双签成功状态与内容/规则/证据版本绑定。
- Worker HTTP 提交仅进入 `submitted`，无法直接置 `done` 或放行下游。可信验证入口不作为外部 HTTP 路由开放。
- 以具名本地主体区分 operator、worker、viewer；operator 不能认领/提交任务，worker 不能批准计划或人工控制，具名 Worker 的 worker_id 必须匹配认证主体。旧租户 key 仍作为 legacy developer 兼容本地 fixture，但没有 control 权限。
- 在 `/console` 创建目标、导入/保存/批准计划、查看任务和人工待办；查看证据时只将匹配当前 task/epoch/content hash 的真实事件作为执行证据，不信任候选自报通过。
- 用事务、幂等回执与审计事件持久化暂停/恢复/终止和有依据的未知模型调用核对；未知调用及未核对预留不会自动退款，终止不计成功。核对后也不会自动发起模型或放行依赖。
- 按 run 原子预留金额/token 上界，结算可幂等；实际用量超过预留时记录超支并挂起，避免隐藏账单。
- 以固定逻辑操作 ID 管理副作用发起记录；未知结果阻止自动再次发起。真实模型请求已接入这一流程，业务外部写仍使用本地桩。
- 图失效传播到下游并撤销旧 claim；已发生的外部效果保留，记录需补偿事项。
- 执行七角色的确定性软件研发样例，展示并行分支、一次验证失败、修复重跑、验收和只读回放。
- 将 SQLite outbox 发布到真实 JetStream，核对持久 PubAck；durable 消费以本地权威事件、inbox 和游标防重复应用。
- 在独立 PostgreSQL 16.15 测试库验证 21 表 DDL、两租户业务/审计 RLS、事件及 claim 约束；默认 API 尚未切换数据库。
- 使用真实 Ed25519 签署事件快照，并以独立配置的公钥白名单、事件前缀和高水位离线核验；本地密钥/文件不是 WORM 锚定。
- 通过私有配置接入 Flash JSON 模式，严格校验唯一 source 字段；只有获准的 developer/fixer 节点可由本机执行器运行 Python JSON 候选。宿主冻结黄金用例，Docker 无网络、只读根、非 root 且有资源/时间/输出限额，不传入密钥或宿主目录。
- 按批准的repair策略运行公开开发检查→有界Rebind→冻结终验；新epoch与输入版本原子交接，重复源码/预算/次数耗尽或未知调用时停止。隐藏验收失败不回流修复提示，通过仍只submitted。[修复入口与边界](docs/bounded-repair.md)
- 模型预算预留与发起意图同事务，暂停不再留下无effect预留；隐藏失败证据与挂起状态一并保存。工作台提供按租户过滤的尝试/修复安全摘要。
- 保存三类结构化候选，将来源具名 Worker、任务 epoch、批准计划、schema、内容摘要与源事件绑定；历史版本不可覆盖，旧版本保留并标 stale。
- operator 可按计划声明，将精确候选版本批准为辅助读取输入，同时绑定来源与目标 epoch；更新候选或换代后须重新审阅批准。
- 预览有效辅助数据与缺失项，标注未验证，最终规范化 UTF-8 响应限制为65536字节；不改变硬依赖、预算、签名或任务完成状态。[候选与预览](docs/artifacts.md)

## 安装与验证

从 GitHub 首次安装、创建私有配置并启动中文工作台，见 [首次启动指南](docs/github-start.md)。已有本机环境继续沿用现有配置。

Python 3.11+，建议使用独立虚拟环境。以下 PowerShell 命令在本项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe scripts/check_spec.py
.\.venv\Scripts\python.exe -m pytest tests --ignore=tests/integration -q
.\.venv\Scripts\python.exe -m loom demo
```

`requirements.lock` 固定本次验证的直接与传递依赖，包含本轮集成驱动。跨操作系统或 Python 版本安装时应重新验证解析结果。项目在 Windows / Python 3.13.12 下验证，未据此声称所有 Python 版本和平台已验证。本工作区已有源码虚拟环境可直接用 `../../work/loom-venv/Scripts/python.exe`，无需重新建环境。上面的命令只运行单元/本地套件；未配置基础设施时直接运行全部 pytest 会跳过 live 项，不能作为本轮完整验证。

样例输出在 `artifacts/demo-report.json`，数据库在 `artifacts/demo.db`。报告明确标记 `deterministic-fixture`、未使用真实模型和零真实外部写；其中模型名称、认证标记及成本都是测试数据，不能作为真实模型双签或业务效果证据。

## 启动本地 API

本次工作区继续沿用原启动器、SQLite 测试库和旧租户 token，并通过私有身份文件增加独立具名 token；已有模型密钥无需重配。服务已运行时不重复启动；原启动器可在前台运行：

```powershell
& '../../work/loom-venv/Scripts/python.exe' '../../work/launch_loom_first_round.py'
```

控制台入口为 <http://127.0.0.1:8796/console>，接口文档为 <http://127.0.0.1:8796/docs>。本机开发模式自动进入操作员界面，无需输入访问凭据；浏览器不接收Bearer或模型API密钥。静态资源无CDN，CSP禁止外部及内联脚本。重载或断开会清除待确认幂等键，须先核对列表再重发。[控制台](docs/console.md) · [本机身份](docs/local-auth.md)

以下是独立安装后的通用启动方式，私有身份文件须按文档预先创建，路径位于源码及公开 outputs 之外；示例路径需替换为自己的私有文件。API health 的 stage 仍为 S1-local，表示 SQLite 本机内核边界：

```powershell
$identities = 'C:\private\loom\local-identities.json'
.\.venv\Scripts\python.exe -m loom serve --port 8796 --identities-file $identities --local-console
```

API 只绑定回环地址。`/health`、静态控制台和接口文档可无认证读取；业务接口按 token 解析主体、角色与租户，不接受请求自行指定租户。不同主体使用不同 token；`LOOM_TENANT_KEYS` 仍可选保留旧本地开发身份，但它不能取得新增 control 权限。

示例 Goal 请求（创建后仍为不可执行草案）：

```json
{"title":"修复本地程序","description":"实现并验证已授权的变更","acceptance":["schema.valid","tests.pass","report.traceable"],"cost_limit_microusd":1000000,"token_limit":10000}
```

通过 `POST /v1/goals` 创建，再用 `PUT /v1/goals/{run_id}/plan` 保存定义及 expected_revision，最后以当前 revision/digest 批准。创建 Goal、保存/批准计划、控制/核对及旧 run/task/提交命令需要 `Idempotency-Key`。注册判据、计划审批与权限见 [API 契约](docs/api/README.md)。已实现本机动作角色边界，**尚未完成生产 OIDC、人员身份认证、双人审批或独立验证服务身份**；同一 Windows 用户管理多个 token 不证明多个独立人员。

本轮实际服务检查140项，另41项新增浏览器断言。两次新角色调用与套件Mock分别登记；旧fixture检查沿用原私有会话，不输出或重新生成token：

```powershell
$firstRoundSession = (Resolve-Path '../../work/loom-first-round-session.json').Path
& '../../work/loom-venv/Scripts/python.exe' scripts/check_api.py --session $firstRoundSession --report 'artifacts/api-validation.json'
& '../../work/loom-venv/Scripts/python.exe' scripts/check_goal_api.py --session $firstRoundSession --report 'artifacts/goal-api-validation.json'
```

这些实际服务检查单独登记，不重复计入 pytest 分阶段总数。真实生成代码执行入口是 `scripts/run_local_task.py`；预览、显式开始调用、项目私有密钥文件及费用/未知态边界均见 [本机执行文档](docs/local-execution.md)。私有文件留在 `work/loom-private/`，不复制到源码、公开报告或交付包；JSON mode 仍需经过平台严格解析。

## 重跑 S2 基础设施检查

本轮使用单独的 loopback NATS 和可丢弃 PostgreSQL 测试 cluster，没有把它们装成系统服务。运行时和私有测试配置保存在工作区 `work/loom-infra/`，不打包进源码。先按 [NATS 记录](docs/nats-integration.md) 和 [PostgreSQL 记录](docs/postgres-integration.md) 启动对应服务，再在项目目录执行：

```powershell
$infra = (Resolve-Path '../../work/loom-infra').Path
$image = (Get-Content '../../work/loom-sandbox-image.id' -Raw).Trim()
& '../../work/loom-venv/Scripts/python.exe' scripts/check_s2.py `
  --sandbox-image $image `
  --nats-url 'nats://127.0.0.1:4223' `
  --nats-server-bin "$infra/nats/nats-server-v2.15.1-windows-amd64/nats-server.exe" `
  --postgres-config "$infra/postgres-session.json" `
  --report 'artifacts/local-execution-alpha-validation.json'
```

`check_s2.py` 强制提供不可变 sha256 本机镜像 ID、broker URL、原生 broker 二进制和私有 PostgreSQL 配置，分开执行本地与真实基础设施两阶段，每阶段删除旧 JUnit；任一跳过、零用例、缺失/无效报告或失败均不计通过。Docker 真实执行，模型网关使用 Mock，套件不调用真实模型。PostgreSQL 检查会重建明确标为可丢弃的 `loom_test_*` 库中的 `loom` schema；不要传入用户数据库配置，凭据只从私有文件读取。故障测试只操作测试自建 broker/容器，不停止共享服务。

需要单独把已存在样例库的 outbox 发送到本轮测试 broker，可使用：

```powershell
.\.venv\Scripts\python.exe -m loom queue-drain --db 'artifacts/demo.db' --server 'nats://127.0.0.1:4223'
```

传输语义为至少一次，broker 去重窗口不替代本地 inbox。离线检查点目前提供 Python API，使用方式与密钥/高水位信任边界见 [检查点文档](docs/checkpoints.md)，没有新增 HTTP 自动签名接口。

## 代码与设计

| 路径 | 用途 |
|---|---|
| `src/loom/store.py` | SQLite 事务适配、事件、outbox/inbox、租约、预算、副作用状态 |
| `src/loom/policy.py` | 确定性门禁、人工例外、进展判断、任务/Agent 选择与上下文预算 |
| `src/loom/api.py` | FastAPI/Pydantic 本地认证 API |
| `src/loom/auth.py`、`src/loom/operator.py` | 本机主体/角色、持久人工控制及未知模型调用核对 |
| `src/loom/web/` | 无外部依赖的中文控制台、自动本机连接与纯文本证据呈现 |
| `src/loom/workflow.py` | Goal 草案、版本化 DAG、审批与七角色冻结合同 |
| `src/loom/artifact_schemas.py`、`src/loom/artifacts.py` | 结构化候选、精确辅助读取批准、只读预览与静态字节限制 |
| `src/loom/model_gateway.py` | 私有配置、Flash JSON 模式、用量及受控错误边界 |
| `src/loom/execution.py` | 获准 developer/fixer 节点的真实模型与隔离执行适配 |
| `src/loom/sandbox.py`、`sandbox/Dockerfile` | Docker Linux 预检、有限 Python 工作区及宿主黄金用例判定 |
| `src/loom/demo.py` | 本地样例执行器与固定测试工具 |
| `src/loom/jetstream.py` | 真实 JetStream 发布、durable 消费及本地去重适配 |
| `src/loom/checkpoints.py` | Ed25519 快照签名、独立公钥与高水位离线验证 |
| `packs/software-dev/workflow-v1.json` | 可保存/批准的七角色计划模板，尚未真实执行全链 |
| `tests/` | 审查反例、并发、恢复与 API 集成测试 |
| `docs/` | 修订方案、实施计划、GitHub 复用记录、验证记录与后续生产工作 |
| `migrations/postgres/` | 已在隔离 PostgreSQL 16.15 执行的 DDL；完整 Store 适配尚未实现 |
| `scripts/check_s2.py` | 强制四项基础设施配置、分阶段且拒绝 skip 的检查入口 |

[实施计划](docs/implementation-plan.md) · [有界修复](docs/bounded-repair.md) · [控制台](docs/console.md) · [本机身份](docs/local-auth.md) · [本机真实执行](docs/local-execution.md) · [Docker](docs/local-sandbox.md) · [GitHub 复用选型](docs/github-reuse.md) · [第三方声明](docs/third-party-notices.md) · [PostgreSQL](docs/postgres-integration.md) · [NATS](docs/nats-integration.md) · [检查点](docs/checkpoints.md)

## 当前边界

候选来源为认证Worker提交，内容及来源模型未验证。预览GET的`execution_connected=false`表示该查询本身不执行；0.7 Python执行器可在有效claim/精确批准下消费typed辅助数据。准入事件不是供应商收据，不授予依赖完成或Gate-V权限。静态字节限制不等于token裁剪，FR-051/052仍partial；Rebind换代须重新批准，最后epoch停止可能需新Goal。

1. API 和任务内核仍由 SQLite 通过数据库事务在**单主机**协调。独立 PostgreSQL DDL/RLS 检查已通过，不代表完整 PostgreSQL Store、多主机调度或主备灾备完成。
2. Ed25519 检查点可以发现与独立受信快照不一致的截断、改写和历史分叉；必须保护公钥白名单和高水位。相同本机账号能同时改 DB、密钥与高水位时没有独立安全边界；本地文件不是 WORM、HSM 或远端不可回滚锚定。
3. 事件检查点使用真实密码学验签；Docker 黄金用例由宿主判定，但真实模型产物仍未取得独立双模型签名。固定演示中的认证标志不是实测模型身份，检查点签名也不证明模型结论或测试指标为真。当前七角色配置路由到同一 Flash，不能视为两个独立模型。
4. `complete`、`finish_run` 等 Python 入口属于可信验证/验收进程；不能把不可信 Worker 作为同进程插件导入。HTTP Worker 只有提交入口。
5. NATS 已实现并在真实单节点验证至少一次传输、durable 消费和进程重启去重。尚未完成生产 accounts/TLS、R3 集群、物理断电耐久与多节点分区验证；强杀后 broker ACK 进度可能回退，本地 inbox 负责防重复效果。
6. Flash 请求使用固定费率版本、保守输入上下文上界和输出上限预留，按有效usage结算；0.4真实探针262µUSD是历史上界记账，0.5/0.6轮次均没有新增付费请求。取消、超时和不确定响应可能仍计费，须保留预算与待核对状态，不能宣称账户级计费硬封顶。
7. 真实模型调用已经接入受控发起/未知态流程；业务外部写及跨灾备窗口恢复尚未完成，不承诺真实零重复。
8. 当前回放重建已实现的业务投影；全量快照恢复、跨版本事件升级、全部生产投影和外部效果补偿仍待实现。
9. 已实现批准的同节点有界Rebind真实执行路径；Substitute仍只有本地fixture记录。完整图RepairPlan、六种原语、工具/模型替代、动态改图、模板库和经验库尚未全通。
10. 事件接口暂为本地整链读取；分页、SSE、生产限流和大规模性能测试尚未实现。
11. 旧run_local_task.py创建implement → independent_review两节点；新run_context_task.py创建spec、implement、independent_review三节点，spec用于明确导入输入，真实模型仅执行获准开发节点。公开开发检查可驱动同节点有界修复；人工控制覆盖暂停/恢复/终止/未知核对。真实七角色协作、全部修复循环、任意仓库与依赖安装、生产部署及完整FR001–062仍未验收。

## 复用与授权

当前实际复用 FastAPI、Pydantic、Uvicorn、nats-py、cryptography，以及 PostgreSQL 集成驱动 psycopg，通过锁版本依赖接入；没有将第三方编排框架作为 Loom 内核。30 个发行包的 36 份许可/署名文件保存在 `third_party/licenses/`。NATS Server 与 PostgreSQL 便携二进制只用于本机隔离试点，来源与摘要见各集成文档，未进入源码包。Temporal、LangGraph 仍为研究候选。

本项目新增代码未在本次工作中指定开源许可证；第三方组件继续按各自许可证使用。

0.7新增入口：[run_context_task.py](scripts/run_context_task.py) 默认仅预览；显式`--approve`才创建本机Goal、通过具名HTTP身份导入/批准输入并调用Flash。详见[输入与执行合同](docs/artifacts.md)及[本机执行](docs/local-execution.md)。

0.8新增入口：[run_role_tasks.py](scripts/run_role_tasks.py)，默认仅预览；显式`--approve`才经具名HTTP身份建立有界分析/设计任务。模型来源已绑定不等于内容通过独立验证；旧Worker候选不获模型来源标签。参见[角色合同](docs/artifacts.md)和[本机执行](docs/local-execution.md)。
