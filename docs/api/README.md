# 本地 API 契约

此目录的 `openapi.json` 由当前 FastAPI 应用生成，描述实际可运行的 S1-local HTTP 子集。设计方案附录中的其余接口仍属后续目标，未因列入规范而自动实现。

| 动作 | 实际接口 |
|---|---|
| 创建/查询 run | `POST /v1/runs`、`GET /v1/runs/{run_id}` |
| 创建运行内任务 | `POST /v1/runs/{run_id}/tasks` |
| 认领就绪任务 | `POST /v1/runs/{run_id}/claim` |
| Worker 提交待验证输出 | `POST /v1/runs/{run_id}/tasks/{task_id}/submit` |
| 事件/历史回放/完整性 | `GET /v1/runs/{run_id}/events`、`.../replay`、`.../integrity` |

设计中的独立 `/v1/tasks` 入口、SSE、分页、人工裁决、真实模型网关和副作用执行接口尚未开放。本地任务路径特意包含 run，避免隐含跨 run 归属。

业务接口从 Bearer token 映射租户；创建/提交命令需 `Idempotency-Key`。输出提交只产生 `submitted`，受信验证引擎的 `complete` 与验收引擎的 `finish_run` 不暴露给 HTTP Worker。

本地 token 是受控开发用途，不代表已完成生产 OIDC 或职责隔离。所有本地操作只允许 `local-fixture` scope。金额为整数微美元，1 USD = 1,000,000 microusd。

接口拒绝未知字段、不严格的预算类型、非有限 JSON 产物和超过 256 KiB 的产物字段；这不是通用生产请求限流/反 DoS 网关。事件整链读取是本地限制，生产分页与流式传输另行实现。
