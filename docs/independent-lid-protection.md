# Continuous lid-ignore with independent safety supervision

InputLock owns a logind `handle-lid-switch` inhibitor and evdev lid grabs
while the explicitly armed local tray lease is active. Disabling the option
releases these restrictions immediately. The normal logind policy or another
installed lid manager (including AAG Power) can resume ownership.
InputLock does not call AAG Power APIs or depend on retired AAG services.

## No timeout while healthy

**The 120/240-second closed-lid deadline was removed by explicit owner
approval on 2026-10-18.** A normal lid closure no longer causes any
time-based sleep after two minutes, four minutes, hours or days. Changing
between AC/battery or maintaining high processor utilization does not
shorten a non-existent deadline. Sustained load can still raise measured
temperatures and trigger the independent thermal protection.

Arming requires an open lid, fresh CPU/chassis/battery telemetry, an
authorized active graphical session and a connected tray controller.
While the lid is closed, high-risk temperature or emergency temperature
(with hysteresis/dwell) and genuinely critical battery cause ordinary
system suspend after InputLock has released its inhibitor and device grabs.
Low battery (above the critical threshold), elevated-but-noncritical
temperature, AC changes and high CPU load may generate warnings but do
**not** start any closed-lid countdown. Temporary loss of telemetry is
granted no more than 15 seconds before fail-safe intervention. Lost device
identity, controller/session, watchdog or daemon are other fail-safe
exceptions: running unmonitored with a closed lid must not be allowed.

## GNOME's separate 15-minute idle suspend on battery

The inspected GNOME power configuration has
`sleep-inactive-battery-type='suspend'` and
`sleep-inactive-battery-timeout=900`. Removing the InputLock timer
alone cannot satisfy continuous operation, since GNOME could otherwise
request ordinary idle sleep after 15 minutes on battery.

The authenticated **per-user tray process** now obtains a temporary
`org.gnome.SessionManager.Inhibit(flags=8)` lease for **idle only**
while the effective lid-ignore state is on. This prevents GNOME from
classifying its session as idle while explicitly requested to remain
running. It **does not** request `flags=4` (suspend inhibition), hold
a logind sleep inhibitor, change any GNOME GSettings value, suppress
the OS's critical-battery protection or obstruct a manual/thermal
suspend. The cookie is unregistered when the feature is disabled or
the tray disconnects. It is held on a **private session D-Bus connection**
which is always closed on disable, including when Inhibit/Uninhibit
acknowledgment is lost, so orphaned inhibitors cannot persist. A GNOME
session-manager owner change reacquires the cookie.
If GNOME idle suppression becomes unavailable, the tray requests
a guarded lid-ignore disable; if this communication fails, it drops
its controller connection to engage the existing daemon
`CONTROLLER_LOST` fail-safe. This is a deliberate dependency on the
already-required session controller, not on any AAG Power process.

The setting is **continuous while healthy**, not persistent across
logouts, InputLock daemon restarts, reboot, missing telemetry or safety
actions. A new session/daemon starts disarmed and must be explicitly
armed again with the lid open. Firmware emergency shutdowns and explicit
operator sleep remain under the operating system's authority. Never use
the laptop closed inside a bag or without ventilation.

## Durable evidence / backwards compatibility

The read-only safety status publishes
`lid_ignore_mode="continuous_until_disabled"`, a `closed_seconds`
counter for diagnostics, and `deadline=null`.
The obsolete `deadline_shortened_by=[]` and
`last_decision.time_limit_seconds=null` fields are retained only
for older consumers; no active code uses them to schedule sleep.
The journal records `CONTINUOUS_MONITORING_STARTED` /
`CONTINUOUS_MONITORING_ENDED` on lid changes, plus
`protective_decision_evidence` on genuine risk or supervision loss.
No normal `CLOSED_LID_DEADLINE` or
`DEADLINE_SHORTENED_*` events are generated.

The compatibility wire field `stack_ready` now means native protection readiness.
`sample.protection_errors` reports the actual refusal. The probe checks logind
ordinary suspend support, blocking sleep inhibitors, the kernel suspend-success
counter and effective InputLock service/failure/watchdog configuration. Logind's
`challenge` capability result is valid for the unprivileged monitor: only the
fixed root helper requests suspend, with interactive authentication disabled.
There is no AAG readiness exemption that simply sets this field true.

## Protection and failure sequence

1. At a genuine safety/fail-safe trigger, release evdev grabs and the low-level lid
   inhibitor, then durably record a generation-tagged request. Failed release
   terminates the daemon so systemd recovery can close its descriptors.
2. `input-lock-protect.service` verifies the current boot/generation, release
   acknowledgment, open-lid cancellation and native capability. It requests
   ordinary Suspend through logind's `SuspendWithFlags(0)` D-Bus API on
   systemd 257+. Hard `block` inhibitors are **still enforced for root**;
   normal `delay` inhibitors/hooks still participate. Weak `block-weak` locks
   (e.g. GNOME Session's own `sleep: block-weak`) intentionally do not block
   the root helper, matching documented native logind semantics. The previous
   flag `SD_LOGIND_ROOT_CHECK_INHIBITORS` (value 1) explicitly promoted weak
   locks to hard blockers and caused a permanent false-unready state on the
   October 18 Ubuntu 26.04 host. We do **not** use
   `SD_LOGIND_SKIP_INHIBITORS` (0x10), bypass hard blocks, or ignore a real
   `sleep: block` inhibitor. This also avoids the separate CLI multi-login
   refusal observed on 2026-10-16. An accepted D-Bus request is not proof
   of a completed kernel transition.
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

Run the read-only preflight with the system Python. For the 2026-10-17
boot failure only, `--recover-known-boot-failure` requires a reviewed source
SHA-256 manifest and exact installed-file and historical-failure hashes. An
administrator must explicitly add `--acknowledge-failed-protection` for that
known recovery installation. This does **not** retroactively make the failed
sleep protection successful; the original latch is backed up and renamed for
forensic retention. Do not use this special path on other machines or failures.
The installer creates a verified root-only backup under root's `ARCHIVE/InputLock`,
preserves installed code, configuration, units and recovery state, and records
source/installed hashes. It replaces the four reviewed Python modules plus
InputLock's reviewed service unit and dependency units, reloads systemd, starts
InputLock disarmed, checks readiness and reconnects the user notifier. It never
changes or restarts AAG Power. Host authentication and writable system paths
are required; a sandboxed source edit is not an installation.

On deployment failure the installer restores the previous files and leaves
InputLock stopped. Manual rollback uses the same script with `--rollback` and
the exact printed backup directory, with the lid open. It preserves current
recovery evidence rather than restoring an old journal. Start InputLock manually
only after reviewing the failure. The new protection-state directory and evidence
are retained during rollback/uninstall.

Isolated tests are not physical acceptance. No lid close, sleep, hibernate,
reboot, hotplug, forced action or AAG Power test is needed for these source checks.
