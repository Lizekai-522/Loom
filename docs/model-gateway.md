# DeepSeek 模型网关

核查日期：2026-10-23。实现为 `src/loom/model_gateway.py`，配置模板为 `configs/models.example.json`。本轮增加可运行 HTTP 适配器和模拟 HTTP 测试；本文件不把模拟响应算作真实模型调用，也不把网关接通算作完整协作流程完成。

## 用户指定模型的官方对应

用户指定的是“DeepSeek 4.1 Flash”。当前官方首次调用文档明确推荐 API ID `deepseek-flash`，由 **DeepSeek-V4.1-Flash-1017** 提供服务；旧 `deepseek-v4-flash` 和 `deepseek-v4-flash-vision-exp` 对应模型已经退役。配置采用当前推荐 ID，没有换用 Pro 或其他模型。[官方首次调用说明](https://api-docs.deepseek.com/quick_start/)

官方模型列表示例同时给出 `id=deepseek-flash` 和显示名称 `DeepSeek-V4.1-Flash`，并区分可传给 API 的 ID 与显示名称。因此不能把用户自然语言直接拼成 `deepseek-4.1-flash` 请求。[官方模型列表](https://api-docs.deepseek.com/api/list-models/)

该 ID 是供应商维护的别名，官方说明其当前后端版本，并不等于 Loom 获得了不可变权重摘要。网关校验响应 `model` 与配置 ID 完全相等；响应不能自行改变 Loom 记录的 provider/model 身份。该检查仍依赖官方 HTTPS 端点与供应商报告，不能宣称为模型权重的独立密码学证明。后续别名变化需要重新核对官方说明；未知 ID 可以保留在配置中，但 readiness 返回 `MODEL_UNVERIFIED`，生成时拒绝调用。

## 公共接口与配置

```python
from loom.model_gateway import ModelGateway, load_model_config
from loom.local_config import load_project_secrets

routes, roles = load_model_config("configs/models.example.json")

async def inspect_readiness(private_config_path):
    secrets = load_project_secrets(
        private_config_path, allowed_names={route.key_env for route in routes}
    )
    async with ModelGateway(routes, role_aliases=roles, environ=secrets) as gateway:
        # 仅检查本地配置和环境变量格式，不发送网络请求。
        return gateway.readiness("deepseek-flash")
```

配置文件仅记录密钥变量名 `DEEPSEEK_API_KEY`，不保存密钥值。额外 `api_key` 字段、重复配置字段/alias、无效角色映射均被拒绝；网关不自动搜索其他文件、登录凭据或 `.env`。本项目由 `local_config.load_project_secrets` 从项目及公开 outputs 目录之外读取专用私有配置，返回独立只读映射，经 `ModelGateway(..., environ=secrets)` 注入；不写入 `os.environ`，因此工作进程与 Docker 子进程不会因继承该环境而拿到项目密钥。网关单独使用且未传 environ 时仍可读取现有进程环境。密钥不得进入源码、日志、测试断言输出或交付包。

`readiness().ready=True` 表示注入的本地密钥映射或环境中存在可用格式的变量，且模型路由已核查；`network_checked` 始终为 `False`。它不证明密钥有效、余额足够、网络可达或远端模型可用。

| 接口/类型 | 约定 |
|---|---|
| `ModelRoute` | 冻结的服务端路由：alias/provider/base_url/model/key_env、超时、输入/响应字节上限、输出 token 上限、thinking/effort |
| `ModelGateway(routes, role_aliases=..., transport=..., environ=...)` | 默认真实 HTTPS transport；测试可注入 `httpx.MockTransport` 和隔离环境映射 |
| `await generate(route_alias, messages, max_output_tokens, *, json_mode=False)` | 仅接受已注册 alias；messages 只含文本 `role`/`content`，角色为 system/user/assistant；json_mode 为严格布尔型服务端参数，拒绝任意额外请求参数 |
| `await generate_for_role(role, messages, max_output_tokens, *, json_mode=False)` | role 先通过服务端固定映射解析为 alias，再调用同一路径 |
| `ModelResult` | content、配置决定的 model_id/provider/route_alias、usage、request_id、finish_reason、已核对的 response_model；`truncated` 标记 length |
| `ModelUsage` | prompt/completion/total tokens，以及存在时的缓存命中/未命中和 reasoning tokens |
| `GatewayError` | 静态 `code` 与可选 HTTP 状态码；不暴露供应商响应体、密钥、prompt 或原始网络异常 |

同一个 `deepseek-flash` 可以分配给规划、实现和验证角色，便于先测单模型调用链。多个角色或不同 alias **不会产生不同模型身份**；这份模板不能满足要求两个不同受信模型的双签门槛。独立验证必须继续基于受信路由的 `(provider, model_id)` 判断，不能使用模型自行声称的角色或自定义名字。

模板显式关闭 thinking，以便首次小请求控制时延和输出预算。服务端可配置 enabled 及 low/high/max；这些参数不能由用户 prompt 替换。只返回最终 `content`，不把供应商的 `reasoning_content` 放入结果或日志。官方文档说明 thinking 的切换及 reasoning 字段；网关当前不支持工具调用、视觉输入、流式响应或自动执行任何模型产生的命令。[官方 thinking 说明](https://api-docs.deepseek.com/guides/thinking_mode/)

需要结构化候选时，由服务端调用 `json_mode=True`，网关追加 `response_format={"type":"json_object"}`。默认 False 保留普通文本连通检查。官方 Flash 示例支持该参数，同时要求 prompt 明确包含 JSON 指令及期望格式示例，且输出可能为空或因 token 上限截断；网关仍须检查 content，执行适配器仍须严格检查 `{"source": ...}` 形状，不能只相信模式开关。消息中的额外 `response_format` 字段不被接受。[官方 JSON Output](https://api-docs.deepseek.com/guides/json_mode/)

## HTTP 与响应边界

当前 DeepSeek 适配器仅允许官方 `https://api.deepseek.com`，可选兼容路径 `/v1`；拒绝 HTTP、用户信息、其他域名/端口、query、fragment 和任意路径。endpoint 固定追加 `/chat/completions`，prompt 不能更改地址、环境变量名或 Authorization。客户端关闭环境代理继承和重定向，传输层重试次数为 0；失败后也没有应用层自动重试。

默认一次调用总时限 60 秒，同时设置 httpx 各阶段超时；配置上限为 300 秒。默认请求最多 256KiB、响应最多 1MiB，另有硬配置范围。非 200 响应立即拒绝且不读取错误正文；200 响应只接受 JSON，拒绝压缩响应，检查声明长度，并在分块读取时再次限制累计字节，避免缺失或虚假的 Content-Length 绕过限制。

响应 JSON 严格拒绝重复字段、NaN/Infinity、溢出为无穷大的浮点数、无效 UTF-8 和结构错误。网关检查对象类型、请求 ID、时间整数、单个 assistant choice、文本 content、finish_reason 和精确 model ID；不接受输出工具调用。usage 必须是非负整数，prompt+completion 必须等于 total，completion 不得超过请求上限；可选缓存和 reasoning 计数也须自洽。非整数、布尔值和字符串不会被隐式转换。[官方 Chat Completions 请求和响应结构](https://api-docs.deepseek.com/api/create-chat-completion/)

`finish_reason="length"` 返回 `truncated=True`，调用者必须将其作为截断结果处理，不能直接计作任务通过。其他中断原因、模型 ID 不符、异常 usage 或不完整结构均失败。网关不会把供应商返回的文本等同于受信测试证据或成功验收。

网关尚不操作 Store 的预算预留/结算、任务幂等键、验证签名或事件提交。编排层必须在网络调用前预留硬上限，并处理调用后不确定状态。超时、断连、无效响应或 usage 缺失都不证明供应商没有执行或没有计费；不能据此直接退款并无上限重试。该边界也是关闭自动重试的原因。

## 官方费率、结算计算与预算边界

以下为 2026-10-23 核查的 Flash 美元费率，单位均为每百万 tokens；供应商可能调整价格，需保留请求使用的费率版本。[官方定价](https://api-docs.deepseek.com/quick_start/pricing/)

| token 类别 | 非高峰 USD / 1M | 高峰 USD / 1M |
|---|---:|---:|
| 输入：缓存命中 | 0.003 | 0.006 |
| 输入：缓存未命中 | 0.15 | 0.30 |
| 输出 | 0.60 | 1.20 |

官方高峰为周一至周五 UTC 01:00–04:00 和 06:00–10:00，中国法定节假日除外；其余时间为非高峰。不能仅用机器本地小时选择费率。当前页面没有明确跨时段长请求应按哪个时间点确定计价档位，不能自行把开始或结束时间当成已保证的账单规则。

令 `H=prompt_cache_hit_tokens`、`U=prompt_cache_miss_tokens`、`O=completion_tokens`，应满足 `H+U=prompt_tokens`。按选定档位计算：

```text
估算 USD = (H * 输入命中单价 + U * 输入未命中单价 + O * 输出单价) / 1,000,000
非高峰估算 µUSD = (3*H + 150*U + 600*O) / 1000
高峰估算 µUSD   = (6*H + 300*U + 1200*O) / 1000
```

整数 µUSD 账本可以在每次请求的总和计算完成后向上取整，用整数分子运算避免浮点误差；这是 Loom 的保守记账选择，并非声称供应商按该粒度舍入。`reasoning_tokens` 已属于 completion 子项，不重复收费计数。缺少缓存明细时，只能按全部输入未命中作保守估算，不能称为精确费用。缓存是 best effort，重复 prompt 不保证命中；应读取该次响应的 usage，而不是预先假设会有折扣。[官方缓存明细与命中边界](https://api-docs.deepseek.com/guides/kv_cache/)

`max_tokens` 只限制一次生成的 token 数，官方范围是 1–393216；输入和生成共同受模型上下文约束。它不限制输入费用，不是美元限额字段。thinking 输出也占生成预算；本地响应字节上限、超时和断开连接均不构成供应商停止计算或停止计费的保证。当前适配器不启用其他按请求收费的搜索/抓取 API。

对已核实的输入 token 上界 `I` 与输出上限 `M`，以当前最高峰价、输入全部未命中进行保守预留，可用 `ceil((300*I + 1200*M)/1000)` µUSD。字符长度/字节长度的启发式估计不等于严格 token 上界；若没有核实 tokenizer 与聊天模板开销，保守输入上界可用官方上下文容量 1048576，虽然会明显高估。这一预留仍以当前价目和供应商遵守 token 契约为前提，不等于账户级强制账单封顶。[官方 token 估算说明](https://api-docs.deepseek.com/quick_start/token_usage/)、[模型容量元数据](https://api-docs.deepseek.com/api/list-models/)

硬预算仍须由 Loom 在发请求前原子预留、限制调用次数与总 token、保留不确定计费预留并对账。仅在响应后拒绝超限 usage 属于事后发现，不能撤回已发生的供应商费用。本文公式计算的是按公开价目得到的费用估算，不是已独立查询的实际扣款或账单。

第二模型尚待用户决定。官方另一个当前 ID 为 `deepseek-v4-pro`，显示版本 V4-Pro-0813；本次没有调用 Pro，也没有因此修改网关的 VERIFIED_MODELS。Flash 与 Pro 为不同 API 模型 ID，但同属 DeepSeek/V4 系列；这不足以自行证明错误独立或证明 Pro 的能力 tier 必然更高。是否可作为第二签须由已发布风险策略与能力评测决定，不能仅按名字推断。[官方当前模型与版本](https://api-docs.deepseek.com/quick_start/pricing/)

## 本轮验证记录

执行：`python -m pytest tests/test_model_gateway.py -q`。

结果：**25 passed，62 subtests passed**。全部使用 `httpx.MockTransport` 或注入的异步响应流，覆盖固定 endpoint/请求参数、角色映射、未知模型拒绝、readiness 无网络、密钥/错误脱敏、禁止重定向/重试、总超时与关闭流、请求/响应字节上限、严格 JSON、模型身份、usage 一致性及截断标记，并增加巨整型超时配置以静态错误拒绝、JSON 模式严格参数与角色映射转发的回归。

这些测试不使用真实密钥、不连接 DeepSeek、不发生模型费用。“先在本机测试”在本模块中表示本机运行网关与测试；当前适配目标是 DeepSeek 远端 HTTPS API，不表示已经下载或在本机运行其模型权重。

主执行者另行报告已经完成一次授权的 Flash 真实连通检查：返回 model 为 `deepseek-flash`，usage 共 28 tokens（25 输入、3 输出），检查通过；没有打印密钥。该次属于独立真实探针，不纳入上述模拟测试数量，也未独立查询实际账单。若这 25 输入全为缓存未命中，按上述价目得到的估算为非高峰 5.55µUSD 或高峰 11.1µUSD；实际需依缓存明细、有效档位及供应商账单对账。
