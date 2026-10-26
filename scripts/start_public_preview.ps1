param([string]$WorkRoot=(Join-Path $PSScriptRoot '../../../work'),[switch]$WithoutWorker)
$ErrorActionPreference='Stop'
$loomRepo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$loomWork=[IO.Path]::GetFullPath($WorkRoot)
$loomPython=Join-Path $loomWork 'loom-venv/Scripts/python.exe'
$loomPrivate=Join-Path $loomWork 'loom-public-preview'
$loomTunnel=Join-Path $loomWork 'loom-tunnel/cloudflared.exe'
if (-not (Test-Path -LiteralPath $loomPython)) { throw '未找到项目虚拟环境，请按 docs/public-preview.md 配置。' }
if (-not (Test-Path -LiteralPath $loomTunnel)) { throw '请从 Cloudflare 官方下载 cloudflared 到 work/loom-tunnel/cloudflared.exe，参见 docs/public-preview.md。' }
& $loomPython (Join-Path $PSScriptRoot 'prepare_public_preview.py') --work-root $loomWork
if ($LASTEXITCODE -ne 0) { throw '公网体验配置准备失败，未启动。' }
$loomProcessFile=Join-Path $loomPrivate 'processes.json'
if (Test-Path -LiteralPath $loomProcessFile) {
  $loomExisting=Get-Content -LiteralPath $loomProcessFile -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($loomOld in $loomExisting.processes) {
    $loomLive=Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $loomOld.id) -ErrorAction SilentlyContinue
    if ($loomLive -and $loomLive.CommandLine -and $loomLive.CommandLine.Contains($loomOld.marker)) { throw '已有体验进程仍运行。先查看私有访问信息；如需重启，请执行 stop_public_preview.ps1。' }
  }
}
$loomConfig=Get-Content -LiteralPath (Join-Path $loomPrivate 'access.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (Get-NetTCPConnection -LocalPort $loomConfig.port -State Listen -ErrorAction SilentlyContinue) { throw '公网体验端口被占用，请先核对占用进程。' }
$loomRecords=@()
try {
  $loomApiScript=Join-Path $PSScriptRoot 'serve_public_preview.py'
  $loomApi=Start-Process -FilePath $loomPython -ArgumentList ('"{0}" --work-root "{1}"' -f $loomApiScript,$loomWork) -WorkingDirectory $loomRepo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $loomPrivate 'api.log') -RedirectStandardError (Join-Path $loomPrivate 'api-error.log') -PassThru
  $loomRecords+=@{id=$loomApi.Id;marker=$loomApiScript;kind='api'}
  $loomApiReady=$false
  for ($loomTry=0;$loomTry -lt 20;$loomTry++) { try { $loomResponse=Invoke-WebRequest -UseBasicParsing -Uri ('http://127.0.0.1:{0}/preview/login' -f $loomConfig.port) -Headers @{Host=([Uri]$loomConfig.origin).Host} -TimeoutSec 1; if ($loomResponse.StatusCode -eq 200) {$loomApiReady=$true;break} } catch { }; Start-Sleep -Milliseconds 250 }
  if (-not $loomApiReady) { throw '体验服务未就绪，参见私有 api-error.log。' }
  $loomTunnelArgs='tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:{0}' -f $loomConfig.port
  $loomTunnelProcess=Start-Process -FilePath $loomTunnel -ArgumentList $loomTunnelArgs -WorkingDirectory $loomPrivate -WindowStyle Hidden -RedirectStandardOutput (Join-Path $loomPrivate 'tunnel.log') -RedirectStandardError (Join-Path $loomPrivate 'tunnel-error.log') -PassThru
  $loomRecords+=@{id=$loomTunnelProcess.Id;marker=$loomTunnel;kind='tunnel'}
  $loomOrigin=$null
  for ($loomTry=0;$loomTry -lt 80;$loomTry++) {
    $loomLog=Get-Content -LiteralPath (Join-Path $loomPrivate 'tunnel-error.log') -Raw -ErrorAction SilentlyContinue
    if ($loomLog -match 'https://[a-z0-9-]+\.trycloudflare\.com') { $loomOrigin=$Matches[0];break }
    if ($loomTunnelProcess.HasExited) { throw '隧道创建失败，参见私有 tunnel-error.log。' }
    Start-Sleep -Milliseconds 500
  }
  if (-not $loomOrigin) { throw '未收到临时公网地址，参见私有 tunnel-error.log。' }
  & $loomPython (Join-Path $PSScriptRoot 'update_preview_origin.py') --work-root $loomWork --origin $loomOrigin
  if ($LASTEXITCODE -ne 0) { throw '地址登记失败。' }
  if (-not $WithoutWorker) {
    $loomWorkerScript=Join-Path $PSScriptRoot 'run_local_executor.py'
    $loomWorkerArgs='"{0}" --db "{1}" --config "{2}" --watch' -f $loomWorkerScript,(Join-Path $loomPrivate 'preview.db'),(Join-Path $loomPrivate 'executor.json')
    $loomWorker=Start-Process -FilePath $loomPython -ArgumentList $loomWorkerArgs -WorkingDirectory $loomRepo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $loomPrivate 'worker.log') -RedirectStandardError (Join-Path $loomPrivate 'worker-error.log') -PassThru
    $loomRecords+=@{id=$loomWorker.Id;marker=$loomWorkerScript;kind='worker'}
  }
  @{version=(& $loomPython -c 'from loom import __version__;print(__version__)');origin=$loomOrigin;processes=$loomRecords;worker_started=(-not $WithoutWorker)} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $loomProcessFile -Encoding UTF8
  Write-Output ('已启动临时公网入口：{0}/console' -f $loomOrigin)
  Write-Output ('访问码保存在：{0}；启动不等于外网验收，请按文档执行体验测试。' -f (Join-Path $loomPrivate '访问信息.txt'))
} catch {
  # Record started helpers for safe inspection and cleanup, even after a failure.
  @{version='startup-incomplete';processes=$loomRecords;worker_started=$false} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $loomProcessFile -Encoding UTF8
  throw
}
