# 本地 API 契约

此目录的 `openapi.json` 由当前 FastAPI 应用生成，描述实际可运行的 HTTP 子集。当前版本 **0.3.0**，本机地址为 <http://127.0.0.1:8796/docs>。设计方案附录中的其余接口仍属后续目标，未因列入规范而自动实现。

`/health` 返回 version，stage 仍为 `S1-local`，表示 HTTP 内核继续使用单实例 SQLite；`external_writes_enabled=false` 表示此 API 没有执行真实外部写的路由。本机脚本的真实模型调用另见 [本机执行说明](../local-execution.md)。`/integrity` 的 `external_checkpoint: "not_configured"` 表示该 HTTP 实例尚未配置独立检查点；离线 Ed25519 组件不在普通查询时自动签名，使用方法见 [检查点说明](../checkpoints.md)。

| 动作 | 实际接口 |
|---|---|
| 创建 Goal 草案 | `POST /v1/goals` |
| 列出本租户目标/读取目标 | `GET /v1/goals`、`GET /v1/goals/{run_id}` |
| 保存/读取版本化计划 | `PUT /v1/goals/{run_id}/plan`、`GET /v1/goals/{run_id}/plan` |
| 批准当前审阅计划并创建任务 | `POST /v1/goals/{run_id}/plan/approve` |
| 创建/查询 run | `POST /v1/runs`、`GET /v1/runs/{run_id}` |
| 为旧 fixture run 创建任务 | `POST /v1/runs/{run_id}/tasks`；拒绝受管 Goal 的增任务请求 |
| 认领就绪任务 | `POST /v1/runs/{run_id}/claim` |
| Worker 提交待验证输出 | `POST /v1/runs/{run_id}/tasks/{task_id}/submit` |
| 事件/历史回放/完整性 | `GET /v1/runs/{run_id}/events`、`.../replay`、`.../integrity` |

设计中的独立 `/v1/tasks` 入口、SSE、分页、人工裁决及模型/副作用 HTTP 执行接口尚未开放。本地任务路径包含 run，避免隐含跨 run 归属。真实 Flash 网关与 Docker 适配器已通过 Python/独立脚本接入，没有因此新增普通 HTTP 执行或密钥操作路由。

业务接口从 Bearer token 映射租户；创建 Goal、保存/批准计划、旧 run/task 创建和产物提交均需 `Idempotency-Key`。同键同参返回原回执，同键异参或异操作返回 409。所有 Goal、计划、run、任务和事件访问受租户归属限制，跨租户资源 ID 返回 404；目标列表只包含本租户数据。

创建 Goal 的请求包含 title、description、acceptance，以及可选的 cost_limit_microusd/token_limit。acceptance 只能引用当前三个已注册判据：schema.valid、tests.pass、report.traceable。目标描述是任务数据，不能授予工具、系统权限或验收成功。

```json
{"title":"修复本地程序","description":"实现并验证已授权的变更","acceptance":["schema.valid","tests.pass","report.traceable"],"cost_limit_microusd":1000000,"token_limit":10000}
```

创建返回 run_id，run 状态为 draft，plan_revision=0。草案没有可领取任务。保存计划时提交 `{"definition":<工作流定义>,"expected_revision":0}`；服务校验代码登记的七角色、差异化技能/工具、local-workspace 范围、DAG 依赖和资源上限，成功后 revision 增加并返回执行字段 digest。实际模板是 `packs/software-dev/workflow-v1.json`；它默认没有冻结执行用例摘要，不能直接充当真实执行授权。

审批时提交当前 `expected_revision` 与 `expected_digest`。只有与数据库内当前定义完全一致的版本才获批，审批在单一事务中核对任务预算总和、冻结七角色合同、创建全部命名空间任务并把 run 改为 open。旧 revision/digest、审批后编辑、从旧 tasks 路由注入任务以及预算超配都会被拒绝。合同包含角色工具/技能、模型别名、指令、依赖、预算、anchor、plan_digest 和可选 evaluation_digest；真实执行还需绑定宿主冻结测试的摘要。

输出提交只产生 `submitted`，不因候选自报通过放行后续依赖；受信验证引擎的 `complete` 与验收引擎的 `finish_run` 不暴露给 HTTP Worker。本轮真实探针通过 4 个黄金用例后仍为 submitted、awaiting_independent_models，不能读作完整 Goal 成功或双模型已验收。

本地 token 用于受控开发，尚无 OIDC、RBAC 或 operator/worker/verifier 主体分离。计划“批准”是本机流程屏障，不能冒充已完成生产人类身份认证或双人审批。旧 fixture run 使用 local-fixture，受管 Goal 使用 local-workspace；二者都不授予任意宿主目录访问。金额为整数微美元，1 USD = 1,000,000 microusd。

接口拒绝未知字段、不严格的预算类型、非有限 JSON 产物和超过 256 KiB 的产物字段；这不是通用生产请求限流/反 DoS 网关。事件整链读取是本地限制，生产分页与流式传输另行实现。

现有启动器和首轮私有 session 继续沿用，无需重配 token。0.3.0 实际服务检查为原 9 项 API 检查 + 新 12 项 Goal/Plan 检查通过，分别由 `scripts/check_api.py` 和 `scripts/check_goal_api.py` 读取同一个私有 session；不要打印或复制其中 token。源码工作环境位于 `../../work/loom-venv`。完整分阶段检查须指定 `check_s2.py --sandbox-image <sha256镜像ID>` 及 NATS/PG 三项配置；网关使用 Mock，真实模型报告另存，最新数量见 [验证记录](../validation.md)。
