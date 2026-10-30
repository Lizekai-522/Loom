# Loom 0.22.0 审查与修复报告

发现0.21.0独立复核器直接从源码根目录读取packs/software-dev/workflow-v1.json；pyproject只打包web文件，正常wheel安装缺少资源，源码可运行不能代表安装包可运行。私有隔离安装复现记录为Loom_0.21.0_wheel_resource_probe_2026-10-30.json，template_resource_available=false，真实模型调用0。CLI示例也依赖工作目录。

修复为importlib.resources读取固定包内JSON，wheel显式包含资源，CLI默认示例也使用包内清单。用户指定的fixture路径仍保持显式行为。内置资源与原packs清单做语义一致性验证，fixture身份不变。

同时补充领域资产的实际加载和版本绑定。审查约束包括：版本不可覆盖、租户隔离、发布与审批分离、代码拥有的权限上限、审计来源重建、冻结TaskSpec和旧计划摘要保持。新包不得携带外部代码、URL、运行专属复核及终验引用。发布追加到现有Goal，不额外占公网Goal数量，也不改变其任务/预算。公网只读，不开放发布操作。

领域包与Agent热注册不是同一功能，本轮不将新包名视为新执行能力，也不将内容摘要视为FR-057完整适用域签名。完整功能、独立人工裁决与前端重构继续按原计划推进。

完整回归首轮1198主/387子通过，1项失败是test_control_api中/v1/me的旧权限列表未包含本轮新增pack.publish；授权行为与专项符合预期。已更新操作员的精确能力预期，legacy/worker/viewer仍不得发布；原失败XML单独保留为domain-packs-full-initial，不转skip。修正后运行最终完整门槛。

浏览器日志由Windows PowerShell重定向为UTF-16，私有汇总器原先只解码UTF-8而失败；已按BOM识别编码，保留原日志并成功汇总17项浏览器检查。此为证据采集修复，没有修改产品行为。历史内置七角色模板仍是需补运行绑定的结构模板，该限制在领域包说明中明确，不能当作七角色执行闭环。
