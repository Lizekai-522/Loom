# 公开检查的编译、审阅、冻结与执行

当前已实现纯编译、具名operator审阅/冻结API、精确execution_reference、新执行草案批准和本机CLI的development/有界repair/宿主验收。来源变更后停止，不能修改旧计划、复制新模型来源或用input grant晋升执行权。0.14工作台已支持审阅、冻结与新执行计划草案准备，使用见console.md。

## 已实现：纯转换和预览

Python 入口为 loom.public_checks.compile_public_checks(body, expected_body_hash=...)；原始 JSON 使用 compile_public_checks_json，拒绝重复字段和非有限数。两层校验分别复用严格候选 schema（32KiB、深度/节点/字段限制、最多32个唯一名称）和执行器 freeze_tests（每个 input/expected 最多16KiB）。输入不被修复、截断或静默忽略。

输出包含 compiler_version、source_body_hash、development_digest、compilation_digest 和 tests。source_body_hash 等于规范化候选正文摘要；development_digest 与原执行器对相同用例计算的摘要一致；compilation_digest 同时绑定前两项和编译版本。对象只保留不可变 JSON 文本，tests 和 preview 每次返回新的树，避免嵌套 list/dict 被改写后继续沿用旧摘要。int/float/bool/null 和大整数保持类型/精度。

本机命令如下。默认只打印摘要、名称和状态；显式 include-cases 才输出输入与预期值：

    python scripts/preview_public_checks.py --body-file <候选body.json>
    python scripts/preview_public_checks.py --body-file <候选body.json> --expected-body-hash <摘要> --include-cases

入口只读取指定、受限大小的正文文件。编译函数无I/O，不读身份/模型配置，不创建DB，不调用模型或Docker。输出 executed=false、confers_authority=false、verified=false；正文hash不证明任务、epoch或模型来源。不能把预览或既有辅助input grant解释为执行许可。

## 协议与接入顺序：单独审阅和冻结

下列协议核心及API、引用批准、执行核对已完成并测试；编译或冻结记录本身不授执行权，仍须新计划批准。

1. 具名 operator 明确审阅候选来源、当前 task/epoch、revision、source plan、schema/body hash 和源事件；在同一事务重新验证 ArtifactService 的内容及完整来源。候选已换代、superseded、终止或来源事件不符必须拒绝。HTTP适配层负责operator权限，内部服务不能把任意subject字符串当作角色认证。
2. 审阅记录使用独立 append-only frozen-suite 注册表，以精确候选引用、原正文摘要、编译版本、按原顺序选定的用例名、development digest、编译摘要和operator绑定。事件、suite正文、回执与outbox同事务。幂等键绑定全部参数；失败不留下部分记录。记录表明“人工批准为development候选套件”，不声明内容正确或来源已独立验证。
3. 已批准的source run/任务合同不修改，也不把生成后的摘要追加进旧repair字段。新执行必须先建立草案执行计划，明确引用该suite记录和独立host acceptance digest，并在首次开发模型调用之前单独批准。跨run使用的是可核对的suite来源引用，不能复制为新run自己的model-gateway候选或伪造原模型call。
4. 将suite绑定引入新执行合同前，必须实现单独的来源引用协议：同租户、来源事件/记录完整性、当前源epoch/revision、suite事件、目标plan/version/预算与编译版本一致。源变化时停止并要求重新审阅；保留旧批准记录，不默默取latest，不改写旧计划。已发生费用和效果保留。
5. development 执行和有界repair沿用原隔离、预算与隐藏验收边界。生成者的公开检查不替代host验收，也不代替独立第二模型；父任务依然只submitted，review硬依赖不被人工suite冻结放行。

验收顺序是注册表与反例 → 权限API/准确预览 → 新草案引用与批准 → 启动时重新核对 → Docker开发检查与repair → 工作台与真实有界交接。每步通过后再推进。纯编译函数始终不执行这些权限动作；0.13本机执行入口见下文。


## 0.11已实现：独立注册表核心

DevelopmentSuiteService 提供 preview、freeze 和 get_suite。构造服务会初始化本地SQLite表；preview方法在已有服务中只读，不同于0.10无I/O纯编译函数。freeze是可信内部入口，调用适配层必须先认证具名operator。0.11阶段没有HTTP路由，0.12已添加下文的认证适配；任意subject字符串不能充当用户角色证明。

preview重新核对ArtifactService完整来源，生成review_digest，绑定源run/任务/epoch/revision/plan/body/event、原origin和可选模型来源摘要，以及选定case、编译版本和development/compilation摘要。选择必须唯一、存在且保持源顺序；不改写候选。freeze要求精确expected_review_digest和actor/key，重新核对后原子写入suite、审计事件、outbox和回执。记录不能UPDATE/DELETE。

get_suite重新核对suite和freeze事件的完整绑定，默认不返回正文；include_body显式返回新拷贝。源换代或暂停等情况保留历史、标stale，新的冻结拒绝；同键历史重放只返回原回执，没有新授权。记录frozen=true同时execution_connected=false、confers_authority=false、producer_verified=false、independently_verified=false；不会放行原依赖或将Goal置成功。

22项注册表测试和真实NATS确认丢失重投/重启测试通过；两个既有真实模型来源在隔离DB副本验证，主库未执行冻结。API角色认证在0.12完成；0.13已接入新草案和受限执行，suite工作台已在0.14接入。


## 0.12已实现：具名operator审批API

三个新路径均按当前身份的tenant过滤；不能通过请求正文指定tenant、actor或verified：

| 方法与路径（均在/v1/runs/{run_id}下） | 权限与行为 |
|---|---|
| GET /artifacts/{artifact_id}/development-preview | read；返回精确来源/选择/编译及review_digest，默认全部case；重复query case可选择保持源顺序的子集，不返回用例正文 |
| POST /artifacts/{artifact_id}/development-suites | 仅具名operator的suite.approve；必须Idempotency-Key，正文expected_review_digest和可选selected_case_names，201返回冻结记录 |
| GET /development-suites/{suite_id} | read；默认不返回正文，显式include_body=true才返回受原schema限制的内容；旧suite可读并标stale |

冻结前同时审阅原artifact正文（既有GET artifact或工作台候选详情）、完整来源和选定case；摘要只保证所选记录一致，不证明内容正确。POST必须使用相同选择的preview digest，不能用all-case摘要批准子集。actor完全从认证主体派生。Worker、viewer和legacy developer无suite.approve；未知套件和跨tenant读取404；暂停/换代后409；错误schema/伪造权限字段422。

这三个接口不加载模型秘密、不调用模型/沙箱、不建立执行计划，也不改变原plan/hard depends/状态/费用。新suite.approve只属于本机operator能力，不代表生产OIDC、独立人员或独立模型验证。0.12初期冻结在隔离副本验收；0.13主库已有单独审阅的真实suite，0.14控制台可显式冻结并读取历史，不重复旧付费执行。

0.13已实现下述新草案和执行核对；0.14已另加suite图形审阅与草案准备，不自动运行。


## 0.13已实现：新执行合同和本机入口

未stale的GET suite增加execution_reference，只有schema/source_run/suite ID、suite/freeze event/review摘要、compiler、development摘要和选定正文hash。将这个对象作为新developer/fixer节点的development_suite，配套原repair.development_digest及不同的evaluation_digest，按原预算/次数批准。None字段省略，旧计划、合同和事件hash不改。

保存草案和批准分别在各自事务中重新验证同tenant的原候选/来源事件、当前epoch/revision、冻结suite及编译版本。新计划不能指向自己的run；来源保留在原run，不复制成新模型候选。套件执行计划的HTTP批准必须具名operator，legacy developer仍可处理旧计划但不能批准该新执行权。

执行器自动载入注册表的精确cases；调用者若另传tests必须摘要一致。即便没有辅助input items，也捕获包含suite引用的执行快照，准入时校验、预占、effect intent和context审计同事务。调用前后、沙箱和提交继续校验；Rebind原子换代前再次校验。源变化停止，不撤销已发生的已知费用，未知intent不自动退款。

产品入口为scripts/run_local_task.py，任务字段加入development_suite并移除development_tests；参考configs/python-task.reviewed.example.json。示例全零摘要/REPLACE ID仅为模板，必须替换成GET suite的execution_reference；不能直接approve模板。host tests由操作器另行冻结，不取生成case作host终验。

    python scripts/run_local_task.py --db <现有DB> --tenant <tenant> --task-file <任务.json> --identities-file <私有身份.json> --secrets-file <私有模型配置.json> --sandbox-image <固定镜像ID> --max-repairs 1 --report <预览.json>

默认仅离线预览，不开DB、不读身份/秘密配置、不调用模型或Docker。显式--approve才检查当前来源和本机operator/worker配置、创建新执行Goal并发起付费；追加--max-output-tokens 768可限制输出，1次修复意味着最多2次请求，隐藏host失败不自动重试。私有身份文件不证明两名独立人员，调用API还须具名operator的真实bearer权限。

真实主库验收run7bf5b872-fae6-4c4d-a154-f2de9470846b：原0.9模型生成3cases经本机审阅冻结，在新计划执行3个development与4个不同输入的host用例通过。1Flash/552tokens/342µUSD上界、预留0，尚未核对账单。目标只submitted，独立review仍等待。


## 0.14工作台入口

在“产物 / 输入预览”选择public-checks候选后，逐项纯文本审阅原JSON，选择子集并点击“核对选中用例”。服务端返回来源、review_digest、编译版本与development_digest；选择改变即清除旧审阅。具名operator勾选确认后才能冻结，viewer/worker/legacy只读预览。

可从“读取此候选的冻结记录”恢复记录，再填独立host evaluation_digest、需求、预算及修复次数。生成草案前重新GET核对来源；来源失效拒绝。下载只含workflow定义与原suite引用，不能当执行批准，不能从开发用例推导黄金答案。

然后在新目标中导入、保存并独立批准；实际执行仍须执行器持有冻结host用例、匹配批准合同和预算。此图形流程没有付费调用入口，也不会创建done或签名；第二模型保持待配置。详见[工作台](console.md)。


## 0.15免输入、中文工作台及既有计划执行（2026-10-25）

按用户最新要求取消手动凭据界面，启用回环/同源自动operator；不暴露Bearer，不授Worker/独立签名权限。界面标签与新默认任务模板中文化，历史冻结记录/协议标识不改。既有批准计划通过run_approved_task.py直接消费：只读预览、宿主摘要、当前preview pin、同事务指定节点领取；无新Goal，不自动恢复或重发。

完整820主/396子，零失败/跳过，含50真实基础设施；新增36本地/2真实Docker已包含。Chromium84（套件44/中文自动进入14/旧来源17/主实例只读9）及旧真实运行60HTTP通过，新增付费0。主库仍37runs/554events/1suite，备份完整。首次UTF-8/浏览器替换及脚手架问题已修，失败记录保留。

下一步reviewer候选schema、精确被审产物与受信证据绑定、operator批准到fixer的受限交接，先测停止/未知/预算/失租与旧签名，再必要真实调用；生产身份/人工处理及恢复并行按原计划推进。第二模型待配置，FR-051/052仍partial；原62项、11/15部署、11/20前提交不变。
