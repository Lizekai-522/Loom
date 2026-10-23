# 本机 Linux Docker 隔离执行

当前实现供本机低并发验证：候选是受限工作区里的 Python 源码；平台在宿主冻结 JSON 输入和期望值，每例启动一个独立容器。模型不提供 shell 命令、Docker 参数或测试结论。程序从 stdin 读取一个 JSON，stdout 必须只包含一个有限 JSON 值，stderr 可输出诊断。

这是有限黑盒功能测试。`passed` 由宿主比较期望值、容器终态和资源限制结果产生；候选打印 `tests.pass=true`、覆盖率或“通过”不会获得信任。期望值从不进入容器。当前不执行任意候选 pytest，也不宣称提供可信覆盖率、完整正确性证明、生产多租户隔离或完整 software-dev 工具链。

## 首次准备

需要已安装的 Docker Desktop 和 Linux daemon。可以先查看状态；服务已运行就不重复启动。原生命令支持有限超时，不要求启用系统功能或重启。[Docker Desktop start 官方说明](https://docs.docker.com/reference/cli/docker/desktop/start/)

```powershell
docker desktop start --help
docker desktop start --timeout 45
docker version
```

只用审核过的 `sandbox/Dockerfile` 构建，构建上下文限定为 `sandbox`，其中没有源码、密钥或宿主工作区复制指令：

```powershell
docker build --pull=false --tag loom-sandbox:local-v1 --iidfile ../../work/loom-sandbox-image.id --file sandbox/Dockerfile sandbox
$image = (Get-Content ../../work/loom-sandbox-image.id -Raw).Trim()
python scripts/check_sandbox.py --image $image --report ../../work/loom-sandbox-live-report.json
```

以上命令在项目根目录执行，用项目虚拟环境的 Python；`../../work` 是当前目录布局下的私有工作目录。其他目录布局可自行指定绝对路径。`--image` 必须显式提供 `sha256:<64位>` 本机构建 ID，或审核过的 `repository@sha256:<64位>`；可变 tag 会被拒绝。执行器不自动 pull、不自动构建、不从待运行镜像自建信任根，也没有失败后退回宿主执行的路径。

基础镜像固定为 `python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530`。2026-10-23 从 [Docker Hub 官方 Python tag 元数据](https://hub.docker.com/v2/repositories/library/python/tags/3.13-slim-bookworm) 核实，该索引更新于 2026-10-22。内联 runner 使用 Dockerfile 的文件 heredoc，离线测试校验其内容摘要与模块常量一致。[Dockerfile 官方说明](https://docs.docker.com/reference/dockerfile/#here-documents)

镜像摘要保证所选字节不漂移；操作人员仍须审核镜像来源及构建过程。runner label 是一致性检查，不是供应链签名，也不能让任意外部镜像自动变可信。Docker 管理权限属于受信宿主边界，不向模型、API 调用者或候选暴露。

## Python 接口

```python
from loom.sandbox import DockerSandbox, FrozenTest, SandboxConfig

# image_id 必须来自操作人员批准的构建，不从模型响应取值。
sandbox = DockerSandbox(SandboxConfig(image=image_id))
sandbox.preflight()
result = sandbox.run_python(
    {"main.py": source_text},
    [FrozenTest("double-21", {"n": 21}, {"value": 42})],
)
# source_text 从 stdin 读入 {"n":21}，stdout 输出 {"value":42}。
print(result.passed)
evidence = result.to_dict()
```

测试必须由平台规则或人工冻结；模型给出的“测试”不能未经审批成为验收标准。输入/期望采用严格 JSON 类型，拒绝 NaN、Infinity、非字符串对象键和重复测试名。比较对值及类型敏感，例如 `true`、`1`、`1.0` 不互相代替。每例只接收当前输入，不接收其他用例或期望值。工作区支持本地模块 import，默认入口是 `main.py`。

`SandboxUnavailable` 表示 Docker/基础设施无法证明所需边界；`SandboxRejected` 表示配置、镜像或输入不符合要求。它们都不能转成“测试通过”。正常返回的 case 状态包括 `passed`、`failed`、`protocol_error`、`runtime_error`、`timeout`、`output_limit`、`memory_limit`。集合中每例通过才有 `SandboxResult.passed=True`。

## 实际边界

执行前检查 Linux daemon、内存/交换/CPU/PID 限制和 seccomp 支持，核对固定镜像 ID、runner 摘要、非 root 用户和入口。创建后再次 inspect 实际隔离配置，通过后才 start。约束包括只读根、UID/GID 10001、无网络、空 capabilities、no-new-privileges、私有 PID/IPC/cgroup、CPU/内存/PID 限额及无宿主挂载。[Docker run 官方参数说明](https://docs.docker.com/reference/cli/docker/container/run/)

默认每例 5 秒执行超时、128 MiB 内存且交换上限与内存相等、0.5 CPU、32 PID；`/workspace` 和 `/tmp` 分别为 16 MiB 的 nosuid/nodev/noexec tmpfs。单个普通文件写入受 1 MiB RLIMIT_FSIZE 限制，运行时总写入受 tmpfs/内存限制；这不等于逐个限制运行时新建文件数量。容器日志驱动为 none，宿主并行排空 stdout/stderr，只保留合计最多 64 KiB；超量即失败并终止该容器。超时含 `docker start --attach` 开销，Docker 控制命令各自另有限时。

输入只接受相对 POSIX `.py` 文件名和内存中的 UTF-8 文本，不接收宿主目录、Path、流或符号链接，不自动读取文件系统。拒绝绝对路径、反斜线、`..`、文件/目录冲突；默认最多 64 文件、每个 256 KiB、合计 1 MiB。宿主生成确定性普通文件 USTAR，通过 stdin 送到固定 runner，runner 再次验证类型和大小后写入 tmpfs。没有 `tar.extractall`、宿主 bind mount、Docker socket、API keys 或 Docker credentials 传递。

runner 在启动候选前替换自身进程并清空环境，仅保留固定的 PATH/LANG/HOME/TMPDIR/PYTHONHASHSEED。Docker 的客户端代理变量显式清空，候选没有父级受信测试进程或容器内期望文件可改。宿主保留源码/测试/runner 摘要和容器镜像 ID、退出码、OOM 状态及输出摘要。这里的“宿主判定”是该本机可信适配器的结果，尚不是外部签名、WORM 锚定或跨机器证明。

单次执行只使用随机生成、带 owner label 的 test-owned 容器名；finally 仅清理该容器，不扫描、不停止共享容器或服务。所有源代码都在容器中执行；宿主仅处理文本、tar、JSON 和 Docker 协议。禁止把这个执行器扩展为任意宿主 subprocess/shell 备用路径。

容器共享 Linux 内核，Docker daemon、宿主、批准镜像和本机平台适配器均属信任边界。内核逃逸防护、生产凭据发放、外网工具代理、任意项目依赖安装、浏览器/原生编译工具和强对抗多租户需要后续单独设计与验证；当前配置默认拒绝这些能力。

## 验证及结果解释

离线测试不需要 daemon；其中固定的宿主传输测试只检查有界 stdout/超时，不执行候选源码。

```powershell
python -m pytest tests/test_sandbox.py -q
python scripts/check_sandbox.py --image $image --report ../../work/loom-sandbox-live-report.json
```

第二条命令强制真实 daemon，执行真实非 root/只读/断网/环境隔离/伪造通过/超时/输出/OOM/PID 探针。缺 daemon、缺批准镜像、任何失败或 skip 都返回非零；没有把 skip 计入通过。直接运行整个 pytest 且未设置 `LOOM_SANDBOX_TEST_IMAGE` 时，这组可选集成用例会跳过，因此不能以全套 pytest 的绿灯替代专用脚本。

本轮完整分阶段检查使用 `check_s2.py`，必须同时指定本机不可变镜像 ID、实际 NATS、原生测试 broker 和隔离 PostgreSQL 配置：

```powershell
$image = (Get-Content ../../work/loom-sandbox-image.id -Raw).Trim()
python scripts/check_s2.py `
  --sandbox-image $image `
  --nats-url nats://127.0.0.1:4223 `
  --nats-server-bin ../../work/loom-infra/nats/nats-server-v2.15.1-windows-amd64/nats-server.exe `
  --postgres-config ../../work/loom-infra/postgres-session.json `
  --report artifacts/local-execution-alpha-validation.json
```

此处的基础设施路径对应当前本机布局；其他机器使用自己的隔离配置。脚本只把私有 PostgreSQL 配置路径交给测试，不在参数或报告中复制密码。`--sandbox-image` 仅接受显式 `sha256:<64位小写十六进制>` 本机镜像 ID，可变 tag 被拒绝；它通过 `LOOM_SANDBOX_TEST_IMAGE` 交给真实 Docker 集成测试。每阶段先删除旧 JUnit，再依次运行本地测试和真实基础设施测试，任一缺失/无效报告、失败、错误或 skip 均不能通过。

报告里程碑为 `0.3.0-local-execution-alpha`：Docker、NATS 和独立 PostgreSQL DDL/RLS 检查使用真实服务，模型网关使用 Mock，没有真实模型调用或费用；真实模型探针报告另存。API/运行时权威存储仍为单实例 SQLite，不能据此声称已接入 PostgreSQL Store 或生产就绪。故障测试只操作它们自行创建的容器或 broker，不停止共享服务。

2026-10-23 本机已实测：离线 17 tests + 64 subtests，通过；真实 Linux Docker 9 tests、0 skipped，通过。真实生成模型调用由上层适配器负责，本脚本不调用模型、不产生模型费用。后续代码修改后应重新运行相应验证，历史通过不是新版本的验收。
