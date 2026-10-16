# Independent temporary lid-ignore

InputLock owns a logind `handle-lid-switch` inhibitor and evdev lid grabs while
the explicitly armed tray lease is active. Disabling releases those restrictions
immediately. The normal logind policy, or another installed lid owner, can resume
ownership. InputLock does not call AAG Power APIs or test retired AAG services.
AAG Power's competitor detection can yield and reclaim its inhibitor without any
service restart or configuration change.

Arming still requires an open lid, fresh CPU/chassis/battery telemetry, an active
authorized graphical session and a connected controller. The existing four-minute
closed-lid limit, shortened to two minutes on battery or sustained high CPU load,
thermal dwell/hysteresis, critical-battery checks and bounded telemetry-loss grace
remain intact. Unknown lid state is supervised conservatively as closed.

The compatibility wire field `stack_ready` now means native protection readiness.
`sample.protection_errors` reports the actual refusal. The probe checks logind
ordinary suspend support, blocking sleep inhibitors, the kernel suspend-success
counter and effective InputLock service/failure/watchdog configuration. Logind's
`challenge` capability result is valid for the unprivileged monitor: only the
fixed root helper requests suspend, with interactive authentication disabled.
There is no AAG readiness exemption that simply sets this field true.

## Protection and failure sequence

1. At a deadline or safety trigger, release evdev grabs and the low-level lid
   inhibitor, then durably record a generation-tagged request. Failed release
   terminates the daemon so systemd recovery can close its descriptors.
2. `input-lock-protect.service` verifies the current boot/generation, release
   acknowledgment, open-lid cancellation and native capability. It requests only
   `systemctl --no-ask-password --check-inhibitors=yes suspend`. Native OS hooks
   and inhibitors remain effective. A request accepted by systemd is not success.
3. Confirm a new successful `systemd-suspend.service` execution plus an increased
   kernel suspend-success counter within 120 seconds of awake monotonic time.
   No AAG receipt or wakeguard state is read. This confirms a kernel cycle, not
   peripheral or storage recovery beyond systemd's result.
4. Primary failure, daemon crash or the 20-second watchdog starts
   `input-lock-failsafe.service`. It stops InputLock synchronously before one
   final ordinary suspend attempt, including when the journal is corrupt.
   Already-running sleep transitions are not duplicated. Each helper has a
   165-second systemd timeout. Automatic daemon restart is disabled to prevent
   new grabs and overwritten evidence racing recovery.
5. Terminal failure persists `/var/lib/input-lock-protection/failure.json`, blocks
   subsequent arming and remains visible in the journal. An `ExecStopPost` handler
   also records externally terminated recovery (including timeout). Normal
   results are recorded in `/var/lib/input-lock/safety-action-result.json`; the
   tray shows a failure state when the daemon is available to receive it.

InputLock cannot guarantee physical sleep when the kernel, logind, systemd, an
administrator's inhibitor or a native hook refuses it. In that case its own
suppression is released and re-arming is blocked; the machine may still be awake.
There is no forced suspend, shutdown, hibernate, inhibitor bypass or infinite retry.
**An absolute guarantee of a sleeping closed-lid machine under native suspend
failure is not provided.** Deployment must not be represented as satisfying that
stronger guarantee. Open the lid and investigate a reported failure. Preserve
the latch and journals before an operator clears a failure after repair.

## Guarded upgrade and rollback

The generic installer includes both InputLock protection units. For the inspected
beta.4 installation use `tools/install_native_lid.py`, whose default mode is
read-only. It rejects unknown installed hashes, unsafe ownership, local unit
overrides, active protection jobs, stale telemetry, a closed lid, locked inputs
or unavailable native capability. It never arms lid-ignore or starts either
protection unit as a test.

Run the read-only preflight with the system Python. A normal terminal command
`sudo /usr/bin/python3 -I -B tools/install_native_lid.py --install` from the project
root performs the guarded upgrade. It creates a verified, root-owned rollback
backup under the desktop user's `~/ARCHIVE/InputLock`, preserves installed code,
configuration, units and recovery state, and records source/installed hashes.
It replaces only three Python modules and three InputLock units, reloads systemd,
starts InputLock disarmed, checks readiness and restarts the user's notifier.
It never changes or restarts AAG Power. Host authentication and writable system
paths are required; a sandboxed source edit is not an installation.

On deployment failure the installer restores the previous files and leaves
InputLock stopped. Manual rollback uses the same script with `--rollback` and
the exact printed backup directory, with the lid open. It preserves current
recovery evidence rather than restoring an old journal. Start InputLock manually
only after reviewing the failure. The new protection-state directory and evidence
are retained during rollback/uninstall.

Isolated tests are not physical acceptance. No lid close, sleep, hibernate,
reboot, hotplug, forced action or AAG Power test is needed for these source checks.
