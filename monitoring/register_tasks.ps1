# monitoring/register_tasks.ps1 -- register the scheduled jobs.
#
#   -Job Health (default): QuantHealthCheck, Mondays 15:30, monitoring\run_health_check.bat
#   -Job Prices          : QuantPricesUpdate, Mondays 15:00, data\run_prices_update.bat
#
#   powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1 -Job Prices -Elevate
#   powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1 -WhatIf
#   powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1 -Unregister
#
# The price task exists because nothing refreshed the prices table automatically and
# it drifted 8 sessions stale. 15:00 Finland = 08:00 New York: the previous close is
# final and the US open is still ahead, so a same-day execution run sizes off current
# ADV and sigma. WEEKLY (Mondays, picking up Friday's close) since 2026-10-04: the
# book rebalances monthly and sizing uses 60-day ADV / 20-day sigma, so a few days
# of staleness barely moves them, and prices_db_fresh (<= 5 sessions) still holds.
# Refresh by hand before an execution run if you want the latest close.
#
# Mirrors QuantForwardTestSnapshot: S4U logon (no stored password, runs with nobody
# logged in), StartWhenAvailable so a missed run catches up after a late boot, and the
# same battery settings.
#
# TIMING: WEEKLY, Monday 15:30 local (since 2026-10-04; it was nightly 23:50). Half an
# hour after the Monday 15:00 price refresh, so prices_db_fresh sees fresh data, and
# before the US open. Friday's 23:30 snapshot is then 1 session behind -- inside the
# 5.3 grace -- so a healthy week reads all-green. The trade-off is accepted: a broken
# snapshot can now go unnoticed by THIS task for up to a week. Every order run still
# executes the same checks itself (assert_pipeline_healthy) before sizing anything,
# so a failure cannot reach a trade.
#
# THIS FILE IS ASCII-ONLY AND SAVED WITH A BOM, ON PURPOSE. Windows PowerShell 5.1
# reads a BOM-less .ps1 as cp1252, so em-dashes in the comments became a parse error.
# It also uses splatting rather than backtick line-continuation: a backtick immediately
# before a CRLF escapes the CR instead of continuing the line, and PowerShell then read
# -LogonType as a command name.
#
# Requires an elevated PowerShell to register a scheduled task.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet("Health", "Prices")]
    [string]$Job = "Health",
    [string]$TaskName,
    [string]$Time,
    [switch]$Unregister,
    [switch]$Elevate
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$Jobs = @{
    Health = @{
        TaskName = "QuantHealthCheck"
        Time     = "15:30"
        Bat      = "monitoring\run_health_check.bat"
        Days     = @("Monday")
        When     = "weekly on Monday (after the 15:00 price refresh)"
        Log      = "monitoring\state\health_log.txt"
        Desc     = ("Quant system pipeline health check. Runs weekly after the Monday " +
                    "price refresh and alerts on any FAIL. Exit 1 means something needs " +
                    "a human. See monitoring/README.md.")
    }
    Prices = @{
        TaskName = "QuantPricesUpdate"
        Time     = "15:00"
        Bat      = "data\run_prices_update.bat"
        Days     = @("Monday")
        When     = "weekly on Monday (before the US open)"
        Log      = "monitoring\state\prices_update_log.txt"
        Desc     = ("Quant system daily price refresh (data/prices_updater.py, full " +
                    "universe). Feeds ADV and sigma into order sizing. Exit 1 means " +
                    "every batch came back empty. See monitoring/README.md.")
    }
}
$Spec = $Jobs[$Job]
if (-not $TaskName) { $TaskName = $Spec.TaskName }
if (-not $Time)     { $Time     = $Spec.Time }
$Bat  = Join-Path $Root $Spec.Bat
$Self = "powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1"
if ($Job -ne "Health") { $Self = "$Self -Job $Job" }

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Output "unregistered $TaskName"
        }
    } else {
        Write-Output "$TaskName is not registered - nothing to do"
    }
    exit 0
}

if (-not (Test-Path $Bat)) { throw "wrapper not found: $Bat" }

# S4U registration needs an elevated shell. Verified on this machine: an Interactive-
# logon task registers fine unelevated, while S4U returns "Access is denied" -- so the
# elevation is required by the logon type, not by Register-ScheduledTask itself.
# We keep S4U rather than falling back to Interactive: Interactive only runs while
# someone is signed in, and a recorder that silently skips nights is the exact failure
# this monitoring exists to prevent.
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin -and -not $WhatIfPreference) {
    if ($Elevate) {
        Write-Output "not elevated - relaunching via UAC..."
        $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     $MyInvocation.MyCommand.Path, "-Job", $Job,
                     "-TaskName", $TaskName, "-Time", $Time)
        try {
            Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait -ErrorAction Stop
        } catch {
            # Declining UAC throws "The operation was canceled by the user". That is a
            # choice, not a crash - report it plainly and leave nothing half-registered.
            Write-Output ""
            Write-Output "UAC was declined (or no interactive desktop could show it)."
            Write-Output "$TaskName was NOT registered. Nothing was changed."
            Write-Output "Open an Administrator PowerShell and run:"
            Write-Output "  $Self"
            exit 2
        }
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Write-Output "$TaskName is registered."
            exit 0
        }
        Write-Output "$TaskName is NOT registered - the elevated run did not complete."
        exit 2
    }
    Write-Output ""
    Write-Output "REFUSED: registering an S4U task needs an elevated PowerShell."
    Write-Output "S4U is deliberate - it runs with nobody logged in, so nights are not"
    Write-Output "silently skipped. Interactive logon registers unelevated but only"
    Write-Output "runs while you are signed in."
    Write-Output ""
    Write-Output "Run ONE of these:"
    Write-Output "  $Self -Elevate"
    Write-Output "  (or open an Administrator PowerShell and re-run without -Elevate)"
    exit 2
}

# Refuse to register a wrapper that swallows its own exit code. That is the bug that
# hid nine days of snapshot failure, and a health check whose LastTaskResult is always
# 0 is worse than none: it reports healthy while doing nothing.
$batText = Get-Content $Bat -Raw
if ($batText -notmatch 'exit /b %RC%') {
    throw ("REFUSED: $Bat does not end with 'endlocal & exit /b %RC%', so its exit " +
           "code would not reach Task Scheduler. Fix the wrapper before registering.")
}

Write-Output "registering $TaskName"
Write-Output "  action : $Bat"
Write-Output "  trigger: $($Spec.When) at $Time"

$action  = New-ScheduledTaskAction -Execute $Bat
if ($Spec.Days) {
    $triggerArgs = @{
        Weekly     = $true
        DaysOfWeek = $Spec.Days
        At         = $Time
    }
} else {
    $triggerArgs = @{ Daily = $true; At = $Time }
}
$trigger = New-ScheduledTaskTrigger @triggerArgs
# Local wall-clock time, no UTC offset. New-ScheduledTaskTrigger writes the offset in
# force on the day of registration ("...T15:00:00+03:00" in summer), which Task Scheduler
# keeps as a fixed instant: after the October clock change both Monday jobs fired at
# 14:00 and 14:30 instead of 15:00 and 15:30. The snapshot task's XML has no offset and
# stayed at 23:30. Without the offset the time follows daylight saving.
$trigger.StartBoundary = ([datetime]$trigger.StartBoundary).ToString("yyyy-MM-dd'T'HH:mm:ss")

# S4U: same as the snapshot task. RunLevel Limited - no elevation needed to run.
$principalArgs = @{
    UserId    = "$env:USERDOMAIN\$env:USERNAME"
    LogonType = "S4U"
    RunLevel  = "Limited"
}
$principal = New-ScheduledTaskPrincipal @principalArgs

# New-ScheduledTaskSettingsSet on this build exposes MultipleInstances (not
# ...Policy) and has no battery parameters at all, so the two battery settings are
# assigned on the returned object. Both default to $true, matching the snapshot task;
# set explicitly so the intent survives a future default change.
$settingsArgs = @{
    StartWhenAvailable = $true
    MultipleInstances  = "IgnoreNew"
    ExecutionTimeLimit = (New-TimeSpan -Minutes 30)
}
$settings = New-ScheduledTaskSettingsSet @settingsArgs
$settings.DisallowStartIfOnBatteries = $true
$settings.StopIfGoingOnBatteries     = $true

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Output "  (replacing the existing registration)"
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    $regArgs = @{
        TaskName    = $TaskName
        Action      = $action
        Trigger     = $trigger
        Principal   = $principal
        Settings    = $settings
        Description = $Spec.Desc
    }
    Register-ScheduledTask @regArgs | Out-Null

    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Output ""
    Write-Output "registered. next run: $($info.NextRunTime)"
    Write-Output ""
    Write-Output "verify now with:"
    Write-Output "  Start-ScheduledTask -TaskName $TaskName"
    Write-Output "  Get-ScheduledTaskInfo -TaskName $TaskName | Select LastRunTime,LastTaskResult"
    Write-Output "  type $($Spec.Log)"
}
