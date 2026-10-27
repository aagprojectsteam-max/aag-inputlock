param(
    [switch]$Armed
)

$ErrorActionPreference = "Stop"
$InstallDirectory = Join-Path $env:ProgramFiles "AAG\LockLock"
$Cli = Join-Path $InstallDirectory "LockLockCli.exe"
if (-not (Test-Path $Cli)) { throw "Install AAG - InputLock before running this checklist." }

& $Cli status
if (-not $Armed) {
    Write-Host "Read windows/README.md, arrange a second recovery route, then rerun with -Armed."
    exit 2
}

Write-Warning "The following test locks the mouse for 5 seconds. Keep the keyboard available."
& $Cli lock mouse --timeout 5
Start-Sleep -Seconds 7
& $Cli status

Write-Warning "Next, lock keyboard and mouse for 30 seconds. Test Ctrl+Alt+Z and Ctrl+Alt+Shift+F12."
$Answer = Read-Host "Type YES to continue"
if ($Answer -cne "YES") { exit 3 }
& $Cli lock all --timeout 30
Start-Sleep -Seconds 32
& $Cli status

Write-Host "Automated safe-timeout checks completed."
Write-Host "Manually record: repeated cycles, service kill, agent kill, suspend/resume, two users, multi-monitor cursor, lid on AC, and lid above/below battery threshold."
Write-Host "Touchscreen remains unsupported; do not mark it as passed from synthesized mouse behavior."
