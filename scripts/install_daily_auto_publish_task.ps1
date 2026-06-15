# Install or update the Windows scheduled task(s) for Hotrank daily publish.
[CmdletBinding()]
param(
  [string]$TaskName = 'HotrankPipelineDailyPublish',
  [Alias('At')]
  [string[]]$PublishTimes = @('11:30', '18:30', '21:30'),
  [int]$LeadMinutes = 30
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$Runner = Join-Path $ProjectRoot 'scripts\run_daily_auto_publish.ps1'
if (-not (Test-Path -LiteralPath $Runner)) {
  throw "Runner not found: $Runner"
}

$ps = (Get-Command powershell.exe).Source
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
  -RestartCount 1 `
  -RestartInterval (New-TimeSpan -Minutes 10)
$currentUser = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

$validTimes = @()
if (-not $PublishTimes -or -not $PublishTimes.Count) {
  $PublishTimes = @('11:30', '18:30', '21:30')
}
foreach ($rawTime in $PublishTimes) {
  if (-not $rawTime) { continue }
  $cleanTime = [string]$rawTime
  foreach ($part in ($cleanTime -replace '[，；;]', ',' -split ',')) {
    $item = $part.Trim()
    if (-not $item) { continue }
    $time = [DateTime]::ParseExact($item, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
    $validTimes += [PSCustomObject]@{
      Raw = $item
      Date = $time
    }
  }
}

if (-not $validTimes.Count) {
  throw 'No valid schedule times provided.'
}

# 清理同名前缀旧任务，避免旧的单任务定义残留。
$existing = @()
try { $existing = Get-ScheduledTask } catch { $existing = @() }
foreach ($task in ($existing | Where-Object { $_.TaskName -like "$TaskName*" })) {
  try {
    Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false
  } catch {
    Write-Host "Skip missing old task: $($task.TaskName)"
  }
}

$created = @()
$lead = [Math]::Max(0, [Math]::Min([int]$LeadMinutes, 180))
foreach ($entry in ($validTimes | Sort-Object Raw -Unique)) {
  $slot = $entry.Raw
  $taskNameForSlot = "$TaskName-$($slot.Replace(':',''))"
  $actionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -ScheduleTime $slot -LeadMinutes $lead"
  $action = New-ScheduledTaskAction -Execute $ps -Argument $actionArgs -WorkingDirectory $ProjectRoot
  $triggerAt = [DateTime]::Today.Add($entry.Date.TimeOfDay).AddMinutes(-$lead)
  $trigger = New-ScheduledTaskTrigger -Daily -At $triggerAt
  Register-ScheduledTask -TaskName $taskNameForSlot -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  $created += [PSCustomObject]@{
    TaskName = $taskNameForSlot
    PublishTime = $slot
    TriggerTime = $triggerAt.ToString('HH:mm')
  }
}

Write-Host "Installed scheduled tasks:"
$created | ForEach-Object { Write-Host " - $($_.TaskName) trigger=$($_.TriggerTime) publish=$($_.PublishTime)" }
Write-Host "Publish times: $((($validTimes | Sort-Object Raw -Unique).Raw) -join ', ')"
Write-Host "LeadMinutes: $lead"
Write-Host "Runner: $Runner"
Write-Host "StartWhenAvailable: enabled"
foreach ($createdTask in $created) {
  Get-ScheduledTask -TaskName $createdTask.TaskName | Format-List TaskName,State,TaskPath
}
