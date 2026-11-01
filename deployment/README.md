# 单机云端受邀体验部署

目标：浏览器直接访问 HTTPS，电脑关机后云端 API、SQLite、执行器、独立复核器继续运行。当前发布仍是受邀共享空间；完整 62 项功能及生产验收另见验收表。

建议 Ubuntu 24.04 x86_64、2 核 4GB、40GB 以上 SSD；2GB 仅作串行低并发试运行，须以目标机器实际验收为准。无需 GPU，模型在 DeepSeek 服务端运行。模型费用、域名和备份不计入主机费。

## 准备

使用专用云主机。先通过发行版或供应商官方渠道安装 Python 3.12/venv、[Docker Engine](https://docs.docker.com/engine/install/ubuntu/)、[Caddy](https://caddyserver.com/docs/install#debian-ubuntu-raspbian)；开放 80/443，SSH 仅限管理来源。禁止对公网开放 Docker、数据库和 8799。DNS 指向服务器公网地址；使用真实 HTTPS 域名，浏览器安全 Cookie 依赖它。

源码解压到 `/opt/loom/source`，私有状态用 `/var/lib/loom`，两者分开。不要把 Windows 私有配置直接复制进源码；其中包含路径、令牌、隐藏用例和签名密钥。旧数据库迁移必须先停止写入、检查未完成请求并做一致备份，保留原签名密钥及审计记录，单独执行迁移验收。本脚本初始化新的体验空间，不迁移或覆盖旧数据。

## 初始化

以下操作在服务器执行，源包中不含任何真实密钥。

```bash
cd /opt/loom/source
python3 -m venv .venv
.venv/bin/python -m pip install 'pip>=26.2.2'
.venv/bin/python -m pip install .
docker build -t loom-sandbox:release sandbox
docker image inspect loom-sandbox:release --format '{{.Id}}'
```

用权限 600 的源码外 JSON 文件配置 `DEEPSEEK_API_KEY`，不要把值作为命令行参数或写入 shell 历史。将上一条实际输出的 `sha256:...` 传给初始化程序，替换域名：

```bash
.venv/bin/python scripts/prepare_cloud.py \
  --work-root /var/lib/loom --origin https://loom.example.com \
  --image sha256:REPLACE_WITH_ACTUAL_IMAGE_ID \
  --secrets-file /root/loom-model-secrets.json
sudo bash deployment/install_services.sh /opt/loom/source /var/lib/loom loom.example.com
```

初始化只生成配置、访问码、加密签名密钥、示例草案和单并发配额，不发起模型调用。已有状态目录拒绝覆盖。安装器遇已有服务拒绝覆盖，不擅自改写全局 Caddy 配置，也不自动开放入口。复核池总上限 $2，单次最多预留 $1.85；生成单项目上限 $1。多人共用空间，预算需由管理员持续观察。

将 `import /etc/caddy/Caddyfile.loom` 加入经审阅的 Caddy 主配置，验证配置后再启动：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl enable --now loom-api loom-executor loom-verification
sudo systemctl reload caddy
```

访问码仅保存在 `/var/lib/loom/loom-public-preview/访问信息.txt`，通过私有渠道交给受邀测试者。

## 上线验收与维护

1. HTTPS 和访问码登录成功；匿名读取项目、跨来源写入及执行器接口均被拒绝。
2. 页面创建完整词频示例，审阅计划，真实 Flash 生成，Docker 冻结用例全部通过，再单独申请 Flash/Pro 双模型复核。检查两份真实签名和精确版本，目标必须由系统判为成功。
3. 公网事件与回放保持脱敏；前端、源包、容器环境均不含模型密钥。实际模型调用失败或意见分歧保留真实结果，不自动重试。
4. `systemctl restart` 三个服务，核对旧目标、审计链和未知预留；不能重发已执行请求。重启云主机、关闭个人电脑，再用移动网络访问并新建一项测试。此项通过才满足不依赖本机。
5. 定期用 SQLite 在线 backup 做一致快照，将签名配置和模型配置加密备份到另一处。源码、数据库与密钥备份分开管理；不得公开 `/var/lib/loom` 或直接复制正在写入的数据库文件。

此单机运行方式沿用现有持久调度，不启用分布式 NATS/PostgreSQL 路径；主机故障会中断服务，当前也不提供高可用、多租户账号和完整灾备。目标云主机验收尚未完成时，不能把本机测试写成云端上线通过。
