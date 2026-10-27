# S2 本地签名检查点

本模块为 `Store.events()` 的确定快照产生真实的 Ed25519 签名，并以**显式配置的外部公钥白名单**验证。验证后再将签过的租户、run、chain、事件数和头摘要与事件快照逐项比较。它解决了“攻击者改写整个 SQLite 事件表并重新计算全部 hash 后，原有自检仍然通过”的缺口。

当前交付为本地签名和离线验证组件；没有开放 HTTP 签名、私钥管理或任意密钥上传接口。模块不调用模型、不执行 Agent 工具、不自动签署待验证链，也不把事件里的 `authenticated=true` 当验签结果。

## 可信边界

需要独立保护的材料有三类：

1. **签名私钥**：由审计签名角色保管，使用加密 PKCS8 PEM。数据库进程、Worker 和模型不能读取。密码从交互输入或独立秘密管理系统获取，不写入代码、命令行参数、事件或日志。
2. **公钥白名单**：由验证方经独立渠道核验并固定，`key_id` 映射到确切 Ed25519 公钥及签名时间窗口。检查点不能附带新公钥让验证器自动信任。
3. **已接受的高水位检查点**：验证方在数据库目录之外保留，后续候选必须沿该事件前缀向前延伸，不能只凭一个仍有效的旧签名回滚历史。

目录分离是路径防误用检查，**不是访问控制的替代品**。同一机器、同一账号若能同时改 DB、私钥、公钥白名单和高水位文件，就不具有独立审计安全边界。生产应使用不同服务身份、权限和故障域；Windows 需实际配置 NTFS ACL，不能依赖 `chmod(0600)` 表示完整 Windows 权限隔离。

本地签名文件不等于外部 WORM 锚定或 HSM。攻击者取得私钥或改动公钥白名单后，签名自身不能证明未被伪造；攻击者同时回滚高水位文件与 DB 时，本地文件协议也不能证明全球最新状态。独立远端只追加锚定、可信单调服务与密钥托管仍为后续生产工作。

## 文件布局

调用方必须明确传入每个路径，没有从 DB 自动寻找密钥的默认逻辑。例如：

```text
C:\LoomData\database\loom.db
C:\LoomSigner\keys\audit-2026-10-private.pem
C:\LoomVerifier\keys\audit-2026-10-public.pem
C:\LoomAudit\snapshots\run-<id>-seq-<n>-<time>.json
C:\LoomVerifier\high-water\tenant-<tenant>\run-<id>.json
```

私钥、公钥输出、检查点、高水位文件不能放在数据库目录或其子目录。导出还要求私钥和检查点处于不同目录。现有私钥、公钥输出与快照文件不会被覆盖；每次导出用新文件名。

这些路径是布局示例，并未由文档创建。生产高水位文件还必须限制 DB/Worker 写入；备份它时不能把信任状态和数据库一起不加判断地恢复到旧版本。

## 检查点格式

所有字段均纳入签名，只有 `signature` 本身除外：

| 字段 | 规则 |
|---|---|
| `schema` | 固定 `loom.checkpoint/v1`，未知版本拒绝 |
| `algorithm` | 固定 `Ed25519`，不从输入选择其他算法 |
| `key_id` | 查显式公钥白名单，也作为被签内容 |
| `tenant_id / run_id / chain_id` | 与调用方提供的预期身份完全相同 |
| `seq / event_count` | 正整数且相等；v1 要求完整链从 seq=1 开始 |
| `head_hash` | 签过的最后事件摘要，64 位小写十六进制 |
| `created_at / expires_at` | UTC、6 位小数、`Z` 结尾；到期必须晚于签署时间 |
| `signature` | 64 字节 Ed25519 签名的严格 Base64 字符串 |

签名字节为 `b"loom:checkpoint:v1\x00"` 加现有 `Store.canonical()` 的 UTF-8 JSON 字节。使用字段排序、无空白分隔和禁止 NaN/Infinity 的版本化编码；未知字段、重复 JSON 字段与嵌入公钥均拒绝。该格式是 Loom v1 本地格式，不宣称自动与其他 JSON 签名规范互通。

事件校验独立检查当前 v1 完整信封字段、归属、事件 ID 唯一、seq 连续、前序摘要、规范编码摘要和原始时间类型。**事件自洽只是必要条件；最终头与数量必须匹配已经验过独立签名的检查点。**

## Python API

主要入口位于 `loom.checkpoints`：

```text
generate_keypair(private_key_path, public_key_path, *, password, database_path)
load_public_key(path)
load_checkpoint(path)                 # 只读 JSON，不会认证或建立信任

TrustedKey(
    key_id, public_key,
    signing_not_before=None, signing_not_after=None, revoked=False,
)

export_checkpoint(
    store, tenant, run_id, *, private_key_path, checkpoint_path,
    key_id, password, now=None, valid_for=timedelta(days=1),
    previous_checkpoint=None, trusted_keys=None,
)

verify_checkpoint(
    checkpoint, events, *, trusted_keys,
    expected_tenant, expected_run, expected_chain,
    previous_checkpoint=None, now=None, max_age=timedelta(days=1),
    clock_skew=timedelta(seconds=60), allow_unanchored_tail=False,
)

advance_trusted_checkpoint(
    checkpoint, events, *, trusted_checkpoint_path, database_path,
    trusted_keys, expected_tenant, expected_run, expected_chain,
    now=None, max_age=timedelta(days=1), clock_skew=timedelta(seconds=60),
    initialize=False,
)
```

`verify_checkpoint()` 返回不可变 `VerifiedCheckpoint`，含已认证 seq/count/head、所观察事件数及 `unanchored_tail_count`。它始终真实验签；不会因为调用方传入一个来源标签而跳过。

`now` 用于受控测试或已校准验证服务时钟；默认使用 UTC 当前时间。默认检查点有效期和验证方最大年龄均为一天，验证方可收紧。过期判断不允许调用方用更大的 `max_age` 绕过签过的 `expires_at`。允许 60 秒未来时钟偏差，超过即拒绝；生产需监测签名与验证服务时钟。

### 首次生成密钥的独立脚本示例

下面是由签名管理员运行的示例代码，不是服务自动启动步骤。路径替换为实际布局；数据库应已存在。第一次建立公钥信任前应通过独立渠道核对公钥或其指纹。

```python
from getpass import getpass
from pathlib import Path
from loom.checkpoints import generate_keypair

database = Path(r"C:\LoomData\database\loom.db")
private = Path(r"C:\LoomSigner\keys\audit-2026-10-private.pem")
public = Path(r"C:\LoomVerifier\keys\audit-2026-10-public.pem")
password = getpass("New signing-key password: ").encode("utf-8")
generate_keypair(private, public, password=password, database_path=database)
```

不要在 Worker、API 请求处理、模型输出处理或普通回放中调用密钥生成。测试口令只存在于测试临时目录和测试代码中，不是部署默认口令。

### 明确签署一个快照

首次导出是签名管理员对当前历史的主动认证，不能证明首次签名前未发生过改写。后续导出必须传入验证方保留的上一份已接受检查点和经过独立配置的公钥白名单，校验不能改写既有前缀。

```python
from getpass import getpass
from pathlib import Path
from loom.store import Store
from loom.checkpoints import (
    TrustedKey, export_checkpoint, load_checkpoint, load_public_key,
)

database = Path(r"C:\LoomData\database\loom.db")
store = Store(database)
tenant, run_id = "tenant-a", "<existing-run-id>"
keys = {
    "audit-2026-10": TrustedKey(
        "audit-2026-10",
        load_public_key(r"C:\LoomVerifier\keys\audit-2026-10-public.pem"),
    ),
}

# 常规续签：文件丢失时停下核对，不自动用当前 DB 替代旧根。
previous = load_checkpoint(r"C:\LoomVerifier\high-water\tenant-a\run-id.json")
signed = export_checkpoint(
    store, tenant, run_id,
    private_key_path=r"C:\LoomSigner\keys\audit-2026-10-private.pem",
    checkpoint_path=r"C:\LoomAudit\snapshots\run-id-seq-next-unique.json",
    key_id="audit-2026-10",
    password=getpass("Signing-key password: ").encode("utf-8"),
    previous_checkpoint=previous,
    trusted_keys=keys,
)
print(signed["seq"], signed["head_hash"])  # 只输出公共元数据
```

**仅在首次经独立审查建立根时**，签名管理员可以不传 `previous_checkpoint`。不要写成“高水位文件不存在就自动设为 None”；缺失也可能表示存储故障或信任状态被删除。导出的签名文件也不自动成为验证方接受的最新根。

`Store` 的容量参数必须与创建该数据库时一致；若使用非默认值，调用示例中的 `Store(...)` 需传同样参数。模块也可由可信适配器提供相同 `path/events()` 接口。

### 只用公钥验证并推进高水位

验证方不需要私钥。先配置好 allowlist，读取外部签名文件和要比对的快照。以下示例是正常更新；首次人工确认接纳时才另行显式传 `initialize=True`。

```python
from pathlib import Path
from loom.store import Store
from loom.checkpoints import (
    TrustedKey, advance_trusted_checkpoint, load_checkpoint, load_public_key,
)

database = Path(r"C:\LoomData\database\loom.db")
store = Store(database)
tenant, run_id = "tenant-a", "<existing-run-id>"
keys = {"audit-2026-10": TrustedKey(
    "audit-2026-10",
    load_public_key(r"C:\LoomVerifier\keys\audit-2026-10-public.pem"),
)}
checkpoint = load_checkpoint(r"C:\LoomAudit\snapshots\run-id-seq-next-unique.json")
verified = advance_trusted_checkpoint(
    checkpoint, store.events(tenant, run_id),
    trusted_checkpoint_path=r"C:\LoomVerifier\high-water\tenant-a\run-id.json",
    database_path=database, trusted_keys=keys,
    expected_tenant=tenant, expected_run=run_id, expected_chain=run_id,
)
print(verified.seq, verified.head_hash)
```

高水位缺失默认失败。首次 `initialize=True` 仍必须通过真实签名、独立公钥、身份、数量、头和时间检查；它只允许创建文件，不是认证豁免。已有文件禁止再次 initialize。写入采用独占 writer 锁与临时文件替换，更新前在锁内重新读取上一高水位，保证正常并发调用不倒退。

崩溃可能留下 `.lock`；不能无条件自动删锁。确认没有存活写者、核对保留的签名根及当前文件后由管理员处理。单机文件系统/备份可能整体回滚或丢失状态，此时应停止自动推进并核对独立副本；本地文件不是抗恶意回滚或跨主机共识存储。

## 精确快照与增长中的 run

默认 `verify_checkpoint()` 要求当前列表的数量和最后头与签过的快照**完全一致**。签名后 run 又新增事件时，旧检查点不能被误称为认证了新尾部。

只需核对已签前缀时，可显式使用：

```python
verified = verify_checkpoint(
    checkpoint, events, trusted_keys=keys,
    expected_tenant=tenant, expected_run=run_id, expected_chain=run_id,
    previous_checkpoint=previous,
    allow_unanchored_tail=True,
)
assert verified.unanchored_tail_count >= 0
```

该模式仍校验整个观察链是否自洽，但只有 `verified.seq` 之前的前缀有签名认证。`unanchored_tail_count > 0` 必须向操作者展示；新尾部没有独立完整性保证。高水位推进 API 使用精确模式，不能把尚未签过的尾部计为已锚定。

没有 `previous_checkpoint` 的单独验证只证明“受信签名者签过这个快照”，不能证明“这是最新历史”。要防仍未过期的旧签名快照回滚，验证方须保留并传入上一份独立高水位。

## 密钥轮换与撤销

1. 独立生成新密钥并通过管理员渠道将其公钥加入 allowlist；检查点中的 `key_id` 不能触发公钥下载或自动批准。
2. 给旧 `TrustedKey` 设置 `signing_not_after`，给新键设置 `signing_not_before`，每个时间均为带时区 datetime。
3. 新键签署延续原链的检查点；签名方和验证方同时保留旧公钥，以验证已接受的历史检查点并检验前缀连续性。
4. 退休键可用于其允许签名时间内的历史验证；`revoked=True` 则直接拒绝，无论检查点时间。旧键被撤销后，依赖它的历史信任迁移需要独立核对和授权，不能静默丢弃旧根。

上一检查点的有效期到期不应抹去其已经知道的 seq/head：比较历史高水位时仍验签和键窗口，但不把旧记录的到期当成允许降低 seq 的理由。新候选则必须满足当前有效期与最大年龄。新 seq 不能小于旧 seq，签名时间不能早于旧记录，且新链在旧 seq 处必须是原先签过的头；即使签名者后来又签了改写后的整条链，保留的高水位也会拒绝该分叉。

## 已覆盖的测试与未完成事项

```powershell
python -m pytest tests/test_checkpoints.py -q
```

定向套件包含真实 Ed25519 签名/验签、字段修改、错误/未知/撤销公钥、签名时间窗口、到期/未来/最大年龄、跨租户/run复制、截断、自洽整链改写、未签尾部、旧签名回滚、新签名改写旧前缀、轮换、显式初始化、文件锁、无覆盖写、路径隔离、加密私钥口令以及异常 JSON/事件格式。

尚未完成：外部 WORM/远端锚定服务、HSM/KMS、跨进程权限部署验证、不可回滚的全局高水位服务、生产签名调度及恢复 Runbook 演练。当前组件只认证事件快照；不替代产物/模型验证签名，不证明事件载荷中的业务结论为真。

## 依赖来源

使用 `cryptography==50.0.2`（官方 PyPI 发布于 2026-09-30），由项目统一依赖锁固定；不复制第三方 Ed25519 实现代码。签名、验签及加密 PEM 序列化调用官方 API。

- [cryptography 官方 PyPI 发行页](https://pypi.org/project/cryptography/50.0.2/)
- [官方 Ed25519 文档](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/)
- [官方密钥序列化文档](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/)
