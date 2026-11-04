# monsoon-harness 前期调查报告（吸收改造方向）

> 调查日期：2026-11-01。性质：**只读调查，未进入实施**。
> 背景：项目所有者提出新方向——rakazo 的内容**吸收后改造成自己的、适配自己的技术栈**（不是整仓 fork）；Loom 与 ms 原本一体、但可分开独立工作；多台 ms 同时向同一台 Loom 提交任务、互不踩踏。本报告为该方向提供事实基础与决策依据。
>
> 本地只读基线（本机路径，与原方案书的 /workspace 路径不同）：
>
> | 仓库 | 本机路径 | 钉住的版本 |
> |---|---|---|
> | rakazo | `~/projects/rakazo` | git 浅克隆，HEAD = `d13ef7afc1a7aab905bedda958c082a82b0e7559`（2026-11-01 提交） |
> | Realtime-Venus | `~/projects/rv` | tarball，main HEAD = `e53ae8d5bde92b30364b26f2e4c13c9db7a8ffea`（无 .git，SHA 以 ls-remote 为准） |
> | Loom | `~/projects/Loom` | v0.23.0（git 浅克隆） |
>
> 原方案书钉不住的 rakazo 主干 SHA 本机一次取到——上一轮的 TLS 失败与 API 限流在本机环境不成立。

---

## 一、三个仓库的现状盘点

### 1.1 rakazo：规模远超方案书的直觉

| 维度 | 事实 | 依据 |
|---|---|---|
| 总规模 | TS/TSX 约 **76.0 万行**，其中非测试约 **35.3 万行**，测试约 1,380 个文件 | 逐包 wc + git ls-files 交叉校验 |
| 最大包 | `packages/adapters` **25.8 万行**（343 个测试文件），按 provider 高度交织（沙箱 e2b/daytona/docker/boat/boxd/novita/railway/wuying/kubernetes/createos、pi-runtime、消息平台、语音、MCP、支付同包） | `packages/adapters/src/` 文件清单 |
| 其余大件 | apps/web 11.1 万行（非测试 5.7 万）、apps/mobile 11.8 万、apps/api 7.4 万（非测试 3.0 万）、packages/core 4.2 万、packages/db 3.4 万、packages/testkit 4.2 万 | 各 package 目录统计 |
| 数据模型 | Prisma schema **2,238 行、105 个 model** | `packages/db/prisma/schema.prisma` |
| 许可证 | **Apache-2.0**（"Copyright 2026 Rakazo contributors"），无独立 NOTICE 文件 | `LICENSE` |
| 活跃度 | 提交极活跃（PR 编号已到 #2996），15 个 CI workflow，上游持续演化 | `.github/workflows/`、git log |
| 技术栈 | TypeScript 7.0.2、React 19.2.3 + Vite + Tailwind 4.3.3、Hono 4.13 + oRPC 1.15、**PostgreSQL + Prisma**（无 Redis，后台任务用 Graphile Worker）、Better Auth 1.7.12、Electron + Expo | `apps/web/package.json`、`apps/api/package.json`、`README.md` Stack 节 |
| 外部服务 | Postgres 必选；S3 兼容存储、KMS/Infisical、Stripe、Resend/SMTP、OTEL/Sentry/PostHog 等均为可选 | `.env.example`（26 KB，等于一份全量配置清单） |

方案书引用的关键模型与字段**全部核实存在**：`Space.isDefault`（schema.prisma:378）、`SpaceMember.role`（:460）、`UserDevice`（:128）、`Bot.computerId/computerSwitching`、`Computer.scope/scopeKey/state/executionLeaseExpiresAt/executionFence/controlHolder`、`ComputerExecutionLease` 含 `@@unique([computerId, botId])`（:1453）、`Run.leaseOwner/leaseFence/leaseExpiresAt`、`DeploymentSettings.signupsEnabled/signupAllowlist/ownerUserId`（ownerUserId 标注 Deprecated，:495）、`ActionApprovalRule`、`McpServer`、`CapabilityInstall`。

**依赖图核对**：`contracts ← core ← db ← adapters ← api` 链式成立，但比方案书多三条边——`logging` 几乎被每层依赖；`adapter-kit` 在 db 之下且还被 auth、memory、api 依赖；`auth` 与 `memory` 绕过 adapters 直连 db。另有一个方案书未提的出入：**apps/desktop 依赖 `packages/adapters`**（apps/desktop/package.json），不是纯壳。

**adapter-kit 契约层（吸收的核心资产，约 3,535 行）亲自核验**：

| 契约 | 内容 | 位置 |
|---|---|---|
| `ConnectorTool` | `name / description / inputSchema` + 可选 `readOnly`（"false 强制审批；true 不放松基于名称的门禁"原文）、`batch`（批量工具逐动作判定）、`route`（进程内路由元数据，不暴露给模型）。**没有联网声明字段**——确认是 ms 要扩展的 | `packages/adapter-kit/src/types.ts:262` |
| `ConnectorProvider` | `describe / discoverTools / resolveCall? / execute` 四方法，**无取消动作** | `packages/adapter-kit/src/interfaces.ts:240` |
| `AgentRuntime` | `describe / run / abort(runId)`，abort 服务运行整体而非单次调用 | `packages/adapter-kit/src/interfaces.ts:331` |
| `ConnectorCall / ConnectorEvent` | 调用体：工具、参数、连接、executionId、route；事件流三类：`log / result / error` | `packages/adapter-kit/src/types.ts:294`、`:302` |
| `AdapterContext` | 12 字段全集（operationId、traceId、spaceId、userId、botId?、runId?、screenLeaseId?、keepScreenWarm?、cancelRunWork?、**signal（AbortSignal，不可序列化）**、connectedConnections?、connectedProviders?） | `packages/adapter-kit/src/types.ts:11` |
| 18 插槽注册表 | runtime、sandbox、memory、home、artifacts、secrets、jobs、realtime、notifications、models、connector、connection-auth、runner、voice、web、browser、cloud-agent、auto-review | `packages/adapter-kit/src/registry.ts`（`slots` 常量） |

**隔离判定与连接治理（亲自核验）**：`requireMembership`（未指定空间取默认空间，无成员关系抛 `IsolationError("No personal space")`）、`Actor.isDeploymentOwner` 硬编码 `false`、`scoped` 的"不区分不存在与无权访问"语义（`packages/db/src/scope.ts:4/:19/:43/:47`）；用户设备客户端"本机主动出网、服务器永不反向连（:31 原话）、全抖动重连（:70）、丢失响应不重试（:127）"（`packages/adapters/src/user-device-client.ts`）；宿主沙箱"docker + this-mac → desktop、`hostRoots: [homedir()]`、命令放行检查"（`packages/adapters/src/host-aware-sandbox.ts:25/:35/:36-38`）。

**方案书没提但值得注意**：`packages/testkit`（4.2 万行一体化 QA：integration/e2e/topology/canary/evals/computer-replay）；`examples/plugins/` 两个插件范例定义了 plugin.json/skills 格式；`SETUP_PROMPT.md` 是高密度操作说明；`SECURITY.md` 的 prompt-injection 与数据外发策略本身可当安全设计参考；AGENTS.md 的迁移规则（删列需 deferred 迁移）与"热路径用 Postgres claims/leases 跨进程协调"的并发原则。

**吸收视角的坑**：adapters 一个包是其余核心之和且按 provider 交织，"按包吸收"不现实，只能按文件/概念粒度裁剪；desktop 依赖 adapters，砍 adapters 需连带处理 desktop-sandbox 与命令守卫；上游活跃意味着 fork 会持续漂移，吸收应以当前快照为准。

### 1.2 RV：方案书描述与当前版本完全吻合

代理逐条核对了 9 组锚点：**8 组行号完全相同、零漂移**。要点：

- 五处扩展边界全部确认（`harness/bridge/serving.py:464`、`harness/agents/contracts.py:80`、`harness/llm/backends.py:12`、`harness/core/backend.py:16`、`harness/bridge/host.py:95`、`harness/skills.py:19/:82/:120/:34`、`harness/factory.py:8/:21`）。
- `DelegateBackend` 确为 plan / execute / oralize / aclose 四方法。
- **`AgentSkill` 只有 `plan` 一个方法**（无执行侧方法），但实际签名比方案书写的 `plan(planner, arguments)` 宽——还有 `played_reply_context / session_context / report_progress` 三个可选 kwargs（executor 用 `inspect.signature` 动态注入，`harness/bridge/execution.py:397`）。
- `_require_planner` :388 是调用点，定义在 `execution.py:982`。
- 工作状态确实落本地 SQLite：`WorkJournal`（`harness/jobs/feedback.py:69`，表 `works`，默认路径 `workspace/work-state.sqlite`，环境变量 `WORK_JOURNAL_PATH` 可覆盖），注释明示"No execution replay on recovery"。
- 规模：Python 约 1.82 万行；harness 内核依赖极薄（Pillow、av、httpx），torch 等重依赖只在 demos。

**对吸收路线的直接含义：RV 与"我们的栈"同为 Python 3.11+，同栈后 RV 执行器可以从"TS 宿主 + MCP 外挂桥接"变成 Python 进程内/子进程直连，批次五最重的一块（planner 注入 + 路由到执行的衔接）成本显著下降。**

### 1.3 Loom 0.23.0：锚点基本零漂移，但方案书的三处"待新增"有一处已经做完了

用脚本对方案书与交接文档的 41 个 file:line 锚点做了逐行核对：**38 个逐字命中，1 个措辞差异，3 个占位跳过**。唯一措辞差异是 `src/loom/store.py:1` 的定位注释，现文为"Durable single-host adapter. PostgreSQL/multi-host guarantees are separate work."，语义与方案书引用一致（旧引文应是更早版本的原文）。

**重要更新：Loom 0.23.0 的 16 动作契约面真实状态**（逐条对照 `src/loom/api.py` 路由清单）：

| 契约动作 | 0.23.0 现状 | 依据 |
|---|---|---|
| 读取计划 `GET /v1/goals/{run_id}/plan` | **已存在**（方案书标记"需新增"已过时），返回 status/revision/digest/definition/task_ids，正好是批准入参 | `src/loom/api.py:327`、`src/loom/workflow.py:453`、`_plan` 于 `src/loom/workflow.py:290` |
| 读取状态 `GET /v1/runs/{run_id}/progression` | 已存在且**已含 plan_revision**（方案书"不含修订号"已过时） | `src/loom/api.py:352`、`src/loom/progression.py:23` |
| 事件游标 `after_seq`/`limit` | **仍缺失**——`events` 处理器直接返回全量，无游标参数 | `src/loom/api.py:543-545` |
| 副作用登记 `POST /v1/runs/{run_id}/effects` | **仍缺失**——只有 reconcile（裁决）与 tool-effects reconcile，没有"动作开始前登记意图"的入口 | `src/loom/api.py:378`、`:383` |
| 续租 `POST .../renew` | 仍缺失（存储层 `renew_claim` 在 `src/loom/store.py:376` 已有；Phase 1 本来就不使用） | `src/loom/api.py` 无 renew 路由 |
| 其余 12 个动作 | 全部已存在（含 `control/{action}` 合一动态路由 `src/loom/api.py:372`） | api.py:239-551 路由清单 |

**批次三安全项在 0.23.0 仍全部待做**（逐行核实）：权限表 operator 仍持 `plan.approve`、worker 仍无 `control`（`src/loom/auth.py:21-23`）；legacy 租户密钥仍派生 `developer` 主体（`src/loom/auth.py:91-95`，subject 为 `legacy:<tenant>`）；批准接口只查能力不查"是不是人"（`src/loom/api.py:335-338`，仅新增了 reviewed-suite 场景的 `suite.approve` 额外检查）。

**多机不踩踏机制在 Loom 侧的支撑现状**（结论：机制齐备，缺的两个口子都在批次三清单里）：

| 机制 | 现状 |
|---|---|
| 租约 + epoch 栅栏（领取带 worker 标识与租约、提交带 epoch 与幂等键） | ✓ `src/loom/api.py:529-541`、`src/loom/store.py:308-339` |
| 幂等键 (tenant, key) 主键、租户内共享 → ms 侧四段式机器前缀即可 | ✓ `src/loom/store.py:77-79` |
| 双具名主体 + operator/worker 互斥 + 主体唯一登记 | ✓ `src/loom/auth.py:68`、`:101-102` |
| 暂停硬闸门（claim 只在运行 open 时发任务；租约回收只改任务状态不改运行状态） | ✓ `src/loom/store.py:313`、`:289-306` |
| 中心并发上限默认 4 工作位 + 1 校验预留 | ✓ `src/loom/store.py:122-124`、`src/loom/approved_execution.py:139` |
| 事件链校验（ms 本机链独立链标识、只校链内连续性） | ✓ `src/loom/store.py:621-632` |
| 副作用账本（调用粒度一行；四处进程内写入点，无外部登记口） | ✓ 写入点确认：`src/loom/execution.py:132`、`:174`、`src/loom/artifacts.py:510`、`src/loom/quotas.py:160` |
| 事件游标分页 | ✗ 待新增（批次三） |
| 副作用登记端点 | ✗ 待新增（批次三，分布式不重放的前提，不能省） |

---

## 二、"吸收改造"路线分析（回应本次新方向）

### 2.1 方案书 Q3 与新方向的冲突

方案书 Q3 拍板"整仓 fork + 双形态开关"，其依据是"租户、行级安全、契约、审计事件是重写成本最高的部分"。本次调查给出的新事实是：**整仓 fork 实际继承的是 76 万行 TypeScript 与 105 个 Prisma model 的 monorepo**，其中与 ms 直接相关的核心层（contracts、adapter-kit、core、db、auth）非测试代码合计约 4.6 万行，而 apps/adapters/mobile/desktop 等约 30 万行非测试代码在 ms 的 Phase 1 里本来就不进运行面。fork 的真实代价不是"复用 4.6 万行"，而是"背上 76 万行的构建、CI、依赖与上游合并"。

而"我们的技术栈"有明确证据链：Loom 是 Python 3.11+ / FastAPI / SQLite / 无外部依赖的中文控制台（`src/loom/web/`）；RV 是 Python 3.11+ 且内核依赖极薄；技术方案书 `docs/Loom.md` 引用的用户先行项目（d2d、NetMind）亦为 Python 系。**栈统一到 Python 后，"Loom 与 ms 原本一体"从口号变成字面事实**：同一语言、同一框架风格（FastAPI + Pydantic）、同一存储形态（SQLite + 相同事件信封）、同一控制台模式（中文、无 CDN、CSP 禁外部脚本）、同一身份模型（具名主体 + token）。

### 2.2 三条路线对比

| 路线 | 内容 | 得 | 失 |
|---|---|---|---|
| A. 概念吸收 + Python 重写（与新方向一致） | 把 rakazo 的**模型与语义**重述为 Python：Space/成员/设备模型、`scoped` 隔离语义、adapter-kit 契约（pydantic + Protocol 重述，进程外插件走 MCP——MCP 官方 Python SDK 成熟）、连接治理七条、审批规则、宿主沙箱语义（`hostRoots=[homedir()]` 等价实现）；**不搬 TS 代码**，界面用 Loom 风格中文控制台 | 栈统一；无上游合并负担；代码全自有；与 Loom 复用最大（事件信封、控制台、身份、SQLite 模式直接同构）；RV 同栈直连 | 放弃 rakazo 现成 TS 实现（React 前端、Better Auth、Prisma 迁移、testkit）；Pi 运行时（TS SDK）不能进程内用，需 MCP 外挂或 Python 运行时替代；重写量落在自己头上 |
| B. 混合：Python 核心 + 保留 rakazo web 壳 | ms 核心用 Python，客户端直接改造 rakazo 的 apps/web（React 19） | 省前端工时 | 双栈维护；前端与 Loom 控制台两套风格；改造 5.7 万行非测试前端代码的成本未必低于重写一个 Loom 式控制台 |
| C. 整仓 fork（方案书 Q3 原决策） | fork + 双开关 | 保上游演进路径；契约层零重写 | 背 76 万行；TS 7.0.2 等新工具链；与"改成自己的"意向相反 |

**推荐 A**，理由有三：其一，rakazo 对 ms 真正不可替代的是**概念与契约形状**（本报告 1.1 节核验的那批），不是代码量；其二，Apache-2.0 允许吸收，只要在交付物里保留原许可与版权声明（建议在 ms 仓库放 `THIRD-PARTY-NOTICES.md` 登记 rakazo/rv 的借鉴清单）；其三，Loom 侧批次三的改造与语言无关，两路线成本相同，差异全部在本机侧——而本机侧 Python 化后与 Loom 的复用（信封、控制台、身份、SQLite 模式）是 fork 路线拿不到的。

### 2.3 吸收清单（路线 A 的"拿什么"）

**必须吸收（概念级，重述为 Python）**：

1. 租户模型：Space（含 isDefault）→ 与 Loom tenant 一一映射；成员关系与隔离判定语义（`scoped` 的"不泄露存在性"错误消息）。
2. adapter-kit 契约形状：`AdapterDescriptor`（含 contractVersion）、`ConnectorTool`（readOnly/batch/route + **新增联网声明**，未声明按可联网）、`ConnectorCall/Event`、`AdapterContext` 字段全集（signal 跨进程映射为取消调用）、18 插槽注册表。
3. 连接治理七条（本机主动出网、服务器永不反向连、设备令牌、授权回调、吊销停机、断线不重试、全抖动重连）——与传输方式无关，HTTP 轮询下全部成立。
4. 宿主沙箱语义：文件根限主目录 + 命令放行检查 + 凭证路径绊线 + 联网声明披露。
5. 审批规则挂载方式：`ActionApprovalRule` 按 Space 生效 + 用户级覆盖（`UserActionApprovalRule`）。
6. DeploymentSettings 的注册收紧（signupsEnabled/signupAllowlist）与部署所有者边界（所有权不等于操作权）。
7. 数据模型裁剪版：ms 实际需要的约 15-20 个 model（User/Space/SpaceMember/UserDevice/Bot/Run/Event/ApprovalRule/McpServer/CapabilityInstall 等），不是 105 个。

**可选吸收（代码级）**：`examples/plugins/` 的插件格式约定；SECURITY.md 的 prompt-injection 策略条款；testkit 的验收组织方式（思想层面）。

**明确不吸收**：105 个 model 全量、Electron/Expo/移动端、消息平台与语音与支付等 adapters（25.8 万行）、Graphile Worker（Python 侧用 APScheduler/自建 SQLite 队列）、Better Auth（Phase 1 用 Loom 式具名主体 + 设备注册表，多用户协作由中心租户模型承担——这是对 Q2 的实质简化，需拍板确认）。

### 2.4 对既有决策记录的变更建议

| 决策 | 原结论 | 建议变更 | 触发的重议点 |
|---|---|---|---|
| Q3 fork 粒度 | 整仓 fork | 改为**概念吸收 + Python 重写**，钉快照 SHA（已钉：`d13ef7a`） | 原推翻条件"adapters 无法切分则退化"不再适用；新风险是重写引入的语义偏差，对策是本报告的契约对照表 + 每项语义带 rakazo file:line 依据 |
| Q2 用户体系 | 沿用 Better Auth + Organization + Space | Space/成员概念保留，但实现改用 **Loom 式具名主体 + 设备注册表**；组织级角色仍不启用（拍板二不变） | 若未来要面向公网多用户自助部署，再重议认证栈 |
| Q4 RV 二开 | 契约沿用 adapter-kit、RV 按**进程外 MCP 插件**接入 | 契约形状沿用；RV 接入形态改为 **Python 同栈直连**（进程内或子进程），MCP 保留给真正的第三方插件 | Pi 运行时（TS）改为 MCP 外挂或 Python 替代，批次五相应减重 |
| 其余（Q1、Q5、拍板一/二/三、三条既有拍板、16 动作契约、六批次框架、37 条判据） | 不变 | — | 方案书正文只需按 1.3 节更新"三处新增"的现状（计划读接口已完成） |

---

## 三、开放问题（需项目所有者拍板，拍板前不动工）

1. **主栈路线**：采纳路线 A（Python 吸收重写）还是保留 fork（路线 C）或混合（路线 B）？本报告推荐 A。
2. **认证实现**：Phase 1 用 Loom 式具名主体 + 设备令牌（本机单用户，多用户协作由中心租户承担），还是要求本机也支持多用户账户体系？
3. **客户端形态**：Loom 风格无外部依赖中文控制台（与 Loom 同模式、浏览器只是壳），还是独立桌面壳？这决定"本机进程存活"承诺的边界（原方案 6.2 节前置决策，仍未拍板）。
4. **Pi 运行时的去留**：TS 的 Pi 用 MCP 外挂、找 Python 等价物替代、还是 Phase 1 先用 RV 执行器顶替 agent 运行时？
5. **方案书文档的修订**：确认后由我按 1.3 节与 2.4 节把变更写回方案书（变更说明 + 决策记录更新），保持"冲突以规格书为准"的纪律。
6. **批次三的启动顺序**：批次三只依赖批次零（本报告已把 SHA 钉住做完大半），是否先启动 Loom 侧改造（事件游标、副作用登记端点、角色调整、关闭机器批准路径）与本机侧并行的排期？

---

## 四、rakazo 可优化点清单（吸收时改进，不照搬）

> 调查方式：两个维度分头核查——数据模型与安全信任模型（亲核），架构与工程质量（静态分析代理核查）。每个问题带证据与吸收处置建议。
> 先说总体判断：rakazo 的**工程纪律出乎意料地好**（全仓 TODO 仅 1 处、非测试代码 `: any` 仅 8 处、console 输出仅 50 处、包级依赖无环），它的问题不在散乱，而在**结构规模失控**（巨型单体模块、进程耦合、配置面膨胀）。吸收时应保留其纪律与契约思维，替换其结构。

### A. 结构性问题（吸收时必须改的）

| # | 问题 | 证据 | ms 处置 |
|---|---|---|---|
| A1 | **上帝工厂**：`createRunExecutor` 函数体 1,632 行，其返回的 `continueRun` 单方法约 1,613 行，闭包捕获几十个依赖，import 61 个本地模块，最深 7 层嵌套；测试只能整体黑盒（executor 系测试 4,246+ 行） | `packages/adapters/src/executor.ts:230`、`:249` | ms 的 run 执行拆成显式阶段管道（领取/栅栏→模型解析→工具装配→执行循环→结算），每阶段独立类/函数，依赖用 Protocol/dataclass 显式注入；单文件不超过几百行 |
| A2 | **adapters 平面单体**：25.8 万行、src 顶层 693 个文件仅 4 个小子目录、52 个外部依赖全部必装、桶文件 183 个 export；每个沙箱提供商都是 1,000+ 行单文件互相 import（createos 1,855 / boat 1,851 / desktop-sandbox 1,003） | `packages/adapters/src/index.ts`（192 行）、`packages/adapters/src/host-computer-runner.ts:5` 等 | ms 按能力域分包（sandbox/runtime/connectors/messaging），薄契约包隔开；Python 用 extras（`pip install ms[docker]`）让提供商依赖真正可选 |
| A3 | **进程模型耦合**：worker 不是瘦消费者，把 API 整个依赖图重建一遍（一次 import 约 70 个符号）；Pi 运行时绑死在 API/worker 进程内；worker 用 uncaughtException 白名单硬撑队列库的未等待 rejection | `docs/computer-runtime.md:13`、`apps/api/src/app.ts:507-508`、`apps/worker/src/index.ts:18-82`、`:404-412` | ms 本机侧用单一常驻 agent 进程 + asyncio 轻量队列（问题 3 的教训：双进程同版本部署才需要"滚动发布兼容"的 deprecated 字段）；若需隔离，运行时按 run 拉起子进程 actor，而非与 HTTP 服务同生共死 |
| A4 | **组合根过大**：`buildApp` 1,192 行集中全部装配与路由挂载；`env.ts` 407 行手写环境变量解析 | `apps/api/src/app.ts:1192`、`apps/api/src/env.ts` | ms 按域拆 `build_xxx()`，装配代码超过 200 行即为信号；配置用 pydantic-settings 声明式校验 |
| A5 | **schema 单体**：105 个 model 挤在一个 2,238 行文件里 | `packages/db/prisma/schema.prisma` | ms 的模型按域分文件（约 15-20 个 model 即够 Phase 1） |
| A6 | **队列双实现**：InMemory 与 Graphile 并存靠 env 切换，相关代码散布 5 处 | `packages/adapters/src/wakeup.ts:505`、`:21` | ms 只留一种持久化队列（SQLite 表 + asyncio），不引入第二实现 |

### B. 语义与数据模型（用 Loom 语义替代或补强）

| # | 问题 | 证据 | ms 处置 |
|---|---|---|---|
| B1 | **审计事件无哈希链**：Event 表仅 `threadId/seq/type/payload`，追加式但不可防篡改 | `packages/db/prisma/schema.prisma` Event 模型 | 维持方案书决策：事件信封统一采用 Loom 形状（前序哈希 + 链校验 + Ed25519 检查点可选） |
| B2 | **幂等语义弱**：仅 `clientNonce` 建唯一约束去重，无幂等记录表（不存响应、无冲突检测） | `@@unique([spaceId, clientNonce])`（Run/Message） | 用 Loom 的 (tenant, key) 幂等记录 + 四段式机器前缀 |
| B3 | **秘密模型碎片化**：5 张秘密相关表（Secret/BotSecret/VaultCredential/AgentSecret/UserModelCredential），BotSecret 冗余持有 userId+spaceId+botId 三重归属 | 各 model 定义 | 收敛为单一 secret 表 + 引用；保留 AES-256-GCM + HKDF 轮换的加密实现（`packages/db/src/content-keys.ts:94/:108`，这部分是成熟资产） |
| B4 | **容量信号缺失**：Computer/ComputerRun 无内存/CPU 水位；`providerRef` 非持久标识 | 方案书 1.1 节已证 | 维持"不做自动路由"拍板；ms 派活记录四字段（执行域/机器标识/活跃任务数/端到端耗时）为将来复算留数据 |
| B5 | **契约缺口**：ConnectorProvider 无取消动作；ConnectorTool 无联网声明；AdapterContext.signal 不可序列化 | `packages/adapter-kit/src/interfaces.ts:240`、`types.ts:262` | 方案书已有处置（新建取消通路 + 联网声明扩展 + signal 映射为取消调用），吸收时落实 |
| B6 | **所有者模型缺口**：`Actor.isDeploymentOwner` 硬编码 false；`ownerUserId` 字段标 Deprecated 却因滚动发布永久保留 | `packages/db/src/scope.ts:43`、`schema.prisma:495-497` | ms 明确部署所有者为真实角色（方案书批次一已列）；deprecated 字段配迁移脚本真正清理 |
| B7 | **热表索引值得抄**：`@@index([status, wakeAt])`、`@@index([status, leaseExpiresAt])`、`@@index([runId, type, seq])`、Gin searchTokens | Run/Message/Event 模型 | ms 的 SQLite 索引按同样查询模式设计 |

### C. 安全信任模型（吸收时加强）

| # | 问题 | 证据 | ms 处置 |
|---|---|---|---|
| C1 | **审批有 "Never ask" 模式**（"every action runs without waiting"），与 ms"不做自动批准"精神冲突 | `docs/approvals.md`（Mode 三档） | ms 不提供全放行档；Ask/Auto 两档保留，Auto 的"审批模型判定"可吸收但要接受 fleet 式硬卡（写操作永远弹卡，`docs/fleet-management.md:47-50`） |
| C2 | **工具门禁单点过重**：`gateToolCall` 单函数 848 行决策读写检查、批量展开、无人值守触发、站点发送、付费请求、效果登记 | `packages/adapters/src/run-tool-gate.ts:136` 起 | 门禁集中是对的（保留单一入口），但拆成规则管道（每条规则一个纯函数：声明→判定→留痕），便于测试与审计 |
| C3 | **值得原样吸收的成熟安全资产**：① RLS——133 行专项迁移、39 处策略、`rakazo_app` 角色 NOBYPASSRLS、security_events 表 REVOKE UPDATE 实现追加不可改、`app.rls_bypass` 受控旁路；② AES-256-GCM + HKDF 主密钥派生与轮换；③ "不区分不存在与无权访问"的隔离错误语义 | `packages/db/prisma/migrations/20261021140000_row_level_security/migration.sql`、`packages/db/src/row-security.ts:74`、`packages/db/src/content-keys.ts`、`packages/db/src/scope.ts:47` | Loom 批次三的 PG 迁移直接以该 RLS 迁移为蓝本；ms 本机 SQLite 用同构的租户列约束；SECURITY.md 的 prompt-injection 条款搬进部署向导 |

### D. 错误处理与可观测性

| # | 问题 | 证据 | ms 处置 |
|---|---|---|---|
| D1 | **36 个分散错误类 + 按消息正则判错**：跨模块判定错误靠遍历 cause 链比较 `error.name` 字符串和 message 正则（约 310 处空 `catch {}`） | `packages/adapters/src/sandbox-errors.ts:6-27`、`skill-tools.ts:221` 等 | ms 建立单一异常基类（code/retriable/safe_message），跨边界按错误码判定；空 catch 必须带日志或显式注释——这条写进 AGENTS.md |
| D2 | **可观测性三套并存且品牌硬绑定**：Sentry 静态 import 硬依赖、Axiom 静态 import、OTEL 反而动态可选；18 个观测相关 env | `packages/logging/package.json:30`、`packages/logging/src/axiom.ts:1`、`packages/adapters/src/pi-telemetry.ts:14` | ms 只走 logging + OTEL 一条通路；sink 插件化；**sink-guard（观测后端故障不反噬主进程）与 redaction（脱敏）两个设计原样吸收**（`packages/logging/src/sink-guard.ts`、`redaction.ts`） |

### E. 配置面与测试

| # | 问题 | 证据 | ms 处置 |
|---|---|---|---|
| E1 | **配置面膨胀**：`.env.example` 约 200 个生效变量（+42 注释可选），大量服务 SaaS/多云/多消息平台场景，与本机常驻 agent 无关 | `.env.example`（491 行） | ms 配置目标 ≤20 项（模型接入、数据目录、沙箱开关、日志级别）；避免"空值降级"式三态判断泛滥 |
| E2 | **测试隔离靠正则扫描源码**：巨型正则决定测试进隔离还是共享 project，写法变化即静默失效；存在 9,593 行测试挂名 164 行文件的错位 | `vitest.config.ts:17-24`、`apps/api/src/router.test.ts` vs `router.ts:164` | pytest 天然无此问题；吸收 testkit 的"模拟器 + 每文件独立数据库 + evals/replay 分层"思想（`packages/testkit`），测试命名与被测边界一致 |
| E3 | **值得学习的纪律**：TODO 全仓 1 处、`: any` 8 处、console 50 处、包级 DAG 无环、日志统一走 @rakazo/logging | 全仓 grep | 这套纪律本身作为 ms AGENTS.md 的底线要求（mypy strict 对齐 any 卫生） |

### 一句话总结

**rakazo 的"值得抄"与"必须改"是同一批东西的两面**：契约先行、门禁集中、RLS/加密/redaction 是要原样拿走的设计；把 1,632 行的执行器工厂、25.8 万行的平面单体、双进程同版本耦合、200 个环境变量拿走则是灾难——ms 用 Python 重写恰好是强制瘦身的机会，方案书 61-96 人日的估算在吸收路线下反而有了下调空间（本机侧从"改造别人的 30 万行"变成"自己写 2-3 万行"）。

---

**VERDICT: PASS**

（生产者自检结论。本报告全部事实性结论带 file:line 或来源；三个只读基线未做任何写入；未运行 pytest 与任何会产生缓存的探针；两份上游文档（方案书、交接文档）的引用行号已与本机钉住版本核对并在差异处注明。）
