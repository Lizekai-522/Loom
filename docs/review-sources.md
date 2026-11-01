# 同一目标内审查来源

reviewer可以在批准计划时指定直接依赖的开发/修复程序节点，免去程序生成前无法填写精确内容摘要的问题：

```json
{
  "id": "review",
  "title": "审查程序",
  "instruction": "仅审查指定程序与冻结需求，用中文输出",
  "role": "reviewer",
  "depends": ["implement"],
  "review_source": {"schema_version": "loom.review-source/v1", "source_node": "implement"},
  "role_output": {"kind": "review-findings", "schema_version": "loom.review-findings/v1"},
  "limits": {"cost_limit_microusd": 1000000, "token_limit": 2000000, "max_attempts": 1}
}
```

该片段需要合并进完整七角色定义。源节点必须是绑定非空宿主判据的developer/fixer，必须是直接硬依赖。review_source与旧review_subject互斥；新选择器仅授权reviewer，单次只读审查，不能附带代码执行、修复或辅助输入权限。

计划批准只冻结选择规则，不派发模型。执行时要求源任务实际完成双模型签名验收，重建精确源引用、宿主证据与已完成复核记录；以同一事务快照核对。原始description/acceptance必须匹配创建事件，源instruction来自冻结合同，并随审查上下文绑定。模型收到原始需求、main.py及安全宿主摘要，不会收到私有用例或宿主输出。

模型返回的review-findings.subject必须等于本次实际解析的精确源引用。错任务、内容摘要或epoch均拒绝。执行前后及提交时重查上下文，撤销源会隔离下游，晚到结果不能提交。原始派发事件保留精确subject和来源验收记录；源撤销后历史意见仍可审计，但标为stale，禁止再次消费。

审查意见submitted后还需自己的质量双签，不能使用源程序签名；两模型对照意见、被审原文、冻结需求和宿主摘要。该步骤不执行原程序，执行用例计数为0。无问题意见的验收不自动创建修复任务或授予修复权限。终验若依赖review，须其质量验收通过后才可实际运行。

旧式跨Goal精确review_subject继续支持，原签名和schema摘要不改写。本轮不扩展到同Goal fixer选择器或任意嵌套审查；完整七角色的条件跳过、修复闭环及其余能力仍按全功能计划推进。
