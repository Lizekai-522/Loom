# Loom 0.13 验证记录

2026-10-25，0.13.0，reviewed-suite-execution-alpha。完成冻结生成用例→新执行计划→执行前核对→development/有界repair→不同宿主验收的本机链路。[0.12历史](validation-0.12.md)

| 完整阶段 | 主测试 | 子测试 | 失败 | 跳过 |
|---|---:|---:|---:|---:|
| unit-and-local | 734 | 382 | 0 | 0 |
| real-infrastructure | 48 | 14 | 0 | 0 |
| 合计 | 782 | 396 | 0 | 0 |

本轮新增32项本地与4项真实Docker主测试，已含在上述总数。相关专项与完整范围重叠，不叠加。完整套件模型HTTP均为替身。旧基线源码/包/HEAD逐文件一致，开发前另做118专项、33实际只读HTTP和固定runner预检，无重复旧付费。

## 实际模型与容器

run 7bf5b872-fae6-4c4d-a154-f2de9470846b：1次真实Flash响应，审阅冻结的3个旧tester生成用例、4个不同输入的host验收全部通过，7个实际Docker用例，无repair/retry。552tokens、342µUSD上界记账、预留0，未核对供应商账单。

原0.9来源run58614923-b31f-43a7-90ea-897703443390及正文不改；先按明确小领域规格核对3个case，再由本机具名operator经API冻结，GET返回execution_reference，产品CLI建立新Goal/计划并以具名operator批准，worker领取后自动从注册表载入精确用例。source model origin只留在原候选，不在新run伪造model-gateway候选。

新的27项实际HTTP核对原来源、suite/合同/context/effect摘要、3个development与4个host报告、1次已知响应、账本和legacy审批拒绝；另33项原四角色只读回归通过。新任务只submitted，review pending，Goal open，第二模型待配置。3个case“已执行”只属于新目标执行，原tester生成任务的历史输出仍表示当时未执行。

## 边界与反例

源epoch/revision/事件/内容、compiler和suite pins不符，存草案/批准/执行必须停止。计划批准要求具名operator；不能在旧批准计划上补字段。suite-only的零辅助item上下文也参与预算/intent/审计同事务；漏传或替换snapshot拒绝，审计失败全部回滚。Rebind前再核对，源换代不能取得新epoch；已知费用保留，已发intent的未知状态保留预留并标usage_complete=false。

真实Docker专项包含3case/host执行、有界repair、隐藏失败不反馈/不追加模型和源变更后不启容器。宿主用例只与开发套件输入不同，来自人工操作器固定样例，不构成统计有效的held-out评测或独立模型证明。

CLI还修复静态发现的real_model_used固定true：现在从实际intent/done事件区分未发起、已返回和未知，并用发起前停止反例验证。异步测试初次误用未安装插件，改标准库asyncio运行，未增加依赖；原7个脚手架失败XML保留，没有转skip。

现有控制台17项Chromium检查通过，模型/沙箱为公开fixture替身；相同JS的16项数值精度证据复用、不算本轮新断言。API27路径；suite专用图形审阅/执行草案表单尚待接入。SQLite单实例、生产身份/恢复、完整PG Store、完整七角色与独立双签仍未完成。
