param(
    [Parameter(Mandatory=$true)][string]$RuntimeRoot,
    [string]$StorageRoot = (Join-Path $env:LOCALAPPDATA 'Docker/wsl'),
    [string]$DockerExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe',
    [switch]$InspectOnly
)
$ErrorActionPreference = 'Stop'
$loomRuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
$loomStorageRoot = [IO.Path]::GetFullPath($StorageRoot).TrimEnd('\')
$loomComparison = [StringComparison]::OrdinalIgnoreCase
if ($loomRuntimeRoot.Equals($loomStorageRoot,$loomComparison) -or
    $loomRuntimeRoot.StartsWith($loomStorageRoot+'\',$loomComparison) -or
    $loomStorageRoot.StartsWith($loomRuntimeRoot+'\',$loomComparison)) {
    throw 'Runtime and existing persistent storage must be separate'
}
if (-not (Test-Path -LiteralPath (Join-Path $loomStorageRoot 'disk/docker_data.vhdx')) -or
    -not (Test-Path -LiteralPath $DockerExe -PathType Leaf)) { throw 'Existing Docker executable and data disk are required' }
$loomExistingRoot = Get-Item -LiteralPath $loomRuntimeRoot -Force -ErrorAction SilentlyContinue
if ($loomExistingRoot -and (-not $loomExistingRoot.PSIsContainer -or
    ($loomExistingRoot.Attributes -band [IO.FileAttributes]::ReparsePoint))) { throw 'Runtime root must be an ordinary directory' }
$loomDockerCache = Join-Path $loomRuntimeRoot 'Docker'
$loomExistingCache = Get-Item -LiteralPath $loomDockerCache -Force -ErrorAction SilentlyContinue
if ($loomExistingCache -and (-not $loomExistingCache.PSIsContainer -or
    ($loomExistingCache.Attributes -band [IO.FileAttributes]::ReparsePoint))) { throw 'Docker cache parent must be an ordinary directory' }
$loomAlias = Join-Path $loomDockerCache 'wsl'
$loomExistingAlias = Get-Item -LiteralPath $loomAlias -Force -ErrorAction SilentlyContinue
if ($loomExistingAlias -and ($loomExistingAlias.LinkType -ne 'Junction' -or
    [IO.Path]::GetFullPath($loomExistingAlias.Target[0]).TrimEnd('\') -ne $loomStorageRoot)) {
    throw 'Unexpected persistent storage mapping; left untouched'
}
$loomRunning = @(Get-Process -Name 'Docker Desktop','com.docker.backend','docker-ai' -ErrorAction SilentlyContinue).Count -gt 0
if ($InspectOnly) {
    @{ mode='inspection'; passed=$true; application_running=$loomRunning; existing_storage_linked=[bool]$loomExistingAlias;
       files_deleted=0; global_environment_changed=$false } | ConvertTo-Json -Compress
    return
}
if ($loomRunning) { throw 'Quit the existing Docker application before using a different runtime cache' }
if (-not $loomExistingRoot) { New-Item -ItemType Directory -Path $loomRuntimeRoot | Out-Null }
if (-not (Test-Path -LiteralPath $loomDockerCache)) { New-Item -ItemType Directory -Path $loomDockerCache | Out-Null }
if (-not $loomExistingAlias) { New-Item -ItemType Junction -Path $loomAlias -Target $loomStorageRoot | Out-Null }
$loomRules = @(
    @{ Path=(Join-Path $loomDockerCache 'run'); Allowed=@('dockerInference','userAnalyticsOtlpHttp.sock') },
    @{ Path=(Join-Path $loomRuntimeRoot 'docker-secrets-engine'); Allowed=@('engine.sock') }
)
foreach ($loomRule in $loomRules) {
    $loomDirectory = Get-Item -LiteralPath $loomRule.Path -Force -ErrorAction SilentlyContinue
    if (-not $loomDirectory) { continue }
    if (-not $loomDirectory.PSIsContainer -or ($loomDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Unexpected socket directory type' }
    if (@(Get-ChildItem -LiteralPath $loomRule.Path -Force | Where-Object { $_.PSIsContainer -or $_.Name -notin $loomRule.Allowed }).Count) { throw 'Unexpected runtime contents; left untouched' }
}
foreach ($loomRule in $loomRules) {
    $loomSource = [IO.Path]::GetFullPath($loomRule.Path)
    $loomBackup = $loomSource+'.preserved-'+[Guid]::NewGuid().ToString('N')
    if (-not $loomSource.StartsWith($loomRuntimeRoot+'\',$loomComparison) -or
        [IO.Path]::GetDirectoryName($loomBackup) -ne [IO.Path]::GetDirectoryName($loomSource)) { throw 'Runtime path leaves the named cache root' }
    if (Test-Path -LiteralPath $loomSource) { Rename-Item -LiteralPath $loomSource -NewName ([IO.Path]::GetFileName($loomBackup)) }
    New-Item -ItemType Directory -Path $loomSource | Out-Null
}
$loomStart = [Diagnostics.ProcessStartInfo]::new($DockerExe)
$loomStart.UseShellExecute=$false
$loomStart.CreateNoWindow=$true
$loomStart.WindowStyle=[Diagnostics.ProcessWindowStyle]::Hidden
$loomStart.EnvironmentVariables['LOCALAPPDATA']=$loomRuntimeRoot
$loomProcess=[Diagnostics.Process]::Start($loomStart)
@{ mode='started'; process_id=$loomProcess.Id; existing_storage_linked=$true; files_deleted=0;
   global_environment_changed=$false; engine_health_verified=$false } | ConvertTo-Json -Compress
