# 0.4.0 验证记录

日期：2026-10-23（Asia/Shanghai）。Windows / Python 3.13.12。阶段 human-control-alpha，非生产就绪。

## 当前验证结论

新增目标控制台、具名角色与人工控制链路已通过本地、真实 HTTP 和浏览器验证。**Docker启动故障恢复后，完整回归门槛已通过：368主测试、265子测试，0失败、0跳过。** 修复前9项setup错误另存历史报告，没有转为skip或删除。

| 阶段 | 实际结果 | 范围 |
|---|---|---|
| unit-and-local | 336 主测试、251 子测试通过 | 核心、API、工作流、认证、人工控制、执行器及模拟网关/沙箱 |
| real-infrastructure | 32主测试、14子测试通过 | NATS live 8、强杀恢复1、PostgreSQL14、Docker9 |
| 合计 | 368主测试、265子测试通过，0失败/跳过 | 完整门槛passed=true |
| 实际HTTP（另计） | 9 + 12 + 31 = 52 个检查通过 | 旧API、Goal/Plan、具名角色/控制/审阅/审计 |
| 真实浏览器（另计） | 14类检查通过 | 浏览器操作及网络故障注入，非真实模型工作流 |

机器结果见同级输出目录的 Loom_0.4_分阶段测试结果_2026-10-23.json。JUnit的主testcase元素与testsuite统计中的子测试分开登记。完整runner在每阶段删除旧报告，任一失败、缺失/损坏报告、零用例或skip均失败。

## 新能力专项

- auth 55 项：具名 operator/worker/viewer、角色矩阵、不可混合 operator+worker、重复身份/token、配置私有路径/大小/schema、错误脱敏与并发读取。
- control API 25 项：主体绑定 worker_id、跨租户/跨角色拒绝、具名审批actor与幂等、伪造测试标记不可信、当前epoch/content_hash绑定、控制/核对。
- operator 33 项及18子测试：暂停保留未知效果、恢复前核对/预算/尝试检查、终止fence、核对不双记账、not_sent不自动重发、原始响应保留、事务回滚与回放。
- execution 26 项及9子测试：增加人工暂停后阻止新沙箱、终止后旧owner不能提交、迟到超支结算不复活cancelled。

这些专项均已包含336项，不重复相加。MockTransport/FakeSandbox不算真实模型或真实隔离。

## 浏览器证据

使用单独loopback临时API、公开测试哨兵凭据和真实浏览器；没有向浏览器自动化提供项目模型密钥或实际操作员token。实测创建草案、上传JSON计划、修订/摘要审批、七任务显示、暂停/恢复/终止、事件链显示、viewer写入受限、凭据不进入本地/会话存储、重载退出、390px无横向溢出。

另注入服务器已创建Goal但回执丢失：同页面重提沿用同一幂等键，返回同一Goal，只创建一次。恶意HTML样式标题显示为纯文本。一次预期的网络错误属于故障注入；普通操作时没有浏览器错误。桌面和手机截图单独保存。

## Docker修复与新的真实任务

Docker Desktop 4.70在Windows26200遇到AF_UNIX残留socket导致Inference manager启动失败。停止核对路径的残留Docker进程后，保留并重建两个仅有socket的运行目录；secrets-engine目录通过Docker WSL内单一shell验证绝对路径/白名单后改名，并恢复新目录原ACL。镜像、卷及WSL数据未删除，未注销发行版或修改全局AI设置。Linux engine29.4.0与原镜像/runner摘要预检恢复；随后9项真实Docker回归通过。详细步骤、备份及旧版本复发风险见同级输出的Loom_Docker_修复记录_2026-10-23.md。

新Unicode连续字符分组任务通过5个宿主冻结用例：312输入+140输出=452tokens，上界记账262微美元，账单未独立核验。本轮仅1次真实模型请求，修复前预检失败未发出请求。CLI使用--tenant first-round与主API共享DB；实际HTTP审阅对账核实当前epoch产物摘要、可信测试、5个case、预算及事件链一致。任务保持submitted/verified=false，双签继续待配置，full_workflow_completed=false。

0.3.0的真实Flash4用例/417tokens/247微美元上界和完整252主测试/247子测试证据保留为历史，见 [0.3验证记录](validation-0.3.md)。双签按用户决定继续待配置，人工核对不产生模型签名，也不能通过有限测试直接验收Goal。

## 安装与后续

0.4.0 editable安装与pip check通过；wheel构建并核对包含3个控制台静态文件和auth/operator模块。无新增应用第三方依赖，现有Starlette TestClient的httpx弃用提示仍保留。

完整复跑命令仍为README中的check_s2.py，显式提供不可变镜像、NATS/native broker和私有PG配置。本次Docker恢复、完整门槛与新任务已经复跑通过；同页面HTTP重提的幂等验证不表示跨浏览器重载/进程重启的exactly-once。

七角色真实协作、修复循环、完整人工裁决/例外接受、生产OIDC/双人授权、PG Store/高可用与上线验收尚未完成。操作控制台本身不会发起模型或执行候选。
