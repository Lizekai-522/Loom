# 本机具名身份与角色边界

更新日期：2026-10-23。本模块为本机受控开发增加具名主体和动作授权，代码位于 `src/loom/auth.py`。它不是 OIDC、账号注册/密码服务或经过生产验收的人员身份系统；同一个 Windows 用户管理多个 token，也不证明对应多个独立人员。

## 角色与动作

| 角色 | read | goal.write | plan.approve | task.claim | task.submit | control |
|---|---|---|---|---|---|---|
| operator | 允许 | 允许 | 允许 | 拒绝 | 拒绝 | 允许 |
| worker | 允许 | 拒绝 | 拒绝 | 允许 | 允许 | 拒绝 |
| viewer | 允许 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| developer（旧租户 key） | 允许 | 允许 | 允许 | 允许 | 允许 | 拒绝 |

`read` 仍受租户边界限制，不授予跨租户资源访问。`control` 用于受控暂停、恢复、终止及未知调用核对；它不授予模型验证签名或把候选标为成功的权限。operator 不能通过普通 Worker 入口认领/提交任务，worker 不能批准计划或处理人工控制。

新具名身份禁止同时拥有 operator 和 worker；可以与 viewer 组合，但 viewer 不增加动作。旧 `tenant_keys` 配置只为已有本地 fixture API 保持兼容，映射为 `legacy:<tenant>` / developer。它保留原来的开发读写权限，却不能获得新 control 权限，不能据此声称生产职责隔离已经完成。

相同租户中的 subject_id 只能登记一次，不能以两条记录给同一主体分别叠加 operator/worker。不同租户可以使用相同 subject_id，但 token 必须在全部具名及 legacy 身份间全局唯一。

## 私有配置

`load_identities(path)` 读取的文件必须位于项目源码及整个公开 `outputs` 目录之外。建议与项目专用私有配置放在同一个受保护的 work 私有目录；不要复制到 configs、报告、截图、交付包或版本库。此处仅展示结构占位，不包含实际凭据：

```json
{
  "schema_version": "loom.local-identities/v1",
  "identities": [
    {
      "tenant_id": "local-test",
      "subject_id": "operator-local",
      "roles": ["operator"],
      "token": "<replace-with-a-long-random-operator-token>"
    },
    {
      "tenant_id": "local-test",
      "subject_id": "worker-local",
      "roles": ["worker"],
      "token": "<replace-with-a-different-long-random-worker-token>"
    }
  ]
}
```

具名 token 为 32–4096 个可见 ASCII 字符，应由安全随机数生成器生成，不使用文档占位值。tenant_id / subject_id 限 1–128 个 ASCII 字符，首字符为字母或数字，其余允许字母、数字、`_.:@-`。旧开发 key 保留至少 16 个可见 ASCII 字符的兼容要求。

文件上限 64KiB，最多 128 条身份；未知/缺失字段、重复 JSON 字段、重复角色、未知角色、非法 token、重复主体或重复 token 都拒绝。具名身份和 legacy 合并后的总数同样最多 128。读取不修改 os.environ，不自动向模型、子进程或浏览器提供任何身份凭据。

Windows 文件 ACL 由本机操作器或部署启动器保护，本模块不自动扩大权限。POSIX 文件有任何 group/other 权限时拒绝读取。直接 symlink、公开目录、目录代替文件、不存在文件及超限文件均拒绝。静态诊断统一为 `LOCAL_AUTH_CONFIG_INVALID`，不把文件路径、原始 JSON 或 token 复制进错误消息。

## 使用接口

```python
from loom.auth import AuthRegistry, load_identities

identities = load_identities(private_identity_file)
registry = AuthRegistry(existing_tenant_keys, identities)
principal = registry.authenticate(incoming_bearer_token)
if principal is None:
    # HTTP 适配器返回 401，不回显 token。
    raise PermissionError("authentication required")
if not registry.authorize(principal, "control"):
    # HTTP 适配器返回 403，不执行动作。
    raise PermissionError("action not permitted")
```

Principal 是不可变的 tenant_id、subject_id 和 frozenset 角色，不包含 token。LocalIdentity 的默认 repr 隐去 token；AuthRegistry 的 repr 固定脱敏。`capabilities(principal)` 只返回该 registry 实际登记的主体权限，角色或租户被替换的 Principal 不取得权限。未经认证的原始请求对象不得直接构造受信 Principal；同进程任意 Python 代码仍处于受信边界之内。

测试使用虚构 token 和临时文件，不读取本机实际私密文件、不连接网络。它们覆盖角色矩阵、越租户/提权拒绝、兼容 key 不获 control、身份与 token 唯一、只读并发、输入限制、路径/权限及错误脱敏。HTTP 资源级授权和每个动作的业务前置条件还需由 API/服务层验证，认证模块本身不放宽计划、预算、claim 或独立验证门禁。


## 0.12审批API补记（2026-10-25）

公开检查套件新增精确预览GET、具名operator冻结POST和suite读取GET，OpenAPI27路径。suite.approve仅operator，不能由Worker/viewer/legacy或正文actor获取；冻结绑定精确review digest/选择及幂等头，计划/任务/费用不变。新增20项API测试，完整746主/396子零失败/跳过；独立副本39项实际HTTP和现有UI17项Chromium通过，无新付费。旧权限快照/HTTP路径夹具失败保留并已复测。

下一步新草案suite来源引用与执行前核对，再真实development/repair和工作台；当前冻结不授执行权，生成case未执行、第二模型待配置、Goal未成功。原62项及部署日期继续追踪。


## 0.15用户要求的本机免输入模式

主启动器使用--local-console，并从私有身份配置选择唯一operator。页面自动进入，不再要求用户复制Bearer；Worker和模型秘密仍留在服务端/CLI的私有配置中。create_app默认不启用该模式，开启时须唯一具名operator；它不是远程匿名API或生产身份服务。同源本机标记不是秘密，其他本机程序可使用它；非回环、跨Origin/端口、转发头和伪造Host被拒绝。显式错误Bearer不能降级成自动operator。

页面身份与原授权矩阵一致，不能凭operator取得Worker权限、生成签名或跳过已批准计划。正式部署须关闭开发便利模式并使用后续生产身份方案。原文中的手动浏览器登录步骤由本节取代，Bearer接口兼容保留供受控工具使用。
