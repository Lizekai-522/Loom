# Loom 0.9 验证记录

2026-10-25，0.9.0，role-handoff-alpha。受限四角色交接本机验收完成：**660主/396子，零失败/跳过；196实际HTTP、33浏览器断言通过**。不是生产完成或七角色完整验收；原0.8记录见[历史](validation-0.8.md)。

| 完整阶段 | 主测试 | 子测试 | 失败 | 跳过 |
|---|---:|---:|---:|---:|
| unit-and-local | 617 | 382 | 0 | 0 |
| real-infrastructure | 43 | 14 | 0 | 0 |

真实基础设施含NATS8、重启1、PG14、原Docker9、repair4、上下文4、新四角色交接3。模型HTTP在此套件均为替身；真实供应商调用另列。专项、完整和重启后的重复检查不累计为新总数。

实际HTTP9组196：API9、Goal12、人工控制31、旧真实任务17、候选31、旧上下文16、旧角色24、单tester23、新四角色33。旧163项对应相同API/执行核心，新增33项核对新真实链；无重复付费。浏览器重新验证角色/权限17项和数值精度/兼容16项，模型/沙箱均为fixture替身，实际服务资产hash与主API一致。

## 四角色真实执行

run 58614923-b31f-43a7-90ea-897703443390，analyst→architect→tester→developer实际4Flash响应、5host冻结Docker用例通过。4901tokens/2440µUSD上界记账，预留0，未核对账单。六个精确输入grant、1/2/3项上下文均核对；三类候选绑定来源，developer使用原host终验摘要，生成checks未执行。

四任务均submitted/未独立验证，review硬依赖保持pending，Goal未成功，第二模型待配置。另有本版单tester1响应623tokens/326µUSD上界，未重复调用。版本0.9真实调用共5次，和上一轮0.8基线3次/5用例分开，不能将其算作同一次全链。

## 修复和恢复

修复非CSV任务误套CSV指令。浏览器还复现较大整数被JSON.parse/JSON.stringify舍入的问题；现保留响应数字原文用于候选和上下文纯文本展示，元数据类型与批准引用不变。16项新浏览器检查涵盖大正负整数、宽整数、float/bool/null、字符串/HTML、精确批准、缺JSON.rawJSON或parse source时停止显示/批准，不能用舍入后的数值审阅。

Docker原系统socket目录普通/管理员改名仍失败。采用仅对Docker进程覆盖LOCALAPPDATA的本机缓存，wsl junction仍指向原数据盘；原设置hash、镜像/runner保持，未删镜像/卷或升级。已真实启动、正常停止、再用scripts/start_local_docker.ps1启动并通过3项真实Docker交接复测。原系统目录问题仍保留；本机请用项目缓存入口，不声称普通快捷方式已修好。

完整原方案SHA不变、OpenAPI仍24路径，源码/安装/凭据与归档校验另列。此前阻塞报告和数值显示失败证据保留，没有转skip。API仍单实例SQLite，PG是独立约束/RLS检查；生产权限/恢复、完整PG Store、收益对照、完整七角色及独立模型尚未完成。[下一步](implementation-plan.md)
