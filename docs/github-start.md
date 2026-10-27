# 从公开源码首次启动

当前版本0.17.4，Windows / Python3.13.12实测。源码不附带模型密钥、访问码、身份文件、数据库或运行日志。下面先启动中文工作台；真实执行须另配项目专用模型和Docker。

## 安装

在一个新工作目录中执行，保持`outputs/Loom`与`work`平级。后者必须在公开源码目录及整个`outputs`之外：

```powershell
git clone https://github.com/Lizekai-522/Loom.git outputs/Loom
Set-Location outputs/Loom
python -m venv ../../work/loom-venv
../../work/loom-venv/Scripts/python.exe -m pip install -r requirements.lock
../../work/loom-venv/Scripts/python.exe -m pip install --no-deps -e .
../../work/loom-venv/Scripts/python.exe scripts/check_spec.py
./scripts/start_local_dev.ps1
```

打开<http://127.0.0.1:8796/console>。启动器仅补建缺少的本机随机身份和配置，不覆盖已有文件；不在页面要求复制凭据。API和执行器只在本机运行，模型调用须逐项显式提交。

## 首轮检查

1. 确认中文工作台自动连接，可创建目标草案、查看或导入计划；草案创建不调用模型。
2. 查看“推进指引”和“人工待办”。缺少模型或宿主用例时先完成配置，不把待配置状态视为执行成功。
3. 执行单元测试：`../../work/loom-venv/Scripts/python.exe -m pytest tests --ignore=tests/integration -q`。该命令不调用真实模型；真实基础设施测试按[README](../README.md)另配。
4. 确需真实执行时，按[模型与沙箱](local-execution.md)、[工作台执行器](local-dispatch.md)设置源码之外的私有文件，构建并登记不可变Docker镜像及冻结宿主用例，再重启服务和执行器。
5. 批准计划与精确输入后，逐项核对预算并提交。已提交的开发/修复产物可[查看和导出源码](source-exports.md)。第二模型尚待配置，宿主测试通过仍不表示独立验收完成。

公网体验按[独立受邀入口说明](public-preview.md)启动，只转发8799专用体验服务；当前维护者的访问码和模型配置不在仓库中。GitHub托管源码本身不会运行后端服务。
