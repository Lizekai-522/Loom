"use strict";
window.LoomZH = (() => {
  const words = {
    TASK_CONTEXT_LIMIT_EXCEEDED: "任务上下文超过批准的字节上限", INVALID_CONTEXT_BUDGET: "任务上下文预算无效",
    analyst: "需求分析", architect: "架构设计", tester: "测试设计", developer: "开发实现", reviewer: "代码审查", fixer: "问题修复", validator: "独立验证",
    business: "业务任务", verification: "独立验证", requirements: "需求说明", design: "设计方案", "public-checks": "公开开发检查", "review-findings": "代码审查意见",
    "role-artifact": "角色候选产物", "python-sandbox": "Python 隔离执行报告", awaiting_independent_models: "等待独立模型复核",
    "python-json-program": "Python 程序隔离测试报告",
    "authenticated-worker-submission": "已认证执行器提交", "model-gateway-response": "模型网关响应",
    "goal.created": "目标已创建", "plan.saved": "计划已保存", "plan.approved": "计划已批准", "run.created": "运行已创建",
    "workflow.plan_saved": "计划已保存", "workflow.plan_approved": "计划已批准", "execution.existing_plan_admitted": "既有批准任务已获准执行",
    "execution.requested": "执行请求已排队", "execution.request_claimed": "执行器已接收请求",
    "execution.request_finished": "执行请求结果已记录", "execution.request_cancelled": "排队请求已取消",
    "execution.request_recovery_stopped": "遗留请求已停止并回收执行槽",
    "intent.created": "执行意图已创建", "task.output_submitted": "候选输出已提交", "task.lease_renewed": "任务租约已续期",
    "task.verification_failed": "独立验证未通过", "artifact.verified": "产物已验证", "task.completed": "任务已完成验证",
    "goal.achieved": "目标验收已通过", "run.completed": "运行已完成", "gate.failed": "验证门槛未通过",
    "tool.indeterminate": "调用结果未知", "tool.compensation_required": "外部效果需要核对", "effect.reconciled": "调用已人工核对",
    "budget.overrun": "费用超出预留", "budget.released": "预算预留已释放", "execution.candidate": "开发候选已登记", "failure.classified": "失败原因已分类",
    "task.created": "任务已创建", "task.claimed": "任务已领取", "task.submitted": "候选已提交", "task.done": "任务已验证",
    "task.lease_expired": "任务租约已过期", "task.invalidated": "任务已失效", "tool.intent": "调用已登记", "tool.done": "调用结果已记录",
    "execution.context_bound": "执行上下文已绑定", "execution.evaluated": "宿主终验已记录", "execution.stopped": "执行已停止",
    "execution.acceptance_failed": "宿主终验失败已保留", "execution.blocked": "执行已转人工处理",
    "development.evaluated": "公开开发检查已记录", "development.suite_frozen": "开发套件已审阅冻结",
    "role.candidate_generated": "角色候选已生成", "repair.applied": "有界修复已批准", "repair.stopped": "修复已停止",
    "budget.reserved": "预算已预留", "budget.settled": "已知用量已结算", "run.paused": "运行已暂停", "run.resumed": "运行已恢复", "run.cancelled": "运行已终止",
    correct: "正确", infrastructure: "基础设施", environment: "运行环境", candidate: "候选问题", evaluation: "评估",
    development: "公开开发检查", acceptance: "宿主终验", rebind: "有界重绑", Rebind: "有界重绑", passed: "通过", failed: "失败",
  };
  return {label: value => value === null || value === undefined ? "—" : words[value] || String(value)};
})();
