# 指定程序终验

validator节点可以在同一Goal的批准计划中指定一个直接依赖的developer/fixer节点。两节点必须绑定同一非空evaluation_digest，终验最多执行一次，使用verification通道。示例片段需要合并进完整计划，并由本机登记与摘要相同的私有宿主用例：

```json
{
  "id": "verify",
  "title": "程序终验",
  "instruction": "复验已独立验收的指定程序，不修改源码",
  "role": "validator",
  "channel": "verification",
  "depends": ["implement"],
  "evaluation_digest": "填写与implement相同的64位宿主用例摘要",
  "validation_source": {
    "schema_version": "loom.validation-source/v1",
    "source_node": "implement"
  },
  "limits": {"cost_limit_microusd": 0, "token_limit": 1, "max_attempts": 1}
}
```

源任务必须具有可重建的当前提交、宿主执行事件和已完成的独立VerificationJobs记录，两个不同模型的真实Ed25519签名必须对应同一版本。只把任务状态设成done、提交旧式断言或复制签名不能授权终验。源内容、签名记录和授权上下文在同一数据库快照核对，执行前后重新检查；源被撤销会使下游旧epoch失效。

终验只把指定main.py原文送入已批准的无网容器，使用相同冻结宿主用例。它不调用生成模型、不加载生成路由或密钥、不修改源码，也不执行模型给出的测试命令。生成费用/词元预留均为0，不等于整个验收免费。工具仍须满足任务能力、持久配额、租约及容器边界。

通过后生成自己的python-json-program候选，保留独立宿主执行证据和validation_source来源绑定，状态是submitted。操作员需在独立验证面板重新核对并申请Flash/Pro双签，使用独立项目复核预算；源签名不能搬到终验节点。双签均认可、依赖/冻结判据/Goal门槛全部满足才可完成目标。分歧或拒绝保持人工处理，不自动重试。

工作台路径：批准计划→源任务执行→源任务独立验收→推进面板读取终验条件→核对来源与用例后提交本机执行→查看新宿主证据→独立验证面板申请新双签。CLI也可使用run_approved_node.py，终验无需secrets-file/model-config，仍需冻结用例、具名身份与不可变镜像。

容器预检查失败不派发；执行失败、暂停、配额拒绝或未知工具响应保留真实状态。已产生未知工具结果的调用保留占额，不会自动释放或重跑。执行中源撤销会隔离下游epoch，晚到结果不能作为旧版本验收。

没有validation_source的历史validator节点继续明确待配置，不擅改已批准计划。不更改既有ReviewSubjectReference枚举或已签审查意见schema摘要。当前仍只支持Python JSON单文件程序；通用工件终验、同Goal审查/修复选择、动态派发及完整62项仍按全功能计划推进。模拟模型响应不算真实提供方验收。
