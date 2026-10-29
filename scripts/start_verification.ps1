param([Parameter(Mandatory=$true)][string]$WorkRoot,[Parameter(Mandatory=$true)][string]$Database,[Parameter(Mandatory=$true)][string]$ExecutorConfig,[Parameter(Mandatory=$true)][string]$SessionFile)
$ErrorActionPreference='Stop'
$loomConfig=Get-Content -LiteralPath $ExecutorConfig -Raw -Encoding UTF8|ConvertFrom-Json
if(-not $loomConfig.verification_config){return}
$loomRepo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$loomPython=Join-Path $WorkRoot 'loom-venv/Scripts/python.exe'
$loomScript=Join-Path $PSScriptRoot 'run_verification.py'
$loomVersion=(& $loomPython -c 'from loom import __version__;print(__version__)').Trim()
if(Test-Path -LiteralPath $SessionFile){
  $loomOld=Get-Content -LiteralPath $SessionFile -Raw -Encoding UTF8|ConvertFrom-Json
  $loomLive=Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $loomOld.process_id) -ErrorAction SilentlyContinue
  if($loomLive -and $loomLive.CommandLine -and $loomLive.CommandLine.Contains($loomScript)){
    if($loomOld.version -ne $loomVersion -or -not $loomLive.CommandLine.Contains($Database) -or -not $loomLive.CommandLine.Contains($loomConfig.verification_config)){throw '复核进程配置或版本不一致，请先核对并停止旧进程。'}
    return
  }
}
$loomArguments='"{0}" drain --db "{1}" --config "{2}" --watch' -f $loomScript,$Database,$loomConfig.verification_config
$loomProcess=Start-Process -FilePath $loomPython -ArgumentList $loomArguments -WorkingDirectory $loomRepo -WindowStyle Hidden -RedirectStandardOutput ($SessionFile+'.log') -RedirectStandardError ($SessionFile+'.error.log') -PassThru
@{process_id=$loomProcess.Id;version=$loomVersion;marker=$loomScript} | ConvertTo-Json | Set-Content -LiteralPath $SessionFile -Encoding UTF8
Write-Output '已启动私有复核执行器；仅处理明确请求，未知结果不重发。'
