param([string]$WorkRoot=(Join-Path $PSScriptRoot '../../../work'))
$ErrorActionPreference='Stop'
$loomRepo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$loomWork=[IO.Path]::GetFullPath($WorkRoot)
$loomPython=Join-Path $loomWork 'loom-venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $loomPython)) { throw '请先按 docs/local-dispatch.md 创建项目虚拟环境。' }
& $loomPython (Join-Path $PSScriptRoot 'bootstrap_local.py') --work-root $loomWork
if ($LASTEXITCODE -ne 0) { throw '本机配置准备失败。' }
$loomSession=Get-Content -LiteralPath (Join-Path $loomWork 'loom-first-round-session.json') -Raw | ConvertFrom-Json
$loomVersion=(& $loomPython -c 'from loom import __version__;print(__version__)').Trim()
$loomHealth=$null
try { $loomHealth=Invoke-RestMethod -Uri ('http://127.0.0.1:{0}/health' -f $loomSession.port) -TimeoutSec 2 } catch { }
if ($loomHealth -and $loomHealth.version -ne $loomVersion) { throw '当前端口仍运行旧版本，请先停止旧开发服务，再启动本版本。' }
if (-not $loomHealth) {
  $loomArgs='"{0}" --work-root "{1}"' -f (Join-Path $PSScriptRoot 'serve_local.py'),$loomWork
  $null=Start-Process -FilePath $loomPython -ArgumentList $loomArgs -WorkingDirectory $loomRepo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $loomWork 'loom-api.log') -RedirectStandardError (Join-Path $loomWork 'loom-api-error.log') -PassThru
  for ($loomAttempt=0;$loomAttempt -lt 20;$loomAttempt++) { try { $loomHealth=Invoke-RestMethod -Uri ('http://127.0.0.1:{0}/health' -f $loomSession.port) -TimeoutSec 1;break } catch { Start-Sleep -Milliseconds 250 } }
  if (-not $loomHealth) { throw '工作台未启动，请查看 work/loom-api-error.log。' }
}
$loomWorkerInfo=Join-Path $loomWork 'loom-local-executor-session.json'
$loomWorkerAlive=$false
if (Test-Path -LiteralPath $loomWorkerInfo) {
  $loomWorkerRecord=Get-Content -LiteralPath $loomWorkerInfo -Raw | ConvertFrom-Json
  $loomWorkerProcess=Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $loomWorkerRecord.process_id) -ErrorAction SilentlyContinue
  $loomWorkerAlive=$loomWorkerProcess -and $loomWorkerProcess.CommandLine -like ('*'+(Join-Path $PSScriptRoot 'run_local_executor.py')+'*') -and $loomWorkerRecord.version -eq $loomVersion
}
if (-not $loomWorkerAlive) {
  $loomWorkerArgs='"{0}" --db "{1}" --config "{2}" --watch' -f (Join-Path $PSScriptRoot 'run_local_executor.py'),$loomSession.database,(Join-Path $loomWork 'loom-private/local-executor.json')
  $loomWorker=Start-Process -FilePath $loomPython -ArgumentList $loomWorkerArgs -WorkingDirectory $loomRepo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $loomWork 'loom-worker.log') -RedirectStandardError (Join-Path $loomWork 'loom-worker-error.log') -PassThru
  @{process_id=$loomWorker.Id;version=$loomVersion} | ConvertTo-Json | Set-Content -LiteralPath $loomWorkerInfo -Encoding UTF8
}
Write-Output ('工作台：http://127.0.0.1:{0}/console；已启动本机执行器。模型仅在显式请求后调用。' -f $loomSession.port)
