param([string]$WorkRoot=(Join-Path $PSScriptRoot '../../../work'))
$ErrorActionPreference='Stop'
$loomWork=[IO.Path]::GetFullPath($WorkRoot)
$loomRecordPath=Join-Path $loomWork 'loom-public-preview/processes.json'
if (-not (Test-Path -LiteralPath $loomRecordPath)) { Write-Output '没有已登记的公网体验进程。';exit }
$loomRecord=Get-Content -LiteralPath $loomRecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
$loomRepo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$loomAllowed=@((Join-Path $PSScriptRoot 'serve_public_preview.py'),(Join-Path $PSScriptRoot 'run_local_executor.py'),(Join-Path $loomWork 'loom-tunnel/cloudflared.exe'))
foreach ($loomItem in ($loomRecord.processes | Sort-Object @{Expression={if ($_.kind -eq 'tunnel') {0} elseif ($_.kind -eq 'worker') {1} else {2}}})) {
  if ($loomAllowed -notcontains $loomItem.marker) { throw '进程记录不符合此项目允许的帮助程序，停止。' }
  $loomProcess=Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $loomItem.id) -ErrorAction SilentlyContinue
  if (-not $loomProcess) { continue }
  if (-not $loomProcess.CommandLine -or -not $loomProcess.CommandLine.Contains($loomItem.marker)) { throw 'PID 已复用，拒绝停止无关进程。' }
  $loomChildren=Get-CimInstance Win32_Process -Filter ('ParentProcessId={0}' -f $loomItem.id) -ErrorAction SilentlyContinue
  foreach ($loomChild in $loomChildren) {
    if ($loomChild.CommandLine -and $loomChild.CommandLine.Contains($loomItem.marker)) { Stop-Process -Id $loomChild.ProcessId -ErrorAction SilentlyContinue }
  }
  Stop-Process -Id $loomItem.id -ErrorAction SilentlyContinue
}
Write-Output '已停止登记的公网体验进程；保留全部数据、日志和凭据。进行中的调用须先核对账本，不自动重发。'
