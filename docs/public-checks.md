# 公开检查的编译、审阅与冻结

当前已实现纯编译器、本机预览入口和独立审阅注册表核心。它们只把未执行的 public-checks 候选转换为确定的开发用例表示，注册表记录单独的development审阅批准，但尚未开放suite HTTP/UI或将模型生成用例接入执行合同。原宿主 development/acceptance 路径继续沿用已有冻结合同。

## 已实现：纯转换和预览

Python 入口为 loom.public_checks.compile_public_checks(body, expected_body_hash=...)；原始 JSON 使用 compile_public_checks_json，拒绝重复字段和非有限数。两层校验分别复用严格候选 schema（32KiB、深度/节点/字段限制、最多32个唯一名称）和执行器 freeze_tests（每个 input/expected 最多16KiB）。输入不被修复、截断或静默忽略。

输出包含 compiler_version、source_body_hash、development_digest、compilation_digest 和 tests。source_body_hash 等于规范化候选正文摘要；development_digest 与原执行器对相同用例计算的摘要一致；compilation_digest 同时绑定前两项和编译版本。对象只保留不可变 JSON 文本，tests 和 preview 每次返回新的树，避免嵌套 list/dict 被改写后继续沿用旧摘要。int/float/bool/null 和大整数保持类型/精度。

本机命令如下。默认只打印摘要、名称和状态；显式 include-cases 才输出输入与预期值：

    python scripts/preview_public_checks.py --body-file <候选body.json>
    python scripts/preview_public_checks.py --body-file <候选body.json> --expected-body-hash <摘要> --include-cases

入口只读取指定、受限大小的正文文件。编译函数无I/O，不读身份/模型配置，不创建DB，不调用模型或Docker。输出 executed=false、confers_authority=false、verified=false；正文hash不证明任务、epoch或模型来源。不能把预览或既有辅助input grant解释为执行许可。

## 协议与接入顺序：单独审阅和冻结

第1–2项的注册表核心已完成，适配层operator认证、后续合同引用和执行仍待实现，不能用作当前执行入口。

1. 具名 operator 明确审阅候选来源、当前 task/epoch、revision、source plan、schema/body hash 和源事件；在同一事务重新验证 ArtifactService 的内容及完整来源。候选已换代、superseded、终止或来源事件不符必须拒绝。HTTP适配层负责operator权限，内部服务不能把任意subject字符串当作角色认证。
2. 审阅记录使用独立 append-only frozen-suite 注册表，以精确候选引用、原正文摘要、编译版本、按原顺序选定的用例名、development digest、编译摘要和operator绑定。事件、suite正文、回执与outbox同事务。幂等键绑定全部参数；失败不留下部分记录。记录表明“人工批准为development候选套件”，不声明内容正确或来源已独立验证。
3. 已批准的source run/任务合同不修改，也不把生成后的摘要追加进旧repair字段。新执行必须先建立草案执行计划，明确引用该suite记录和独立host acceptance digest，并在首次开发模型调用之前单独批准。跨run使用的是可核对的suite来源引用，不能复制为新run自己的model-gateway候选或伪造原模型call。
4. 将suite绑定引入新执行合同前，必须实现单独的来源引用协议：同租户、来源事件/记录完整性、当前源epoch/revision、suite事件、目标plan/version/预算与编译版本一致。源变化时停止并要求重新审阅；保留旧批准记录，不默默取latest，不改写旧计划。已发生费用和效果保留。
5. development 执行和有界repair沿用原隔离、预算与隐藏验收边界。生成者的公开检查不替代host验收，也不代替独立第二模型；父任务依然只submitted，review硬依赖不被人工suite冻结放行。

验收顺序是注册表与反例 → 权限API/准确预览 → 新草案引用与批准 → 启动时重新核对 → Docker开发检查与repair → 工作台与真实有界交接。每步通过后再推进。当前编译器没有提前实现或绕过这些后续权限。


## 0.11已实现：独立注册表核心

DevelopmentSuiteService 提供 preview、freeze 和 get_suite。构造服务会初始化本地SQLite表；preview方法在已有服务中只读，不同于0.10无I/O纯编译函数。freeze是可信内部入口，调用适配层必须先认证具名operator；当前没有新增HTTP路由，因此不能把任意subject字符串当成用户角色证明。

preview重新核对ArtifactService完整来源，生成review_digest，绑定源run/任务/epoch/revision/plan/body/event、原origin和可选模型来源摘要，以及选定case、编译版本和development/compilation摘要。选择必须唯一、存在且保持源顺序；不改写候选。freeze要求精确expected_review_digest和actor/key，重新核对后原子写入suite、审计事件、outbox和回执。记录不能UPDATE/DELETE。

get_suite重新核对suite和freeze事件的完整绑定，默认不返回正文；include_body显式返回新拷贝。源换代或暂停等情况保留历史、标stale，新的冻结拒绝；同键历史重放只返回原回执，没有新授权。记录frozen=true同时execution_connected=false、confers_authority=false、producer_verified=false、independently_verified=false；不会放行原依赖或将Goal置成功。

22项注册表测试和真实NATS确认丢失重投/重启测试通过；两个既有真实模型来源在隔离DB副本验证，主库未执行冻结。上文协议中的“API角色认证、新草案来源引用、执行前核对、development/repair/UI”仍是下一步，不能因为注册表存在而视为已完成。
