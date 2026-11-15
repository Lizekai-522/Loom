param(
    [ValidateSet('desktop','web')][string]$Mode = 'desktop',
    [string]$WorkRoot = (Join-Path $env:LOCALAPPDATA 'Verilo')
)
$ErrorActionPreference = 'Stop'
$veriloRepo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$veriloRoot = [IO.Path]::GetFullPath($WorkRoot)
$veriloPython = Join-Path $veriloRoot 'loom-venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $veriloPython)) {
    throw '请先运行 scripts/install_verilo.ps1 安装本地依赖。'
}
$veriloSessionPath = Join-Path $veriloRoot 'loom-first-round-session.json'
$veriloPort = 8796
if (Test-Path -LiteralPath $veriloSessionPath) {
    $veriloPort = (Get-Content -LiteralPath $veriloSessionPath -Raw -Encoding UTF8 | ConvertFrom-Json).port
}
$veriloListener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $veriloPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($veriloListener) {
    $veriloProcess = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $veriloListener.OwningProcess) -ErrorAction SilentlyContinue
    $veriloServer = Join-Path $PSScriptRoot 'serve_local.py'
    if (-not $veriloProcess -or -not $veriloProcess.CommandLine -or
        -not $veriloProcess.CommandLine.Contains($veriloServer) -or
        -not $veriloProcess.CommandLine.Contains($veriloRoot)) {
        throw '本地端口已被其他进程占用；请先核对该进程，避免连接到错误的数据目录。'
    }
}
& (Join-Path $PSScriptRoot 'start_local_dev.ps1') -WorkRoot $veriloRoot
$veriloSession = Get-Content -LiteralPath (Join-Path $veriloRoot 'loom-first-round-session.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$veriloUrl = 'http://127.0.0.1:{0}/console' -f $veriloSession.port
if ($Mode -eq 'web') {
    Start-Process $veriloUrl
} else {
    $veriloEdge = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft/Edge/Application/msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft/Edge/Application/msedge.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $veriloEdge) { throw '桌面窗口需要 Microsoft Edge。也可以使用 -Mode web 打开本地网页版。' }
    $veriloProfile = Join-Path $veriloRoot 'desktop-profile-v4'
    $null = Start-Process -FilePath $veriloEdge -ArgumentList @('--no-first-run', '--no-default-browser-check', "--app=$veriloUrl", "--user-data-dir=`"$veriloProfile`"") -PassThru
    $veriloBrowser = $null
    for ($veriloAttempt = 0; $veriloAttempt -lt 20; $veriloAttempt++) {
        $veriloBrowser = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object {
            $_.CommandLine -and $_.CommandLine.Contains($veriloProfile) -and $_.CommandLine -notmatch '--type='
        } | Select-Object -First 1
        if ($veriloBrowser) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $veriloBrowser) { throw '桌面窗口进程未启动，请使用 Verilo 网页版入口。' }
    $veriloIconScript = Join-Path $PSScriptRoot 'set_verilo_window_icon.ps1'
    $veriloIcon = Join-Path $veriloRepo 'src/loom/web/verilo-taskbar-v4.ico'
    $veriloIconRecordPath = Join-Path $veriloRoot 'verilo-icon-helper.json'
    $veriloIconRunning = $false
    if (Test-Path -LiteralPath $veriloIconRecordPath) {
        $veriloIconRecord = Get-Content -LiteralPath $veriloIconRecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $veriloIconProcess = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $veriloIconRecord.process_id) -ErrorAction SilentlyContinue
        $veriloIconRunning = $veriloIconProcess -and $veriloIconProcess.CommandLine -and
            $veriloIconProcess.CommandLine.Contains($veriloIconScript) -and
            $veriloIconRecord.browser_process_id -eq $veriloBrowser.ProcessId
    }
    if (-not $veriloIconRunning) {
        $veriloHelperArgs = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -BrowserProcessId {1} -IconPath "{2}"' -f $veriloIconScript,$veriloBrowser.ProcessId,$veriloIcon
        $veriloHelper = Start-Process -FilePath (Join-Path $env:WINDIR 'System32/WindowsPowerShell/v1.0/powershell.exe') -ArgumentList $veriloHelperArgs -WindowStyle Hidden -RedirectStandardOutput (Join-Path $veriloRoot 'verilo-icon.log') -RedirectStandardError (Join-Path $veriloRoot 'verilo-icon-error.log') -PassThru
        @{process_id=$veriloHelper.Id;browser_process_id=$veriloBrowser.ProcessId} | ConvertTo-Json | Set-Content -LiteralPath $veriloIconRecordPath -Encoding UTF8
    }
}
