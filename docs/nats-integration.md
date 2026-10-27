# NATS JetStream 本地集成记录

记录日期：2026-10-23（Asia/Shanghai）。范围为 Loom S2 的真实单节点消息适配与故障边界检查，不能据此声明集群高可用、生产租户隔离或完整 S2 验收通过。

## 固定版本与运行时来源

| 项目 | 本轮固定值 | 核查来源 |
|---|---|---|
| NATS Server | `v2.15.1`，Windows amd64；官方稳定版，2026-10-09 发布 | [官方 release](https://github.com/nats-io/nats-server/releases/tag/v2.15.1) |
| Python 客户端 | `nats-py==2.16.0`；2026-10-23 核查时官方 latest 仍是该版本 | [官方 release](https://github.com/nats-io/nats.py/releases/tag/v2.16.0) |
| Server 许可证 | Apache-2.0，保留随下载包的 LICENSE | [固定版本原文](https://github.com/nats-io/nats-server/blob/v2.15.1/LICENSE) |
| 下载物 | `nats-server-v2.15.1-windows-amd64.zip`，7,484,670 字节 | [官方资产](https://github.com/nats-io/nats-server/releases/download/v2.15.1/nats-server-v2.15.1-windows-amd64.zip) |
| 官方校验清单 | `SHA256SUMS` | [同一 release 的摘要清单](https://github.com/nats-io/nats-server/releases/download/v2.15.1/SHA256SUMS) |

已把实际下载 ZIP 的 SHA-256 与官方 `SHA256SUMS` 中对应项、GitHub asset digest 逐项比对，结果一致：

```text
ZIP SHA-256: 4f7edfa376750b2720463bd22766ed469a5f13f378a643097866fcd953cbf7e3
EXE SHA-256: 308b0ac72d7079a19f7221bf2326a3c8abfe861be0034fd73d9da444ca3fe055
LICENSE SHA-256: c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4
```

下载与校验脚本位于工作区 `work/loom-infra/nats/prepare_nats.py`，固定版本及摘要后才允许解压，验证 ZIP 路径不得越出预定目录。`runtime-manifest.json` 保存来源、实际摘要、配置位置与许可证位置。没有把 NATS 安装为系统服务，没有把 EXE 加入源码交付目录。本记录说明已验证的发行包摘要，不声称独立完成了发行者签名或所有传递组件的供应链审计。

## 本地运行方式与实际检查

本轮运行时目录：

```text
<工作区>/work/loom-infra/nats/
  prepare_nats.py
  runtime-manifest.json
  SHA256SUMS
  nats-server-v2.15.1-windows-amd64.zip
  nats-server-v2.15.1-windows-amd64/nats-server.exe
  nats-server-v2.15.1-windows-amd64/LICENSE
  loopback.conf
  jetstream-data/
```

启动前已经实际执行：

- `nats-server.exe -v` 返回 `nats-server: v2.15.1`。
- `nats-server.exe -t -c loopback.conf` 返回配置有效。
- 主执行者通过前台受控会话启动真实 broker，测试客户端使用 `nats://127.0.0.1:4223`。

```powershell
$natsRoot = [IO.Path]::GetFullPath('../../work/loom-infra/nats')
& "$natsRoot/nats-server-v2.15.1-windows-amd64/nats-server.exe" -c "$natsRoot/loopback.conf"
```

配置明确绑定客户端 `127.0.0.1:4223`、监控 `127.0.0.1:8223`，JetStream 文件数据放在工作目录，节点资源上限为 64MB 内存与 1GB 文件存储。实例没有配置生产认证，仅用于本机受控测试；不作为对局域网或公网开放的部署方案。前台会话结束时由其拥有者停止进程，不创建自动启动项。

## 已核实的客户端 API 契约

来源同时核对官方发行包中 `nats/js/client.py`、`nats/js/api.py`、`nats/aio/msg.py` 与 [官方 Python API](https://nats-io.github.io/nats.py/modules.html)。本轮使用的参数名称以安装的 `2.16.0` 实现为准。

### 发布与 outbox

```python
receipt = await js.publish(
    target_subject,
    encoded_event,
    stream=owned_stream,
    headers={"Nats-Msg-Id": immutable_event_id},
    timeout=3,
)
```

`stream=` 是预期流名称，客户端将其编码成 `Nats-Expected-Stream`；不存在 `expected_stream=` 这个方法参数。发布要等待 `PubAck`，并核对其 stream 与有效序号，再在本地事务标记 outbox 已发布。网络超时不证明消息没写入，应保留 outbox 待重试。`Nats-Msg-Id` 只在流的去重时间窗内抑制重复存储，不能替代长期 inbox 去重。[官方发布说明](https://docs.nats.io/learn/jetstream/publishing)

### durable pull 与消费确认

本轮采用显式确认的 durable pull consumer。首次创建时明确过滤 subject、`DeliverPolicy.ALL`、`AckPolicy.EXPLICIT`、ack 超时和待确认上限；重新绑定时读取并核对已有持久配置，不因 durable 名称相同就默认兼容。`pull_subscribe()` 自身只要发现 durable 已存在便会绑定，不保证替调用者验证其过滤范围或消费策略。[官方读取流说明](https://docs.nats.io/learn/jetstream/reading-back)

特别注意：`nats-py 2.16.0` 的 `add_consumer()` 是创建或更新。两个调用先后都读到 NotFound 后，第二个创建可能更新第一个已经绑定的 durable，改变过滤范围或 AckWait。对单个调用的创建后检查不能阻止这个竞态，同一 scope 下不同 AckWait 也会破坏已经返回的消费契约。本轮已改为 NATS API 顶层 `action: "create"` 的原子仅创建请求；此操作在配置相同时允许幂等成功，配置不同则返回 `code=400, err_code=10148`，不更新原 durable。[固定版本请求结构及动作](https://github.com/nats-io/nats-server/blob/v2.15.1/server/consumer.go#L170)、[仅创建比较逻辑](https://github.com/nats-io/nats-server/blob/v2.15.1/server/consumer.go#L1123)、[错误码定义](https://github.com/nats-io/nats-server/blob/v2.15.1/server/jetstream_errors_generated.go#L116)

适配器当前通过固定客户端 `2.16.0` 的私有 `js._api_request()` 封装该原始请求，使用其 API 前缀、超时与错误解析；首次创建要求服务端版本至少 `2.15.1`，该版本已在真实 broker 核实。仅把 `10148` 视为并发冲突并重新读取完整配置，其余权限、传输或 API 错误直接失败。校验完成后调用公开的 `pull_subscribe_bind(consumer=durable, stream=stream)`，避免 SDK 再次自动创建或更新。实际 broker durable 名称仍为调用者提供的名称，不额外附加 scope 哈希；同一 stream 中跨 scope 复用该名字被拒绝。客户端升级必须重新验证这段私有接口和仅创建语义。

独立隔离 stream 探针已核实：首次创建与相同配置重试均成功；改 filter 或 AckWait 都抛 `BadRequestError`（`APIError` 子类），`err_code=10148`，原 filter 和 AckWait 保持不变。该机制防止本适配器的并发创建改绑，生产环境仍须限制其他身份直接调用 consumer 更新/删除 API。

消费次序为：严格解析消息 → 校验 subject/租户/run/完整摘要 → 对照本地权威事件 → 在同一数据库事务提交应用效果、inbox 和消费游标 → `await message.ack_sync()`。确认丢失后的重投命中已提交 inbox，不重复应用。`ack_sync` 等待服务端处理确认，不能据此承诺单节点强杀后消费进度永不回退；本轮观察见下节。处理失败保持未确认，按 AckWait 重投。`nak(delay=...)` 可请求重投，`in_progress()` 可延长在途处理；`term()` 会结束重投，因此仅用于已经持久记录的不可恢复拒绝。[确认与重投说明](https://docs.nats.io/learn/jetstream/acknowledgment)

`fetch(batch, timeout=...)` 的无消息超时与实际消费失败应分开处理。业务事务尚未提交时，不能因超时或进程退出预先 ACK。[Pull consumer 说明](https://docs.nats.io/learn/jetstream/pull-consumers)

## 独立复核发现及本轮加固要求

复核 `src/loom/jetstream.py` 和单元/真实 broker 测试后，向主执行者提出以下检查；这些属于 Loom 适配器要求，不能声称 broker 默认配置已经替应用满足。

| 边界 | 必须验证的约束 | 原因 |
|---|---|---|
| subject | 前缀禁止通配符/空 token；租户和 run 编码为不可注入通配符的固定 token；消息归属须再次校验 | subject 分区不能代替消息来源与租户鉴权 |
| 已有 durable | 精确 filter，拒绝非空额外 `filter_subjects`；显式 ACK、pull、预期 pending 上限、ALL 起点、无限重投、无自动过期、无额外 backoff 且 AckWait 与请求一致 | 既有 NEW 起点可能漏历史，有限 `max_deliver` 可让未处理消息不再重投，自动过期可丢失进度，backoff 会改变有效重投时效 |
| 并发 durable 创建 | 使用服务端原子仅创建，冲突后比对配置，再仅绑定；不得用查不存在后 create-or-update | 后到创建者可能在先到订阅已返回后改绑租户 filter 或 ACK 契约 |
| 已有 stream | 受控精确 subjects、文件存储、预期 retention；不得静默继承年龄/条数/字节淘汰策略 | 已 PubAck 的消息仍可能在消费前被丢弃；应拒绝不兼容流，不自动扩大或重用 |
| 发布成功与本地标记 | PubAck 后才标 published；标记前退出允许同 Msg-Id 重发 | broker 成功与本地事务仍非跨系统原子提交 |
| 本地提交与消费 ACK | 本地效果、inbox 和必要游标同事务；提交后再 ACK | ACK 丢失允许重投，但不能重复本地效果 |
| 消息伪造 | 不能只验证重算后的自洽哈希，须与授权 origin log 一致 | 攻击者可修改字段后自行重算摘要 |
| 并发发布顺序 | 不假定两个发布器按数据库 seq 到达；基于权威日志前缀和持久游标校验/追赶 | notification 的到达顺序不能决定业务状态顺序 |
| 毒消息 | 拒绝摘要、原因与次数先持久化，再请求终止重投；拒绝不应生效业务状态 | 防止无限重试与静默丢弃拒绝证据 |

特别说明：NATS 服务端通常禁止同时使用单过滤与多过滤配置。适配器仍显式核对两者，以免把单字段匹配误写成完整过滤契约。流的无静默淘汰约束和 `DiscardNew`、有限容量的拒绝行为应在真实 broker 上验证；不能把丢旧消息的有限流当成可靠长期通知日志。

## 单节点强杀后 ACK 进度回退的实际观察

主执行者增加了真实 NATS 进程强制终止与同目录重启试验。在一次试验中，3 条已经获得 `PubAck` 的消息都被恢复，但消费者先前已经 `ack_sync()` 返回的确认进度，重启后观察到 `ack_floor` 可从 1 回退到 0。因此“ACK 同步返回后 broker 永远不会再次投递”不是本轮可以采用的验收假设。

固定 `v2.15.1` 源码提供了这一窗口的机制依据：单节点非复制路径的 `consumer.updateAcks` 调用 store 的 `UpdateAcks` 后便发送回复；`consumerFileStore.UpdateAcks` 更新状态并触发 flusher，`kickFlusher` 标记 dirty，独立 `flushLoop` 再合并写入消费者状态。该后台循环有约 100ms 的写入节奏，实际写文件还经过可选同步路径。ACK 已被处理与消费者状态已完成同步持久化需要区分。[固定版本 ACK 回复路径](https://github.com/nats-io/nats-server/blob/v2.15.1/server/consumer.go#L3129)、[消费者文件存储实现](https://github.com/nats-io/nats-server/blob/v2.15.1/server/filestore.go#L13697)

官方说明也区分副本数与存储耐久：File 存储并非每次发布都立刻同步到持久介质；消费者的进度和待确认状态也有自己的复制保护。本轮仅 R=1 单节点，没有验证多数副本持久化。源码机制与本轮观察相符，但不能把一次进程强杀归因为已经模拟了操作系统崩溃、物理断电或磁盘损坏。[官方节点故障与存储边界](https://docs.nats.io/learn/jetstream/surviving-node-loss)

相应重启验收应检查：消息可重新读取，允许重复投递；本地持久 inbox/游标继续存在，已完成效果不重复应用，最终三个逻辑事件总共只应用三次。原先要求重启后 broker `ack_floor` 必须立即仍为 1 的断言不对应 Loom 的至少一次传输契约；改为上述业务不变量，并保留观察到的回退现象。

## 本轮测试记录与边界

最终 NATS 专项：**19 项通过，包括 10 项适配器单元测试、8 项真实单节点 broker 测试和 1 项原生 broker 进程强杀/重启试验**。结果由主执行者运行并汇总；不是全部项目测试数量。

| 层级 | 数量 | 覆盖内容 |
|---|---|---|
| 单元 | 10 | 发布失败保留 outbox、错误 stream ACK 拒绝、ACK 丢失后的本地去重、效果失败与 inbox 同回滚、重算哈希的伪造载荷拒绝、跨租户 subject 拒绝、重复 JSON key 拒绝、乱序通知追赶权威前缀且仅生效一次、异步回调拒绝、subject token 不能注入通配符 |
| 真实 broker | 8 | 正常 durable 发布消费、PubAck 后本地标记前退出重发、ACK 丢失后重投去重、跨租户 durable 复用拒绝、已有 NEW/有限重投 consumer 拒绝、已有静默淘汰 stream 拒绝、双 NotFound 后跨 scope 改绑竞态拒绝、双 NotFound 后同 scope 改写 AckWait 竞态拒绝 |
| 原生进程恢复 | 1 | 强杀 NATS 后使用同一数据目录重启；3 条消息恢复，允许 broker 重投，本地持久 inbox/游标保持三个逻辑事件效果恰好三次 |

已有 consumer 的 AckWait 与 backoff 也已加入严格配置比对。新增两项竞态测试让两个初次查询都返回 NotFound，并让第二个创建等待第一个订阅已经返回后才执行；旧实现两项均失败，原子仅创建修复后两项均通过。源码分别为 `tests/test_jetstream.py`、`tests/integration/test_nats_live.py` 与 `tests/integration/test_nats_restart.py`；live 测试必须配置真实 `LOOM_NATS_TEST_URL`，原生进程恢复还需 `LOOM_NATS_SERVER_BIN`，未配置时的跳过不能算通过。原生强杀/重启用例现已纳入 pytest 集成套件；NATS 的 9 项真实基础设施用例是本轮全部 23 项集成检查的一部分，不应在总数之外重复累加。

故障位置中的“发布后本地标记前退出”和“ACK 丢失”分别使用了明确异常/网络确认丢失注入；额外进程恢复试验才实际强杀并重启 broker。不能将它们扩写成已经完成物理断电、磁盘损坏或多节点网络分区实验。

尚不能由本轮结果推出：

- 单节点文件写入的所有物理断电/fsync 耐久承诺、R3 集群失去节点后的高可用。
- 生产 NATS accounts/用户权限、TLS、跨租户连接认证与受限 JetStream API 权限。
- 数据库到 broker 的 exactly-once；本轮使用至少一次传输与持久 inbox。
- 真实模型调用、外部非幂等写、生成代码沙箱或整个 S2 已验收。

本地 broker 的 subject 检查是适配器边界，生产租户隔离还需 accounts 与权限；独立账户不会因 subject 名字相同就共享消息。[官方账户与多租户说明](https://docs.nats.io/learn/security/accounts-and-multitenancy)
