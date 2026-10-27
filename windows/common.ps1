# Shared installer preconditions. Dot-source only from the packaged directory.
function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed with exit code $LASTEXITCODE" }
}

function Assert-PlainPath {
    param([string]$Path)
    $CurrentPath = [IO.Path]::GetFullPath($Path)
    while ($CurrentPath) {
        if (Test-Path -LiteralPath $CurrentPath) {
            $Item = Get-Item -LiteralPath $CurrentPath -Force
            if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing an elevated operation through a reparse point: $CurrentPath"
            }
        }
        $ParentPath = Split-Path -Parent $CurrentPath
        if ($ParentPath -eq $CurrentPath) { break }
        $CurrentPath = $ParentPath
    }
}

function Assert-Administrator {
    $Principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script from an elevated PowerShell window.'
    }
}

function Resolve-TargetProfile {
    param([string]$TargetSid)
    $Sid = [Security.Principal.SecurityIdentifier]::new($TargetSid)
    if (-not $Sid.IsAccountSid()) { throw 'TargetSid must identify a user account.' }
    $ProfileKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$TargetSid"
    $ProfilePath = [Environment]::ExpandEnvironmentVariables((Get-ItemProperty -LiteralPath $ProfileKey -Name ProfileImagePath).ProfileImagePath)
    if (-not [IO.Path]::IsPathRooted($ProfilePath)) { throw 'The target user profile is not an absolute path.' }
    return $ProfilePath
}

function Assert-AgentStopped {
    # Never terminate another user's agent or send commands to the elevation
    # account by accident. Exit the tray in its interactive session first.
    $Agents = @(Get-Process -Name LockLockAgent -ErrorAction SilentlyContinue)
    if ($Agents.Count) { throw 'Exit AAG - InputLock from each running user session before installing or removing its binaries.' }
}

function Stop-PowerServiceSafely {
    param([string]$DataDirectory)
    $Service = Get-Service -Name AAGLockLock -ErrorAction SilentlyContinue
    $Journal = Join-Path $DataDirectory 'power-restore.json'
    if ($Service) {
        if ($Service.Status -ne 'Running' -and (Test-Path -LiteralPath $Journal)) {
            Invoke-Checked sc.exe @('start', 'AAGLockLock') | Out-Null
            $Service.WaitForStatus('Running', [TimeSpan]::FromSeconds(20))
        }
        $Service.Refresh()
        if ($Service.Status -ne 'Stopped') {
            Invoke-Checked sc.exe @('stop', 'AAGLockLock') | Out-Null
            $Service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(20))
        }
    }
    if (Test-Path -LiteralPath $Journal) {
        throw 'Power restoration is incomplete. Service, journal and installed files have been retained.'
    }
    return $Service
}
