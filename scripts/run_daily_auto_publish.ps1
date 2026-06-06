# Hotrank Pipeline 每日自动发布兜底入口
# 由 Windows 计划任务调用；也可手动运行。
[CmdletBinding()]
param(
  [switch]$Force,
  [Nullable[int]]$DraftLimit = $null,
  [Nullable[int]]$PublishLimit = $null
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $ProjectRoot

$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
$env:PYTHONIOENCODING = 'utf-8'

$LogDir = Join-Path $ProjectRoot 'data\scheduler'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("windows_task_{0}.log" -f (Get-Date -Format 'yyyyMMdd'))

$argsList = @('-X', 'utf8', '-m', 'hotrank_pipeline.main', 'run-daily-auto-publish', '--trigger', 'windows-task')
if ($Force) { $argsList += '--force' }
if ($DraftLimit.HasValue) { $argsList += @('--draft-limit', [string]$DraftLimit.Value) }
if ($PublishLimit.HasValue) { $argsList += @('--publish-limit', [string]$PublishLimit.Value) }

$header = "`n===== {0} start force={1} draft={2} publish={3} =====" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Force.IsPresent, $DraftLimit, $PublishLimit
$header | Tee-Object -FilePath $LogFile -Append | Out-Null

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
  & python @argsList 2>&1 | ForEach-Object {
    $line = [string]$_
    $line | Tee-Object -FilePath $LogFile -Append
  }
  $exitCode = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $oldErrorActionPreference
}

$footer = "===== {0} end exit={1} =====" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $exitCode
$footer | Tee-Object -FilePath $LogFile -Append | Out-Null
exit $exitCode
