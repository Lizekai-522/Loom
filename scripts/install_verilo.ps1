param([string]$WorkRoot = (Join-Path $env:LOCALAPPDATA 'Verilo'))
$ErrorActionPreference = 'Stop'
$veriloRepo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$veriloRoot = [IO.Path]::GetFullPath($WorkRoot)
$veriloPython = Join-Path $veriloRoot 'loom-venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $veriloPython)) {
    New-Item -ItemType Directory -Path $veriloRoot -Force | Out-Null
    & python -m venv (Join-Path $veriloRoot 'loom-venv')
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败。' }
}
& $veriloPython -m pip install -r (Join-Path $veriloRepo 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw '安装依赖失败。' }
& $veriloPython -m pip install -r (Join-Path $veriloRepo 'requirements-terminal.lock')
if ($LASTEXITCODE -ne 0) { throw '安装终端依赖失败。' }
& $veriloPython -m pip install --no-deps -e $veriloRepo
if ($LASTEXITCODE -ne 0) { throw '安装 Verilo 源码失败。' }
Write-Output '安装完成。请运行 scripts/start_verilo.ps1。'
