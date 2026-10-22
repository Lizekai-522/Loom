# GitHub 复用选型记录

检索日期：2026-10-22（Asia/Shanghai）。对应 Loom v2.1 修订与首轮确定性内核建设。

## 选型结论

本轮实际复用 **FastAPI 0.159.1 与 Pydantic 2.14.1** 作为 HTTP/API 与数据契约基础，并使用 Uvicorn 提供本地服务；它们已在项目隔离环境中安装。事件信封、幂等回执、事务 outbox、验证放行、预算预留、claim/fencing 和副作用状态由 Loom 实现。没有克隆第三方编排项目后改名为 Loom，也没有把调研的框架写成已经接入的能力。

NATS Python 客户端列为后续队列适配器候选。Temporal、LangGraph 暂缓作为运行时内核：两者可复用的执行基础真实存在，但引入后仍需实现 Loom 的验证与安全契约，并裁决其历史/检查点与 Loom 事件日志之间的权威归属。

SQLite 仅作为本地开发适配器；它的可运行结果不能证明 PostgreSQL RLS、多副本协调或 NATS 投递已经通过生产验证。

## 官方项目对照

| 项目 | 核实版本与许可证 | 可复用部分 | Loom 仍需实现的边界 | 本轮决定 |
|---|---|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | `0.159.1`，MIT | 路由、请求响应模型、依赖注入与 OpenAPI | 租户身份可信绑定、状态迁移授权、事件事务和业务门禁 | 实际依赖 |
| [Pydantic](https://github.com/pydantic/pydantic) | `2.14.1`，MIT | 类型化输入、枚举、字段约束、schema 与序列化 | 字段值是否来自受信执行器、签名有效性、领域结论是否正确 | 实际依赖 |
| [nats.py](https://github.com/nats-io/nats.py) | `2.16.0`，Apache-2.0 | asyncio 客户端、JetStream 发布/消费与确认 | 数据库到队列的可靠桥接、消费幂等、非幂等外部写的发起控制 | 后续适配器，未安装/接入 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | `1.2.16`，MIT | 有状态图执行、检查点、人工介入 | 不可放宽的门禁、双签绑定、完整审计信封、共享成本账本及副作用未知态 | 调研参考，未安装/接入 |
| [Temporal Python SDK](https://github.com/temporalio/sdk-python) | `1.34.0`，MIT | 持久工作流、Activity、事件历史与故障恢复 | 外部动作幂等、Loom 验收语义、预算与权限；需要部署 Temporal 服务 | 调研参考，未安装/接入 |

API 功能依据 [FastAPI 官方功能说明](https://fastapi.tiangolo.com/features/)；数据验证严格模式依据 [Pydantic 官方文档](https://pydantic.dev/docs/validation/latest/concepts/strict_mode/)。Pydantic 默认可能进行类型转换，必须按契约明确使用严格校验及禁止额外字段；类型验证不能被描述为语义可信证明。

NATS Python API 依据 [官方客户端文档](https://nats-io.github.io/nats.py/modules.html)。JetStream 消息确认不与 PostgreSQL 提交自动成为同一事务，Loom 仍需事务 outbox 与持久化去重。[NATS JetStream 官方说明](https://docs.nats.io/concepts/jetstream)

LangGraph 的持久层采用 checkpointer 与 store，应在未来适配时明确其与 Loom 的事件日志关系。[LangGraph 持久化文档](https://docs.langchain.com/oss/python/langgraph/persistence) Temporal 官方把 Activity 幂等性作为应用设计责任；不能据“持久执行”推断外部写天然只发生一次。[Temporal Activity 定义](https://docs.temporal.io/activity-definition)

## 可固定的上游版本

以下 SHA 由官方远端的 `git ls-remote --tags` 核对。带注解 tag 使用解引用后的 commit；本轮没有下载上游源码。

| 项目 | Git tag | commit SHA | 许可证原文 |
|---|---|---|---|
| FastAPI | `0.159.1` | `4bdfbe4513084b19e22421ce1f916d8c1ac462a0` | [MIT](https://github.com/fastapi/fastapi/blob/4bdfbe4513084b19e22421ce1f916d8c1ac462a0/LICENSE) |
| Pydantic | `v2.14.1` | `b2212c787fb5c73386dc770681ec49cd09da8e73` | [MIT](https://github.com/pydantic/pydantic/blob/b2212c787fb5c73386dc770681ec49cd09da8e73/LICENSE) |
| nats.py | `v2.16.0` | `3547a63b2658db4596928668d75fab7c673f6fe6` | [Apache-2.0](https://github.com/nats-io/nats.py/blob/v2.16.0/LICENSE) |
| LangGraph | `1.2.16` | `41c9c22fdc83d68a2859d0c9a2b627c8615a792f` | [官方仓库许可证](https://github.com/langchain-ai/langgraph/blob/main/LICENSE) |
| Temporal Python SDK | `1.34.0` | `a4c2bb38c3bad43498dfa6bd849e8d85bb684e94` | [MIT](https://github.com/temporalio/sdk-python/blob/a4c2bb38c3bad43498dfa6bd849e8d85bb684e94/LICENSE) |

FastAPI 官方 release 为 [0.159.1](https://github.com/fastapi/fastapi/releases/tag/0.159.1)，Pydantic 为 [v2.14.1](https://github.com/pydantic/pydantic/releases/tag/v2.14.1)。LangGraph 为多包仓库，其 GitHub “latest release” 本次指向 CLI 包，不能将 CLI 版本误当 `langgraph` Python 包版本。本表使用核实存在的核心库 tag 与 PyPI 包版本。

精确版本的包元数据另从官方 PyPI JSON 核验：FastAPI 0.159.1 要求 Python ≥3.10、Pydantic ≥2.9.0；Pydantic 2.14.1 要求 Python ≥3.10、pydantic-core==2.50.1。二者元数据相容；运行兼容性以项目测试为准。来源：[FastAPI 包元数据](https://pypi.org/pypi/fastapi/0.159.1/json)、[Pydantic 包元数据](https://pypi.org/pypi/pydantic/2.14.1/json)。

| 包 | 已核验的源码发行包 SHA-256 |
|---|---|
| fastapi 0.159.1 | `b45b1ec57f3aa4d2f80f09bfbcf068b46764a2ee892e56753f35bb3d9d845ce2` |
| pydantic 2.14.1 | `94f478203dd03404682a1ada216965651dd74b1d2d5ffd62e00e0837caab5c26` |
| nats-py 2.16.0 | `1d137ed7afc9b59033b3199324c6237df2016a5091935871344a787eba6b72fc` |
| langgraph 1.2.16 | `e4dd63480bfd3ee14a6ab80ecf1311698780519693d8e3b67a28f3486e69e1f7` |
| temporalio 1.34.0 | `6453cb20e18df485e16578b22c82a9c4bcb1cf7eedd94147dfd373551d80f5b6` |

这些摘要对应 PyPI 元数据中的 sdist，不等同于本机安装 wheel 的摘要。安装与交付应保留项目的实际依赖锁，不能直接把本表充当全平台可重复安装锁。

## 替代成本与复议条件

- **Temporal：** 若后续确需大量长工作流、跨 Worker 恢复与定时信号，可做独立 spike；首先决定 Temporal 历史与 Loom 事件日志的主从关系，并为 Activity 重试逐工具配置幂等/未知态协议。没有完成这个裁决前，同时维护两套权威执行状态会增加一致性负担。
- **LangGraph：** 可在 Worker 内作为执行适配器，向 Loom 提交结构化产物；不能使图节点直接写 `verified`，也不能绕过出口网关和预算账本。只有在适配原型证明减少实现量且不破坏不变量后，再考虑成为默认执行器。
- **nats.py：** 在 outbox 和消费 inbox 契约稳定后引入，验证“提交后发布前退出”“确认丢失”“重复投递”故障矩阵。当前本地投递演示不能写成 NATS 实测通过。

## 复用、归属与交付记录

本轮复用方式为依赖公开发行包，未复制上述项目源码、README 示例或提示词来替代 Loom 核心。MIT/Apache/BSD 等声明按实际已安装包记录；应用自己的许可证尚需项目拥有者选定，不因使用依赖而自动继承某个项目的许可。

已安装依赖及主要传递依赖的版本、官方来源与随包许可证原文见 [第三方声明](third-party-notices.md) 和 [许可证清单](../third_party/licenses/manifest.json)。Temporal、LangGraph、nats.py 的调研记录不意味着这些依赖已经打包。
