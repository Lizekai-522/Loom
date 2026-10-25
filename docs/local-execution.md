# 本机真实模型与 Python 隔离执行

记录日期：2026-10-23。本轮实际接通 `DeepSeek 4.1 Flash → Python JSON 候选 → Docker 冻结用例 → 任务提交`。本机运行的是 Loom 和 Docker 执行器；模型请求仍访问 DeepSeek 官方远端 HTTPS API，没有在本机部署 DeepSeek 权重。

## 当前实现到哪里

**0.5扩展：** 下面的原单次执行命令继续兼容；新增可选development_tests和显式--max-repairs 1或2，在同一冻结节点内运行公开检查驱动的有界Rebind。冻结终验不回流修复，仍不最终成功；入口与限制见[有界修复](bounded-repair.md)。0.5本轮新增验证是Mock模型HTTP+真实Docker，不把历史Flash探针冒充本轮真实自动修复证据。

`src/loom/execution.py` 的 `LocalTaskRunner` 读取已经批准的工作流合同，只接受 developer/fixer 且同时获准 workspace.write、tests.run 的节点。宿主侧 `freeze_tests` 固定用例和摘要；该摘要必须等于批准节点的 evaluation_digest，None 或不匹配均拒绝。`WorkflowService.get_task_spec` 重新验证批准定义摘要，按冻结 node/role 重建完整合同并核对缓存，防止指令、路由、工具、依赖、预算或判据被单独替换。

当前程序协议很小：候选只能提交 `{"source":"...main.py 内容..."}`；main.py 从标准输入读取一个 JSON 值，并向标准输出写一个 JSON 值。只支持 Python 标准库，不安装候选依赖、不执行任意仓库 pytest，也不把模型输出的 shell 命令直接交给宿主。

默认单次模型prompt包含固定协议及Goal description/节点instruction；启用Rebind时另加入获准的公开修复上下文。测试的黄金 expected 值保留在宿主，不进入模型 prompt，也不进入候选容器。容器每次只收到该用例输入；宿主严格解析候选 stdout 并与黄金 JSON 比较，候选不能用打印“通过”替代受信判断。

调用 `generate(..., json_mode=True)` 会发送 `response_format={"type":"json_object"}`，平台 prompt 同时给出 JSON 格式示例。收到响应后仍检查严格 JSON、唯一 source 字段、字符串类型和源码字节上限；Markdown 代码围栏、额外字段、重复字段、空源码或截断输出均不能进入执行。模式开关本身不等于响应已经验证。[DeepSeek 官方 JSON Output](https://api-docs.deepseek.com/guides/json_mode/)

## 配置与 CLI

从项目根目录运行。配置模板 `configs/models.example.json` 固定官方端点 `https://api.deepseek.com`、模型 ID `deepseek-flash` 和密钥变量名 `DEEPSEEK_API_KEY`。官方当前将该 ID 映射到 DeepSeek-V4.1-Flash-1017，属于供应商维护的别名，不是不可变模型权重摘要。[官方首次调用说明](https://api-docs.deepseek.com/quick_start/)

项目专用密钥放在源码和公开 outputs 之外的私有 JSON 文件；本工作区的位置约定为 `work/loom-private/model-secrets.json`。以下仅展示结构占位，**不是实际密钥**：

```json
{"DEEPSEEK_API_KEY":"<本项目专用密钥>"}
```

不要把私有文件复制到 configs、artifacts、报告或交付包。`load_project_secrets` 只接受本轮注册的变量名，返回脱敏的只读映射，不写入 os.environ；Docker 和其他子进程不会因继承这个映射拿到密钥。Windows 上私有文件访问权限由本机操作器配置，当前不是中央密钥服务。

真正执行的入口是 `scripts/run_local_task.py`，不是 HTTP 执行端点，也不是名为 run-task 的 CLI 子命令。下面命令先只预览计划，不读取私有文件、不访问网络、不启动 Docker、不创建数据库；脚本参数仍要求提供私有文件路径和镜像标识。

```powershell
$python = '..\..\work\loom-venv\Scripts\python.exe'
$secrets = '..\..\work\loom-private\model-secrets.json'
$image = 'sha256:19d840be00b00dd952e065969de532fd28670b8c9e99c7c597408d195f67ac7e'
& $python scripts/run_local_task.py --db artifacts/local-task.db --task-file configs/python-task.example.json --model-config configs/models.example.json --secrets-file $secrets --sandbox-image $image --report artifacts/local-task-preview.json
```

明确开始一次真实模型调用时，使用同一命令增加 `--approve`，并另存报告：

```powershell
& $python scripts/run_local_task.py --db artifacts/local-task.db --task-file configs/python-task.example.json --model-config configs/models.example.json --secrets-file $secrets --sandbox-image $image --max-output-tokens 512 --approve --report artifacts/local-task.json
```

`--approve` 表示本机操作器确认本次任务计划，可能由自动化测试显式传入。审计 actor 为 operator:local，不冒充真实个人审批主体，也不等于 Gate-V 已经通过。脚本每次带 approve 运行都会建立新的 Goal；重复运行可能产生新的付费请求，不能把它当作免费的重试或对先前不确定调用的自动恢复。

任务文件需要title、description、tests，可选公开development_tests；每项测试包含 name、input、expected。可以参考 `configs/python-task.example.json` 换成新的同类 JSON 程序任务。黄金 expected 属于宿主测试数据，不要另外拼入 description。单文件最多 256KiB；测试数量为 1–32，每项 input/expected 各最多 16KiB。该脚本构造“implement 开发节点 + 依赖它的 independent_review 验证节点”，沿用完整七角色注册，但本轮只执行开发节点。

单独检查连通性可用 `scripts/check_model.py --secrets-file <私有文件路径>`；它同样可能产生一次真实付费请求，不执行生成代码。网关 readiness 只做本地路由和密钥格式检查，不证明网络、余额或远端可用。完整 HTTP 边界、错误脱敏和模型身份说明见 [模型网关文档](model-gateway.md)。

## Docker 运行时与隔离边界

本次真实执行使用的**本地镜像 ID**是：

```text
sha256:19d840be00b00dd952e065969de532fd28670b8c9e99c7c597408d195f67ac7e
```

它是本机已构建的 Loom runner 镜像标识，不表示已经发布到镜像仓库，另一台机器不能仅凭该 ID 自动拉取。`sandbox/Dockerfile` 的 Python 基础镜像固定为 `python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530`；构建方式、平台和构建器可能影响最终本地镜像 ID，迁移时需明确批准实际构建结果，不能拿可变 tag 代替。

执行前检查 Linux Docker daemon、内存/交换空间/CPU/PID 限制和 seccomp 支持，并检查镜像用户、入口、runner 版本及摘要标签。配置只接受固定摘要或 image ID；运行使用 `--pull never`，不会在执行时临时下载镜像。runner 源码摘要为 `ab3fd758ec426a61fe642f853a5872e67808805ef2e900718250ea1b12dc1e41`。

每个用例创建独立容器：非 root 用户 10001:10001、只读根文件系统、无网络、cap-drop ALL、no-new-privileges、私有 cgroup、无宿主目录/设备/Docker socket 挂载。临时 workspace 和 tmp 位于有限 tmpfs，候选文件通过 stdin 中的受检归档传入；凭据、宿主文件路径和 Docker 参数不由模型决定。创建后再 inspect 核对实际策略，缺失约束直接拒绝。

默认每用例 5 秒、128MiB 内存、0.5 CPU、32 PID，workspace 与 tmp 各 16MiB，合计保留 stdout/stderr 最多 64KiB。超时或输出超限后终止本执行器拥有的容器，验证停止状态并在 finally 清理；不枚举清除其他 Docker 资源。JSON 输出中重复字段、非有限数值、协议错误或运行错误都不能通过黄金比较。容器是本机有限任务隔离，不构成已经验收的生产恶意代码多租户平台。

## 预算、lease 与未知结果

调用前先原子预留 Goal 与任务预算，再登记模型外部调用意图。没有可靠 tokenizer/聊天模板上界时，当前适配器用 Flash 上下文容量 1,048,576 作为保守输入 token 上界，输出使用实际 max_output_tokens；这会明显高估预留，不把字符数启发式当成严格上界。

本轮固定费率版本 `deepseek-flash-2026-10-23-peak-upper`，按高峰、输入全部缓存未命中计算：

```text
预留/记账上界 µUSD = ceil((300 * 输入 tokens + 1200 * 输出 tokens) / 1000)
预留 tokens = 1,048,576 + max_output_tokens
```

默认输出 512 tokens 时，预留 1,049,088 tokens 与 315,188µUSD，约 0.315188 美元；不是承诺这一笔会实际花费这么多。脚本的 Goal cap 为 1,000,000µUSD/2,000,000 tokens，开发节点 cap 为 500,000µUSD/1,100,000 tokens、最多一次尝试；输出参数限制为 1–1024。计划批准检查各任务 cap 总和，Store 在预留时继续检查全 run 与每任务累计预留/已结算用量。

有效响应返回后按实际 provider usage 保守结算，释放差额；费用值是基于本轮固定价目的整数上界记账，**不是独立查询的供应商账单**，也不是账户级强制美元封顶。价目更新时须重新核实费率版本。[官方价格说明](https://api-docs.deepseek.com/quick_start/pricing/)

超时、断连、错误响应、无效 usage 或取消均不能证明供应商没有执行或没有计费。网关和执行器没有自动重试；不确定调用保留预算与 indeterminate 外部意图，进入 awaiting_human 等待核对，不能直接退款再发。有效响应已知后发生源码格式或沙箱错误，已发生的模型 usage 也不撤销。

执行开始续租至 90 秒，每 10 秒心跳续租；完成提交前再次检查 epoch/worker/有效 lease。取消或失租使执行失去提交权，未知模型调用需要核对。Docker 工作在受限后台线程时，取消等待不保证线程立即停止；线程仍由用例上限和 finally 清理约束，不能把本机任务取消解释为远端停算或免单。

## 2026-10-23 的真实结果

已核对 [真实模型与隔离执行报告](../../Loom_真实模型与隔离执行结果_2026-10-23.json)：本次 Flash 请求 usage 为 **282 输入 + 135 输出 = 417 tokens**；4 个冻结用例 mixed_duplicates、empty、negative_and_zero、all_same 全部通过。保守费用记账为 **247µUSD**，剩余本次成本预留为 0，实际账单未独立查询。

报告同时明确：任务状态为 **submitted、verified=false**，verification=awaiting_independent_models；run 仍 open，事件链有效，依赖此任务的验证节点不能领取。报告的 passed 只表示这次受限适配链路及冻结用例达到探针验收，counts_as_success=false、full_workflow_completed=false，不能读作产品 Goal 已成功或完整七角色已经协作完成。

之前一次普通文本响应返回 Markdown，按 JSON source 合同被阻断并单独保留记录；随后显式启用官方 JSON 模式才完成上述调用。两次是分别授权的检查，不是后台自动重试，失败调用不能因为未执行代码就视为未计费。

默认 `packs/software-dev/workflow-v1.json` 仍是 spec → design/case → code → review → fix → verify 的七角色规划模板，evaluation_digest 默认 None，未完成该模板的真实全链执行。当前配置的七角色都路由到同一个 Flash，也不能形成两个不同受信模型身份。用户已确认本阶段先接通 Flash，双签保留待配置；最终 Goal 验收继续受独立验证约束，此执行器不会伪造签名，也不把有限用例通过直接改成 done/succeeded。

本轮模拟 HTTP/模拟 sandbox 的状态机测试与上述真实探针分别统计。工作流专项测试验证冻结合同、事务和能力边界；它们都不能替代真实第二模型、全工作流、生产权限及恢复演练。

## 0.7 已批准辅助输入执行

新入口`scripts/run_context_task.py`，默认只读task/pack并预览，不读身份或密钥、不打开DB、不联网或启动Docker。显式`--approve`后才通过本机具名HTTP Worker发布本机准备的requirements，operator审批计划/当前target epoch输入，随后执行Flash与Docker；这不是analyst模型生成。API与本机DB必须一致，错误库最多留下一个draft，不批准/领取。参数见第七轮测试指南和脚本help。

本轮真实run`849020d2-47cb-4f07-a506-15ebf3884c2a`，5用例通过，452tokens、251µUSD上界，剩余预留0，非账单。实际输入绑定摘要`07e3266fd893bcdb4f01839f3fddb2c26026a9012f00d98f5550b4c06a7af0f8`；只发typed user数据，权限和隐藏用例不送模型。task只submitted/verified=false，独立review未领取。

快照/预算/intent/绑定审计同事务，返回后先结已知usage再检查版本；沙箱及提交再次复核。失效转人工，旧grant不跟新revision或Rebind epoch。不能把本机准入称远端收据；网络期间不持DB锁。没有自动补attempt或等待状态，最后epoch停止可能需新Goal。

## 0.8 受限分析与设计

`scripts/run_role_tasks.py`默认仅预览，不开DB/私有配置、不联网。显式`--approve`后，已有DB及容量检查在HTTP写前；draft创建后立即同库核验，再保存/批准合同。每角色最多一次Flash，共最多两次、1024输出tokens/次，无自动重试、代码执行或文件导出。spec/design之间采用明确批准的辅助数据，没有新增硬依赖；独立review仍依赖两者验证完成。

本轮run`1d3a9c8c-df0e-43f3-93d6-b386658e5584`真实响应两次，3005tokens/2120µUSD费用上界，预留0，未核对账单；requirements/design均只submitted，内容未独立验证。命令参数及浏览器首轮检查见公开第八轮测试指南。旧Python/Docker与已批准输入路径保留。来源已记录不表示Gate-V通过。


## 0.9 四角色入口

scripts/run_workflow_task.py与configs/workflow-task.example.json提供同run的analyst→architect→tester→developer辅助交接。默认仅读公开job/Pack并预览，不开DB/私密配置、HTTP或Docker。批准后先核验已有库/容量与Docker，再具名HTTP创建draft并立即同库核对；计划与host验收摘要在模型请求前冻结。

上限4Flash请求，每次输出1–1024tokens，最多8host用例，无重试/repair。Goal cap 2001000µUSD/4401000tokens，四业务节点各500000µUSD/1100000tokens、1attempt。六grant按1/2/3项输入分配；生成checks只作未执行参考，host-authored测试才构成developer终验。四业务任务只submitted，review硬依赖全部四父；具名自动流程不代表两名真人独立审阅。

2026-10-25：Docker残留socket及父目录访问错误阻塞，真实四角色链未验收。API与单独真实tester可用，代码执行当前不可用。先恢复Docker并通过check_s2，再按第九轮指南预览/显式批准；不能绕过preflight或在宿主执行生成代码。


## 0.9 最终验收与本机Docker入口

2026-10-25已恢复Docker，完整660主/396子、196HTTP、33浏览器通过。真实四角色run58614923-b31f-43a7-90ea-897703443390取得4Flash响应/5host用例通过，4901tokens/2440µUSD上界、预留0，未核对账单。六次grant与1/2/3项上下文均核对，生成checks未执行，四任务submitted、review pending，第二模型仍待配置。

原系统secrets-engine目录即使管理员也无法改名。项目通过进程专用LOCALAPPDATA缓存启动Docker，缓存wsl junction仍指向原磁盘；未改全局环境或设置，原镜像/runner不变。源码入口scripts/start_local_docker.ps1接受必填RuntimeRoot；先用-InspectOnly检查，退出已有Docker后去掉该参数启动。只有已知socket文件的目录才可改名保留，异常链接/额外文件停止，不删除。此入口已实际正常停止/再启动并通过3项真实交接复测；普通Desktop快捷方式的原目录故障没有解决。

本机命令：powershell -NoProfile -File scripts/start_local_docker.ps1 -RuntimeRoot ../../work/loom-docker-cache 。Docker启动后仍须通过固定镜像preflight才进入执行，启动成功不等于引擎健康。详情见第九轮测试指南。


## 0.13审阅套件执行补记（2026-10-25）

新增可省略的development_suite精确引用，旧计划/合同hash保持。存草案、具名operator批准、执行准入/返回/沙箱/提交与Rebind前验证同tenant来源、epoch/revision、suite事件/编译与摘要。零辅助items也绑定suite上下文；引用不复制模型来源、不改旧计划，未知intent预留保留。

产品CLI支持新引用任务，默认离线preview，显式approve须现有DB及私有operator/worker配置。自动载入注册表用例，拒绝混用raw development_tests；host验收单独冻结且失败不回流修复。GET suite给出当前execution_reference；API路线仍27条。

完整782主/396子，0失败/跳过，含48真实基础设施。真实run7bf5b872-fae6-4c4d-a154-f2de9470846b，1Flash、3生成+4host用例通过，552tokens/342µUSD上界、预留0、未核对账单，目标submitted/独立review pending。27新+33旧HTTP和17Chromium通过；源码/包/凭据另核。suite图形表单和完整七角色/生产/双签仍未完成。


## 0.15免输入、中文工作台及既有计划执行（2026-10-25）

按用户最新要求取消手动凭据界面，启用回环/同源自动operator；不暴露Bearer，不授Worker/独立签名权限。界面标签与新默认任务模板中文化，历史冻结记录/协议标识不改。既有批准计划通过run_approved_task.py直接消费：只读预览、宿主摘要、当前preview pin、同事务指定节点领取；无新Goal，不自动恢复或重发。

完整820主/396子，零失败/跳过，含50真实基础设施；新增36本地/2真实Docker已包含。Chromium84（套件44/中文自动进入14/旧来源17/主实例只读9）及旧真实运行60HTTP通过，新增付费0。主库仍37runs/554events/1suite，备份完整。首次UTF-8/浏览器替换及脚手架问题已修，失败记录保留。

下一步reviewer候选schema、精确被审产物与受信证据绑定、operator批准到fixer的受限交接，先测停止/未知/预算/失租与旧签名，再必要真实调用；生产身份/人工处理及恢复并行按原计划推进。第二模型待配置，FR-051/052仍partial；原62项、11/15部署、11/20前提交不变。
