# The two listeners -- `agent bot` and `agent serve` -- as Task Scheduler tasks.
#
# Run once, as yourself, from any directory:
#     powershell -ExecutionPolicy Bypass -File deploy\register-listeners.ps1
# Running it again replaces both tasks with these same settings.
#
# Why these settings (2026-10-17):
#   * At logon, as this user, only while logged on (Interactive): no admin
#     rights needed. pythonw.exe has no console, so nothing flashes on screen.
#   * Then EVERY MINUTE, forever, with "do not start a new instance" (IgnoreNew).
#     This is the restart. Task Scheduler's own "restart on failure" does NOT
#     fire when a program starts and later exits non-zero -- measured: a pythonw
#     script exiting 3, with restart set to every minute, was never restarted in
#     18 minutes. A repetition does fire, and IgnoreNew makes it a no-op while
#     the process is alive. A dead listener is back within a minute.
#   * No time limit, not stopped on battery, allowed to start on battery.
#   * agent bot also holds DATA_DIR\bot.lock, so a second bot -- however it got
#     started -- leaves at once; two bots polling Telegram fight over updates.
#     agent serve cannot run twice either: the second cannot bind port 8000.
#
# The live checkout is the one these run. Development happens in a worktree
# (CLAUDE.md, Working style); nothing here points at it.

$ErrorActionPreference = "Stop"
$live = "C:\dev\classroom-agent"
$pythonw = Join-Path $live ".venv\Scripts\pythonw.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"

foreach ($name in @("bot", "serve")) {
    $task = "classroom-agent-$name"
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m agent.cli $name" -WorkingDirectory $live
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 1)).Repetition
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Force `
        -Description "classroom-agent: agent $name. At logon, then every minute it is not running. See deploy\register-listeners.ps1." | Out-Null
    Write-Output "registered $task"
}

# The old Startup launchers, so nothing starts twice.
$startup = [Environment]::GetFolderPath("Startup")
foreach ($old in @("classroom-agent-bot.vbs", "classroom-agent-serve.vbs")) {
    $path = Join-Path $startup $old
    if (Test-Path $path) {
        Remove-Item $path
        Write-Output "removed $path"
    }
}
