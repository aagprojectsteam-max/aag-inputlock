# AAG - InputLock for Windows 11 x64 — beta validation build

This build runs without a terminal. `LockLockAgent.exe` owns the notification
area icon and documented low-level input hooks in the interactive user session.
`LockLockService.exe` is a non-interactive, narrowly scoped helper that journals
and restores optional lid power-policy changes.

## Build

From a Windows PowerShell prompt with Python 3.11 or newer:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
./windows/build.ps1
```

The unsigned bundle is created in `windows/out`. Sign all executables and
installer scripts with the publisher certificate before distributing it.

## Install

Open an elevated PowerShell in `windows/out`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
./install.ps1 -TargetSid "S-1-5-21-REPLACE-WITH-TARGET-USER-SID"
```

Use the tray Settings window to enable start-at-login. This affects the user
agent only; the small power helper is installed machine-wide.

## Safe first test

Arrange another recovery route first. Run:

```powershell
./test-hardware.ps1
./test-hardware.ps1 -Armed
```

Do not remove the auto-unlock timeout from the first manual trials. Agent crash
or exit removes the hooks. The temporary lid policy uses a 45-second renewable
lease; agent loss makes the service restore the journaled policy automatically.

## Current Windows feature status

| Feature | Status before real-hardware sign-off |
|---|---|
| Tray, settings and local IPC | Implemented; Windows acceptance pending |
| Keyboard lock and emergency chord | Experimental |
| Mouse and precision-touchpad pointer lock | Experimental |
| Automatic lock | Experimental |
| Cursor hiding | Unsupported |
| Temporary lid policy and battery threshold | Experimental |
| Independent touchscreen lock | Unsupported and disabled |

Windows Secure Attention (`Ctrl+Alt+Del`) is never blocked. F12 is not accepted
as the primary Windows shortcut; the fixed emergency chord is recognized by the
hook and does not use `RegisterHotKey`.

## Uninstall

Run elevated:

```powershell
./uninstall.ps1 -TargetSid "S-1-5-21-REPLACE-WITH-TARGET-USER-SID"
```

Use `-TargetSid "S-1-5-21-REPLACE-WITH-TARGET-USER-SID"` only when the per-user preferences should also be
removed. Uninstall requests unlock, stops the service so it restores its power
journal, and then removes the installed files. Update and uninstall abort before
replacing/removing binaries if the power restoration journal still exists.

## Beta.2 remediation and acceptance

Run the installer elevated with an explicit `-TargetSid` obtained from the intended user's non-elevated session (`whoami /user`). Exit running AAG - InputLock agents first. The installer never launches an elevated input agent; launch the Start Menu shortcut from the target user's desktop afterward. Uninstall also requires the installed `-TargetSid` and its loaded user registry hive. Service ACLs use well-known numeric SIDs and every native command exit is checked. Backups and failed power-restoration journals are retained.

Global cursor hiding is unsupported and disabled. Input remains experimental until native hardware acceptance. Run `python tools/run_tests.py shared`, then `python tools/run_tests.py windows-native` before building. Pipe I/O uses a single deadline and nonblocking message mode with a bounded consumption ACK. No worker calls Tk directly. The session monitor requires the active console's ordinary input desktop; disconnect/secure-desktop transitions disable locking eligibility.

Power plan changes end the current lease, restore recorded values without intentionally selecting the old plan, and require explicit disable/rearm. Native Windows power-setting APIs do not provide a transaction with unrelated simultaneous administrator changes; test plan-switch races on the target Windows build. The Linux AAG thermal/suspend adapter is not a Windows feature.

User settings are retained at uninstall. Any optional deletion belongs in the target user session without elevation, avoiding privileged traversal of user-owned profile paths.
