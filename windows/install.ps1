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
foreach ($Name in @('LockLockAgent.exe', 'LockLockCli.exe', 'LockLockService.exe')) {
    $SourceFile = Join-Path $PSScriptRoot $Name
    if (-not (Test-Path -LiteralPath $SourceFile -PathType Leaf)) { throw "Missing build output: $SourceFile" }
    Assert-PlainPath $SourceFile
}
$FinalConfig = Join-Path $DataDirectory 'service.json'
if (Test-Path -LiteralPath $FinalConfig) {
    $PreviousSid = (Get-Content -LiteralPath $FinalConfig -Raw | ConvertFrom-Json).authorized_sid
    if ($PreviousSid -ne $TargetSid) { throw 'Uninstall the existing target-user deployment before changing TargetSid.' }
}
# Preserve installed code/configuration before any replacement.
$BackupDirectory = Join-Path $env:ProgramData ('AAG\LockLock-Backups\' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ'))
Assert-PlainPath $BackupDirectory
New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
Invoke-Checked icacls.exe @($BackupDirectory, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
if (Test-Path -LiteralPath $InstallDirectory) { Copy-Item -LiteralPath $InstallDirectory -Destination (Join-Path $BackupDirectory 'program') -Recurse }
if (Test-Path -LiteralPath $DataDirectory) { Copy-Item -LiteralPath $DataDirectory -Destination (Join-Path $BackupDirectory 'data') -Recurse }
$ExistingService = Stop-PowerServiceSafely $DataDirectory
New-Item -ItemType Directory -Force -Path $InstallDirectory, $DataDirectory | Out-Null
Invoke-Checked icacls.exe @($DataDirectory, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
Invoke-Checked icacls.exe @($InstallDirectory, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-32-545:(OI)(CI)RX') | Out-Null
foreach ($Name in @('LockLockAgent.exe', 'LockLockCli.exe', 'LockLockService.exe')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $Name) -Destination (Join-Path $InstallDirectory $Name) -Force
}
$TemporaryConfig = Join-Path $DataDirectory 'service.json.tmp'
$ServiceConfig = @{ authorized_sid = $TargetSid } | ConvertTo-Json
[IO.File]::WriteAllText($TemporaryConfig, $ServiceConfig + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $TemporaryConfig -Destination $FinalConfig -Force
$ServiceExe = Join-Path $InstallDirectory 'LockLockService.exe'
$BinaryPath = '"' + $ServiceExe + '"'
if ($ExistingService) {
    Invoke-Checked sc.exe @('config','AAGLockLock','binPath=', $BinaryPath,'start=','auto','DisplayName=','AAG - InputLock Power Helper') | Out-Null
} else {
    Invoke-Checked sc.exe @('create','AAGLockLock','binPath=', $BinaryPath,'start=','auto','DisplayName=','AAG - InputLock Power Helper') | Out-Null
}
$ServiceSddl = "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)(A;;LCRPWPLOCR;;;${TargetSid})"
Invoke-Checked sc.exe @('sdset','AAGLockLock',$ServiceSddl) | Out-Null
Invoke-Checked sc.exe @('failure','AAGLockLock','reset=','60','actions=','restart/2000/restart/5000') | Out-Null
Invoke-Checked sc.exe @('start','AAGLockLock') | Out-Null
(Get-Service AAGLockLock).WaitForStatus('Running', [TimeSpan]::FromSeconds(20))
$Programs = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs'
Assert-PlainPath $Programs
$LegacyShortcutPath = Join-Path $Programs 'AAG LockLock.lnk'
Assert-PlainPath $LegacyShortcutPath
if (Test-Path -LiteralPath $LegacyShortcutPath) { Remove-Item -LiteralPath $LegacyShortcutPath -Force }
$ShortcutPath = Join-Path $Programs 'AAG - InputLock.lnk'
Assert-PlainPath $ShortcutPath
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $InstallDirectory 'LockLockAgent.exe'
$Shortcut.WorkingDirectory = $InstallDirectory
$Shortcut.Save()
Write-Host "AAG - InputLock installed for $TargetSid. Rollback files: $BackupDirectory"
Write-Host 'Start AAG - InputLock from the target user desktop (without elevation).'
Write-Warning 'Windows native and hardware acceptance is required. Global cursor hiding and independent touchscreen locking are unavailable.'
