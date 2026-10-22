# 本轮验证记录

更新日期：2026-10-23（Asia/Shanghai）。包版本：**0.2.0**。平台：Windows，Python 3.13.12。范围：**S1-local 内核 + S2 基础设施试点；S2 仍进行中，非生产就绪**。

## 分阶段汇总

`scripts/check_s2.py` 正式分阶段执行得到 **112 项单元/本地 + 23 项真实基础设施 = 135 项通过、0 跳过、0 失败**；另有 19 + 14 = 33 个参数子测试通过，不将其混成主用例数。覆盖率采集时也重跑同一套 135 项及 33 个参数子测试。报告的 `real_models_used=false`、`production_ready=false` 保留了实际范围。

| 阶段/专项 | 主用例数 | 结果与范围 |
|---|---|---|
| 单元/本地阶段 | 112 | 策略、SQLite 存储/API/演示及下列新增消息/检查点单元测试；另 19 子测试；0 skip |
| └ NATS 适配器单元 | 10，已含于 112 | 发布失败、确认丢失、事务回滚、伪造载荷、跨租户 subject、乱序及配置边界 |
| └ 检查点单元 | 37，已含于 112 | 真实 Ed25519 签名/验签、密钥信任与时间、快照/前缀/高水位和文件边界 |
| 真实基础设施阶段 | 23 | 下列 PG 14 + NATS live 8 + 强杀重启 1；另 14 子测试；0 skip |
| └ PostgreSQL 16.15 | 14，已含于 23 | 实际迁移、真实业务/审计登录 RLS、复合外键、hash/outbox、冻结判据和 claim 约束 |
| └ NATS live | 8，已含于 23 | 真实 JetStream durable 发布消费、重发去重、租户 durable、不兼容配置及并发创建覆盖拒绝 |
| └ 原生 NATS 进程恢复 | 1，已含于 23 | 测试自建 broker 强杀、同目录重启、消息恢复与本地持久 inbox 防重复效果 |

机器汇总另交付为工作区输出目录的 `Loom_S2_分阶段测试结果_2026-10-23.json`。脚本重跑默认写 `artifacts/s2-validation.json` 与分阶段 JUnit XML；以对应版本和运行日期的报告核对结果。

## 已执行的实际检查

| 检查 | 证据与边界 |
|---|---|
| 强制分阶段执行 | `scripts/check_s2.py` 要求 NATS URL、原生 NATS binary、私有 PG config 三项参数；任一 skip、零用例、缺报告或失败都不能通过 |
| PG 运行时 | EDB 便携 PostgreSQL 16.15，新的 loopback cluster；未安装系统服务、未提权，来源/文件摘要见 postgres-integration.md |
| PG 实际迁移/RLS | 21 张表均 ENABLE/FORCE RLS；两个业务登录只读本租户，审计登录仅有明确授权，无 SUPERUSER/BYPASSRLS/owner |
| PG 数据约束 | 跨 tenant/run 外键、完整信封、append-only、事务 outbox、冻结 Anchor/ruleset、递增 epoch、active claim 唯一与双连接竞争通过 |
| JetStream 运行时 | NATS Server 2.15.1、nats-py 2.16.0；官方发行摘要核对及真实文件存储/durable 测试见 nats-integration.md |
| Broker 强杀/重启 | 3 个逻辑事件恢复并最终只生效 3 次；允许重投，没有假设 ACK floor 不回退 |
| 检查点密码学 | cryptography 50.0.2 实际 Ed25519；独立配置公钥白名单、快照身份/头/数量、过期/撤销、前缀连续性与高水位验证通过 |
| API 0.2.0 HTTP 回归 | 保留原首轮 SQLite 数据库与 token 重启；9 项 HTTP 检查全部通过，单独登记，不重复加入 135 项总数 |
| 演示 | 保留七角色确定性 fixture：7 任务、最大同时派发 2、一次验证失败与 Rebind 修复、62 条事件及成功验收；没有真实模型调用或外部写 |
| CLI 实际联调 | 实际执行 demo；`queue-drain` 先发布 62 条 outbox、再次运行发布 0 条；这是另行 CLI 联调，不加入 pytest 行覆盖率 |
| 本轮行覆盖率 | `docs/coverage-s2.json`：1199/1382 行，86.7583%；policy 92.1875%，store 92.1114%；包括未被 pytest 采集到的 CLI 入口 0% |
| 依赖归属 | 30 个发行包、36 份许可/署名文本归档；运行/集成/测试用途见 third-party-notices.md |

API 的 `/health` 仍报告 S1-local，默认任务内核仍是 SQLite。PostgreSQL 集成测试没有将 API 切到 PG；真实事件检查点验签也没有把 fixture 的产物/模型信任标志变成真实双模型结论。

## 复现入口

在项目根目录安装固定依赖并使用对应 Python 环境。已有首轮服务仍按同一输出目录中的 `Loom_首轮测试指南.md` 启动，沿用原测试数据库和 token。

仅执行单元/本地阶段：

```powershell
python -m pytest tests --ignore=tests/integration -q
```

PG 与 NATS 测试服务运行后，执行本轮两阶段检查：

```powershell
$infra = (Resolve-Path '../../work/loom-infra').Path
python scripts/check_s2.py `
  --nats-url 'nats://127.0.0.1:4223' `
  --nats-server-bin "$infra/nats/nats-server-v2.15.1-windows-amd64/nats-server.exe" `
  --postgres-config "$infra/postgres-session.json" `
  --report 'artifacts/s2-validation.json'
```

PG 检查会重建明确标为可丢弃的 `loom_test_*` 库中的 `loom` schema；不要将生产或用户库配置用于检查。凭据只从私有文件读取，命令参数和报告不输出密码。默认不配置环境时，直接运行全部 pytest 会明确跳过 live 项；这些 skip 不算基础设施通过。显式坏配置已验证为非零失败，不会转换成 skip。

API 保持运行时，另行重跑 9 项 HTTP 检查：

```powershell
$firstRoundSession = (Resolve-Path '../../work/loom-first-round-session.json').Path
python scripts/check_api.py --session $firstRoundSession --report 'artifacts/api-validation.json'
```

`queue-drain` 的实际命令见 README；检查点目前使用受信管理员调用的 Python API，见 [检查点说明](checkpoints.md)。不在 API 启动或普通回放时自动生成密钥、签署任意数据库或批准新公钥。

## 有意义的拒绝与故障路径

- 相同幂等键的并发请求只创建一次；异参数或异操作拒绝，验证证明的全部字段参与摘要。
- 数据库提交后的 outbox 可恢复投递；PubAck 后本地标记前失败可重发，ACK 丢失后的消息只产生一次本地效果。
- 重算自洽 hash 的伪造消息不能替代本地权威事件；subject/durable 跨租户复用、重复 JSON key、乱序与不兼容保留策略被拒。
- durable 并发创建不能静默更新已有配置；跨租户争用同名 durable、同 scope 使用不同 AckWait 的真实回归均已验证拒绝不兼容覆盖。
- 单节点强杀后允许 broker ACK 进度回退；持久 inbox/游标阻止重复业务效果。这不是物理断电、磁盘损坏或 R3 高可用试验。
- 业务满载仍保留验证容量，过期/替换/失效的 claim 不能提交完成或取得新的本地副作用发起权。
- 并发预算预留不越过本地账本；实际超预留被真实记录并冻结 run，其他有效 claim 不能继续新发动作。
- 未知副作用阻止产物提交与自动重发；逻辑 ID 不因字符串分隔符碰撞，也不因更换 attempt 产生第二逻辑操作。
- 图失效传播下游 stale 并撤销旧 epoch，已发生效果仅记录补偿需求，不能冒充已撤销。
- PG 使用真实受限业务/审计登录测试可见性；更改 tenant GUC 不扩大权限，默认运行角色不能直接修改成功状态。
- PG 对事件元数据/载荷/hash 不一致和非法链头追加拒绝，失败追加不推进 head；UPDATE/DELETE/TRUNCATE 受阻。
- 独立公钥验签拒绝错误/未知/撤销密钥、过期/未来签名、跨租户/run 复制、截断和整链改写；保留的高水位拒绝旧历史与分叉前缀。
- Worker 自报 `verified=true` 仍只进入 submitted；非成功双签、内容/规则/证据错版、重复验证者或缺少独立模型不能通过本地验证策略。
- 验收失败时人工接受只能成为例外，停滞不计成功；API 跨租户 ID、未注册判据、非严格预算和非有限数值被拒。

## 仍未计为通过

1. **完整 PostgreSQL Store/生产写过程。** 目前 PG 检查验证 DDL 与受限读取角色，部分合成约束数据由测试管理员写入；不能冒充真实应用身份下所有命令已经实现。
2. **多主机与生产消息部署。** 未完成 PG/全局容量预算的完整调度协议、NATS accounts/TLS/R3、网络分区及物理断电耐久。
3. **独立远端审计信任。** 当前真实密码学检查点仍由本机密钥/文件支撑；目录分离不是 ACL，同一账号能改 DB、私钥、公钥白名单和高水位时不抗整体回滚。尚无 WORM/HSM/远端不可回滚锚定。
4. **真实模型/业务效果。** 没有调用真实模型或验证真实模型双签，没有 held-out 收益对照，演示成本与身份均为 fixture。
5. **真实外写、沙箱与灾备。** 未执行真实非幂等外部写，没有生成代码沙箱、生产出口/任务凭据隔离、RPO/PITR 恢复冻结及补偿演练。
6. **覆盖率不能证明的性质。** 本轮确实重跑全部 135 项及 33 子测试并采集 1199/1382 行（86.7583%），但这不证明并发、物理耐久或生产安全。CLI 入口在该 pytest 采集范围内仍为 0%，实际 demo/queue-drain 联调另计，不能混入其覆盖数据。`coverage.json` 的 88.88% 是 2026-10-22 首轮历史；本轮使用独立的 `coverage-s2.json`，分母和模块范围不同。

PostgreSQL 便携二进制、NATS Server、私有配置和测试数据只在工作目录，未作为源码分发。原始演示结果和本轮分阶段报告分别交付；重跑产生新的 run ID、时间戳和摘要，不要求随机字段字节一致。
