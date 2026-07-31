<#
.SYNOPSIS
    Wrapper around icloud_shared_albums_backup.py: checks dependencies,
    runs the script, and reports where the log/cache files ended up.

.DESCRIPTION
    This does NOT contain any iCloud logic itself - all the actual work
    happens in the Python script. This wrapper just makes it easier to run
    repeatedly (dependency check, consistent working directory, optional
    Task Scheduler registration) without having to remember pip/python
    invocations.

.NOTES
    Requires Python 3.9+ on PATH (as `python` or `py`).
    The Python script's APPLE_ID / TARGET_DIR constants must be edited
    directly in icloud_shared_albums_backup.py before running.
#>

param(
    [switch]$InstallOnly,      # only install/check dependencies, don't run the backup
    [switch]$Register          # register a daily Windows Scheduled Task instead of running now
)

$ErrorActionPreference = "Stop"

# Directory this .ps1 lives in - keeps log/cache files colocated regardless
# of where the script is invoked from.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonScript = Join-Path $ScriptDir "icloud_shared_albums_backup.py"

function Get-PythonCommand {
    foreach ($candidate in @("python", "py")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            return $candidate
        }
    }
    return $null
}

function Test-PythonPackage {
    param([string]$PythonCmd, [string]$PackageName)
    & $PythonCmd -c "import $PackageName" 2>$null
    return ($LASTEXITCODE -eq 0)
}

# ---------------- Checks ----------------

if (-not (Test-Path $PythonScript)) {
    Write-Error "icloud_shared_albums_backup.py not found next to this script (expected at: $PythonScript)"
    exit 1
}

$python = Get-PythonCommand
if (-not $python) {
    Write-Error "Python was not found on PATH. Install it from https://python.org (check 'Add python.exe to PATH' during setup), then re-run this script."
    exit 1
}

Write-Host "Using Python: $python ($(& $python --version))"

# ---------------- Dependencies ----------------

$requiredPackages = @("pyicloud", "requests")
$missing = @()
foreach ($pkg in $requiredPackages) {
    if (-not (Test-PythonPackage -PythonCmd $python -PackageName $pkg)) {
        $missing += $pkg
    }
}

if ($missing.Count -gt 0) {
    Write-Host "Installing missing packages: $($missing -join ', ')"
    & $python -m pip install $missing
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pip install failed. Try running manually: python -m pip install $($missing -join ' ')"
        exit 1
    }
} else {
    Write-Host "All required packages already installed."
}

if ($InstallOnly) {
    Write-Host "Dependency check complete (-InstallOnly specified, not running the backup)."
    exit 0
}

# ---------------- Optional: register a Scheduled Task instead of running now ----------------

if ($Register) {
    $taskName = "iCloud Shared Albums Backup"
    $action = New-ScheduledTaskAction -Execute $python -Argument "`"$PythonScript`"" -WorkingDirectory $ScriptDir
    $trigger = New-ScheduledTaskTrigger -Daily -At 3am
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Description "Downloads private iCloud Shared Albums via icloud_shared_albums_backup.py"
    Write-Host "Scheduled task '$taskName' registered (daily at 3:00 AM)."
    Write-Host "Note: this script prompts interactively for your password and 2FA code -"
    Write-Host "a scheduled/unattended run will fail once the cached Apple session expires."
    exit 0
}

# ---------------- Run ----------------

Write-Host "`nStarting backup (working directory: $ScriptDir)`n"
Push-Location $ScriptDir
try {
    & $python $PythonScript
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

$logFile = Join-Path $ScriptDir "icloud_shared_albums.log"
if (Test-Path $logFile) {
    Write-Host "`nLog file: $logFile"
}

exit $exitCode
