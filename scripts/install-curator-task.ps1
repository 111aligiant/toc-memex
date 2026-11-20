# Register the Toc Memex curator as a Windows scheduled task.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-curator-task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-curator-task.ps1 -IntervalMinutes 180
#
# The task runs one pass (`curator run --once`), so each invocation takes the
# same single-instance lock as a manual run and can never overlap it. Prefer
# this over `curator run --every` when the machine is not always on: Task
# Scheduler survives reboots and sleep.
param(
    [string]$TaskName = "Toc Memex Curator",
    [int]$IntervalMinutes = 360
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m app.curator run --once" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Description "Toc Memex curator: one maintenance pass every $IntervalMinutes minutes." `
    -Force | Out-Null

Write-Host "registered '$TaskName' (every $IntervalMinutes min)"
Write-Host "  action: $python -m app.curator run --once"
Write-Host "  remove: Unregister-ScheduledTask -TaskName '$TaskName'"