# Install or update the Windows scheduled task for Hotrank daily publish.
[CmdletBinding()]
param(
  [string]$TaskName = 'HotrankPipelineDailyPublish',
  [string]$At = '07:00'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$Runner = Join-Path $ProjectRoot 'scripts\run_daily_auto_publish.ps1'
if (-not (Test-Path -LiteralPath $Runner)) {
  throw "Runner not found: $Runner"
}

$time = [DateTime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$ps = (Get-Command powershell.exe).Source
$actionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
$action = New-ScheduledTaskAction -Execute $ps -Argument $actionArgs -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At ([DateTime]::Today.Add($time.TimeOfDay))
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
  -RestartCount 1 `
  -RestartInterval (New-TimeSpan -Minutes 10)
$currentUser = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Time: $At"
Write-Host "Runner: $Runner"
Write-Host "StartWhenAvailable: enabled"
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName,State,TaskPath
