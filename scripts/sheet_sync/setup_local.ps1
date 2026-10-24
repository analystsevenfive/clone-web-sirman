<#
  setup_local.ps1
  One-step setup for running the Sirman -> Google Sheet sync directly on this Windows machine.

      powershell -ExecutionPolicy Bypass -File scripts\sheet_sync\setup_local.ps1

  It installs the Python packages, asks for the settings once (saved as Windows user
  environment variables - no secret is written to any file), and schedules a daily run at 08:00
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

Write-Host "== 3/4 Settings (saved as Windows user environment variables)" -ForegroundColor Cyan
function Set-UserEnv($name, $value) {
    [Environment]::SetEnvironmentVariable($name, $value, "User")   # persists for this Windows user
    Set-Item -Path "Env:$name" -Value $value                       # and for this session
}
$have = [Environment]::GetEnvironmentVariable("SIRMAN_USER", "User")
$redo = $true
if ($have) {
    $redo = (Read-Host "Settings already exist (SIRMAN_USER=$have). Replace them? [y/N]") -match '^[yY]'
}
if ($redo) {
    $user  = Read-Host "Sirman login (email)"
    $pw    = Read-Host "Sirman password" -AsSecureString
    $pwTxt = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
    $sheet = Read-Host "Google Sheet ID (the part between /d/ and /edit in the sheet URL)"
    # offer a key file found in the project folder, otherwise ask for its path (until it exists)
    $repoRoot = Resolve-Path (Join-Path $Here "..\..")
    $found = Get-ChildItem $repoRoot -Filter "*.json" -File -ErrorAction SilentlyContinue |
             Where-Object { $_.Name -like "facebook-sheet-autoposter-*" -or $_.Name -like "*service*account*" } | Select-Object -First 1
    $key = ""
    if ($found) {
        $use = Read-Host "Found key file $($found.FullName). Use it? [Y/n]"
        if ($use -notmatch '^[nN]') { $key = $found.FullName }
    }
    while (-not $key -or -not (Test-Path -LiteralPath $key)) {
        $key = (Read-Host "Path to the service account key .json file").Trim('"').Trim()
        if (-not $key -or -not (Test-Path -LiteralPath $key)) { Write-Host "File not found, try again." -ForegroundColor Yellow; $key = "" }
    }
    $rate  = Read-Host "EUR -> THB rate [38]"
    if (-not $rate) { $rate = "38" }
    Set-UserEnv "SIRMAN_USER" $user
    Set-UserEnv "SIRMAN_PASSWORD" $pwTxt
    Set-UserEnv "GOOGLE_SHEET_ID" $sheet.Trim()
    Set-UserEnv "GOOGLE_SERVICE_ACCOUNT_B64" ([Convert]::ToBase64String([IO.File]::ReadAllBytes($key)))
    Set-UserEnv "EUR_THB" $rate
    Write-Host "Saved. You can delete the key file now if you have another copy: $key"
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
