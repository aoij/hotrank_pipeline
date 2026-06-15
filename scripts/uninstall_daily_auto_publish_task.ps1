[CmdletBinding()]
param([string]$TaskName = 'HotrankPipelineDailyPublish')

$ErrorActionPreference = 'Stop'

$matched = @()
try {
  $matched = Get-ScheduledTask | Where-Object { $_.TaskName -like "$TaskName*" }
} catch {
  $matched = @()
}

if (-not $matched.Count) {
  Write-Host "No scheduled task matched prefix: $TaskName"
  exit 0
}

foreach ($task in $matched) {
  Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false
  Write-Host "Uninstalled scheduled task: $($task.TaskName)"
}
