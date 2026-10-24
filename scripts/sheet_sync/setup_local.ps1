<#
  setup_local.ps1
  One-step setup for running the Sirman -> Google Sheet sync directly on this Windows machine.

      powershell -ExecutionPolicy Bypass -File scripts\sheet_sync\setup_local.ps1

  It installs the Python packages, asks for the settings once (saved in .env, git-ignored),
  copies the Google service account key next to the script, and schedules a daily run at 08:00
  with Windows Task Scheduler (runs when the PC is on, or right after it is next started).
  Needs Python 3.10+ in PATH.
#>
param(
    [string]$Time = "08:00",
    [switch]$NoSchedule
)
$ErrorActionPreference = "Stop"
$Here    = $PSScriptRoot
$EnvFile = Join-Path $Here ".env"
$Script  = Join-Path $Here "sync_sirman_to_sheet.py"
$TaskName = "Sirman Sheet Sync"

Write-Host "== 1/4 Checking Python" -ForegroundColor Cyan
$ver = & python --version 2>&1
if ($LASTEXITCODE -ne 0) { throw "Python not found in PATH. Install Python 3.10+ (tick 'Add python.exe to PATH') and run again." }
Write-Host $ver

Write-Host "== 2/4 Installing Python packages + Chromium" -ForegroundColor Cyan
python -m pip install -r (Join-Path $Here "requirements.txt")
python -m playwright install chromium

Write-Host "== 3/4 Settings" -ForegroundColor Cyan
if (Test-Path $EnvFile) {
    Write-Host ".env already exists - keeping it (delete it to be asked again)"
} else {
    $user  = Read-Host "Sirman login (email)"
    $pw    = Read-Host "Sirman password" -AsSecureString
    $pwTxt = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
    $sheet = Read-Host "Google Sheet ID (the part between /d/ and /edit in the sheet URL)"
    $key   = (Read-Host "Path to the service account key .json file").Trim('"')
    if (-not (Test-Path $key)) { throw "Key file not found: $key" }
    Copy-Item $key (Join-Path $Here "service_account.json") -Force
    $rate  = Read-Host "EUR -> THB rate [38]"
    if (-not $rate) { $rate = "38" }
    $lines = @("SIRMAN_USER=$user", "SIRMAN_PASSWORD=$pwTxt", "GOOGLE_SHEET_ID=$sheet",
               "GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json", "EUR_THB=$rate")
    [IO.File]::WriteAllLines($EnvFile, $lines, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Saved $EnvFile"
}

Write-Host "== 4/4 Daily schedule" -ForegroundColor Cyan
if ($NoSchedule) {
    Write-Host "Skipped (-NoSchedule)"
} else {
    $py      = (Get-Command python).Source
    $action  = New-ScheduledTaskAction -Execute $py -Argument "`"$Script`"" -WorkingDirectory $Here
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time
    $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $set -Force | Out-Null
    Write-Host "Scheduled '$TaskName' every day at $Time"
}

Write-Host "`nTest it now (nothing is written to the sheet):" -ForegroundColor Green
Write-Host "    `$env:DRY_RUN='1'; python `"$Script`""
Write-Host "Real run:  python `"$Script`"   (or double-click run_sync.bat)"
