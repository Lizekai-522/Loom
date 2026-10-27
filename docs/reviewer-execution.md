# 代码审查生成与单次修复

0.17.2可从[七角色推进指引与统一入口](unified-nodes.md)定位并执行既有reviewer/fixer；本轮真实Flash修复受控缺陷通过原终验与额外诊断。初始来源使用替身、真实审查与修复各一次，不等于真实独立验收。

0.17已实现受限reviewer→精确候选→具名operator批准→单次fixer→原宿主验收。这里只覆盖Python JSON程序；没有通用仓库访问、任意工具执行或独立验收。七角色完整编排和人工裁决分支仍待后续0.17迭代。

## 冻结合同

先用[审查输入接口](reviewer-input.md)取得原先submitted/done代码的完整subject。新审查计划须保留软件包的七角色配置；reviewer节点设置`review_subject`为该精确引用，`role_output`为`{"kind":"review-findings","schema_version":"loom.review-findings/v1"}`，并拥有artifacts.read/write。来源是同租户既有另一run的代码；不会改写原执行计划。

fixer节点声明相同review_subject、原`evaluation_digest`、`max_attempts:1`，并设置必需输入`{"source_node":"review","kind":"review-findings","schema_version":"loom.review-findings/v1","required":true}`。review是示例节点ID。新审查修复模式不允许repair/development_suite或改变原宿主判据。旧有界Rebind模式继续保留。

辅助输入与硬依赖含义不同：只设置明确的辅助读取声明，可在源意见submitted后由操作员批准使用；若同时设置depends，硬依赖仍要求上游真正done，不会被意见批准绕过。新计划必须由具名operator批准，legacy租户凭据不能批准含review_subject的计划。

## 生成审查意见

下面均在项目目录运行，占位参数须换成实际已有计划。默认仅以只读SQLite预览；不建Goal、不加载私有配置或调用模型：

```powershell
python scripts/run_reviewer_task.py --db '<数据库>' --tenant '<租户>' --run-id '<已批准运行>' --node-id review --expected-plan-digest '<计划摘要>' --report '<预览报告.json>'
```

确认该预览后，使用相同参数加`--execute --preview-digest '<当前预览摘要>' --identities-file '<本机私有身份JSON>' --secrets-file '<本机私有模型JSON>'`，报告换新文件。入口只认领指定未开始节点，同事务核对源代码/证据、硬依赖、预算、容量、未知效果与审批，并登记认领审计。生成前原子预留预算/登记intent/绑定上下文，返回和提交前再次复核。一次响应严格解码，来源、候选、提交与回执原子保存。未知计费不自动重试或释放预留。

意见中文，协议ID与subject保持原样；内容仍未验证。模型获得原源码、明确指令和执行摘要，隐藏用例输入/答案/名称及运行输出不进入prompt。每次最多1024输出tokens、32KiB候选和64KiB上下文；超限停止。schema、subject或行号错误会保留已知费用并转人工，不伪装成功。

## 审阅与执行修复

在中文工作台选择审查run→候选产物，读取“代码审查意见”正文、来源及subject，在[审查处置](review-decisions.md)选择处理方式、理由并勾选核对。明确findings可批准精确fixer输入；no_findings可登记等待独立复核，不能批准修复。批准与处置记录原子保存，不调用模型、不完成任务；第二模型仍待配置。

批准后用原宿主用例文件执行[既有任务CLI](approved-execution.md)，node-id改为fixer节点。先获取新的preview，再显式execute。准入将当前候选/原批准及完整上下文摘要纳入preview；明确执行时由具名operator在认领同一事务为epoch1重新批准精确意见。不是把旧grant无条件带入新轮次；来源、意见或预览变动即拒绝，并发只能一个领取成功，审计失败全回滚。

fixer只生成一份候选程序，在真实Docker运行同一宿主判据。通过仍只submitted、等待独立验证；失败报告与转人工状态同事务保存，不进入普通待执行队列、不自动继续修复。暂停、终止、失租和来源变化继续受原控制约束。

## 当前真实证据与下一步

本机run `9ffdcf62-ffc0-43f0-bff7-2eec709c5d35`取得一次真实Flash审查，候选 `78657098-bfed-46a8-b9c7-5ff3b1a87afa`，返回no_findings。用量2141tokens、费用记账上界979µUSD、预留0，未核对供应商账单。没有为该结果执行fixer，没有声称独立复核或Goal成功。

0.17.1为上述既有真实候选追加一条独立复核待办，未重新调用模型；原费用及任务状态不变。fixer通过/失败转人工两条路径以模拟提供方和真实Docker验收，不能称已取得真实模型修复效果证据。下一步补统一七角色入口、完整人工裁决与必要真实fixer任务；独立二模型仍按用户决定待配置。生产身份、恢复和工具完整性按[上线路线](release-roadmap.md)继续追踪。
