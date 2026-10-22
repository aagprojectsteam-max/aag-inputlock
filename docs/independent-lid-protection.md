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
**not** start any closed-lid countdown. Telemetry counts as lost only
when a whole sensor category (for example every CPU source) has produced no
value for at least 30 seconds of distinct samples. A readiness-probe failure
(another component's suspend already `preparing`, or a hard `block`
inhibitor) is never telemetry loss: it only prevents arming, and while logind
reports `preparing` supervision takes no action. Lost device
identity, controller/session, watchdog or daemon are other fail-safe
exceptions: running unmonitored with a closed lid must not be allowed.

## Thermal policy (2.0.0-beta.5)

Software policy only; the firmware and kernel trips at 110 °C are untouched.
Evaluation happens only when a **new** telemetry sample arrives (about every
2 seconds), never on every daemon tick, and uses the per-sensor median of the
last 5 samples. Timers count time spent **at or above** a level, so a reading
that flaps across a threshold still accumulates. CPU decisions use TCPU,
`x86_pkg_temp` and coretemp `temp1` (package); per-core readings are kept for
corroboration and logging only.

| Level | CPU | Chassis SEN1/SEN2 | NVMe |
|---|---|---|---|
| Warning (notify only) | median ≥95 °C for 30 s | ≥65 °C for 60 s | ≥77 °C for 20 s |
| Protective suspend | median ≥104 °C for 60 s (≤2 samples below 101 °C tolerated) | ≥70 °C for 60 s | ≥80 °C for 20 s |
| Emergency | ≥107 °C raw for 3 consecutive samples, confirmed by a second CPU source within 6 °C; or one source ≥108 °C for 10 s | ≥73 °C for 10 s | — |

No action is ever taken on a single sample. Critical battery acts only while
discharging (`discharging` true or AC absent). The samples inside the dwell
window and the per-sensor evidence are kept in `last_decision`.

## Sleep window

Between `PrepareForSleep(true)` and resume plus one controller timeout, the
controller and lease deadlines are paused and the daemon's systemd watchdog
is extended to 300 seconds (restored to 20 seconds afterwards). The
system-sleep hook calls `input-lock prepare-sleep` before and
`input-lock resume-sleep` after a sleep. A kernel freeze that stalls for
20 seconds therefore no longer kills the daemon or drops lid-ignore.

## When lid-ignore turns itself off

Every disable is logged as `event=LID_IGNORE_DISABLED reason=…
requested_by_user=…` and published as `last_lid_disable` in the daemon
state. When the user did not request it, the tray shows a **critical,
persistent** notification that names the reason and offers "Re-enable…",
which goes through the normal confirmation dialog and requires an open lid.
Lid-ignore is never re-armed automatically. Stopping the tray
(`input-lock-notifier.service`) also ends lid-ignore; this is by design.

## Preventive CPU cooling without bypassing thermal protection

A short, elevated CPU-package reading can cross the existing InputLock software
thermal-emergency limit even when total CPU utilization is low, and then trigger
an ordinary OS safety suspend. Removing the old timed lid deadline did not
remove this explicitly approved safety intervention.

While the user explicitly enables continuous lid-ignore, the **per-user tray**
now acquires a temporary power-profiles-daemon
`net.hadess.PowerProfiles.HoldProfile("power-saver", reason, app_id)`
lease **before** asking the daemon to arm the lid inhibitor. The cooling
lease remains active while lid-ignore is effective, even if the lid reopens;
this prevents renewed CPU boost spikes when the user closes it again.

This is a reversible profile **hold**, not a persistent choice: the user's
previous `balanced` or `performance` selection is restored automatically by
power-profiles-daemon when the lease ends. The tray uses an isolated private
system D-Bus connection and explicitly releases the hold on disable, session
disconnect and exit. If D-Bus fails, closing that connection releases any
unconfirmed hold. A power-profiles-daemon owner restart reacquires a fresh
hold; the tray verifies the **effective** profile is actually
`power-saver`. A failed/inapplicable cooling lease prevents arming. If the
effective profile changes while lid-ignore is on (for example a manual Power
Mode pick in Quick Settings, which makes power-profiles-daemon drop all
holds), the tray keeps lid-ignore, re-acquires the power-saver hold at once
and notifies: "Power mode was changed manually; power-saver restored while
Ignore Lid Close is on". Only when a fresh hold is still ineffective after one
retry does the tray disable lid-ignore, and the critical notification then
names that reason. There are no privileged daemon writes to sysfs.

**No thermal thresholds, hysteresis/dwell, systemd sleep hooks, root safety
units, battery-critical rules or genuine sleep inhibitors are bypassed.** If
the CPU/chassis really remain dangerously hot even under cooling, InputLock
will still request protective suspend. Users must not close an unventilated
laptop inside a bag; a software hold cannot guarantee hardware cooling.

When thermal intervention occurs, the durable
`last_decision.thermal_evidence` additionally records each known sensor
identity, observed temperature, high/emergency thresholds and instantaneous
risk; it includes candidate/intervention durations. This makes future
thermal emergency diagnoses evidence-based rather than inferring a specific
sensor from the generic `THERMAL_EMERGENCY` reason. No continuous raw
high-frequency temperature recording is introduced.

## GNOME's separate 15-minute idle suspend on battery

The inspected GNOME power configuration has
`sleep-inactive-battery-type='suspend'` and
`sleep-inactive-battery-timeout=900`. Removing the InputLock timer
alone cannot satisfy continuous operation, since GNOME could otherwise
request ordinary idle sleep after 15 minutes on battery.

The authenticated **per-user tray process** now obtains a temporary
`org.gnome.SessionManager.Inhibit(flags=8)` lease for **idle only**
while the effective lid-ignore state is on. This is held alongside the
reversible CPU cooling profile. It prevents GNOME from classifying its
session as idle while explicitly requested to remain running. It **does not** request `flags=4` (suspend inhibition), hold
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
4. Primary failure, daemon crash or the 20-second watchdog (300 seconds during
   a sleep transition) starts
   `input-lock-failsafe.service`. It stops InputLock synchronously before one
   final ordinary suspend attempt, including when the journal is corrupt.
   Already-running sleep transitions are not duplicated. Each helper's systemd
   start timeout exceeds its suspend-observation budget by at least 60 seconds. Automatic daemon restart is disabled to prevent
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
the latch and journals before an operator clears a failure after repair:

```bash
journalctl -u input-lock-failsafe.service -b --no-pager
sudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure
```

`acknowledge-failure` archives the latch instead of deleting it.
`input-lock status` and `input-lock doctor` print this hint when arming is
refused because a previous emergency suspend failed.

## Upgrade and rollback

Use `sudo ./update.sh` (or `sudo ./install.sh`). Since 2.0.0-beta.5 the
installer:

- refuses a checkout that another account could modify (owner must be root or
  the desktop user; group write only for root's group or the owner's private
  group; no symbolic links), from `/` down to every payload file;
- copies the payload into a root-owned staging directory and compiles and
  validates it there, including the effective configuration, **before** the
  running service is stopped; root never runs Python from the checkout;
- backs up every managed path under `/var/backups/input-lock/<timestamp>`;
- on a failure after it started changing files, stops the daemon, restores the
  backup automatically and restarts the previous service and tray if they
  were running (the daemon always starts unlocked and disarmed);
- waits for the daemon's `READY=1` and control socket; a later problem (for
  example a doctor FAIL item or a missing GNOME session) is reported as a
  post-install warning and never stops the new daemon;
- removes files in `/usr/lib/input-lock` that the release does not install
  (such as manual `*.bak-*` copies); they remain in the backup.

The older `tools/install_native_lid.py` was retired to `tools/legacy/` and
refuses to run: its baselines matched no current installation, it omitted
payload files and it imported code as root from the unverified checkout.

`uninstall.sh` stops `input-lock-protect.service` and
`input-lock-failsafe.service` (and confirms they stopped) before deleting their
files, and archives `/var/lib/input-lock` and `/var/lib/input-lock-protection`
(including any latch) to a root-only folder under `/var/backups/input-lock`
before removing them, so a later reinstall does not inherit a stale latch.

Isolated tests are not physical acceptance. No lid close, sleep, hibernate,
reboot, hotplug, forced action or AAG Power test is needed for these source checks.
