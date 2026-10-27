# 0.5.0 验证记录

日期：2026-10-23（Asia/Shanghai）。阶段bounded-repair-alpha，Windows / Python3.13.12，非生产就绪。

## 结论与统计

先复测0.4基线368主/265子零失败/跳过及52项HTTP，再通过额外浏览器故障注入发现并修复界面问题；之后推进0.5并进行最终完整回归。

| 阶段 | 结果 | 范围 |
|---|---|---|
| unit-and-local | 396主测试、253子测试通过 | 原内核/API/权限、41项修复专项、7项原子admission、5项摘要API、7项CLI等 |
| real-infrastructure | 36主测试、14子测试通过 | NATS8、broker重启1、独立PG14、原Docker9、新修复Docker4 |
| 合计 | **432主测试、267子测试，0失败、0跳过** | 完整runner门槛passed=true |
| 实际HTTP（另计） | **69项通过** | 原9 + Goal/Plan12 + 具名控制31 + 摘要/旧真实任务兼容17 |
| 真实浏览器（另计） | **34项断言通过** | 慢网络/强制click/迟到响应/文件/队列失败、正常流程、权限、摘要、390px |
| 新修复Docker专项 | 4项通过，6次真实容器用例、6次Mock HTTP | 已包含基础设施36项，不重复加总；模型响应均mock |

新核心41项已包含本地396项；其余专项与主总数同理。JUnit主testcase与testsuite中的子测试分别统计。check_s2.py仍强制4项基础设施配置，拒绝skip、零用例、失败或旧/损坏JUnit。

## 修复与反例

修复了慢切换显示A却控制B、待办失败显示为空、迟到文件覆盖、沙箱返回时暂停不能收敛、隐藏验收失败报告丢失及分事务停态、reserve/intent之间暂停留下孤立预留。原子admission失败注入验证预算/意图/事件/outbox一并回滚；hidden失败最后blocked事件写失败时，报告/分类/状态一并回滚，已发生的模型usage仍保留。

Rebind专项验证同节点新epoch、容量槽不释放、旧claim拒绝、并发幂等一次交接、跨任务/租户与伪造事件拒绝、预算/次数/重复源码停止、二轮暂停/取消、未知HTTP不重发或退款、上下文仅公开失败数据。旧pre-Rebind序列化shape和实际0.4批准合同继续可核验。

## 真实与模拟边界

本轮未新增付费模型调用。新修复专项两轮模型HTTP均为MockTransport，候选程序真实在批准的Docker沙箱执行，不能表述为真实Flash自动修复收益已被证明。此前0.4真实Unicode任务的5用例、452tokens、262微美元上界保持历史；当前0.5API实际读取并核对产物、旧合同、预算与事件链一致，没有重新调用模型。

公开开发检查先执行；隐藏验收失败不进入修复prompt，受信报告单独保留。通过仍submitted/verified=false，独立模型复核待配置；full_workflow_completed=false、counts_as_success=false。模型能力收益、完整七角色与六原语尚未验收。

浏览器使用独立公开sentinel服务8797与独立session，未使用主8796身份或模型密钥；测试结束已关闭自建环境。安全attempt摘要只显示白名单标量，不输出source/stdout/input/expected，HTML样式内容仍为纯文本。

## 安装与交付检查

0.5.0隔离editable构建/安装、pip check通过。最初尝试no-build-isolation因该venv无setuptools失败，改用项目标准隔离构建后成功，无应用依赖变动。原Starlette TestClient/httpx弃用警告仍为非失败提示。

OpenAPI已按0.5生成19个路径；原技术方案的81个数字引用、10个ADR、Python/JSON示例与24项修订映射机械检查通过。主API更新前使用SQLite备份，具名身份和旧token保留；未发布公网或推送GitHub。

机器报告保存在源码目录之外的outputs：Loom_0.5_分阶段测试结果、API回归结果、目标规划API实测、人工控制API实测、旧任务与新增API实测、修复循环隔离实测及0.4推进前浏览器复测。全部日期为2026-10-23。

历史见 [0.4验证](validation-0.4.md)、[0.3验证](validation-0.3.md)。入口与限制见 [有界修复](bounded-repair.md)。Docker仍为旧Desktop4.70临时恢复，升级/复发风险、生产身份/完整恢复/PG Store/高可用等边界继续保留。
