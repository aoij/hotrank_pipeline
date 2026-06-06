[CmdletBinding()]
param([string]$TaskName = 'HotrankPipelineDailyPublish')
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Uninstalled scheduled task: $TaskName"
