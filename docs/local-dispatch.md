# 工作台与本机执行器

0.17.3 将执行准备接到持久化请求，再由独立本机进程处理。API 不读取模型密钥，不在 HTTP 请求中执行模型或 Docker。

```powershell
.\scripts\start_local_dev.ps1
```

从源码目录运行。沿用 `work/loom-first-round-session.json` 和 `work/loom-private` 既有配置，首次只生成缺少的本机身份与执行器配置，不覆盖现有密钥。虚拟环境路径为 `work/loom-venv/Scripts/python.exe`，原安装步骤见 README。

在已批准节点的“推进指引”中准备执行，核对摘要与预算、填写理由并显式提交。结构化候选、代码、宿主终验、精确辅助输入批准及待独立验收规则均沿用既有合同。Python 节点须先在私有执行器配置登记冻结宿主用例：

```powershell
..\..\work\loom-venv\Scripts\python.exe scripts/register_host_tests.py --config ..\..\work\loom-private\local-executor.json --tests-file <私有宿主用例文件> --expected-digest <已批准计划中的摘要>
```

只允许摘要完全匹配的既有用例；此操作不修改计划、不调用模型。修改模型路由/身份/镜像等稳定配置后须重启 API 和 worker。

请求记录与完整审计信封、幂等回执同事务。排队不认领、不预留；worker 开始时原子认领并批准 epoch1 精确输入。并发只有一个赢家。进程重启只从可信账本恢复状态，不自动重复已开始的付费调用；失去租约的任务回收执行槽并保留第一次尝试和未知预留，转人工核对。排队未开始时可取消，已开始的工作使用运行控制。

公网体验使用独立配置与数据库，见 [公网体验说明](public-preview.md)。原本机免凭据自动操作员仍只限回环，不向公网直接转发。
