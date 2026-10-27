param(
    [Parameter(Mandatory=$true)][string]$TargetSid
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Administrator
$TargetProfile = Resolve-TargetProfile $TargetSid
Assert-AgentStopped
$InstallDirectory = Join-Path $env:ProgramFiles 'AAG\LockLock'
$DataDirectory = Join-Path $env:ProgramData 'AAG\LockLock'
Assert-PlainPath $InstallDirectory
Assert-PlainPath $DataDirectory
$ConfigPath = Join-Path $DataDirectory 'service.json'
if ((Test-Path -LiteralPath $ConfigPath) -and (Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json).authorized_sid -ne $TargetSid) {
    throw 'TargetSid does not match the installed service account configuration.'
}
$UserRunKey = "Registry::HKEY_USERS\$TargetSid\Software\Microsoft\Windows\CurrentVersion\Run"
if (-not (Test-Path -LiteralPath "Registry::HKEY_USERS\$TargetSid")) {
    throw 'Log in as the target user before uninstalling so its startup entry can be removed from the correct loaded hive.'
}
$Service = Stop-PowerServiceSafely $DataDirectory
if ($Service) { Invoke-Checked sc.exe @('delete','AAGLockLock') | Out-Null }
foreach ($ShortcutName in @('AAG - InputLock.lnk', 'AAG LockLock.lnk')) {
    $ShortcutPath = Join-Path $env:ProgramData ('Microsoft\Windows\Start Menu\Programs\' + $ShortcutName)
    Assert-PlainPath $ShortcutPath
    if (Test-Path -LiteralPath $ShortcutPath) { Remove-Item -LiteralPath $ShortcutPath -Force }
}
foreach ($Directory in @($InstallDirectory, $DataDirectory)) {
    if (Test-Path -LiteralPath $Directory) { Remove-Item -LiteralPath $Directory -Recurse -Force }
}
if (Get-ItemProperty -LiteralPath $UserRunKey -Name 'AAG LockLock' -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -LiteralPath $UserRunKey -Name 'AAG LockLock'
}
Write-Host "AAG - InputLock removed for $TargetSid. Rollback backups were preserved."
Write-Host 'User settings are retained. Remove them from the target user session without elevation if desired.'
