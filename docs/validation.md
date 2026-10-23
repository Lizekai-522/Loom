# Loom 0.7 验证记录

日期2026-10-23，版本0.7.0，execution-context-alpha。**530主测试、326子测试，零失败/跳过；另116项实际HTTP、29项新增真实浏览器断言通过。** 专项与完整套件有重叠，不加总；真实模型另列。

按用户确认的逐步循环，先核对0.6提交0c0a79b/148文件源码包/对应证据及主API健康，再执行A1网关精确预检28主/70子、A2核心101主/61子、B执行86主/9子、C摘要API12主及浏览器29断言。新增真实Docker上下文专项4主通过，已纳入最终完整门槛。

| 完整阶段 | 主测试 | 子测试 | 失败 | 跳过 |
|---|---:|---:|---:|---:|
| unit-and-local | 490 | 312 | 0 | 0 |
| real-infrastructure | 40 | 14 | 0 | 0 |

基础设施40主/14子：JetStream8、原生重启1、独立PostgreSQL14、原Docker9、Rebind Docker4、新上下文Docker4。新增上下文专项实际运行3个容器用例，模型HTTP均Mock，不使用真实key。完整套件real_models_used=false与下方单独真实探针区分。

独立审查发现缓存提交回执仅绑定自报context_digest，篡改完整snapshot后同key仍拿旧回执；无新写入或派发，但同key异参边界不完整。已额外绑定整个有界snapshot摘要，body/标记/bytes/refs篡改拒绝，原样历史重放仍可用。修复前528主/318子通过记录单独保留，不能代替已补拒绝测试后的最终门槛；独立窄复测另列报告。

## 实际HTTP与浏览器

主8796接口：原API9、Goal12、人工控制31、旧真实任务17、候选交接31、新绑定对账16，合计116。旧任务合同、5用例、452 tokens/262µUSD上界、事件链不变。绑定摘要只返回可信local-executor事件的白名单标量，不返回refs/body/source/隐藏数据；读取不写入事件。

浏览器29项是本轮实际新断言，不把旧46重计。独立8797/loom07、公开sentinel身份，覆盖摘要脱敏/非法类型/历史epoch、精确CAS、预览与批准不自动执行、viewer、慢响应/退出和移动端。该fixture启动时实际health为0.6.0/S1-local，报告完整保留观测；目标0.7标签为人工元数据，最终web源hash一致，未用标签替代健康观测。测试会话/服务已关闭，日志保留并排除源码包。

## 一次真实Flash与Docker

独立记录run `849020d2-47cb-4f07-a506-15ebf3884c2a`：具名HTTP Worker发布本机明确批准的requirements输入，先claim开发节点，再按当前epoch由operator精确批准；输入来源不冒充模型分析，也不标已验证。开发器只在user数据中使用typed body，本地身份/授权refs与隐藏测试不送模型。一次Flash响应，5个真实Docker冻结用例通过，452 tokens、费用上界记账251µUSD、剩余预留0。并未查询供应商账单。

任务仍submitted/verified=false，独立review仍被硬依赖阻挡，full_workflow_completed=false。execution.context_bound只证明本机发起准入，不能据此保证远端收到；本次另有实际模型响应和沙箱结果。DB事务不跨网络，输入失效后拒绝继续提交，已发生费用仍保留。

## 安装与边界

标准隔离editable安装及pip check通过，OpenAPI仍24路径（无新付费HTTP执行入口），原方案81数字引用/10ADR/24映射检查通过。升级前SQLite备份完整性通过。代码包/清单/Git逐文件对账与凭据值扫描另记交付校验。未发布公网或推送GitHub。

target epoch0批准不能沿用到领取后的epoch1；Rebind不自动继承辅助授权。失效转人工、释放lease，不补冻结attempts；最后epoch停止可能须新Goal。本轮不是完整七角色/自动交接修复或FR-051 token裁剪。双签按用户决定待配置，生产身份/恢复、完整PG Store、多主机等继续保留。历史见[0.6](validation-0.6.md)，详细输入合同见[artifacts](artifacts.md)。
