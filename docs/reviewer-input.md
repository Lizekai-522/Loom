# 审查输入与候选校验

0.16先交付只读输入与意见校验；0.17已接通受限reviewer生成、候选保存、操作员精确批准及单次fixer执行。[执行与批准步骤](reviewer-execution.md)。本页两个预览/校验接口仍然只读，不批准执行、不改变依赖或独立验收。

## 只读获取

`GET /v1/runs/{run_id}/tasks/{task_id}/review-input` 使用现有读取身份，租户由服务端身份确定。默认仅返回摘要，显式 `?include_source=true` 才返回 `files.main.py`。本机自动中文工作台保持原有进入方式。

只接受批准计划下当前 submitted/done 的 developer/fixer Python JSON 产物。服务在同一数据库读取事务内重新构造冻结合同，核对源码、宿主测试摘要、runner、完整本地事件链、认领轮次/主体、local-executor 执行事件及随后的标准提交事件；存在未知外部效果、失效 epoch 或不一致投影时拒绝。不存在模型自报“通过”提升为受信证据的入口。

`subject` 精确绑定 run/task/epoch、plan/content/source/evaluation 摘要及执行/提交事件哈希；`input_digest` 绑定包含源码的完整审查输入，因此默认摘要与显式源码预览的摘要相同。新审查上下文最多65536字节，超限明确停止，不静默裁切代码。宿主证据只提供执行数量、通过数量、结果和报告摘要；不提供隐藏用例名称、输入、expected、stdout、stderr 或失败诊断。源码本身仍是不受信数据。

命令行默认以 SQLite `mode=ro`、`query_only` 打开既有文件，不新建库，不加载私有配置，不调用模型或 Docker：

```powershell
python scripts/preview_review_input.py --db '<现有数据库>' --tenant '<租户>' --run-id '<运行ID>' --task-id '<完整任务ID>'
```

仅在需要审查正文时增加 `--include-source`；源码输出应按原业务数据权限保管。

## 校验意见

`POST /v1/runs/{run_id}/tasks/{task_id}/review-findings/validate` 是只读校验。读取身份可使用，无保存或执行副作用，不表示操作员批准。正文示例中的 subject 必须逐字段复制当前预览引用，不能使用占位引用实际提交：

```json
{
  "schema_version": "loom.review-findings/v1",
  "subject": {"从当前预览复制完整subject": "此项仅为说明"},
  "summary": "审查意见候选，尚未独立验证",
  "verdict": "findings",
  "findings": [{
    "id": "boundary-one", "severity": "minor", "path": "main.py",
    "start_line": 1, "end_line": 1,
    "description": "描述可定位的问题", "recommendation": "建议的修复方向"
  }]
}
```

verdict 仅 `findings` / `no_findings`，后者要求空 findings；最多32项，ID唯一，severity 仅 blocker/major/minor/info，路径仅 main.py，行号须是源文件内的正整数闭区间。正文最多32768字节；未知字段、伪造 verified/passed/签名、重复ID和过期引用均拒绝。CLI 加 `--candidate-file '<意见JSON>'` 可执行同样校验，重复 JSON 字段和错误不会回显正文。

返回 `persisted=false`、`model_source_recorded=false`、`counts_as_success=false`、`execution_authorized=false`。意见即使为 no_findings 也不完成任务或Goal。本机事件自洽和宿主有限测试不代表生产独立信任根；第二模型与受信签名仍待配置。

0.17通过新合同的review_subject冻结该引用，预算/intent/输入同事务准入并在返回/提交前复核；fixer还须独立批准精确意见且保留原宿主判据。不能直接将这个只读校验响应当执行许可。
