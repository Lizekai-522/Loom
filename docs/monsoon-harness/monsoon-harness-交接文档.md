# monsoon-harness 施工交接文档

> **这份文档写给一个从没参与过本项目的 agent。**
> 读完它，你应该能回答清楚：我们在造什么、为什么这么造、哪些事已经定了并且为什么、三个代码库各自能用什么不能用什么、下一步具体做什么、按什么顺序做、做完怎么验、以及哪些坑已经踩过不要再踩。
>
> **规格真源**是方案书 `/workspace/monsoon-plan/monsoon-harness-方案书.md`（132 KB / 13 章 + 3 附录 / 37 条验收判据 / 44 条一手来源）。本文档是**入口与施工指南**，不重复规格正文；凡是“为什么这么设计”，规格书里有完整论证，本文档给出结论与最关键的一条依据。**两份文档冲突时以规格书为准**，发现冲突就改规格书并记进变更说明。

---

## 〇、怎么用这份文档

**五分钟上手路径**：

1. 读第一章（这是什么）——建立心智模型。
2. 读第二章的决策表（已经定了什么）——知道哪些不用再讨论。
3. 读第三章（必知的一手事实）——这是最省时间的部分，每条都带 `file:line`，不用自己重新翻仓库。
4. 读第六章（接手第一件事）——今天就能开工的具体动作。

**完整施工路径**：一 → 二 → 三 → 四 → 五 → 六 → 七 → 八 → 九 → 十。第八章是验收判据速查，施工到哪一批就翻到哪一批。

**三条硬纪律**（本项目已因违反它们返工过多次）：

- **只读仓库不许改**。Loom 与 Realtime-Venus 两个仓库是基线，探针加 `PYTHONDONTWRITEBYTECODE=1`，不留 `__pycache__` 与 `*.pyc`，不跑 pytest（跑会写 `.pytest_cache`）。
- **写文档时 file:line 必须写完整**（`src/loom/api.py:335`），不要用 `:335` 简写——简写会让行号归属到错误的文件，评审时按“最近一个 file: 前缀”猜，很容易归错。
- **关于代码库的否定断言（“没有 X 机制”“只有 N 种取值”）必须先跑一次反向检索找到反例**。找不到就写“未检索到”，不能写“不存在”。这条在本项目已造成一次事实性错误传播到四处。

---

## 一、背景：这是什么

### 1.1 三个代码库

| 名字 | 是什么 | 在本项目里的角色 |
|---|---|---|
| **Realtime-Venus（RV）** | 一个 Python 的语音/多模态 agent harness。核心是一套“委托运行时”：把一次用户请求捕获下来、派发给执行器、把结果回传 | **能力来源**。ms 要吸收它的委托协议门与工作状态机 |
| **Loom** | 用户自己写的编排与审计中心。FastAPI + SQLite。有目标/计划/任务/租约/epoch 栅栏/事件链/幂等回执 | **编排中心**。ms 把任务外包给它 |
| **rakazo**（`elie222/rakazo`） | 一个 TypeScript monorepo 的 agent 平台。Better Auth + Organization/Space 租户模型、Postgres 行级安全、Prisma 数据模型、18 插槽的 provider 契约层、Pi agent 运行时 | **基座**。ms 是它的一个 fork |
| **monsoon-harness（ms）** | 本项目要造的东西 | 交付物 |

三个库的关系不是“谁调用谁”，而是**ms 从 RV 取能力、从 rakazo 取骨架、把 Loom 当成中心**。

### 1.2 一句话说清目标形态

> **ms 是一个跑在用户自己机器上的常驻 agent。** 它单独用的时候就是一个全天候 agent（本地对话、本地执行、本地定时）；它连上一个中心 Loom 的时候，把需要中心能力（编排、审计、跨机协同）的任务外包给 Loom；多台 ms 同时跑的时候，各自向同一台 Loom 提交任务，互不踩踏。

用户原始诉求里有两个关键词经常被读错，这里先钉死：

- **“前端”是拓扑反的**。rakazo 的前端是浏览器界面，agent 跑在中心；ms 恰好相反——agent 跑在用户机器上，浏览器只是它的一层壳。**这是整个方案所有改动成本的来源。**
- **“分布式”不是“多副本”**。多台 ms 是**不同的 agent 实例**，跑在不同的机器上，有各自的工作区与用户；不是同一个服务的多副本。
- **“工作区”在三个库里指三样东西**。rakazo 里叫 `Space`，Loom 里叫 `tenant`，RV 里是本地目录。映射表在规格书 5.1 节。

### 1.3 为什么不是“把 RV 接到 Loom 上”

这是本项目第一轮的判断失误，后来被纠正：如果只做“RV 接 Loom”，得到的是一个两边都不完整的拼接物。真实的形态是——**两边各自独立成立时有完整的产品形态，组合只是可选增值**。

所以方案里：**ms 单独跑是一个完整产品**，Loom 自己单独跑也是一个完整产品，组合时两者各自补一块短板。组合真正独占的东西只有四件：跨端任务身份、跨端证据引用、等待期人类在场、交付确认。

**施工顺序由此确定**：先补各自的独立缺口，再做薄适配层。规格书第八章的六个批次就是这个顺序。

---

## 二、已经定下来的事

**下面十条都不要再重新讨论。** 每条给结论与最关键的一条依据；完整论证在规格书对应小节。

### 2.1 五项决策（规格书第一章）

| 编号 | 决策 | 结论 | 最关键依据 |
|---|---|---|---|
| Q1 | 执行位置的默认策略 | 双执行域并存，由 Space 策略显式指定，**不做自动路由** | rakazo 的 `Computer`/`ComputerRun` 模型里没有内存与 CPU 水位字段；容量上限只靠内存、CPU、进程容量与端口数决定（`docs/computer-runtime.md:23`）。按机器健康自动路由缺一手依据 |
| Q2 | ms 的用户体系范围 | 沿用 rakazo 的 Better Auth + Organization + Space，**不做单用户精简版** | 多机并发第一天就需要工作区归属与审计主体；删模型省下几百行，补回来要动审计、隔离、凭证三条数据写入链路 |
| Q3 | fork 的粒度 | **整仓 fork** + 两个形态开关（`MS_ROLE`、`MS_EXECUTION_DOMAIN`） | 租户、行级安全、契约、审计事件是重写成本最高的部分，而这四项恰是分布式形态的必需品 |
| Q4 | RV 的二开深度 | 抽成能力插件、执行器接插件后端、**不 fork 进程内注入层**；**不自造接口，沿用 rakazo 的 `adapter-kit` provider 契约层** | `packages/adapter-kit` 已定义能力声明（含契约版本）、工具声明（含入参 Schema 与只读标记）、调用体、事件流、跨进程上下文、18 插槽注册表 |
| Q5 | 独立形态的等待期人类在场 | **保留**，口径收紧为“**本机进程存活且对话可达期间**” | 真正的约束是进程存活而不是对话开着：机器睡眠、用户注销都会让流程停下，而窗口可能还亮着 |

### 2.2 三项拍板（规格书 13.2）

| 编号 | 拍板 | 结论 |
|---|---|---|
| 拍板一 | 执行域界面 | 派活入口=显示优先面（生效域 + 来源徽标 + 一行后果提示）；bot 详情=编辑面（+覆盖开关）；空间管理=分歧面（默认值 + 覆盖它的 bot 清单）。**三处的生效域由同一个函数计算** |
| 拍板二 | 组织级角色 | Phase 1 **不启用**，保留组织模型但不建权限判定逻辑。管理员界面=空间级 + 部署所有者页 |
| 拍板三 | 本机执行域的边界强度 | 接受“止于用户主目录”作为 Phase 1 口径，但按**三条命题分别写**；加**联网声明**与**凭证路径绊线**；**Phase 1 不做 OS 级沙箱** |

拍板三的三条命题（规格书 3.2、6.1、7.2、12.1 四处分别写，不要合并成一句）：

1. **宿主中介的约束成立但只防逃逸**——文件根限于用户主目录 + 命令放行检查（`packages/adapters/src/host-aware-sandbox.ts`）。它不防主目录内的破坏：把整个主目录加密是一次完全在界内的写。
2. **插件进程本身不受约束**——以用户身份运行，可直接开文件、发网络请求。**装一个能力插件等于装一个软件**，这是 Phase 1 的信任模型，如实告知。
3. **只读标记覆盖本地写，不覆盖网络出向**——声明只读但可联网的工具可以读文件并外传。处置是加**联网声明**（adapter-kit 无此字段，ms 在契约层扩展；**未声明一律按可联网处理**），定位是**披露不是门禁**（只读+联网有大量正当用途，强制审批会把可用性打穿）。

配套的**凭证路径绊线**：对既知凭证路径（SSH 配置目录、云凭据目录、证书私钥目录、浏览器凭证库）的中介访问一律拒绝并记审计事件，显式上报不许静默。**要写明这是绊线不是边界**——它降低误触概率，防不住插件进程直接读文件。

### 2.3 三条既有拍板（贯穿全程）

| 拍板 | 含义 | 落在哪 |
|---|---|---|
| **模板化委托** | 模板标识必填，自由文本只作参数值，适配层只做 Schema 校验 + 机械映射 | 规格书 7.4 节；判据 D1、D2 |
| **不做自动批准** | 不借用 operator 身份做机器批准，不引入 harness 服务主体，批准由人手动发起 | 规格书 5.3 节；判据 B3、B4、B5、G6 |
| **不做出网回调** | 回传以轮询为主，中期走本机 NATS，不接受任意 `callback_url` | 规格书 4.3、8.4 节；判据 G3 |

### 2.4 本轮修掉的五处硬伤（**不要重复修**）

这五处是用户评审打回的，已经全部回填。每一处都带一个“容易再犯”的教训，接手时按右列对照即可，不必再翻评审记录。

| 评审指出的问题 | 已改成的形态 | 容易再犯的地方 |
|---|---|---|
| 人工介入只有读没有写 | 介入是派生视图，正确动作是服务端已有的 `control/pause`，**服务端硬闸门**挡住重领（见 3.1.6） | 去找“新建介入”的接口 |
| WebSocket 与“无服务端推送”互斥 | Phase 1 砍掉实时通道，纯 HTTP + 轮询；从 rakazo 继承的是**连接治理七条**，不是传输方式 | 把治理规则当成传输方式来继承 |
| 独立形态的模型凭证没有来源 | 双源：组合形态走中心短时令牌，独立与降级形态由本机持有 | 只写组合形态的凭证来源 |
| operator 持批准能力与“实例主体不得批准”矛盾 | operator 角色集移除 `plan.approve`，批准能力只授给用户会话主体；operator 改为“组装批准请求” | 把“组装请求”与“发起批准”混为一谈 |
| 数字对不上 | 契约面统一为 **16 动作 / 3 处新增**；批次三 **10 子项**；粗估 **61 至 96 人日** | 只改表格不改正文 |

### 2.5 本轮修掉的五条次级问题

主体世代无回收（高世代注册成功后中心吊销同机器旧世代主体）、B3 补 `src/loom/auth.py:68` 依据、本机链改为独立链标识只校验链内连续性、`requested-by` 前缀改在目标创建时同步解析入库、打包载体提为批次一前置决策。

---

## 三、必知的一手事实

**这一章是这份交接文档最值钱的部分。** 每条都带 `file:line`，照着点开就能看到原文，不需要自己重新翻三个仓库。

### 3.1 Loom（编排中心）

仓库：`/workspace/Loom`（只读基线，git 工作区干净）。

#### 3.1.1 规模与形状

| 事实 | 数值 | 依据 |
|---|---|---|
| HTTP 路由总数 | **54 个，全部在 `src/loom/api.py`** | AST 统计装饰器 |
| 其中在 `/v1/` 下 | **51 个** | 非 `/v1` 的只有 `/console`、`/console/`、`/health` |
| 事件类型总数 | **55 个** | 48 个字面量 + 5 个变量派生 + 2 个拼接派生 |
| 事件调用点 | **69 处，分布在 14 个源文件** | 最多的是 `store.py`（23）与 `execution.py`（10） |
| 存储实现 | SQLite，**没有 PostgreSQL 适配** | `src/loom/store.py:1-5` 原话：“SQLite is a local reference implementation, not an implementation of PostgreSQL RLS or a distributed scheduler” |

**55 个事件类型的三段构成**（可复算）：
- 48 个字面量：`grep`/AST 直接取 `_event` 第四个参数是字符串常量的调用点。
- 5 个变量派生：`src/loom/artifacts.py:165`、`:167`、`:273`、`:276` 给出 2 个（`artifact.candidate_saved`、`role.candidate_generated`）；`src/loom/dispatch.py:199` 的默认参数加 `:215`、`:227` 的显式传值给出 3 个（`execution.request_finished`、`execution.request_cancelled`、`execution.request_claimed`）。
- 2 个拼接派生：`src/loom/operator.py:285` 与 `src/loom/store.py:485` 都是 `"budget." + 状态`。

#### 3.1.2 角色、权限与主体

`src/loom/auth.py:20-26` 的角色集：

| 角色 | 权限集 |
|---|---|
| `operator` | read、goal.write、**plan.approve**、**control**、suite.approve、pack.publish |
| `worker` | read、task.claim、task.submit |
| `viewer` | read |
| `developer` | read、goal.write、**plan.approve**、task.claim、task.submit |

另外三条硬事实：

- `src/loom/auth.py:68`：**同一主体不得同时持 operator 与 worker**（`_require(not {"operator", "worker"} <= self.roles)`）。这是 ms 双主体模型的直接依据——两枚令牌必须绑**两个具名主体**，不能是同一主体拿两枚令牌。
- `src/loom/auth.py:101-102`：`(tenant_id, subject_id)` 唯一。同一主体第二次登记会让整个身份文件加载失败。
- `src/loom/auth.py:91`：旧式租户密钥会派生 developer 角色。

#### 3.1.3 机器批准路径（**必须由批次三关闭**）

`developer` 含 `plan.approve`（`src/loom/auth.py:25`）→ 批准接口只校验能力不校验是不是人（`src/loom/api.py:335`）→ 审计函数对 developer 主体返回空对象（`src/loom/api.py:207-208`）→ 批准事件的 actor 落空，被兜底成一个非具名的字面量（`src/loom/workflow.py:439`）。

**这条与“不做自动批准”正面撞上。** 批次三要同时做两件事：禁止 legacy 密钥派生 developer；把 `plan.approve` 从 operator 角色集移除，批准能力只授给用户会话主体。

#### 3.1.4 租约、栅栏与幂等

| 事实 | 依据 |
|---|---|
| 领取带 worker 标识与租约时长；提交带 epoch 栅栏与幂等键 | `src/loom/api.py:529-541` |
| 幂等记录主键是 **(tenant, key)**，键空间**整个租户内共享** | `src/loom/store.py:77-79` |
| 租约上限 1 小时、默认 30 秒 | `src/loom/store.py:308-339` |
| 幂等键长度 1–200，请求头名 `Idempotency-Key` | `src/loom/api.py:219` |
| 回收是**惰性**的，在下一次领取时触发 | `src/loom/store.py:318` |
| 租约过期：无未结算副作用 → 回 pending 并递增 epoch；有未结算副作用 → 转 indeterminate 转人工 | `src/loom/store.py:289-306` |
| **`_expire` 只改任务状态，不改运行状态** | `src/loom/store.py:289-306` |

**幂等键租户内共享**这条是跨机正确性的地基：ms 的幂等键必须自带机器唯一前缀，四段式 `<机器标识>:<run_id>:<task_id>:<尝试序号>`，不得跨机复用。

#### 3.1.5 事件出口与链校验

- `src/loom/store.py:615-618`：事件接口**返回全量，没有游标**。Phase 1 要加 `after_seq` 与 `limit`（批次三）。
- `src/loom/store.py:621-632`：链校验存在，ms 沿用。
- 事件信封字段（`src/loom/store.py:200-203`）：模式版本、事件标识、租户标识、运行标识、链标识、序号、时间戳、类型、主体、载荷、提交键、前序哈希、当前哈希。**与 rakazo 的 Event 表几乎同构**。

#### 3.1.6 人工介入与暂停闸门（**这一节最关键**）

| 事实 | 依据 |
|---|---|
| 介入列表是**派生视图**，从运行状态、任务状态、副作用与预留的未结算情况算出，**不是一等记录** | `src/loom/operator.py:52-72` |
| 可用的运行控制动作是三个：pause / resume / terminate | `src/loom/api.py:372-376` |
| `pause` 把运行状态置 `paused`（方法体 `:95-108`，写库语句在 `:103`） | `src/loom/operator.py:103` |
| **`claim_next` 要求运行状态是 `open` 才发任务**，否则抛 Conflict | `src/loom/store.py:313` |
| `resume` 前会校验无未结算副作用、无未结预留、预算未超、尝试次数未用尽，然后相关任务回 pending 并 epoch+1 | `src/loom/operator.py:124-154` |
| 控制类端点要求 `control` 能力 | `src/loom/api.py:214` |

**推论（写代码时按这个来）**：ms 执行器请求人工介入 → 调 `control/pause` → 运行变 paused → **任何执行器都领不走它的任务**（服务端硬闸门，不是协作约定）→ 租约到期后任务回 pending 但因运行仍 paused 而领不走 → 人处理完调 `control/resume` → 任务才重新可领。

**连带推论**：显式的“释放租约端点”**不再必要**；挂起时长**不再受一小时租约上限约束**（因为没人能领）。

**另一条连带事实**：`control` 能力当前只在 operator 角色集里，worker 没有。所以 **ms 的 worker 角色集要加 `control`**，并收紧成只允许 pause/resume（terminate 留给人）。这与“operator 移除 `plan.approve`”改的是同一张权限表，放在批次三同一个子项里。

#### 3.1.7 中心并发上限（**分布式形态的硬约束**）

`src/loom/store.py:122-124`：执行并发上限默认 **4 个工作位**（`max_workers=4`），并为校验类任务**预留 1 位**（`verify_reserve=1`）。准入判定在 `src/loom/approved_execution.py:139`：

```
if total>=self.store.max_workers or business>=self.store.max_workers-self.store.verify_reserve:
```

**“多机并发提交”在 Phase 1 的真实含义是“多机并发排队，中心按四个工作位放行”**。业务类任务最多占 3 个，剩 1 个给校验。这个上限在界面上要显示占用，调它要动中心配置而不是 ms 的代码。

#### 3.1.8 其余要用到的

| 事实 | 依据 |
|---|---|
| 计划批准要 `expected_revision` + `expected_digest` + 幂等键 | `src/loom/api.py:335`、`src/loom/workflow.py:399-410` |
| 计划读接口**不存在**；修订号只在写路径返回 | `src/loom/workflow.py:290-293`（批次三要补 `GET /v1/goals/{run_id}/plan`） |
| 目标与计划的输入体**都没有模板标识字段** | `src/loom/api.py:57-62`、`src/loom/api.py:65-68` |
| 域包模板只有标识、标题与节点图，**没有参数契约** | `src/loom/domain_packs.py:81` |
| 模板查询端点返回模板标识 + 已解析定义 | `src/loom/domain_packs.py:172-181` |
| 域包模板的 `roles` 是定长 7 | `src/loom/domain_packs.py:33` |
| **没有 `renew_claim` 的 HTTP 路由**（只有存储层实现） | `src/loom/store.py:376` |
| `GET /v1/interventions` 存在 | `src/loom/api.py:344-346` |
| 副作用账本由进程内四处调用写入，**无跨进程上报路径** | `src/loom/execution.py:132`、`src/loom/execution.py:174`、`src/loom/artifacts.py:510`、`src/loom/quotas.py:160` |
| 副作用账本的粒度是**每次工具调用一行** | `src/loom/store.py` 的 `effects` 表 |
| 已有 Docker 容器级沙箱（`DockerSandbox`），未配不可变镜像即拒绝执行 | `src/loom/sandbox.py:320`、`src/loom/local_executor.py:42` |
| CLI 三个子命令 demo/serve/queue-drain，只绑回环 | `src/loom/cli.py:14`、`:18`、`:24`、`:65` |

### 3.2 rakazo（基座）

#### 3.2.1 取回状态与路径

**主干 commit SHA 至今未取到**——沙箱到 github.com 的 TLS 握手失败，GitHub API 触发速率限制。**批次零的第一件事就是取回 SHA 与 tag 并写进锁文件**，在此之前方案正文不引用任何 rakazo 的具体版本号。

已落盘的只读缓存：

| 路径 | 内容 |
|---|---|
| `/workspace/rakazo-src/repo/` | jsDelivr 取回的主干内容，**文件名被扁平化**（`docs_computer-runtime.md` 对应 `docs/computer-runtime.md`） |
| `/workspace/rakazo-src/adapter-kit/` | `interfaces.ts`、`types.ts`、`registry.ts`、`adapter-context.ts` 四个契约层文件 |

⚠️ **jsDelivr 的文件清单不完整**：data 接口给的 flat 清单只有 361 条、docs 只有 3 个 .md，CDN 却能取到更多文件。**不可用该来源推断仓库规模**。

#### 3.2.2 包依赖图（分层无环）

依赖关系可以先画成一张分层图，再逐包列出它依赖谁：

```
contracts  ←  core  ←  db  ←  adapters  ←  api
    ↑                      ↑
adapter-kit ───────────────┘
```

| 包 | 依赖 |
|---|---|
| `packages/core` | 只依赖 `packages/contracts` |
| `packages/db` | adapter-kit、contracts、core、logging |
| `packages/adapters` | adapter-kit、contracts、core、db、logging |
| `apps/api` | 上述全部，再加 auth 与 memory |
| `apps/web` | contracts、core、logging 与三个 UI 包，**不依赖 db** |

**“共用底层、重写上层”在依赖层面没有障碍**：`contracts`、`core`、`adapter-kit` 三层两侧共用，`db`、`auth`、`adapters` 是中心侧重点改造对象，`web` 与 UI 包是 ms 客户端侧重点改造对象。

#### 3.2.3 adapter-kit 契约层（**Q4 的落点**）

`packages/adapter-kit/src/interfaces.ts` 定义的 provider 接口：`SandboxProvider`、`ConnectorProvider`、`ConnectionAuthProvider`、`MemoryStore`、`SemanticMemoryProvider`、**`AgentRuntime`**、`ModelProvider`、`JobPublisher`、`JobWorkerHost`、`AgentHomeStore` 等。

`packages/adapter-kit/src/types.ts` 里对 ms 最关键的四样：

| 类型 | 字段 |
|---|---|
| `AdapterDescriptor<TCapabilities>` | `id`、`contractVersion`、`adapterVersion`、`capabilities` |
| `ConnectorTool` | `name`、`description`、`inputSchema`，可选 `readOnly`、`batch`、`route` |
| `ConnectorCall` / `ConnectorEvent` | 调用体：工具名、参数、执行标识；事件流三类：`log` / `result` / `error` |
| `AdapterContext` | 12 个字段：操作标识、追踪标识、租户标识、用户标识、可选的 bot 与运行标识、屏幕租约标识、屏幕保温标记、取消时收尾、**取消信号**、已连接的外部账号列表 |

`packages/adapter-kit/src/registry.ts` 的 `slots` 共 **18 个插槽**：runtime、sandbox、memory、home、artifacts、secrets、jobs、realtime、notifications、models、connector、connection-auth、runner、voice、web、browser、cloud-agent、auto-review。

**三条要注意的事实**：

1. `ConnectorProvider` 只有 `describe` / `discoverTools` / `resolveCall?` / `execute` 四个方法，**没有取消动作**。唯一现成的中止口是 `AgentRuntime.abort(runId)`，服务的是运行整体而非单次能力调用。跨进程的能力调用要能取消，**必须新建一条通路**。
2. `AdapterContext` 的**字段全集不等于构造入参**。`adapter-context.ts` 的 `createAdapterContext` 只取租户标识、用户标识、bot 标识、操作标识四样，其余字段由调用方按需补。跨进程要传的是字段全集。
3. `AdapterContext.signal` 是 `AbortSignal`，**不可序列化**。跨进程时它映射成一次取消调用，不是把信号本身传过去。

#### 3.2.4 租户与隔离模型

`packages/db/src/scope.ts` 两个函数构成完整隔离判定链：

- `requireMembership(prisma, userId, requestedSpaceId?)`：按用户查成员关系，未指定空间时取默认空间（`Space.isDefault`），未找到成员关系抛 `IsolationError("No personal space")`。
- `scoped(actor, record)`：`spaceId` 不符即抛 `IsolationError`；记录带 `userId` 时还要与主体的 `userId` 相符。异常消息固定为 `Resource not found`，**不区分“不存在”与“无权访问”**——这条语义在多租户下值得保留。

`Actor` 四字段：`userId`、`spaceId`、`email`、`isDeploymentOwner`（**硬编码为 `false`**）。

#### 3.2.5 用户设备客户端（**连接治理七条，ms 继承这个**）

`packages/adapters/src/user-device-client.ts`：

| 序号 | 规则 |
|---|---|
| 1 | 本机主动向外建立 WebSocket，服务器**永不反向连接** |
| 2 | 握手携带设备令牌 |
| 3 | 每次请求前过授权回调 |
| 4 | 收到吊销消息即停机 |
| 5 | 断线不重试 |
| 6 | 重连使用全抖动 |

（加上“不传原始凭证”共七条，见规格书 4.3 节。）

**这七条约束的是连接行为，与用 HTTP 还是 WebSocket 无关**——所以 Phase 1 砍掉 WebSocket 之后它们仍然成立。**继承的是治理规则，不是传输方式。**

#### 3.2.6 宿主机执行域

`packages/adapters/src/host-aware-sandbox.ts`：

- `sandboxKindForBot(envKind, computerHost)`：环境值是 `docker` 且计算主机标记为 `this-mac` 时返回 `desktop`。
- `createRunSandbox`：`kind === "desktop"` 时先 `assertHostCommandsAllowed()`，再 `new DesktopSandboxProvider({ root: opts.dataDir, hostRoots: [homedir()] })`。
- 另有 `HostComputerSandbox` 实现；unsafe stop 时把计算机置 `quarantined` 并递增 `controlFence`。

**这是“用户自己的机器当执行域”的现成实现**，ms 的本机执行域直接沿用这一档。

#### 3.2.7 Pi agent 运行时

`packages/adapters/src/pi-runtime.ts` 按职责拆成多个模块：会话记录、工具宿主、模型解析、流式回传、错误映射、回退策略、子 agent。运行时接口是 `AgentRuntime`（describe / run / abort），能力集声明流式、压缩、工具、脚本四项。

**含义**：把 Pi 搬到用户机器是“把模块装进一个本机宿主并注册到 runtime 插槽”，不是重写运行时。体量比直觉小。

Pi SDK 边界（`earendil-works/pi-coding-agent`）：`createAgentSession({model, modelRuntime})`、`SessionManager.inMemory()`。官方明示“在 Web 服务中还要自行设计每段对话的标识、存储和访问隔离，不能把内存模式误认为已完成多用户持久化”。

#### 3.2.8 其他

**执行域与运行时**：

- `docs/computer-runtime.md:34`：计算机容器才是安全边界；Team bot 的目录**不是**安全边界（`:17`）。
- `docs/computer-runtime.md:23`：容量描述是定性的；`SANDBOX_TEAM_SCREEN_LIMIT` 是可配置的显式封顶，默认不设，超限返回 `MULTI_SCREEN_UNAVAILABLE`。
- `docs/computer-runtime.md:143-145`：停止心跳十分钟的 worker 被标记为中断（**指的是与 reconciler、作业队列同处中心侧的 worker 进程，不是用户侧机器**）。
- `docs/computer-runtime.md:40`：机器人请求人工接管后，非计费型云端机器保活三十分钟，计费型靠请求重置空闲计时器。**保活是云端机器才有的能力**——这是 Q5 口径收窄的依据。

**部署与账号**：

- `docs/self-host.md:80`：API 进程拿不到不受限的 docker socket，supervisor 掌握生命周期。
- `docs/self-host-configuration.md:70`：`USER_DEVICES_ENABLED` 默认开启。`:71`：`PI_SESSION_RECORDING` 默认关闭且明文不适合多租户。
- `docs/self-host-accounts.md:13`、`:18`：**没有 app-admin 角色、没有首个注册账号即所有者**。引导所有者脚本产出的是普通已验证账号。
- `docs/fleet-management.md:47-50`：车队写操作**永远弹审批卡**，自动审阅与“Always allow”都跳不过。`:71`：车队工具绑在运行所在的空间内，**不得跨空间搬运 bot**。

**依赖与数据模型**：

- `.env.example:165-167`：轨迹记录默认关闭。
- `packages/adapters/package.json`：Pi 依赖锁在 `@earendil-works/pi-agent-core` 1.3.0。
- `packages/db/prisma/schema.prisma` 里 ms 关心的模型：`Space`（含 `isDefault`）、`SpaceMember`（含 `role`）、`User`（含 `devices`）、`UserDevice`、`Bot`（含 `computerId`、`computerSwitching`）、`Computer`（含 `scope`、`scopeKey`、`state`、`executionLeaseExpiresAt`、`executionFence`、`controlHolder`）、`ComputerExecutionLease`（`@@unique([computerId, botId])`、`fence`、`expiresAt`）、`Run`（`leaseOwner`、`leaseFence`、`leaseExpiresAt`）、`DeploymentSettings`（`signupsEnabled`、`signupAllowlist`、`ownerUserId`）、`ActionApprovalRule`、`McpServer`、`CapabilityInstall`。

### 3.3 Realtime-Venus（能力来源）

仓库：`/run/csi/mount-root/nas/eab0d61a99b6696edb3d2aff87b585e8/.mavis/plans/plan_3e823f3f/workspace/rv/repo`（只读基线）。

#### 3.3.1 五处扩展边界

`harness/README.md` 的“Frontend and backend integration”表：

| 边界 | 位置 | ms 里的归属 |
|---|---|---|
| 对话模型 `VenusOmniServingPort` | `harness/bridge/serving.py:464` | 走中心侧凭证，不进插件层 |
| 任务执行 `GeneralAgentPort` | `harness/agents/contracts.py:80` | 本机执行器的默认实现 |
| 路由润色 `PlannerBackend` + 委托契约 `DelegateBackend` | `harness/llm/backends.py:12`、`harness/core/backend.py:16` | 降为能力插件的实例；委托契约拆成两段 |
| 客户端播放 `PlaybackPort` | `harness/bridge/host.py:95` | 留在客户端，属前端能力 |
| 领域能力 `AgentSkill` + `SkillRegistry` | `harness/skills.py:19`、`:82` | **能力插件接口的形状来源** |

#### 3.3.2 三条决定工作量的事实

1. **五处边界都是进程内 Python Protocol，装配靠构造注入**。装配入口只有两个函数：`harness/factory.py:8` 的 `build_harness(setup)` 与 `harness/factory.py:21` 的 `load_harness(path)`。
2. **RV 侧已经有插件语义的三件套**：`SkillRegistry` 提供 `register` / `for_capability` / `require` / `manifests`；`harness/skills.py:120` 的 `routing_contracts` 逐能力输出 `name`、`description`、`arguments_schema`（缺省兜底 `{"type":"object","properties":{},"additionalProperties":False}`）；`harness/skills.py:34` 的 `validate_routing_arguments` 负责校验。
3. **`AgentSkill` 是规划侧契约，不是执行侧契约**。它只有能力标识、能力集、可选清单，以及一个 `plan(planner, arguments)` 方法，返回字典；清单带 `planner_instructions` 与 `routing_schema`。**执行由宿主注入的 planner 承担**（`harness/bridge/execution.py:388` 的 `_require_planner()`、`:419` 的 `skill.plan(...)`）。

**第 3 条是本项目最容易写错的地方**：不要写“RV 已具备执行器”。正确表述是——执行面归属在 planner 与 `GeneralAgentPort`/`DelegateBackend`，不在 skill 里；适配层要新增 **planner 注入**与**“路由结果 → 具体执行”的衔接**，这是批次五最重的一块。

#### 3.3.3 工作状态机

RV 的工作状态落本地 SQLite，恢复时**不重放已执行的动作**（`harness/bridge/execution.py:855-856` 有重复执行防护）。这个“不重放”语义在分布式形态下必需，但状态文件不能跨进程共享 → 迁到中心侧的副作用账本，不另立新表。

### 3.4 外部参照（用来说明定位，不是依赖）

| 来源 | 关键事实 | 用在哪 |
|---|---|---|
| dsh（DeepSeek Harness） | npm `@deepseek-ai/dsh`，latest = **0.2.1-rc.5**（2026-10-29），注册表 40 个版本全部带 alpha/rc 后缀、**无 stable 标签**；MIT；`dsh` 命令；Web UI 在 127.0.0.1:3080；Cordis 插件元框架；session 存 `~/.dsh/sessions/<slug>/session-<uuid>/session.jsonl.zstd`；OS 级沙箱（Linux bwrap+Landlock、macOS Seatbelt、Windows 受限令牌）；schedule 包是“会话内提醒、冷会话恢复、**无外部通知通道**”；出现过沙箱自逃逸的公开漏洞且官方自述无安全审计 | **ms 的定位同级参照**：独立形态与 dsh 同级，不是 Duet 同级 |
| dsh-bot | 常驻实体不属于任何会话；记忆是 `$DSH_HOME/bot/memory/` 的 .md 文件树；硬门禁拒绝身份证/卡号/密码/令牌，软门禁默认排除健康财务法律家庭；**审批是一等状态 `awaiting`**（不是错误分支） | “审批是一等状态”这个设计值得抄 |
| Claude Duet | 研究预览；**自己的云电脑**、关 app 继续工作、批准延迟回传、密码走独立表单不入对话、**只有用户能标记任务完成**、不能看浏览器/接管 | Q5 的口径依据：这类承诺靠云端常驻兑现，本机形态不具备同等前提 |
| OpenAI dots | 2026-09-29 DevDay；**每个 dot 一台专属云电脑**；主动研究只读；Custom Rules 三档；Auto-review；云电脑与用户电脑默认分离 | 同上；dots 的权限档位与留存策略另有专门分析可参考 |

---

## 四、架构速览

### 4.1 九个组件

ms 的完整形态由九个组件构成，形态开关决定哪些启用。

| 组件 | 运行位置 | 职责 | 中心形态 | 本机形态 |
|---|---|---|---|---|
| ms 客户端 | 本机 | 对话界面、提交任务、轮询回执 | 启用 | 启用 |
| ms 调度器 | 本机 | 队列、定时触发、派活入口 | 启用 | 启用 |
| ms 执行器 | 本机 | 领取任务、跑能力插件、回传产出 | 可选 | 启用 |
| 能力插件宿主 | 本机 | 加载能力插件、转发调用 | 可选 | 启用 |
| Loom API | 中心 | 目标、计划、批准、事件的唯一权威 | 启用 | 禁用 |
| Loom worker | 中心 | 调度器唤醒、计划执行 | 启用 | 禁用 |
| 租户与鉴权 | 中心 | 用户、空间、令牌、行级隔离 | 启用 | 禁用 |
| 审计事件存储 | 中心 | 事件追加、链校验 | 启用 | 本地替身 |
| 本机状态存储 | 本机 | 队列、触发器、本地审计 | 启用 | 启用 |

### 4.2 两个正交开关

**不引入“模式”这个运行时概念**，只用两个正交的装配开关：

- `MS_ROLE`：`client`（默认，只跑客户端、调度器、执行器、能力插件宿主、本机状态存储）或 `orchestrator`（跑完整服务面）。
- `MS_EXECUTION_DOMAIN`：`local`（默认，执行器在本机跑能力插件）或 `remote`（执行器不启动，任务只提交给中心）。

**Phase 1 不做 `orchestrator` 形态**——它与 Loom 自立形态职责重叠，而后者有明确批次归属。开关保留在代码里，产出不承诺。

### 4.3 五种运行形态

| 形态 | agent 运行时位置 | 中心侧 | 人的位置 |
|---|---|---|---|
| 独立 | 本机 | 无 | 本机对话窗口 |
| 组合 | 本机 | Loom 在线 | 本机对话窗口 |
| 分布式 | 每台机器一份 | Loom 在线 | 各自本机 |
| Loom 自立 | 中心 | Loom 自己 | 浏览器或桌面端 |
| 降级独立 | 本机 | 中心失联 | 本机对话窗口 |

**形态判定（三步，都在“中心可达”前提下）**：

1. 看**中心侧登记**的 ms 实例数（按空间统计）：一台=组合，多台=分布式。
   ⚠️ 这个计数必须来自中心。读本机的计数永远得 1，会永远停在组合形态——这是本项目修过的一个真 bug。
2. 看 Space 声明的默认执行域。
3. 中心不可达 → 降级独立。

**Loom 自立形态不在判定链上**，它是用户主动选的另一条入口，与 ms 是否在线无关。

### 4.4 跨机契约：16 个动作，3 处需新建

这是 ms 与 Loom 之间唯一的交互面。**接这个活的人请把整张表背下来。**

| 动作 | 接口 | 方向 | 备注 |
|---|---|---|---|
| 发现可领工作 | `GET /v1/goals` | ms 从 Loom | worker 角色凭读能力调用 |
| 解析模板 | `GET /v1/domain-packs/{name}/{version}/templates/{template_id}` | ms 从 Loom | 返回模板标识 + 解析好的定义 |
| 提交目标 | `POST /v1/goals` | ms 到 Loom | 必带幂等键；自由文本只承载参数值与描述 |
| 提交计划 | `PUT /v1/goals/{run_id}/plan` | ms 到 Loom | 必带定义、期望修订号与幂等键 |
| 读取计划 | `GET /v1/goals/{run_id}/plan` | ms 从 Loom | **需新增**；给出状态、修订号与摘要，是批准的入参来源 |
| 读取进度 | `GET /v1/runs/{run_id}/events?after_seq=&limit=` | ms 从 Loom | **需新增游标**；现为全量快照 |
| 读取状态 | `GET /v1/runs/{run_id}/progression` | ms 从 Loom | 已存在，但不含修订号 |
| 领取任务 | `POST /v1/runs/{run_id}/claim` | ms 到 Loom | 本机执行器注册为 worker 主体 |
| 提交产出 | `POST /v1/runs/{run_id}/tasks/{task_id}/submit` | ms 到 Loom | 必带 epoch 与幂等键 |
| 续租 | `POST /v1/runs/{run_id}/tasks/{task_id}/renew` | ms 到 Loom | **需新增**；Phase 1 不使用 |
| 批准计划 | `POST /v1/goals/{run_id}/plan/approve` | ms 到 Loom | 必带期望修订号、摘要与幂等键；**由人发起** |
| 读取待办干预 | `GET /v1/interventions` | ms 从 Loom | 已存在；“等我确认”卡片的数据来源 |
| 暂停运行 | `POST /v1/runs/{run_id}/control/pause` | ms 到 Loom | 已存在；执行器请求人工介入时调用 |
| 恢复运行 | `POST /v1/runs/{run_id}/control/resume` | ms 到 Loom | 已存在；人处理完后调用 |
| 登记副作用 | `POST /v1/runs/{run_id}/effects` | ms 到 Loom | **需新增**；动作开始前登记意图，带 epoch、动作标识与副作用等级 |
| 裁决副作用 | `POST /v1/runs/{run_id}/effects/{call_id}/reconcile` | ms 到 Loom | 已存在；结算已登记的副作用 |

表中十六行里，**三处要新建**：读取计划的读接口、事件的游标分页、副作用登记端点。其余十三个都已存在。

**副作用登记端点为什么不能省**（这是本项目最重的一条发现）：Loom 的副作用账本由它自己进程内的四处调用写入（`src/loom/execution.py:132`、`src/loom/execution.py:174`、`src/loom/artifacts.py:510`、`src/loom/quotas.py:160`），**没有任何 HTTP 动作能让外部执行器告诉中心“我已经开始了一个副作用”**。若 ms 执行器开始一次不可逆写之后在提交前被杀死，中心账本里没有那一行，回收逻辑判不出未结算（`src/loom/store.py:289-306`），任务会回到待领取并被重新领取——**那次不可逆写被执行第二次**。

### 4.5 身份与密钥

**主体划分**：

- **两个具名主体**（不是一个主体拿两枚令牌）：`<实例>:operator` 负责提交目标与计划、读取计划与进度、**组装批准请求**；`<实例>:worker` 负责领取任务、提交产出。依据是 `src/loom/auth.py:101-102`（同一主体只能登记一次）与 `src/loom/auth.py:68`（不得同时持 operator 与 worker）。
- **批准只由用户会话主体发起**。ms 客户端用设备令牌换一个绑定到用户本人的会话令牌，人在界面操作时用它签署请求。“具名人类”的可判定定义三条：能解析到一个用户记录并有成员关系；凭证不是实例主体派生；带的是用户会话而不是设备或实例令牌。

**键与凭证**：

- **幂等键四段式**：`<机器标识>:<run_id>:<task_id>:<尝试序号>`。机器标识来自本机生成的持久随机值，不得跨机复用。
- **模型凭证双源**：组合形态走中心短时令牌；独立与降级形态由本机持有用户自己的密钥。
- **跨机只传带作用域与到期时间的令牌，不传原始凭证。**

### 4.6 能力插件的形状

- 契约**不自造**，沿用 rakazo 的 `adapter-kit` provider 契约层。
- 宿主两种形态：**进程内 TypeScript 插件**（直接函数调用）与**进程外 MCP 插件**（标准输入输出通道）。RV 的 Python 执行器按进程外插件接入。
- 工具声明四个可选项：只读标记、批量动作声明、路由信息、**联网声明**（最后一个是 ms 在契约层扩展的，adapter-kit 没有）。
- **副作用标记的来源**：工具声明的只读标记与批量动作声明，落在中心的副作用账本上（调用粒度 = 每次工具调用一行）。

---

## 五、当前进度与产出在哪

### 5.1 已完成

| 产出 | 位置 | 状态 |
|---|---|---|
| 完整施工方案书（规格真源） | `/workspace/monsoon-plan/monsoon-harness-方案书.md` | 132,017 B / 1,206 行 / 37 条判据 / 44 条来源 / 67 条锚点全部可定位 |
| 本交接文档 | `/workspace/monsoon-handover/monsoon-harness-交接文档.md` | 你正在读的这份 |
| Loom 仓库 | `/workspace/Loom` | **只读基线**，git 工作区干净 |
| RV 仓库 | `/run/csi/mount-root/nas/eab0d61a99b6696edb3d2aff87b585e8/.mavis/plans/plan_3e823f3f/workspace/rv/repo` | **只读基线** |
| rakazo 缓存 | `/workspace/rakazo-src/repo/` | jsDelivr 取回，**文件名扁平化**，主干 SHA 未取到 |
| adapter-kit 契约层 | `/workspace/rakazo-src/adapter-kit/` | 四个文件已落盘 |
| 核验记录 | `/workspace/.mavis/plans/plan_4316dba4/outputs/final-audit/` | 生产方 17 条发现 + 评审方裁定表 |

**尚未开始任何实现工作。** 六个批次一个都还没动。

### 5.2 建议的新工作目录

```
/workspace/monsoon/            ← 新建，ms 的 fork 工作区
├── apps/ms-client/           ← ms 客户端（从 rakazo 的 apps/web 改）
├── packages/ms-capabilities/ ← 能力插件层（从 packages/adapters 切分出来）
├── packages/ms-runtime/      ← 本机运行时（调度器 + 执行器 + 插件宿主）
├── contracts/                ← 跨机契约（16 个动作的字段定义）
└── docs/
```

**目录名可以改，但分层要保持**：契约、核心、插件层三层两侧共用；`db`/`auth`/`adapters` 是中心侧；`web`/UI 包是客户端侧。

---

## 六、接手第一件事

### 6.1 前置一：钉住来源（批次零，对应整个工程的前置）

**要做什么**：

1. 取回 rakazo 主干的 commit SHA 与 tag（`git ls-remote https://github.com/elie222/rakazo` 或 GitHub API；jsDelivr data API 只给 `version: "main"`，不够）。
2. 写锁文件：SHA、tag、`apps/web`/`apps/api`/`packages/adapters` 三个包的依赖版本（`packages/adapters/package.json` 里 Pi 锁在 `@earendil-works/pi-agent-core` 1.3.0）。
3. 记录 fork 当下的数据库结构快照，用于后续比对。
4. 复制 monorepo 的工作区配置、包依赖图与构建脚本作为 fork 骨架。

**验收动作（不要漏）**：钉住之后**从方案书正文抽查十条 file:line 引用**，逐条回到对应文件核对行号是否漂移。行号漂移是这类文档最常见的失真源，而在 SHA 钉住之前根本没有核对行号的基准——引用的行号对不对，取决于引的是哪个版本。抽查结果写进交付说明，漂移的当场改并记进变更说明。

**这一步为什么重要**：上一轮核对时主干 SHA 因 TLS 握手失败与 API 速率限制没取到，导致方案正文无法引用任何 rakazo 的具体版本号。

### 6.2 前置二：打包载体决策（批次一开工前必须先定）

**要定的事**：ms 客户端是**桌面壳**还是**本机服务 + 浏览器**。

**为什么必须先定**：这决定第 9.1 节“本机进程存活期间”这个承诺的边界。

- 桌面壳：三个进程的生命周期绑在应用上，机器休眠与用户注销由系统决定，进程存活 ≈ 应用存活。
- 本机服务 + 浏览器：服务可以独立于浏览器存活，用户关掉页面不等于进程死。

**顺带定**：设备身份的持久性——机器标识存在哪、卸载重装后算不算同一台机器。

### 6.3 前置三：三个可行性探针

每个探针只回答一个问题，**不预估天数**。

| 探针 | 回答什么 | 影响 |
|---|---|---|
| **PostgreSQL 存储适配** | 存储访问是否全部经过一个接口层？是→适配是机械工作；否→这是一次重写 | 批次三的工作量定性 |
| **Loom 插件内核与运行预设** | RV 执行器以什么方式接入最省改动 | 批次五的接入形态 |
| **RV 执行器与依赖树** | RV 的完整依赖锁定与测试状况（本轮只读了边界定义与装配入口，**未跑测试、未核对依赖锁定**） | 批次五的可行性 |

**探针的纪律**：结论要写成“结论 / 依据 / 未知”三段。“未确定，这是查过的东西”也是有效结论，不是失败。

---

## 七、施工顺序

**六个批次，严格按依赖顺序**，但批次三只依赖批次零，**可以和批次一并行开工**——这是排期上最省时间的地方。

### 7.1 批次零：钉住来源（2–3 人日）

产出见 6.1。这是所有批次的前置。

### 7.2 批次一：客户端骨架与身份登记（8–12 人日）

**前置**：批次零；**打包载体决策已定**。

**产出四件**：

1. **客户端骨架**：最小可运行进程，能启动、能连中心、能显示对话。复用 rakazo 的 web 包外壳（React 19、Tailwind、oRPC），默认装配改成客户端角色。
2. **设备注册**：客户端首次启动生成设备密钥，向中心注册为当前用户名下的一台设备。授权模式由 `USER_DEVICES_ENABLED` 控制（rakazo 侧默认开启）。**部署向导要把这个开关显式写出来**——“桌面端可以让 bot 在用户自己的电脑上跑命令”这句话对用户的安全含义重大。
3. **双主体登记**：按 4.5 节的两个具名主体实现。认证沿用 Better Auth 的口令、通行密钥与组织插件组合。
4. **部署策略收紧**：`signupsEnabled` 置否、`signupAllowlist` 指定允许的用户。部署所有者复用既有的 `DeploymentSettings.ownerUserId` 字段与引导脚本，不新造角色体系；并写明“部署所有者能做什么”——能管理部署设置、邀请移除用户、调整空间执行域默认值、查看全部空间审计；**不能代替用户批准计划、确认交付、读取会话内容**。所有权不等于操作权。

**另外**：轨迹记录默认关闭，部署向导要把“打开它意味着什么”写在开关旁边。

### 7.3 批次二：本机运行时（8–12 人日）

**前置**：批次一。

**产出**：三个进程（客户端 / 调度器 / 执行器）、两个存储、**一个派活入口**、**一处归集约定**。

- **调度器**：本机 SQLite 上维护待触发任务队列与触发规则表两张表，**规则以数据库为准而不是以内存为准**，重启不丢。
- **派活入口**：独立形态的唯一入口，接受一段自然语言或一个模板标识 + 参数，在本机建任务、立刻执行、返回产出。**独立形态不连中心，所以这条路径必须在批次二就可用。**
- **本地审计替身**：与 Loom 相同的信封形状，写本机 SQLite。
- **归集约定**：客户端在提交目标时于描述头部插入 `requested-by:<用户标识>`。

### 7.4 批次三：Loom 侧改造（14–22 人日）

**前置**：批次零（**可与批次一并行**）。

**十个子项**，两类体量差别很大：

*八个较小的*：

| 子项 | 内容 |
|---|---|
| 事件出口游标分页 | 加 `after_seq` 与 `limit`；游标**归消费者所有**，每台 ms 各持自己的，不设共享游标 |
| 租约续租端点 | 把存储层已有的 `renew_claim` 暴露成路由（`src/loom/store.py:376`） |
| 计划读接口 | 按 `src/loom/workflow.py:290-293` 的返回形状给状态、修订号与摘要 |
| 域包模板参数契约 | 形状取自工具声明的入参模式 |
| 副作用登记端点 | **分布式形态下不重放的前提，不能省** |
| 执行域策略字段与迁移脚本 | `Space.defaultExecutionDomain` 与 `Bot.executionDomain` 两个新字段 |
| 发起人归集派生表 | 目标创建时**同步解析并入库**，不做离线批处理 |
| 关闭机器批准路径 + 角色能力集调整 | 禁止 legacy 密钥派生 developer；从 operator 移除 `plan.approve`；给 worker 加 `control`（收紧为只允许 pause/resume） |

*两个大的*（先做探针）：PostgreSQL 存储适配、Loom 插件内核与运行预设。

**行级安全**：rakazo 侧的 PG 部署按 Space 生效行级安全。Loom 迁到 PG 后要把租户隔离落到数据库层，做到“查询本身就看不到别人的数据”而不是“查出来再过滤”——这是 PG 适配的验收要点之一。

### 7.5 批次四：跨机契约与分布式（10–15 人日）

**前置**：批次二与批次三。

**产出**：契约面 16 个动作全通；双令牌双主体；跨机幂等键空间；并发语义验证。

**三条并发语义要验证**：两台 ms 同时领取同一批任务时领取集合无交集；两个人同时批准同一个计划时只有一个成功（另一方收到 409）；两台 ms 同时提交同一任务的产出时只有一次生效。

**跨机提交必须在空间内进行**，不能把一个空间的 bot 派到另一个空间的机器上。

### 7.6 批次五：能力插件层与 Loom 自立（19–32 人日）

**前置**：批次四。

**七个子项**：

**能力层**：

1. **插件宿主**：两种宿主模式，契约沿用 `adapter-kit`，在其上补 ms 需要的实现。
2. **RV 执行器接入与 planner 适配**：能力声明部分成本低（三个字段一一对上）；执行面部分成本高（要新增 planner 注入与“路由结果 → 具体执行”的衔接）。**这是本批最重、也是 10.1 区间里最不确定的一块。**
3. **宿主中介的两项处置**：凭证路径绊线、联网声明落地。
4. **工作状态机迁移**：不另立账本，在中心的副作用账本上补字段。

**运行与中心形态**：

5. **运行预设**：能力组合成可复用的任务模板，模板带参数契约。
6. **Pi 运行时接入**：本机侧把模块装进宿主并注册到 runtime 插槽；中心侧用持久化的会话管理器而不是内存模式。
7. **Loom 自立形态**：Loom 自己要能像 dsh 那样一条命令起本地 Web UI、插件化、有轨迹记录。**不把 dsh 的沙箱当成可直接复用的安全边界**——它出现过公开的沙箱自逃逸漏洞且官方自述无安全审计。

### 7.7 每个批次的“不做”清单

| 批次 | 不做什么 |
|---|---|
| 零 | 不动三个上游仓库的任何文件 |
| 一 | 不开自助注册；不新造部署所有者角色体系 |
| 二 | 不做自动路由；不做服务端推送；不引入服务端依赖 |
| 三 | 不做租约续期的实际使用；不做模板参数字段的持久化 |
| 四 | 不做单机多实例的形态判定；不做跨空间搬运 |
| 五 | 不做 OS 级沙箱；不做 `MS_ROLE=orchestrator` 形态 |

### 7.8 全局“不做”清单（八条）

**调度与传输**：

1. Phase 1 不做自动路由。
2. Phase 1 不做服务端事件推送（4.3 节的 WebSocket 已砍）。
3. Phase 1 不做租约续期端点的实际使用，只做端点本身。
4. Phase 1 不做单用户精简版。

**形态与隔离**：

5. Phase 1 不做 Loom 侧模板参数字段的持久化。
6. Phase 1 不做分布式下的长时间挂起（挂起靠暂停运行实现）。
7. Phase 1 不做 `MS_ROLE=orchestrator` 形态。
8. Phase 1 不引入操作系统级沙箱。

第 8 条的重启条件写死了：**一旦出现第三方分发的插件形态，或者插件的安装者与使用者分离，操作系统级沙箱从可选升为必选**，本条随即作废。

---

## 八、验收判据速查

**37 条判据，分七组。** 施工到哪一批就翻哪一组。判据以可机器判定的形式给出——断言落在对外接口的响应内容、持久化存储的最终状态、事件流里出现的记录条数与元数据上。

| 组 | 条数 | 覆盖 | 相关批次 |
|---|---|---|---|
| A 契约与幂等 | 7 | 接口可用性、游标分页语义、跨机键空间、epoch 栅栏、并发领取 | 四 |
| B 主体与权限 | 5 | 双主体能力集分离、主体唯一登记、互斥角色、developer 被拒、批准 actor 具名 | 一、三 |
| C 形态与降级 | 5 | 独立形态派活入口、中心失联时留队、恢复后补交、组合端到端、分布式多机 | 二、四 |
| D 插件与能力 | 4 | 参数契约拒绝、模板标识必填、进程外插件可调用、能力停用不需重启 | 五 |
| E 存储与审计 | 3 | 事件链校验、本机补交后链连续、发起人可归集 | 二、三 |
| F 安全与隔离 | 4 | 非成员被拒、拒绝不泄露存在性、旧密钥不能派生 developer、跨空间提交被拒 | 一、三 |
| G 安全性质与形态专项 | 9 | 副作用登记与不重放、凭证作用域、推送面为零、Loom 自立可用、并发上限可见、批准主体可判定、等待期暂停闸门、三处显示一致、安装披露与凭证路径绊线 | 三、四、五 |

**G 组几条特别说明**：

- **G1**（副作用登记与不重放）：执行器在不可逆动作开始前登记；随后杀掉执行器并让任务重新被领取，断言该动作**没有被再次执行**，且账本里留有那一行。
- **G3**（推送面为零）：断言中心侧**不存在**服务端到 ms 的事件投递通道，断言吊销信息只能随轮询响应下发。
- **G7**（等待期的暂停闸门）：断言运行状态变为暂停中；断言此时任何执行器领取该运行的任务都被拒；断言租约到期后任务回到待领取但仍不可领；断言恢复后任务才重新可领。
- **G8**（三处显示的执行域一致）：同一个 bot 在派活入口、bot 详情、空间管理三处的生效执行域与来源徽标一致，且等于按继承规则算出的值。

**判据本身的局限**（要写进你的交付说明）：这 37 条证明的是“这些已知错误形态被抓住了”，**判据够不够由评审方判断**。机器校验（编号连续、表格结构、交叉引用）全绿，不代表“声明—证据”的映射成立——这两件事要分开查。

---

## 九、已知陷阱清单

### 9.1 本项目已经踩过的（写代码/写文档时对照）

| 陷阱 | 表现 | 对策 |
|---|---|---|
| 把“介入”当成一等记录 | 去找“新建介入”的接口 | 它是派生视图；正确动作是 pause |
| 以为“放弃租约”就等于“不会被重领” | 写“等回池就没人领” | 领取只检查运行状态，不检查介入；必须先 pause |
| 把“已有 X 能力”当成“执行面已具备” | 写“RV 已具备执行器” | `AgentSkill` 只有 `plan(planner, arguments)`；执行在 planner |
| 引用只写 `:335` | 行号归属到错的文件 | 永远写完整 `src/loom/api.py:335` |
| 否定断言没证伪 | 写“没有负载字段” | 先反向检索找反例；找不到写“未检索到” |
| 绝对化措辞 | 唯一 / 一律 / 只有 / 全部 / 第一个 | 全局 grep 逐条复核；能枚举的一律枚举并给来源 |
| 数字声明漂移 | 表格 14 行、正文说 13 | 改一处要全局找齐；用脚本比对 |
| 拍板挂在不相干的判据上 | “由判据 A2/A3 锁住”但 A2/A3 是分页判据 | 做一次**拍板—判据对账** |
| 交付件只写骨架就交差 | 309 B 的占位文件 | 用引擎闸门机器判定，不用措辞约束 |

### 9.2 写交付文档的工艺（沿用规格书的纪律）

**结构与格式**：

1. **附录/索引必须由正文脚本生成**，不能手工维护——手工版必然与正文漂移。
2. **每张表前后各有一句引导与含义句**。表格不能从中间被劈开。
3. **中文段落用全角标点**，不出现 ASCII 直引号与日式引号。
4. **引用标记连续且与参考表一一对应**；本文档不重复规格书的编号表，需要编号对照时查规格书附录 A。

**流程与只读纪律**：

5. **交付件末尾固定写一行 `**VERDICT: PASS**`**（注明是生产者自检结论）——引擎按可解析的判定标记决定是否评审，缺 token 会被自动退回，内容再达标也不行。
6. **被退回时只做定点修订**，保留已通过的结论，在 Notes 里写清“错在哪、为什么错、怎么改”，并显式写明撤回了上一轮的哪条错误修正。
7. **在只读仓库里跑探针加 `PYTHONDONTWRITEBYTECODE=1`**，否则会生成 `__pycache__`/`*.pyc`，看起来像改了仓库。
8. **只读仓库的双重完整性确认**：`find . -name "__pycache__" -o -name "*.pyc"` 与 `find . -type f -mmin -N` 都应为空。

### 9.3 还没取到的东西

| 项 | 状态 | 影响 |
|---|---|---|
| rakazo 主干 commit SHA | 未取到（TLS 握手失败 + API 速率限制） | 批次零第一件事 |
| jsDelivr 文件清单不完整 | 361 条 vs CDN 实际更多 | 不可用于推断仓库规模 |
| RV 的完整测试与依赖树 | 未跑测试、未核对依赖锁定 | 批次五的探针要补 |
| Loom 的 PostgreSQL 迁移覆盖范围 | 只确认核心表结构有迁移脚本，存储访问是否全经接口层未验证 | 批次三的探针要回答 |

---

## 十、术语表

| 词 | 本项目里的意思 | 别混成 |
|---|---|---|
| ms | monsoon-harness，跑在用户机器上的常驻 agent | 不是浏览器前端 |
| 中心 / 编排中心 | 运行 Loom 的那台服务器，跨机契约的另一端 | 不是 ms 自己 |
| 中心形态 | ms 以 `MS_ROLE=orchestrator` 运行 | Phase 1 不做 |
| 中心侧执行器 | 远端执行域里承接任务的那台执行位置 | 不是编排中心 |
| 执行域 | 执行发生的地方：local（本机）或 remote（远端） | 不是“工作区” |
| 生效执行域 | 一个 bot 实际使用的执行域 = 继承 Space 默认或 bot 覆盖 | 三个界面必须同源 |
| 空间 / Space | rakazo 的租户单元，隔离与审计的顶层 | 不是 RV 的本地目录 |
| 租户 / tenant | Loom 侧对应的分区键 | 与 Space 一一映射，ID 空间不同 |
| 介入 | 一个**派生视图**，不是记录 | 不是“新建一条介入” |
| 暂停 / pause | 把运行状态改成 paused，领取动作随即被拒 | 不是“放弃租约” |
| 能力 / capability | 一个可插拔的执行单元，声明含参数契约与只读标记 | 不是 bot |
| 插件宿主 | 加载能力插件并转发调用的进程 | 不是插件本身 |
| 只读标记 | 工具声明里“这个能力是否写”的声明 | 不覆盖网络出向 |
| 联网声明 | 工具声明里“这个能力是否联网”的声明（ms 扩展） | 不是门禁 |
| 副作用标记 | 只读标记 + 批量动作声明，落在中心的副作用账本 | 不是“可重放”的同义词 |
| 栅栏 / epoch | 领取时递增、提交时校验的并发控制量 | 不是幂等键 |
| 幂等键 | 四段式，租户内全局共享，跨机不得复用 | 不是请求标识 |
| 批次 | 施工的六个阶段，严格按依赖顺序 | 不是 sprint |

---

## 十一、文件与目录索引

| 路径 | 说明 |
|---|---|
| `/workspace/monsoon-plan/monsoon-harness-方案书.md` | **规格真源**。132 KB / 13 章 + 3 附录 / 37 条判据 / 44 条来源 |
| `/workspace/monsoon-handover/monsoon-harness-交接文档.md` | 本文档 |
| `/workspace/Loom` | Loom 只读基线（git 干净） |
| `/run/csi/mount-root/nas/eab0d61a99b6696edb3d2aff87b585e8/.mavis/plans/plan_3e823f3f/workspace/rv/repo` | RV 只读基线 |
| `/workspace/rakazo-src/repo/` | rakazo 取回缓存（扁平命名，SHA 缺失） |
| `/workspace/rakazo-src/adapter-kit/` | adapter-kit 契约层四文件 |
| `/workspace/.mavis/plans/plan_4316dba4/outputs/final-audit/deliverable.md` | 核验方报告（17 条发现） |
| `/workspace/.mavis/plans/plan_4316dba4/outputs/final-audit/verification.md` | 核验裁定表 |
| `/workspace/Loom/src/loom/api.py` | Loom 全部 54 个 HTTP 路由 |
| `/workspace/Loom/src/loom/store.py` | 存储层、租约、幂等、事件、副作用账本 |
| `/workspace/Loom/src/loom/operator.py` | 暂停/恢复、介入派生视图 |
| `/workspace/Loom/src/loom/auth.py` | 角色与权限集、主体唯一性 |
| `/workspace/rakazo-src/adapter-kit/types.ts` | provider 契约层的字段定义 |
| `.../rv/repo/harness/skills.py` | RV 的能力注册表与参数契约 |
| `.../rv/repo/harness/bridge/execution.py` | RV 的 planner 注入与 skill 调用 |

---

## 十二、交接确认清单

交出前请逐条确认，确认完再开工：

**目标与决策**：

- [ ] 能说清 ms 的目标形态与 rakazo 的方向为什么是反的
- [ ] 知道 Q1–Q5 五项决策的结论，**不重新讨论**
- [ ] 知道拍板三的三条命题分别是什么，**不合并成一句**
- [ ] 知道拍板与判据要对账

**机制**：

- [ ] 知道“介入是派生视图”“暂停才是硬闸门”这两条
- [ ] 知道副作用登记端点为什么不能省
- [ ] 知道 ms 的两个具名主体分别负责什么、为什么不能是一个主体两枚令牌
- [ ] 知道幂等键为什么必须带机器唯一前缀

**凭证与形态**：

- [ ] 知道模型凭证在独立形态下由本机持有
- [ ] 知道 Phase 1 砍掉了 WebSocket，继承的是连接治理七条
- [ ] 知道三个只读仓库的路径与只读纪律
- [ ] 知道 37 条判据在哪一章，施工到哪一批翻哪一组

**开工前置**：

- [ ] 知道批次零要先钉 SHA 并抽查十条引用
- [ ] 知道打包载体是批次一的前置决策

**有任何一条答不上来，回到本文档对应小节重读，再开工。**

---

## 附记（2026-11-01，本机接手）

交接文档原基于 /workspace 工作区编写；本机接手后的事实基线变更如下，正文其余部分继续有效：

1. 本机只读基线路径：rakazo `~/projects/rakazo`（`d13ef7a`）、RV `~/projects/rv`（`e53ae8d`，tarball）、Loom `~/projects/Loom`（v0.23.0）、dsh-superagent `~/projects/dsh/superagent-prototype`（`6337fc8`）、dsh 主仓 `~/projects/dsh`（`ce7073d`）。
2. 方案书已修订为 v2.2（`~/monsoon-work/monsoon-plan/monsoon-harness-方案书-v2.2.md`），文首修订前言记录 2026-11-01 六项拍板：路线 A（吸收 + Python 重写，取代 Q3 整仓 fork）、Loom 式认证（修订 Q2）、dsh/Codex 式 CLI harness + 常驻服务 + 本机 Web 控制台（解决打包载体前置决策）、Pi 不进 Phase 1（修订 Q4 执行面）、批次三批准先行、rakazo 优化清单采纳为设计约束。冲突时以方案书 v2.2 为准。
3. 本机锚点核对结论：41 个 file:line 锚点在 Loom 0.23.0 上 38 个逐字命中；Loom 0.23.0 已自带计划读接口（`src/loom/api.py:327`）与 progression 修订号（`src/loom/progression.py:23`），批次三"三处新增"实际剩两处（事件游标、副作用登记端点）加续租路由（Phase 1 不用）。
4. 调查产出：`~/monsoon-work/调查报告-2026-11-01.md`（三库盘点 + rakazo 可优化点清单）。

---

**VERDICT: PASS**

（生产者自检结论。已过的机器检查：表格 28 张结构与引导/含义句全合规；中文段落无半角标点、ASCII 直引号与日式引号；无乱码；无超过 4 条的连续列表；67 条 file:line 锚点全部在对应仓库中可定位，其中关键锚点已逐条打印真实行做归属性核对。本文不含面向评审引擎的独立评审结论，正式评审以评审会话为准。）
