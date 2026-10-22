# Loom

基于 Loom v2.1 修订方案启动的 Agent 编排内核。当前交付 **S1-local 本地参考实现**：持久化、确定性验证和失败恢复路径已经可运行；真实模型、外部写工具和生产集群尚未接入。

## 当前可以做什么

- 将 Goal / Intent / Task、完整事件信封、幂等回执与 outbox 持久化到 SQLite。
- 按依赖和优先级派发任务，原子预占共享容量，保留验证通道，拒绝旧 epoch 提交。
- Gate-P 使用受信范围、Anchor 和判据注册表；Gate-V 校验明确的双签成功状态与内容/规则/证据版本绑定。
- Worker HTTP 提交仅进入 `submitted`，无法直接置 `done` 或放行下游。可信验证入口不作为外部 HTTP 路由开放。
- 按 run 原子预留金额/token 上界，结算可幂等；实际用量超过预留时记录超支并挂起，避免隐藏账单。
- 以固定逻辑操作 ID 管理副作用发起记录；未知结果阻止自动再次发起。此版本只测试本地桩。
- 图失效传播到下游并撤销旧 claim；已发生的外部效果保留，记录需补偿事项。
- 执行七角色的确定性软件研发样例，展示并行分支、一次验证失败、修复重跑、验收和只读回放。

## 安装与验证

Python 3.11+，建议使用独立虚拟环境。以下 PowerShell 命令在本项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe scripts/check_spec.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m loom demo
```

`requirements.lock` 固定本次验证的直接与传递依赖。跨操作系统或 Python 版本安装时应重新验证解析结果。项目在 Windows / Python 3.13.12 下验证，未据此声称所有 Python 版本和平台已验证。

样例输出在 `artifacts/demo-report.json`，数据库在 `artifacts/demo.db`。报告明确标记 `deterministic-fixture`、未使用真实模型和零真实外部写；其中模型名称、认证标记及成本都是测试数据，不能作为真实模型双签或业务效果证据。

## 启动本地 API

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

## 代码与设计

| 路径 | 用途 |
|---|---|
| `src/loom/store.py` | SQLite 事务适配、事件、outbox/inbox、租约、预算、副作用状态 |
| `src/loom/policy.py` | 确定性门禁、人工例外、进展判断、任务/Agent 选择与上下文预算 |
| `src/loom/api.py` | FastAPI/Pydantic 本地认证 API |
| `src/loom/demo.py` | 本地样例执行器与固定测试工具 |
| `packs/software-dev/manifest.json` | 七角色及拓扑声明，JSON DSL 的第一份样例 |
| `tests/` | 审查反例、并发、恢复与 API 集成测试 |
| `docs/` | 修订方案、实施计划、GitHub 复用记录、验证记录与后续生产工作 |
| `migrations/postgres/` | PostgreSQL 候选迁移设计，执行状态见相应集成文档 |

[实施计划](docs/implementation-plan.md) · [GitHub 复用选型](docs/github-reuse.md) · [第三方声明](docs/third-party-notices.md) · [生产适配说明](docs/postgres-integration.md)

## 当前边界

1. SQLite 适配器通过数据库事务在**单主机**协调；不等同于多主机调度、PostgreSQL RLS 或主备灾备。
2. 哈希覆盖完整事件信封；已支持对照预期链头和数量检查，但尚未接独立签名服务或外部检查点。当前库内自算哈希不能证明全库重写或未知链尾丢失未发生。
3. `authenticated/evidence_trusted` 只允许可信适配器构造；本地样例是固定测试数据。真正的签名校验、独立模型与受信测试执行器需另行接入。
4. `complete`、`finish_run` 等 Python 入口属于可信验证/验收进程；不能把不可信 Worker 作为同进程插件导入。HTTP Worker 只有提交入口。
5. outbox 已有持久投递和去重契约，本轮使用回调 broker 桩；NATS 运行时适配尚未实现。
6. 预算预留保证本地接纳/结算流程一致，真实提供方计费上界、取消后计费和超时收费尚未验证；未确定最大计费量前不能宣称真实硬封顶。
7. 副作用仅验证稳定逻辑 ID 与未知态协议，未连接外部系统，尚不承诺跨灾备窗口的真实零重复。
8. 当前回放重建已实现的业务投影；全量快照恢复、跨版本事件升级、全部生产投影和外部效果补偿仍待实现。
9. 六种修复原语目前仅实现本地 `Rebind/Substitute` 记录及验证失败重跑；动态改图、模板库、经验库和可视化前端仍在后续计划。
10. 事件接口暂为本地整链读取；分页、SSE、生产限流和大规模性能测试尚未实现。

## 复用与授权

当前实际复用 FastAPI、Pydantic、Uvicorn 等上游组件，通过锁版本依赖接入；没有将第三方编排框架作为 Loom 内核。第三方许可证与署名原文保存在 `third_party/licenses/`。Temporal、LangGraph 和 NATS 是已研究候选，是否接入以实施计划和决策记录为准。

本项目新增代码未在本次工作中指定开源许可证；第三方组件继续按各自许可证使用。
