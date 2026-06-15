# Hotrank Pipeline 每日自动发布正式入口
# 由 Windows 计划任务调用；也可手动运行。
[CmdletBinding()]
param(
  [switch]$Force,
  [string]$ScheduleTime = '',
  [Nullable[int]]$DraftLimit = $null,
  [Nullable[int]]$PublishLimit = $null,
  [Nullable[int]]$LeadMinutes = $null
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

$TriggerName = if ($ScheduleTime) {
  'windows-task-{0}' -f ($ScheduleTime -replace ':', '')
} else {
  'windows-task'
}

$leadArg = if ($LeadMinutes.HasValue) { [string]$LeadMinutes.Value } else { $env:HOTRANK_PUBLISH_LEAD_MINUTES }
if (-not $leadArg) { $leadArg = '30' }

function Write-Utf8LogLine {
  param([string]$Text)
  $clean = if ($null -eq $Text) { '' } else { [string]$Text }
  [System.IO.File]::AppendAllText(
    $LogFile,
    $clean + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
  )
}

$argsList = @('-X', 'utf8', '-m', 'hotrank_pipeline.main', 'run-daily-auto-publish', '--trigger', $TriggerName)
if ($Force) { $argsList += '--force' }
if ($ScheduleTime) { $argsList += @('--schedule-time', $ScheduleTime) }
if ($DraftLimit.HasValue) { $argsList += @('--draft-limit', [string]$DraftLimit.Value) }
if ($PublishLimit.HasValue) { $argsList += @('--publish-limit', [string]$PublishLimit.Value) }
if ($leadArg) { $argsList += @('--publish-lead-minutes', [string]$leadArg) }

$header = "`n===== {0} start trigger={1} force={2} schedule={3} lead={4} draft={5} publish={6} =====" -f `
  (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), `
  $TriggerName, `
  $Force.IsPresent, `
  $ScheduleTime, `
  $leadArg, `
  $DraftLimit, `
  $PublishLimit
Write-Host $header
Write-Utf8LogLine $header

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
  & python @argsList 2>&1 | ForEach-Object {
    $line = [string]$_
    Write-Host $line
    Write-Utf8LogLine $line
  }
  $exitCode = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $oldErrorActionPreference
}

$footer = "===== {0} end exit={1} =====" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $exitCode
Write-Host $footer
Write-Utf8LogLine $footer
exit $exitCode
