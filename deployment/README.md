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

用权限 600 的源码外 JSON 文件配置 `DEEPSEEK_API_KEY` 和 `DASHSCOPE_API_KEY`（阿里云百炼北京地域的 API Key，用于独立复核的第二个模型），不要把值作为命令行参数或写入 shell 历史。缺少 `DASHSCOPE_API_KEY` 时可以初始化，但独立复核会显示未就绪，自动会话停在复核前，不会把未复核的结果当作通过。将上一条实际输出的 `sha256:...` 传给初始化程序，替换域名：

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

`/var/lib/loom/loom-public-preview/访问信息.txt` 里的访问码是管理员本人的（身份 `preview-user`），不要转给别人。每位团队成员单独开通：

```bash
.venv/bin/python scripts/manage_preview_members.py add --work-root /var/lib/loom --subject xiaoming --display 小明
sudo systemctl restart loom-api loom-executor
```

访问码只写入 `loom-public-preview/invites/xiaoming.txt`（权限 600），不打印到终端，通过私有渠道发给本人。`rotate --subject` 换码、`remove --subject` 移除成员都立即生效（旧码和已登录的浏览器同时失效），历史记录保留用于审计；`list` 只列名字。新增成员需要重启 API 与执行器，因为它们在启动时读取身份文件。

## 上线验收与维护

1. HTTPS 和访问码登录成功；匿名读取项目、跨来源写入及执行器接口均被拒绝。
2. 页面创建完整词频示例，审阅计划，真实 Flash 生成，Docker 冻结用例全部通过，再单独申请 DeepSeek Flash + 阿里云千问 `qwen3.8-max-0902` 双模型复核。检查两份真实签名和精确版本，目标必须由系统判为成功。
3. 公网事件与回放保持脱敏；前端、源包、容器环境均不含模型密钥。实际模型调用失败或意见分歧保留真实结果，不自动重试。
4. `systemctl restart` 三个服务，核对旧目标、审计链和未知预留；不能重发已执行请求。重启云主机、关闭个人电脑，再用移动网络访问并新建一项测试。此项通过才满足不依赖本机。
5. 定期用 SQLite 在线 backup 做一致快照，将签名配置和模型配置加密备份到另一处。源码、数据库与密钥备份分开管理；不得公开 `/var/lib/loom` 或直接复制正在写入的数据库文件。

此单机运行方式沿用现有持久调度，不启用分布式 NATS/PostgreSQL 路径；主机故障会中断服务，当前也不提供高可用、多租户账号和完整灾备。目标云主机验收尚未完成时，不能把本机测试写成云端上线通过。

## 域名邮箱（0.34.0）

助手可以接一个自有域名邮箱（目前支持阿里云企业邮箱，`provider` 取 `aliyun-qiye` 或香港区域的 `aliyun-qiye-hk`）。邮件收发由执行器完成，只连服务商固定的 `imap(hk).qiye.aliyun.com:993` 与 `smtp(hk).qiye.aliyun.com:465`；不需要服务器或公网 IP，电脑关机期间邮件留在服务商处，开机后按游标补收。

1. 开通邮箱、在 7 天内完成域名解析（MX、SPF，可配 DKIM 与 DMARC），在管理后台为业务账号放行三方客户端并开启 IMAP/SMTP，用该账号生成“三方客户端安全密码”。不要使用 postmaster 管理员账号。
2. 新建私有文件 `mail-secrets.json`（权限 600，放在执行器配置旁、源码外）：`{"MAIL_PASSWORD": "三方客户端安全密码"}`。
3. 在执行器私有配置加入：

```json
"mail": {"provider": "aliyun-qiye", "address": "agent@你的域名", "subject": "你在身份文件里的用户名",
         "owner_addresses": ["你本人的邮箱"], "secrets_file": "mail-secrets.json", "send": false}
```

`subject` 必须是身份文件里唯一的具名用户（user 角色），邮件进入这个人的助手对话。首次只同步最近 7 天、最多 100 封，标为历史邮件，不自动处理；之后的新邮件自动摘要，回复草稿要你点“生成回复”。`send` 默认关闭，此时只收不发，草稿可复制到网页版发送；打开后，草稿需你批准当前版本才会提交。提交中断线会标为“发送结果待核对”，不会重发，请到网页版“已发送”按 Message-ID 核对。改了密码文件后在“邮件”页签点“立即检查新邮件”即可。

## 独立复核的第二个模型（0.33.0）

独立复核由两家厂商的模型分别审查：DeepSeek Flash 和阿里云百炼的千问 `qwen3.8-max-0902`（固定快照，关闭思考模式）。生成流水线里的审查角色仍用 DeepSeek V4 Pro，不受影响。

- 模型配置：`configs/models.dual.example.json` 已含千问路由，地址只接受北京地域的 `https://dashscope.aliyuncs.com/compatible-mode/v1` 或业务空间专属域名 `https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。北京与新加坡的 Key 不通用。
- 密钥：复核器的模型密钥文件里加一行 `"DASHSCOPE_API_KEY": "…"`，和 `DEEPSEEK_API_KEY` 放在同一个私有文件即可（文件仍按该模型配置里出现的键名严格读取）。
- 签名密钥：复核配置要求 `deepseek-flash` 和 `qwen3.8-max-0902` 两把签名密钥。已有的复核配置（Flash + V4 Pro）升级后无法加载，需要用 `scripts/configure_verification.py` 在一个新的私有目录重新生成，再把执行器配置的 `verification_config` 指向新文件。旧的 Flash + V4 Pro 复核记录仍可读取和校验。
- 计费：千问按北京标价每百万 Token 输入 12 元、输出 36 元，按 6.5 元兑 1 美元折算成预留上限，宁可多留不少留；按两万 Token 左右的复核输入估算，千问一次约 0.3 元；每次预留上限约 0.13 美元，仍受复核池上限约束。
- 模型身份：响应里的模型名必须与请求一致，否则调用失败。服务商若原样回显请求的模型名，客户端仍无法证明实际运行的是哪个模型；换成另一家厂商后，至少两份复核不再出自同一服务商。

## 常驻助手与搜索（0.31.0）

常驻助手的对话通道、后台调研、巡检和每日简报运行在执行器进程里（`scripts/run_local_executor.py --watch`），与已批准请求的处理循环并行；API 进程只存消息并推送事件，不持有模型或搜索密钥。部署到云主机后，电脑关机不影响后台工作，回到页面会从事件流续上。

联网调研需要在执行器私有配置里加入可选的 `search` 段。搜索密钥单独放在一个私有 JSON 文件里（只含这一个键，如 `{"BOCHA_API_KEY": "…"}`），用 `secrets_file` 指向它（相对路径以执行器配置所在目录为准）；不要写进模型密钥文件——那个文件被多个工具按“只允许模型密钥名”严格读取，多一个键会让它们全部拒绝加载：

```json
"search": {"provider": "bocha", "key_env": "BOCHA_API_KEY", "secrets_file": "search-secrets.json", "max_results": 5}
```

`provider` 可选 `bocha`、`tavily`、`searxng`（自建，需 `base_url`）或 `none`。未配置时助手只读取你在消息里给出的 https 链接。网页抓取只访问 https 公网地址，拒绝 IP 字面量与解析到内网/保留地址的域名，每次跳转重新检查，单页上限 1.5MB、10 秒。提醒只在浏览器本机显示（页面在后台时的系统通知），不使用外部推送服务。

## 内容规则与脱敏（0.32.0）

助手内置一组内容规则：色情与性暗示（含任何涉及未成年人的性内容）、违法或伤害他人的请求直接拒绝，不调用模型；流露自我伤害念头时给出温和回应和心理援助热线 12356；涉政话题照常回答，但附加“只陈述公开事实、不表态”的指令。命中记录写入事件流和租户台账，只含类别、处理方式、规则编号和阶段，不含原文。

运维方可以在执行器私有配置里加一个词库文件，和其他私有文件放在一起（相对路径以执行器配置所在目录为准），不要放进源码或公开目录：

```json
"safety": {"lexicon_file": "safety-lexicon.json"}
```

```json
{"schema_version": "loom.safety-lexicon/v1",
 "political_action": "neutral",
 "sexual": ["词条"], "unethical": ["re:正则"], "self_harm": [], "political": [],
 "allow": ["放行短语"], "builtin": true}
```

普通词条忽略大小写、全半角和中间的空格或符号；`re:` 开头的是正则。`political_action` 设为 `refuse` 时涉政话题直接拒答；`builtin: false` 只用你的词条。执行器启动时校验词库，格式不对拒绝启动；运行中修改会自动重新加载，改坏时保留上一版有效规则。

脱敏不需要配置：手机号、邮箱、身份证号（校验位正确）、银行卡号（Luhn 校验通过）以及常见密钥、令牌、密码赋值，在发给模型或搜索服务之前、写入数据库/事件流/内核账本之前替换为占位符，读取和导出时再过一遍（覆盖升级前的旧记录）。链接里的路径和参数数字保持原样，只隐藏其中的密钥。

