# 查看与导出生成程序

0.17.4，2026-10-27。在中文工作台选择已提交的开发或修复任务，打开“任务”，点击“查看源码与导出”。本机与受邀公网入口均可使用。

页面展示精确任务、轮次、计划/产物绑定、输入版本、宿主终验汇总及源码。源码以纯文本显示；读取或下载都不调用模型、不运行程序、不改变任务状态。

- “下载 main.py”：导出原始 UTF-8 源码字节，每次下载前重新读取并核对版本。
- “下载版本记录 JSON”：导出 `loom.source-export/v1` 记录，包含精确 subject、输入摘要、终验汇总与文件 SHA256，不包含隐藏用例正文、标准输出或诊断。
- 记录中的 `file_sha256` 是下载文件原始字节的 SHA256；`subject.source_sha256` 是协议规范化 `{"main.py":源码}` 的摘要，两者用途不同。
- 下载时发现版本变化、权限/读取失败或摘要不一致，会清空旧正文并禁用导出；先重新读取、审阅。切换运行或断开会清理源码；切换标签页会取消尚未完成的导出。

具有该租户读取权限的只读身份也可查看和导出，不能因此批准计划或控制运行。版本记录固定保留未独立验证状态；宿主测试通过和源码下载都不代替独立模型验收。

## 离线界面测试

下面的样例使用明确的 HTTP 模型替身和沙箱替身，不读取项目密钥、不产生真实模型费用、不修改开发/公网数据库。每次使用新的私有目录，默认回环端口 8804。

```powershell
..\..\work\loom-venv\Scripts\python.exe scripts/serve_source_fixture.py --work-root ..\..\work\source-ui-check-2026-10-27
```

另一个终端在源码目录执行：

```powershell
npx --yes --package @playwright/cli playwright-cli -s=source-ui open http://127.0.0.1:8804/console
npx --yes --package @playwright/cli playwright-cli -s=source-ui snapshot
npx --yes --package @playwright/cli playwright-cli -s=source-ui run-code --filename scripts/check_source_panel.js
```

覆盖 Unicode/标记转义、源码与上下文摘要、读取失败、证据变化、迟到响应、隐藏标签页中的在途导出、只读身份和断开清理。下载样例保存在 `output/playwright`，测试产物不打入源码包。当前真实模型产物的导出另外通过本机与公网浏览器验收，没有重新生成代码。
