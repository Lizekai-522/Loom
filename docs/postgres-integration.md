# PostgreSQL 生产适配候选设计与验证计划

更新日期：2026-10-23。目标：PostgreSQL 16+。对应迁移：`migrations/postgres/001_core.sql`。

**状态：DDL 已在隔离的 PostgreSQL 16.15 上应用，14 项真实数据库检查通过；属于 S2 的部分进展。** Docker daemon 未运行，本次使用官方页面指向的 EDB 便携二进制，新建 loopback 测试 cluster，没有安装系统服务或提权。当前应用内核仍使用 SQLite；PostgreSQL Store 适配器、生产写命令过程、全局容量/预算并发协议、真实副作用和灾备恢复仍未完成。

## 1. 已提供的数据库结构

| 设计对象 | 候选 DDL 的约束或字段 | 仍依赖实现的行为 |
|---|---|---|
| 租户/run 归属 | 所有运行资源使用 `(tenant_id, run_id, resource_id)` 复合键与同域外键 | OIDC 身份到数据库主体映射、对象下载授权 |
| 三层与证据 | goals、facts、intents、intent_facts、tasks、task_dependencies；统一 UUID 事实引用 | Intent 至少一个支撑事实的事务级验证；改图无环验证 |
| 不可变验收 | ruleset_version、anchor_digest、结构化判据；触发器拒绝原位更换 | 仅经受控命令新建版本；不能向业务连接授予任意 UPDATE |
| 完整事件 | 类型、操作者、租户、run、链、序号、修订、时间、载荷、幂等键、前序摘要均进入信封 | 固定跨语言编码规范、受控追加过程、外部签名核验 |
| 链顺序 | 追加前锁 run 行，校验 seq、prev_hash、run_revision | 正确的锁顺序和死锁重试；不能绕过受控链头更新 |
| 持久投递 | 事件插入触发同事务 outbox；receipts 与 inbox 去重键 | 业务命令/回执同事务；发布确认、重发与消费者事务 |
| Claim/fencing | 每任务有效 claim 部分唯一索引，任务行锁递增 epoch，数据库时间签发租约 | 配额预占、心跳、回收器、出口每次发起前重新校验 |
| 预算 | 整数微美元与 token、预留、结算、未知态、不可变 ledger | 原子账户锁、幂等结算、网关最大计费量边界 |
| 副作用 | 跨尝试稳定 logical_operation_id 唯一键，claim 绑定、参数摘要、去重有效期 | 发起权原子获取、旧 Worker 拦截、恢复冻结与人工核对 |
| 验证 | Attestation 绑定产物摘要、证据修订、规则、验证者、模型和签名记录 | 真正验签、可信证据、独立模型、Gate-V 状态迁移 |
| 审计 | 受 RLS 约束的 audit scope、外部 checkpoint 引用 | 审计请求本身的追加记录、签名服务与外部只追加存储 |

SQLite 演示与此候选模型并非字节兼容迁移：SQL 中使用 UUID、明确的 `verified` 状态与微秒事件时间；本地 adapter 仍有简化字段。接入时需实现事件版本转换，不能直接复制本地 JSON 并声称已兼容。

## 2. 角色与迁移权限

迁移文件故意不创建真实登录账号，不设置任何密码，不修改现有集群角色。请先由 DBA 在专用测试集群创建下列组角色，确认名称未被其他项目使用：

```sql
CREATE ROLE loom_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE loom_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE loom_gateway NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE loom_auditor NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE loom_scope_admin NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
```

创建角色需要 `CREATEROLE` 或相应管理员权限。迁移登录主体需具备 `SET ROLE loom_owner` 的成员权限，`loom_owner` 需对目标数据库具有 `CREATE` 权限。迁移中没有 `CREATE EXTENSION`，SHA-256 使用 PostgreSQL 自带的 `sha256(bytea)`。示例角色和数据库授权只应作用于专用数据库，凭据通过部署系统管理。[角色权限](https://www.postgresql.org/docs/16/sql-createrole.html)、[内置 SHA-256](https://www.postgresql.org/docs/16/functions-binarystring.html)

运行账号与组角色分开：业务连接只属于 `loom_app`，网关连接只属于 `loom_gateway`，审计连接只属于 `loom_auditor`。业务和审计账号不得成为 `loom_owner`、`loom_scope_admin` 的成员，不得有 `SUPERUSER/BYPASSRLS/CREATEROLE`，也不能获得这些角色的 `SET ROLE` 能力。分别配置连接池，避免同一登录身份同时承担业务和审计用途。

**迁移后的业务/网关/审计默认只有 SELECT。** RLS 中的 `FOR ALL` 策略只表达行范围，不赋予 SQL 写权限。尚未完成受控命令过程、验证专用写入角色及授权测试前，不授予这些账号通用 INSERT/UPDATE/DELETE。当前 FastAPI 服务不能切换到这份 DDL 直接使用；需要新增 PostgreSQL adapter。

## 3. RLS 的真实边界

租户可见性使用管理员维护的 `principal_scopes`，按数据库认证身份 `session_user`、租户、访问模式和有效期判定。授权表对普通主体也启用 RLS，只可查看自己的条目；修改权限仅属于独立 scope 管理角色。审计角色只能看到其 `audit` 条目授权的租户，**没有 BYPASSRLS**。

没有使用 `SET loom.tenant_id = ...` 充当认证。任意可设置的自定义 GUC 不能证明调用者属于某租户。推荐测试阶段使用每租户独立数据库登录，并仅映射其租户。若未来共享连接池登录被授权多个租户，该凭据本身就能读取这些租户；应用的请求级鉴权仍必须正确，不能将 RLS 宣称为被攻破的共享应用凭据之上的完整隔离。

所有租户表同时 `ENABLE ROW LEVEL SECURITY` 与 `FORCE ROW LEVEL SECURITY`。这能使普通表所有者查询也受行策略约束，但不能约束超级用户或具有 BYPASSRLS 的主体；TRUNCATE/REFERENCES 等也不受普通行策略控制，故权限撤销与拒绝触发器同时存在。拥有对象定义权限的迁移管理员仍能改变规则，属于显式信任边界。[PostgreSQL RLS](https://www.postgresql.org/docs/16/ddl-rowsecurity.html)、[策略与 WITH CHECK](https://www.postgresql.org/docs/16/sql-createpolicy.html)

角色默认读权限不代替 Gateway 的工具授权、签名验真和业务状态机。不得把数据库连线或组角色授给模型、Worker 子进程、领域 Pack 或任意工具执行器。

## 4. 事件、幂等与链头协议

`events` 同时存 typed metadata、完整 JSON 信封和精确 UTF-8 文本。约束要求：文本解析结果等于 JSON；JSON 等于所有 typed 字段构建的完整信封；`event_hash=SHA256(UTF8(envelope_canonical))`。空 `submission_key` 按 JSON null 编码，不能调用空值 `.encode()`。

这验证精确存储字节的摘要，不自动实现 RFC 8785 或证明所有语言序列化一致。生产适配前需固定 `canonical-v1` 编码规则及跨语言向量，包括 Unicode、JSON null、整型范围、嵌套键顺序和浮点禁用/表示。当前本地 Python JSON 规则不能不经验证就当成跨语言标准。

同 run 追加先锁 `runs` 行，读 head，在事务中写业务变更、完整 event、command receipt 与 outbox。`events_outbox` 触发器只保证事件与 outbox 同事务；业务变更和回执仍需命令层置于同一事务。回执请求摘要必须涵盖操作、资源、输入版本、所有验证证明和参数，不能仅摘要验证者名字。

发布器仅在收到持久 broker ack 后标记 published；在 ack 与本地标记之间崩溃允许重投。消费者在自己的业务事务内检查/插入 inbox；发生异常时，效果与去重标记一起回滚。建议按稳定锁顺序使用 `FOR UPDATE SKIP LOCKED` 领取发布批次，先验证隔离级别、领取租约和多发布者行为再上线。

表级权限和触发器禁止 UPDATE、DELETE、TRUNCATE 已追加事实、事件、验证记录、账本、回执及检查点。run/Goal 的 Anchor 与冻结验收另有更新拒绝触发器。数据库内链头能发现普通损坏，但要发现尾部丢失或全库重写，还需独立签名检查点及外部锚定。签名列存在不等于验签实现完成。

## 5. Claim、预算与外部副作用的事务要求

### Claim 与容量

Claim 插入触发器使用数据库时钟并锁住 task/run 行，递增当前 epoch；部分唯一索引阻止同任务两条 `active` claim。索引按状态判断，**不把本地时间或 `now()` 放进部分索引条件**。已过期但仍 active 的行仍会阻止新认领；回收器必须先在同一协议内将其置 expired，再签发新 claim。

候选 DDL 尚未实现全局业务/验证容量账本、任务 ready 判定或跨分片领导者 fencing。生产命令过程必须先获取容量预占与 task 行锁，并在同事务创建 claim；不得仅依据内存计数。Gateway 在发起动作前重查 run open、任务修订、claim state、epoch 和数据库时间；不能只检查 Worker 最后提交时的 token。

### 预算

预算使用整数微美元。每次预留需锁 `budget_accounts`，读取已花费与在途预留，再插入 reservation、ledger 和 event。结算必须幂等，未知收费不能直接当零费用释放。

实际收费超出预留时，应保存真实收费、将账户标 overrun、将 run 冻结，并停止其他有效 claim 的新动作。DDL 允许 overrun 状态记录超上限实际值；不能通过拒绝那条结算记录来制造“从未超支”的假象。跨表账本与聚合账户一致性仍需受控过程、重建校验及故障测试。

### 外部副作用

`(tenant_id,run_id,task_id,logical_operation_id)` 在不同 epoch 间保持唯一。不要用未编码的字符串拼接构造调用身份；规范编码或独立 UUID 并配合唯一逻辑键。持久发起权获取完成后，Gateway 才能实际发出请求，且非幂等传输不得透明重试。

恢复必须核对完整记录；发生 RPO 窗口、PITR 回退或独立账本缺失时，不能把“没有 tool.intent”直接等同“未执行”。冻结有关写操作并核对外部状态。对端幂等键还需明确参数绑定、去重窗口及过期行为。

提交 Worker 产物前必须先处理未决外部效果，避免将 task 改为 submitted、清除租约后导致确认通路被切断。提交结果仅为 submitted；只有受信验证流程才可将产物/任务置 verified 并开放依赖。人工例外保留独立状态，不计正常成功。

## 6. 在隔离 PostgreSQL 实例上的验证步骤

下列步骤是完整集成门禁。本次已在新建合成数据库中执行 DDL、角色/RLS、触发器、复合外键和双连接 claim 竞争检查；生产写命令层、完整账本/投影重建、主备/RPO 与真实 Gateway 行为仍待验证。测试不接真实工具、用户数据库或模型计费账户。

1. 启动固定镜像版本的 PostgreSQL 测试实例，记录确切版本与数据库参数，使用新建专用数据库。
2. 由 DBA 建立上述组角色和独立测试登录；迁移登录取得最小创建与 SET ROLE 权限，业务登录无 owner/scope_admin 成员关系。
3. 通过 `psql -v ON_ERROR_STOP=1 -f migrations/postgres/001_core.sql` 应用。连接信息通过环境变量或受控凭据配置提供，不将密码写进 SQL、仓库或报告。
4. 检查所有表、外键、触发器、索引及政策；断言业务/审计角色 `rolsuper=false`、`rolbypassrls=false`，租户表均启用并强制 RLS。
5. 使用独立 scope 管理身份录入两个合成租户及有限授权；受控测试装载器写入样例运行与关联数据。由于当前 runtime 默认只读，生产写测试必须等命令过程和最小 writer 权限明确后执行，不能为了通过测试长期授予业务账号表 owner。
6. 用实际租户 A/B 的登录分别执行查询，验证跨租户不可见；用只授权租户 A 的审计登录验证不能读取 B；改变自定义 tenant GUC 不应扩大任何授权。
7. 在事务中临时授予隔离测试写角色必要权限，验证跨 run/tenant 外键、append-only、错误 hash、跳号、旧 head、active claim 唯一与过期认领。测试后撤销临时权限；最终以真实生产角色重跑命令级用例。
8. 两连接并发争抢最后容量、同任务 claim、同幂等键和最后预算；在每个提交/确认边界注入进程退出和连接中断。
9. 完成 ledger、output、claim epoch、图修订、验证记录、effect 状态的事件重建等值测试；审计回放禁止调用模型或外部写接口。
10. 进行主备切换、RPO/PITR 回退、外部账本核对、检查点截断检测；测试未完成前禁止声明“零重复外部写”或生产就绪。

### 最低集成验收矩阵

| 测试 | 预期 |
|---|---|
| 错误 tenant/run 引用 | 复合外键或 RLS 拒绝 |
| 审计 scope 未授权 | 不返回行；不能靠 BYPASSRLS 获取数据 |
| 同 run 并发追加同序号 | 只有正确 head 的一次追加成功，另一请求重读/重试 |
| 修改事件 type/actor/tenant/seq/修订 | 完整信封校验或摘要校验失败 |
| UPDATE/DELETE/TRUNCATE 追加表 | 实际业务角色被权限或触发器拒绝 |
| 数据提交后发布器退出 | outbox 仍可恢复派发 |
| broker 已确认、本地未标记 | 可重投，但 consumer/inbox 只生效一次 |
| 同任务两个 active claim | 数据库唯一约束拒绝第二条 |
| 旧 epoch 或 run 已熔断 | Gateway 不授予新的发起权 |
| 预算最后额度并发预留 | 预留与账户总额不越限 |
| 对端实际收费违约超上限 | 保留实耗、冻结 run，不能静默丢结算 |
| 未知外部效果与 PITR 回退 | 挂起并核对，不自动重做非幂等写 |
| 非 signed/旧版本/同一验证者 | 验证层拒绝成功迁移 |
| 基准失败后人工接受 | accepted_with_exception，不进入正常成功统计 |

## 7. 本次验证范围

2026-10-22 完成 SQL 编写、官方语义核对和离线解析检查：

| 项目 | 结果 |
|---|---|
| 工具 | `pglast==7.21`，内嵌 PostgreSQL 17.7 解析器 |
| 顶层 SQL | 63 条语句均可解析 |
| PL/pgSQL | 8 个函数/DO 主体均通过 `parse_plpgsql` |
| SQL 函数 | 1 个 SQL 函数主体通过解析 |
| 动态 SQL | 7 个 format 模板以安全占位表名展开后可解析 |
| 结构检查 | 21 张表全部含 tenant_id |
| 当日实例执行 | 未执行；以下 2026-10-23 记录补充了实际数据库检查 |

解析器临时安装在本次工作区 `work/postgres-sql-parser`，没有加入项目 requirements、pyproject 依赖或项目虚拟环境。PostgreSQL 17.7 解析器接受语法不等同于 PostgreSQL 16 实例通过迁移；下方 2026-10-23 记录单独补上了 16.15 实例执行，其他目标版本仍需分别验证。

解析检查不解析动态 SQL 的实际对象权限，不执行触发器或角色切换，也不验证 JSON 编码、外键数据、策略可见性与运行时函数语义。

### 2026-10-23 真实数据库记录

运行 `scripts/check_postgres.py --config <private work JSON>`，真实 PostgreSQL 16.15 上 **14 项通过、0 跳过**。已检查：

- 21 张表全部启用并强制 RLS；两个实际业务登录各自只读其租户，改变自定义 tenant GUC 不扩大权限。
- 实际审计登录仅见授权租户，无 SUPERUSER、BYPASSRLS 或 owner 成员关系，不能改授权表；业务登录不能直接改 run 成功状态。
- 跨租户/run 复合外键；冻结 Anchor、验收判据和规则版本不可原位替换。
- 事件与 outbox 同事务插入；actor、type、chain、schema_version、submission_key、payload 或 hash 不匹配时拒绝；失败追加不推进链头。
- UPDATE、DELETE、TRUNCATE 已追加事件被触发器拦截，包括测试管理员连接。
- 数据库签发递增 epoch；旧 epoch 不再符合 active/current/未过期的发起资格查询；未验证前置、暂停 run 拒绝 claim。
- 两个真实数据库连接同时认领同任务，只有一个成功，最终仅一条 active claim。
- 预算硬约束拒绝超预留；显式 overrun 状态能记录实际超额；同逻辑工具操作跨 attempt 的唯一性约束生效。

这些测试验证 DDL 和受限读取角色，没有冒充完整 PostgreSQL 命令适配器或真实出口 fencing。涉及合成插入/约束的测试使用独立测试管理员，RLS 可见性和拒绝写入测试使用真实业务/审计登录；两者区分明确。

默认没有 `LOOM_POSTGRES_TEST_CONFIG` 时，14 项 live 测试会明确标为 skipped。显式配置后，缺驱动、文件不存在、连接失败、迁移失败或测试失败均返回失败；已经验证显式错误配置不会转换为 skip。专用脚本遇到任何 skip 或零项测试也返回非零。

## 8. 便携运行时来源与边界

| 项目 | 本次记录 |
|---|---|
| 来源链 | [PostgreSQL Windows 官方下载页](https://www.postgresql.org/download/windows/) 指向 EDB ZIP 二进制 |
| 发布页 | [EDB PostgreSQL binaries](https://www.enterprisedb.com/download-postgresql-binaries) |
| 固定文件 | [postgresql-16.15-1-windows-x64-binaries.zip](https://get.enterprisedb.com/postgresql/postgresql-16.15-1-windows-x64-binaries.zip) |
| 文件大小 | 332,441,502 字节 |
| 本地计算 SHA-256 | `25e6fcdfb8caec38691bf461125e7564508760666f7b8e5dc6a5f0818f58f81e` |
| 二进制实际版本 | `postgres (PostgreSQL) 16.15`，Windows x64 |
| 提取范围 | `pgsql/bin`、`pgsql/lib`、`pgsql/share`，以及 server/命令行工具许可证；未提取 pgAdmin 或 StackBuilder |
| 项目外运行时 | 工作区 `work/loom-infra/postgres-runtime`；未纳入源码或发布包 |
| 测试 cluster | 工作区 `work/loom-infra/postgres-cluster`，初始化默认数据库与合成测试库 `loom_test_s2` |
| 网络/进程 | 前台 exec 托管，绑定 `127.0.0.1:55432`；没有安装系统服务 |
| 客户端 | 项目工作虚拟环境 `psycopg==3.3.6`；本子任务未改 pyproject 或依赖锁文件 |

SHA-256 是本次下载文件的本地计算摘要，用于之后对照完整性；本次没有取得独立发布者签名或单独发布的校验清单，不将本地摘要称为独立认证。下载通过官方指向的 HTTPS 域名完成，许可证保存在工作运行时目录。

测试凭据随机生成，只存于 `work/loom-infra/postgres-session.json`，不打印、不写入仓库。配置含可核对的数据目录和 cluster system identifier，用于避免误连其他数据库。此私有配置只允许 `disposable_cluster=true`、loopback 主机和 `loom_test_*` 库；检查脚本会重建该库的 `loom` schema。

## 9. 重跑与停止

在项目根目录，且便携测试服务仍运行时：

```powershell
$pgTestConfig = (Resolve-Path '../../work/loom-infra/postgres-session.json').Path
& '../../work/loom-venv/Scripts/python.exe' -B scripts/check_postgres.py --config $pgTestConfig
```

这会重置专用合成测试库中的 `loom` schema。不要将生产或用户数据库配置传入该脚本。密码不作为命令参数传递。

若需将 live 测试纳入整套测试，用相同私有路径设置 `LOOM_POSTGRES_TEST_CONFIG`。没有配置时，默认测试只报告跳过数据库集成项，不表示数据库通过。

测试结束后可用以下命令正常关闭该工作区的测试 cluster；再次使用时需以前台方式启动，不要安装服务：

```powershell
& '../../work/loom-infra/postgres-runtime/pgsql/bin/pg_ctl.exe' -D '../../work/loom-infra/postgres-cluster' stop -m fast
```
