# 版本化领域包

0.22.0的领域包为数据资产，组合现有七种受控角色、模型别名及DAG模板，不加载Python、动态工具或任意主机权限。内置software-dev/0.1.0只读；deterministic-fixture示例清单仍只用于演示，不是可授予真实执行权限的包。

普通wheel包含内置工作流和示例资源，通过importlib.resources读取，工作目录无需包含源码packs目录。`loom demo --db <私有临时路径> --report <报告路径>`默认使用包内示例；显式`--pack`继续读取指定fixture清单。示例不调用模型。

接口均需认证：

| 方法与路径 | 行为 |
|---|---|
| GET /v1/domain-packs | 列出内置及本租户的版本和模板索引 |
| GET /v1/domain-packs/{name}/{version} | 导出完整bundle、摘要及发布来源 |
| GET /v1/domain-packs/{name}/{version}/templates/{id} | 将模板展开为带pack_ref的工作流定义 |
| POST /v1/domain-packs | 具名操作员发布新版本，要求Idempotency-Key |

发布体只有audit_run_id和bundle。audit_run_id必须是同租户现有、未终结的Goal，发布事件附着其审计链；不会新建Goal、计入额外Goal配额、改变任务、预算或批准状态。调用者不能传actor、tenant、approved或execute。公网受邀会话只允许读取领域包，禁止发布。

bundle含schema_version=`loom.domain-pack/v1`、name、三段整数version、title、description、scope=`local-workspace`、roles及templates。roles仍必须为七个已注册角色，工具及技能不能扩大。每个模板含id、title、nodes；节点为标准TaskNode。禁止模板携带绑定运行的review_subject、development_suite、evaluation_digest或repair；导出工作流后由实际使用者重新审阅和绑定当前环境的终验资料。硬依赖/角色/输入契约仍由原工作流验证器检查。

同租户name/version不可覆盖或删除。修改内容需新版本；重试完全相同的幂等命令返回原回执。不同租户的版本独立。内置包的固定版本不能被租户遮蔽。

展开模板只产生草稿数据，调用者可编辑节点并按原流程保存、明确批准。pack_ref包含name、version、content_digest；保存、批准及读取执行合同都会重新校验版本内容、角色和发布事件。发布新版本不改变任何已批准任务的路由、工具或预算。选择旧版本可以创建新草稿，不会回滚执行历史；没有隐式latest或全局默认切换。

内置seven-roles保留历史七节点结构模板，需要按实际任务补充类型化输出、复核引用与当前环境的终验绑定；它不表示七角色全部可直接执行。发布及导出接口不检查本机模型、执行器和终验是否就绪，真实可执行条件仍以任务execution-preview为准。当前增量验证的是资产加载和版本冻结，不将结构模板的审批当作七角色完整执行验收。

旧software-dev工作流可继续省略pack_ref；字段省略时旧摘要和TaskSpec结构不变。新自定义包必须有匹配的精确引用，未知或不兼容引用直接拒绝。此版本尚未实现新Agent适配器热加载、完整适用域签名、策略权重独立资产及通用领域语义验收，FR-050/057仍登记为部分实现。
