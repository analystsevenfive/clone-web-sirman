<#
  setup_runner.ps1
  One-step setup of a GitHub self-hosted runner for the Sirman -> Google Sheet sync.

  Run in PowerShell on the machine that should do the sync (needs Python 3.10+ in PATH):
      powershell -ExecutionPolicy Bypass -File setup_runner.ps1 [-Repo https://github.com/<owner>/<repo>]

  You will be asked for a registration token:
      GitHub repo > Settings > Actions > Runners > New self-hosted runner  (copy the token after "--token")
  The token is valid for about 1 hour.

  The runner starts automatically when this Windows user logs in (the screen may be locked).
#>
param(
    # Must match the repo shown on GitHub > Settings > Actions > Runners > New self-hosted runner (--url ...)
    [string]$Repo = "https://github.com/analystsevenfive/clone-web-sirman"
)
$ErrorActionPreference = "Stop"
$RunnerDir = "C:\actions-runner"
$TaskName  = "GitHub Actions Runner (sirman-sync)"
$ReqFile   = Join-Path $PSScriptRoot "requirements.txt"

Write-Host "== 1/5 Checking Python" -ForegroundColor Cyan
$ver = & python --version 2>&1
if ($LASTEXITCODE -ne 0) { throw "Python not found in PATH. Install Python 3.10+ (tick 'Add python.exe to PATH') and run again." }
Write-Host $ver

Write-Host "== 2/5 Installing Python packages + Chromium" -ForegroundColor Cyan
python -m pip install -r $ReqFile
python -m playwright install chromium

Write-Host "== 3/5 Downloading GitHub runner" -ForegroundColor Cyan
$existing = Test-Path (Join-Path $RunnerDir ".runner")
if (-not $existing) {
    New-Item -ItemType Directory -Force $RunnerDir | Out-Null
    $rel = Invoke-RestMethod "https://api.github.com/repos/actions/runner/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -like "actions-runner-win-x64-*.zip" } | Select-Object -First 1
    $zip = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    Expand-Archive $zip -DestinationPath $RunnerDir -Force
    Remove-Item $zip

    Write-Host "== 4/5 Registering runner" -ForegroundColor Cyan
    $token = Read-Host "Paste the registration token"
    Push-Location $RunnerDir
    .\config.cmd --url $Repo --token $token --name "$env:COMPUTERNAME-sirman" --labels "sirman-sync" --unattended --replace
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Runner registration failed: check the token (valid ~1 hour) and that -Repo matches the --url on the GitHub runner page" }
    Pop-Location
} else {
    Write-Host "Runner already registered in $RunnerDir - skipping"
}

Write-Host "== 5/5 Starting runner at every logon" -ForegroundColor Cyan
$action  = New-ScheduledTaskAction -Execute (Join-Path $RunnerDir "run.cmd") -WorkingDirectory $RunnerDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $set -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "`nDone. In GitHub > Settings > Actions > Runners the runner should show 'Idle' within a minute." -ForegroundColor Green
