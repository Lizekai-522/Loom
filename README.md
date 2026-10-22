# Loom

基于 Loom v2.1 修订方案的 Agent 编排内核，当前包版本 **0.2.0**。本轮推进到 **S2 基础设施试点，S2 仍进行中**：S1-local 内核继续使用 SQLite，新增真实 NATS JetStream 适配、PostgreSQL DDL/RLS 实测和本地 Ed25519 检查点。真实模型、外部写工具与生产集群尚未接入。

2026-10-23 分阶段检查：**112 项单元/本地测试 + 23 项真实基础设施测试 = 135 项通过，0 跳过**；另有 19 + 14 = 33 个参数子测试通过。23 项集成为 PostgreSQL 14 项、NATS live 8 项及原生 broker 强杀/重启 1 项；NATS 的 10 项单元测试和检查点的 37 项单元测试已经包含在 112 项中，不重复相加。[验证记录](docs/validation.md)

## 当前可以做什么

- 将 Goal / Intent / Task、完整事件信封、幂等回执与 outbox 持久化到 SQLite。
- 按依赖和优先级派发任务，原子预占共享容量，保留验证通道，拒绝旧 epoch 提交。
- Gate-P 使用受信范围、Anchor 和判据注册表；Gate-V 校验明确的双签成功状态与内容/规则/证据版本绑定。
- Worker HTTP 提交仅进入 `submitted`，无法直接置 `done` 或放行下游。可信验证入口不作为外部 HTTP 路由开放。
- 按 run 原子预留金额/token 上界，结算可幂等；实际用量超过预留时记录超支并挂起，避免隐藏账单。
- 以固定逻辑操作 ID 管理副作用发起记录；未知结果阻止自动再次发起。此版本只测试本地桩。
- 图失效传播到下游并撤销旧 claim；已发生的外部效果保留，记录需补偿事项。
- 执行七角色的确定性软件研发样例，展示并行分支、一次验证失败、修复重跑、验收和只读回放。
- 将 SQLite outbox 发布到真实 JetStream，核对持久 PubAck；durable 消费以本地权威事件、inbox 和游标防重复应用。
- 在独立 PostgreSQL 16.15 测试库验证 21 表 DDL、两租户业务/审计 RLS、事件及 claim 约束；默认 API 尚未切换数据库。
- 使用真实 Ed25519 签署事件快照，并以独立配置的公钥白名单、事件前缀和高水位离线核验；本地密钥/文件不是 WORM 锚定。

## 安装与验证

Python 3.11+，建议使用独立虚拟环境。以下 PowerShell 命令在本项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe scripts/check_spec.py
.\.venv\Scripts\python.exe -m pytest tests --ignore=tests/integration -q
.\.venv\Scripts\python.exe -m loom demo
```

`requirements.lock` 固定本次验证的直接与传递依赖，包含本轮集成驱动。跨操作系统或 Python 版本安装时应重新验证解析结果。项目在 Windows / Python 3.13.12 下验证，未据此声称所有 Python 版本和平台已验证。上面的命令只运行单元/本地套件；未配置基础设施时直接运行全部 pytest 会明确跳过 live 项，不能据此声称 135 项全部通过。

样例输出在 `artifacts/demo-report.json`，数据库在 `artifacts/demo.db`。报告明确标记 `deterministic-fixture`、未使用真实模型和零真实外部写；其中模型名称、认证标记及成本都是测试数据，不能作为真实模型双签或业务效果证据。

## 启动本地 API

本次工作区已有的首轮服务继续沿用原启动器、SQLite 测试库和租户 token，详见同一输出目录中的 [首轮测试指南](../Loom_首轮测试指南.md)。不用因包升级到 0.2.0 重新生成首轮测试身份。以下是独立安装后的通用启动方式，API 仍为 S1-local：

```powershell
$env:LOOM_TENANT_KEYS = '{"demo":"replace-with-a-random-local-token-at-least-16-characters"}'
.\.venv\Scripts\python.exe -m loom serve --port 8796
```

浏览器打开 <http://127.0.0.1:8796/docs>，在 Authorize 中填入设置的 token。API 只绑定回环地址。`/health` 和接口文档可公开读取；业务接口按 token 解析租户，不接受请求自行指定租户。不同租户使用不同 token。

示例 run 请求：

```json
{"acceptance":["schema.valid","tests.pass","report.traceable"],"cost_limit_microusd":1000000,"token_limit":10000,"ruleset":"kernel-v1"}
```

创建 run、创建 task、提交结果需要 `Idempotency-Key` 请求头。规则使用本地注册判据 ID；自然语言判据转换和规则发布权限属于后续工作。API 当前面向本机可信开发者，**尚未实现 OIDC、operator/worker/verifier 职责隔离及生产认证**。

本轮保留原首轮会话重启后，另执行了 9 项 HTTP 回归。服务运行时可复现，不必输出或重新生成 token：

```powershell
$firstRoundSession = (Resolve-Path '../../work/loom-first-round-session.json').Path
.\.venv\Scripts\python.exe scripts/check_api.py --session $firstRoundSession --report 'artifacts/api-validation.json'
```

该 HTTP 检查结果单独登记，不重复加进 135 项分阶段测试总数。

## 重跑 S2 基础设施检查

本轮使用单独的 loopback NATS 和可丢弃 PostgreSQL 测试 cluster，没有把它们装成系统服务。运行时和私有测试配置保存在工作区 `work/loom-infra/`，不打包进源码。先按 [NATS 记录](docs/nats-integration.md) 和 [PostgreSQL 记录](docs/postgres-integration.md) 启动对应服务，再在项目目录执行：

```powershell
$infra = (Resolve-Path '../../work/loom-infra').Path
.\.venv\Scripts\python.exe scripts/check_s2.py `
  --nats-url 'nats://127.0.0.1:4223' `
  --nats-server-bin "$infra/nats/nats-server-v2.15.1-windows-amd64/nats-server.exe" `
  --postgres-config "$infra/postgres-session.json" `
  --report 'artifacts/s2-validation.json'
```

本机原有工作虚拟环境也可使用 `../../work/loom-venv/Scripts/python.exe`。`check_s2.py` 强制提供 broker URL、原生 broker 二进制和私有 PostgreSQL 配置，分开执行单元/本地与真实基础设施两阶段；任一跳过、零用例、缺报告或失败均不计通过。PostgreSQL 检查会重建明确标为可丢弃的 `loom_test_*` 库中的 `loom` schema；不要传入用户数据库配置，凭据只从私有文件读取。

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
| `src/loom/demo.py` | 本地样例执行器与固定测试工具 |
| `src/loom/jetstream.py` | 真实 JetStream 发布、durable 消费及本地去重适配 |
| `src/loom/checkpoints.py` | Ed25519 快照签名、独立公钥与高水位离线验证 |
| `packs/software-dev/manifest.json` | 七角色及拓扑声明，JSON DSL 的第一份样例 |
| `tests/` | 审查反例、并发、恢复与 API 集成测试 |
| `docs/` | 修订方案、实施计划、GitHub 复用记录、验证记录与后续生产工作 |
| `migrations/postgres/` | 已在隔离 PostgreSQL 16.15 执行的 DDL；完整 Store 适配尚未实现 |
| `scripts/check_s2.py` | 强制三项基础设施配置、分阶段且拒绝 skip 的检查入口 |

[实施计划](docs/implementation-plan.md) · [GitHub 复用选型](docs/github-reuse.md) · [第三方声明](docs/third-party-notices.md) · [PostgreSQL](docs/postgres-integration.md) · [NATS](docs/nats-integration.md) · [检查点](docs/checkpoints.md)

## 当前边界

1. API 和任务内核仍由 SQLite 通过数据库事务在**单主机**协调。独立 PostgreSQL DDL/RLS 检查已通过，不代表完整 PostgreSQL Store、多主机调度或主备灾备完成。
2. Ed25519 检查点可以发现与独立受信快照不一致的截断、改写和历史分叉；必须保护公钥白名单和高水位。相同本机账号能同时改 DB、密钥与高水位时没有独立安全边界；本地文件不是 WORM、HSM 或远端不可回滚锚定。
3. 事件检查点使用真实密码学验签；产物/模型验证中的 `authenticated/evidence_trusted` 仍只允许可信适配器构造，演示为固定测试数据。两者用途不同，检查点签名不证明模型结论或测试指标为真。
4. `complete`、`finish_run` 等 Python 入口属于可信验证/验收进程；不能把不可信 Worker 作为同进程插件导入。HTTP Worker 只有提交入口。
5. NATS 已实现并在真实单节点验证至少一次传输、durable 消费和进程重启去重。尚未完成生产 accounts/TLS、R3 集群、物理断电耐久与多节点分区验证；强杀后 broker ACK 进度可能回退，本地 inbox 负责防重复效果。
6. 预算预留保证本地接纳/结算流程一致，真实提供方计费上界、取消后计费和超时收费尚未验证；未确定最大计费量前不能宣称真实硬封顶。
7. 副作用仅验证稳定逻辑 ID 与未知态协议，未连接外部系统，尚不承诺跨灾备窗口的真实零重复。
8. 当前回放重建已实现的业务投影；全量快照恢复、跨版本事件升级、全部生产投影和外部效果补偿仍待实现。
9. 六种修复原语目前仅实现本地 `Rebind/Substitute` 记录及验证失败重跑；动态改图、模板库、经验库和可视化前端仍在后续计划。
10. 事件接口暂为本地整链读取；分页、SSE、生产限流和大规模性能测试尚未实现。

## 复用与授权

当前实际复用 FastAPI、Pydantic、Uvicorn、nats-py、cryptography，以及 PostgreSQL 集成驱动 psycopg，通过锁版本依赖接入；没有将第三方编排框架作为 Loom 内核。30 个发行包的 36 份许可/署名文件保存在 `third_party/licenses/`。NATS Server 与 PostgreSQL 便携二进制只用于本机隔离试点，来源与摘要见各集成文档，未进入源码包。Temporal、LangGraph 仍为研究候选。

本项目新增代码未在本次工作中指定开源许可证；第三方组件继续按各自许可证使用。
