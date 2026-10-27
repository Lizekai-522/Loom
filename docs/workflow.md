# 本地版本化软件开发工作流

本轮交付 `loom.workflow/v1` 的基础 DAG 子集：持久目标、草案修订、人工审阅摘要批准、冻结角色能力与任务合同。FR-007 仍为部分实现；这里没有条件边、循环、子工作流、动态拓扑修改或六种完整修复引擎。

## 数据与接口

`WorkflowDefinition` 固定 `pack=software-dev`、`scope=local-workspace`，严格拒绝未知字段和隐式类型转换。`AgentRole` 定义七角色：analyst、architect、tester、developer、reviewer、fixer、validator，每个角色配置 skills、tools 和 model_alias。代码中的注册表限定角色能力上限；角色或计划不能自行授权 shell、任意宿主路径、平台管理或其他角色的写权限。tools 是执行器的能力需求，不是已执行工具的证据。model_alias 只是服务端路由别名，实际模型身份必须由模型网关确认。

每个 `TaskNode` 包含 id、title、instruction、role、depends、priority、channel、limits 和可选 evaluation_digest、repair、input_artifacts。validator 使用 verification 通道，其余角色使用 business。依赖必须引用本定义中的节点；重复 ID、自依赖、缺失依赖、循环、跨 run 形式的引用均拒绝。当前没有路径输入字段，scope 只接受平台命名的受控本地工作区。Goal、说明、instruction 和上游产物始终是任务数据，不能提升为平台系统指令。

`input_artifacts` 最多声明8个辅助读取来源，逐项绑定 source_node、kind、schema_version 和 required；来源角色必须能产生该类型，禁止自身/未知来源和重复选择器。该字段参与批准摘要与合同，未填写时省略这一新增字段，保持旧合同摘要；旧 evaluation_digest:null 语义不变。它不修改 `depends`，不产生派发边。0.6提供候选与只读预览；0.7在当前有效claim下，将operator精确批准的辅助数据接入Python执行器的user消息，并在发起与提交事务中复核。pending epoch0的批准不能沿用到claim epoch1，Rebind也不复制旧批准；失效转人工且不自动扩充尝试额度。[候选合同与批准](artifacts.md)

`evaluation_digest` 如有值必须是 64 位小写十六进制摘要，并纳入完整计划摘要、冻结合同及独立投影列。真实代码执行器必须要求宿主侧 FrozenTest suite 的摘要与已批准值一致；None 表示没有授权的执行判据，不能当作“跳过校验”。黄金 expected 值保留在宿主侧，不放入模型 prompt。当前示例七节点均未绑定实际 suite，因此只是可批准的规划模板，不凭此授权真实生成代码执行。旧本地数据库会新增可空列，旧合同不会自动获得判据绑定。

公开接口：

```python
service = WorkflowService(store)
goal = service.create_goal(tenant, key, title=title, description=description,
                           acceptance=["tests.pass"], cost_limit=1_000_000, token_limit=10000)
plan = service.save_plan(tenant, goal["run_id"], save_key, definition, expected_revision=0)
approved = service.approve_plan(tenant, goal["run_id"], approve_key,
                               expected_revision=plan["revision"], expected_digest=plan["digest"])
goal = service.get_goal(tenant, goal["run_id"])
plan = service.get_plan(tenant, goal["run_id"])
goals = service.list_goals(tenant)
contract = service.get_task_spec(tenant, goal["run_id"], task_id)
```

创建返回 run_id、goal_id、intent_id、anchor_digest、status=draft 和 plan_revision=0，冻结注册的验收判据及 Goal 预算。save_plan 只修改草案，每次使 revision 加一并返回 digest。审批必须同时匹配最新 revision 和 digest；批准后定义不可再保存。需要改变目标验收或批准后的计划，应创建新 Goal，不能复用旧审批。

摘要覆盖 schema、scope、角色 skills/tools/model_alias、所有节点说明、依赖、优先级、通道和预算/重试上限等完整定义。列表顺序也是当前规范化 JSON 的一部分，改变顺序会产生新摘要。批准时重新解析并计算摘要，不仅比较数据库存放的摘要字符串。

## 原子性与执行边界

workflow_goals、workflow_tasks 和现有 runs/tasks 位于同一 SQLite 数据库。创建 Goal 的三个根事件、投影和回执在一次 `BEGIN IMMEDIATE` 命令事务内写入。批准的任务、完整合同、事件、outbox、开放 run 状态和幂等回执也在单次事务内提交；中途失败全部回滚。并发相同幂等键返回同一回执，不同键竞争批准只有一个成功。租户级幂等键沿用 Store 的跨操作隔离语义。

实际 task_id 为 `run_id:node_id`，防止同租户不同 Goal 的同名节点冲突；depends_on 改为这些实际任务 ID。`get_task_spec` 重新验证 approved definition 的完整摘要，按冻结 node/role 重建合同，并逐字段对比合同投影；指令、模型别名、能力、依赖、预算和判据绑定中的任何差异均拒绝。它忽略缓存中的 status 并返回重建的冻结合同；实时状态仍由 Store 管理。任务创建事件包含全部合同，可追溯至 plan_digest。

具名 HTTP 创建、保存和批准事件使用认证 `subject:<id>`；本机可信调用未传 actor 时保留 `operator:local` 兼容标记。HTTP 已按 operator/worker/viewer 区分动作并绑定 Worker 主体，旧共享租户 key 仅保留 legacy developer 兼容面。本机 token 和 actor 不证明真人或多人职责分离，生产 OIDC、人员与独立验证服务身份仍需接入。

每项 limits 的 cost_limit_microusd/token_limit 是逻辑任务包含全部尝试的累计上限，max_attempts 限定重试次数。批准时所有任务上限的总和必须不超过 Goal 冻结预算；这只是计划准入。执行器仍须在实际调用前预留，并根据真实使用结算、维护每任务累计用量和尝试次数；此模块不执行模型、代码或自动重试，也不能替代 Store 的全 run 预算强制与 Gate-V。

Store 对受管 run 的执行入口需要配套保护：拒绝通过旧 add_task API 追加任务，且仅 approved 的计划可 claim；初始 draft run 本身不可领取。传统非受管 run 保持旧契约。事件回放应保留初始 draft，遇 workflow.plan_approved 后转换为 open。

示例 `packs/software-dev/workflow-v1.json` 提供 spec → design/case → code → review → fix → verify 的七角色 DAG。默认七角色使用同一个 DeepSeek Flash 路由，因此不能声称两个不同模型完成了独立双签；Gate-V 的模型独立性约束保持有效。

## 验证范围

`tests/test_workflow.py` 使用真实本地 SQLite 事务，不进行模型付费调用或代码执行。覆盖创建/批准中途失败的全回滚、并发幂等批准、竞争批准、旧修订与错误摘要、预算超限、未知判据、跨操作幂等键、租户隔离、DAG/能力/通道拒绝、摘要覆盖执行字段和保存边界重新验证。此处通过不能代表整个软件开发闭环、隔离沙箱或生产权限已完成。
