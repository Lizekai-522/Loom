# 既有批准计划的本机执行

0.17.2优先使用[统一节点入口](unified-nodes.md)：支持六种执行角色，按冻结合同自动分流；具名审批、精确输入、宿主判据与单次约束继续保留。以下旧命令继续兼容。

0.15新增scripts/run_approved_task.py，不创建新Goal，不修改工作台已经批准的合同。原路径支持尚未开始、带审阅套件及有界修复、没有辅助输入选择器的developer/fixer；0.17另支持精确批准审查意见后的单次fixer，保留原宿主判据。reviewer生成使用独立CLI。[审查修复步骤](reviewer-execution.md)。第二模型未配置时只提交候选，不最终成功。

## 1. 冻结独立宿主用例

用例文件是仅含tests数组的UTF-8 JSON，每项name/input/expected。configs/host-tests.example.json是“严格正整数统计”样例；只有任务规则相同且自行核对过判据时才使用，不能套到其他业务。host用例与生成公开检查应分开，不把隐藏输入或expected写入需求。

    python scripts/preview_host_tests.py --tests-file <本机宿主用例文件.json>

该命令只读用例并给出evaluation_digest和数量，不加载身份/模型秘密、数据库或容器。将摘要填入工作台新计划的宿主终验摘要，保存后单独批准，保留原文件给执行器。摘要一致不证明用例本身正确。

## 2. 预览已经批准的任务

    python scripts/run_approved_task.py --db <现有主库> --tenant <租户> --run-id <已批准运行ID> --node-id implement --expected-plan-digest <工作台当前计划摘要> --tests-file <宿主用例文件.json> --report <预览报告.json>

默认只读SQLite，不新建库或目标，不读身份/模型秘密，不联网或调用Docker。报告含preview_digest；源suite、冻结合同、host摘要、硬依赖、未开始状态、未知调用和预算不符即拒绝。报告不输出host正文、隐藏答案或凭据。

## 3. 显式执行同一任务

在同一命令追加：

    --execute --preview-digest <本次预览摘要> --identities-file <私有身份文件> --secrets-file <项目模型私有配置> --sandbox-image <已批准sha256镜像ID>

不要把密钥值写入参数。私有配置必须有同租户唯一operator和worker，镜像预检通过后同事务重新核对并只领取指定节点；竞争只一个成功，审计失败全部回滚。预算、效果和执行上下文沿用原执行器原子门槛，修复次数共用原合同预算。

任务已开始、已提交、源换版、运行暂停或先前调用未知时拒绝自动重发；先核对台账再按后续人工恢复流程处理。领取后中断不会自动重试。通过的候选仅submitted，独立review仍被硬依赖阻挡，不能以本机operator或同一Flash多角色冒充双签。

当前本机工作台：http://127.0.0.1:8796/console，直接打开，无需输入访问凭据。主库、镜像及私有配置的本机路径见原启动说明；不要把这些运行目录复制到源码包。
