# Verification and physical acceptance

## Automated tests

```bash
python3 tools/run_tests.py shared
python3 tools/run_tests.py linux
bash tests/test_config.sh
python3 -m build
python3 tools/check_artifacts.py dist --linux
```

The first group requires only the Python standard library. The Linux group additionally requires the distro evdev/pyudev/GI/GTK modules. Tests write only temporary fixtures and use a temporary local socket. Native device, session, battery and power operations are injected, never executed. The safety suite covers deadlines, temperature dwell/hysteresis, missing sensors/dependencies, AC/battery, owner loss, stale requests, abnormal startup, durable journals and independent ordinary suspend/recovery (all power calls mocked).

The artifact checker imports from the built wheel in isolated Python mode. Omit `--linux` when Linux native dependencies are unavailable; that still checks portable imports and complete archive inventory. The package CI job runs this portable check.

On Windows, after installing the declared build dependencies:

```powershell
python tools/run_tests.py shared
python tools/run_tests.py windows-native
```

Native automated tests open only temporary named pipes and read a module handle. They do not install input hooks or alter power settings. CI also parses PowerShell and builds the unsigned executables. Windows hardware acceptance is a separate step.

## Production deployment verification

The beta.4 reference deployment completed on 2026-09-21. The checklist below
is the required procedure for future deployments or requalification; the current
reference result is recorded in
[BETA4-QUALIFICATION.md](BETA4-QUALIFICATION.md).

1. Authenticate for a complete root-readable installed-code/configuration/unit/recovery-state backup before replacement.
2. Run the reviewed installer/updater. It must finish unlocked, with effective lid-ignore off. Do not run `input-lock-protect.service` directly.
3. Compare source/installed hashes and inspect unit ownership/hardening. Check `input-lock status --json`, `input-lock doctor`, both LockLock journals and `systemd-inhibit --list`.
4. Verify no owned legacy `CriticalPowerAction=Ignore` drop-in remains, GNOME recovery completed, and live UPower critical action is non-Ignore. **Separately qualify the pre-existing OS critical action against the AAG platform acceptance record**; the observed HybridSleep baseline is not accepted by the inspected plain-hibernate report.
5. Confirm the daemon sees CPU and both chassis sensors, AC/battery, current UPower/logind observations and the effective InputLock protection/recovery units and native suspend capability. Unsupported prerequisites must prevent arming.

## Short supervised lid acceptance — not performed by development tests

Use a ventilated desk, an open lid, idle/light work and a present operator. Keep another recovery channel available. Do not put the laptop in a bag, stress it, drain its battery or wait for high temperature.

1. With lid open, explicitly enable continuous lid-ignore from the tray. Confirm `LID_IGNORE_ARMED`, a connected controller, GNOME's idle-only session inhibitor (flag 8), and the lid-switch inhibitor. Native suspend and critical-battery protection must remain enabled.
2. Close for **5–10 seconds only** during supervised acceptance. Observe `LID_CLOSED_MONITORED`, fresh sensor values, `lid_ignore_mode=continuous_until_disabled`, `deadline=null`, and no timed protective action. Do **not** physically test hours of closure; source tests simulate that with injected monotonic clocks.
3. Reopen immediately. Confirm monitoring ended and return to armed/open state. Disable lid-ignore and confirm neither InputLock lid-grab nor its GNOME idle-only inhibitor remains. GNOME's original idle power settings must be unchanged.
4. Exercise warning/high-risk/emergency and failure paths through the automated simulations, not by raising the machine's temperature or waiting for an unattended shutdown.

Actual suspend/resume, storage release/recovery, modem/GNSS recovery, session logout and reboot acceptance must be scheduled separately with the existing AAG procedures. Development did none of these. A queued systemd transition may already be beyond cancellation when the lid reopens.

## Input acceptance — separate opt-in operation

Use a verified recovery channel and a finite timeout before any real input lock. Start with `input-lock lock mouse --timeout 5` so the keyboard remains available. Then validate primary/emergency chords, allowed-key forwarding, per-mode behavior, session switching and hotplug under finite timeouts. Include the emergency chord while an unrelated key is held. No source test performs these physical input locks.

The existing `tests/test_toggle.sh --armed` and Windows `test-hardware.ps1 -Armed` are explicit hardware operations, not ordinary CI tests. Read them and arrange recovery before use. Do not interpret a successful source suite as hardware acceptance.
