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
`x86_pkg_temp` and coretemp `temp1` (package); per-core readings are kept as
evidence and for logging only.

| Level | CPU | Chassis SEN1/SEN2 | NVMe |
|---|---|---|---|
| Warning (notify only) | median ≥95 °C for 30 s | ≥65 °C for 60 s | ≥77 °C for 20 s |
| Protective suspend | median ≥104 °C for 60 s (≤2 samples below 101 °C tolerated) | ≥70 °C for 60 s | ≥80 °C for 20 s |
| Emergency | ≥107 °C raw for 3 consecutive samples, confirmed by an independent witness (a chassis zone or the NVMe drive at or above its own warning level); or one source ≥108 °C for 10 s | ≥73 °C for 10 s | — |

How the table is implemented (`THERMAL_RULES` in `input_lock_safety.py`):

- The temperatures are the EliteBook values. Each limit is capped below the
  sensor's own critical trip: CPU `min(95, trip−15)` / `min(104, trip−6)` /
  `min(107, trip−3)`, chassis `min(65, trip−15)` / `min(70, trip−10)` /
  `min(73, trip−7)`, NVMe `min(77, trip−10)` / `min(80, trip−7)`.
- Once a timer has started it keeps running while the median stays within the
  hysteresis (3 °C for CPU, 2 °C for chassis and NVMe) below the limit. For the
  CPU protective level up to 2 samples below that (below 101 °C) are tolerated
  without a reset.
- A level needs at least 3 distinct samples as well as its dwell time. A gap of
  more than 10 s between samples (a suspend, a frozen telemetry thread) resets
  the timers instead of counting as dwell time.
- The "one source ≥108 °C for 10 s" rule uses raw readings; every other rule
  uses the per-sensor median (chassis emergency too, per sensor).
- CPU sources are the `TCPU` and `x86_pkg_temp` thermal zones and the
  coretemp/k10temp package channel (`Package id`/`Tctl`, else `temp1`).
- Sensors are grouped by physical origin. `TCPU`, `x86_pkg_temp` and the
  coretemp package channel (with its per-core readings) all read the same CPU
  package sensor, so one of them never confirms another. The 107 °C rule needs
  a witness from a different origin: `SEN1`, `SEN2` or the NVMe drive at or
  above its own warning level (65 °C chassis, 77 °C NVMe) in each of the 3
  samples. Without such a witness, or on hardware without chassis/NVMe
  sensors, only the "≥108 °C for 10 s" rule leads to an emergency.
- Flapping heat with low dips is a known, accepted limit (lead decision
  2026-10-22). A reading that alternates between a low and a high value on
  every sample, for example 100/108 °C or 99/109 °C (mean 104 °C), takes no
  protective action: the 5-sample median alternates as well, every dip below
  101 °C counts against the 2 tolerated samples, and the raw 108 °C rule needs
  10 s without a dip. The status shows the warning level, and the firmware and
  kernel 110 °C trips still protect the hardware. Sustained or slowly varying
  heat (for example 103/106 °C) acts after 60 s as in the table.

No action is ever taken on a single sample. Critical battery means ≤10 % while
discharging (`discharging` true or AC absent), or a UPower critical warning
level, seen in 2 samples at least 2 s apart. The samples inside the dwell
window and the per-sensor evidence are kept in
`last_decision.thermal_evidence`. While a closed lid is supervised,
`input-lock status` lists a whole missing telemetry category as
`Missing telemetry:` (`missing_categories` in the safety snapshot) while its
30-second loss timer runs.

## Sleep window

`input-lock.service` is `Type=notify` with `WatchdogSec=60s`: longer than
one 20-second kernel freezer timeout (`/sys/power/pm_freeze_timeout`), so a
single freeze outside a sleep window is not fatal. Between
`PrepareForSleep(true)` and resume plus one controller timeout (35 s), the
controller and lease deadlines are paused and the daemon's systemd watchdog
is extended to 300 seconds with `sd_notify WATCHDOG_USEC`. The system-sleep
hook calls `input-lock prepare-sleep` before and `input-lock resume-sleep`
after a sleep; the daemon also reacts to logind's `PrepareForSleep` signal,
and either source opens or closes the same window. A failing suspend can make
several 20-second freeze attempts inside one `PrepareForSleep` window (on
2026-10-22 two attempts each, about 105 s from `PrepareForSleep` to the final
failure); none of them kills the daemon or drops lid-ignore.

**Watchdog restore.** The window can close for supervision purposes (resume
signal, `post` hook, resume evidence or the cap), but the watchdog goes back
to 60 s only once logind reports `PreparingForSleep=false` (the
`PrepareForSleep(false)` signal or a telemetry sample read after the
extension). It is never restored while logind still reports
`PreparingForSleep=true`. When that state is unknown (no reading, or
telemetry stopped for more than 30 s) it stays extended until the window cap
has passed. The same `PreparingForSleep=false` reading tells the logind
monitor that the sleep is over, so it takes its delay inhibitor again even if
the `PrepareForSleep(false)` signal itself was missed.

**Window cap.** Read at runtime when the window opens:
`InhibitDelayMaxSec + 60 s + 3 × pm_freeze_timeout`, clamped to 150–300 s.
`InhibitDelayMaxUSec` comes from logind (read by the monitor's refresh
thread; 30 s if unknown) and `pm_freeze_timeout` from sysfs (20 s if
unreadable). With this host's 30 s and 20 s that is 30 + 60 + 3 × 20 = 150 s,
which covers the 105 s failing suspend above.

Who can open it, and how the daemon sees the signal in time:

- `prepare-sleep` and `resume-sleep` are accepted **only from root**: the
  system-sleep hook runs as root under `systemd-sleep`. The same commands from
  the user's own processes are refused with
  `prepare-sleep is accepted only from root (the system-sleep hook)`, so no
  same-user process can pause supervision or stretch the watchdog.
- The daemon holds a logind `sleep` inhibitor in mode **delay** (never
  `block`; polkit allows `input-lockd` exactly
  `org.freedesktop.login1.inhibit-delay-sleep`). Its `PrepareForSleep(true)`
  handler extends the watchdog first and only then releases the lock, so
  logind waits (at most `InhibitDelayMaxSec`, 5 s by default, 30 s on this
  host) until the extension is in place. The lock is taken again after
  resume.
- The logind monitor dispatches signals on its own private connection; the
  periodic session and UPower queries run on a separate thread and
  connection, so a signal is never queued behind them. Every monitor restart
  opens fresh connections, and a bus disconnect (for example a dbus-broker
  restart) stops the monitor so that the daemon restarts it. Each monitor run
  tags its events with a generation; events of a retired run (its refresh
  worker is joined for only 1 s) are dropped.

Inside the window:

- the safety policy is told about it (`note_sleep_window`) and still receives
  every sample, so evidence keeps accumulating; it takes no protective action
  while it knows a sleep is being prepared (for at most 300 s of its own), and
  `input-lock status` prints `OS sleep: in progress` (`os_sleep_in_progress`
  in the safety snapshot, `system_sleep_in_progress` in the state);
- a protection reason that arises (for example a lost tray controller) is
  deferred. At the end of the grace period it is acted on only if its cause
  still holds (controller still gone, session still lost, lid switch still
  missing or unreadable); thermal and telemetry causes are re-evaluated by the
  safety policy from fresh samples. A decision the policy itself already made
  is acted on as soon as the window closes;
- the window closes early on evidence that the machine is awake again:
  logind reporting `PreparingForSleep=false` in two consecutive samples, or a
  change of `/sys/power/suspend_stats/success` or `fail` **together with** a
  `PreparingForSleep=false` reading. The kernel bumps those counters after
  every failed freeze attempt, and one suspend can make several attempts
  while logind is still preparing, so a counter change alone never closes
  the window;
- a window without a resume signal or such evidence closes at the window cap
  (150 s on this host) of awake time (the monotonic clock does not run while
  suspended).

With an armed lid-ignore, a closed lid, a TCPU at 106 °C and a forged
`prepare-sleep` that never resumes, protection now acts after about 62 s
(user request refused) or, for a real root-opened window without any resume
evidence, at the window cap (150 s here), with the heat seen during the
window already counted. Firmware and hardware thermal trips are independent
of this window.

## When lid-ignore turns itself off

Every disable is logged as `event=LID_IGNORE_DISABLED reason=…
requested_by_user=… user_initiated=…` and published as
`last_lid_disable` (`reason`, `requested_by_user`, `user_initiated`, `at`)
in the daemon state.

- `requested_by_user` is true only for "set-lid-ignore false" (the tray's
  switch or `input-lock lid-ignore off`).
- `user_initiated` is also true for the user's own actions that end
  lid-ignore as a side effect: the physical emergency hotkey, the GNOME
  emergency binding, the TTY recovery chord, an `emergency-unlock` request
  (also the installer's), and settings changes requested over IPC by the
  tray or CLI (hotkey, lock kinds, allowed keys, reload). A `systemctl
  reload` (SIGHUP) is not user-initiated.
- When `user_initiated` is false, the tray shows a **critical, persistent**
  notification that names the reason and offers "Re-enable…", which goes
  through the normal confirmation dialog and requires an open lid. A
  user-initiated end gets an informational note instead.

The `reason` is a short key such as `controller-disconnected` (the tray's
connection closed or broke), `controller-lease-expired` (the tray stopped
sending heartbeats), `session-inactive`, `emergency-unlock`,
`settings-reset`, `thermal-emergency` or `telemetry-lost`; the journal line
also carries the internal detail. Before the daemon closes the tray's
controller connection for any reason, it sends the final state event (with
`last_lid_disable`) on it, best effort and without waiting, so the tray can
name the real cause instead of "service unavailable".

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
   kernel suspend-success counter within 240 seconds of awake monotonic time
   (`OBSERVE_BUDGET_SECONDS`). No AAG receipt or wakeguard state is read. This
   confirms a kernel cycle, not peripheral or storage recovery beyond
   systemd's result.
4. Primary failure, daemon crash or the 60-second watchdog (300 seconds during
   a sleep transition, until logind stops preparing) starts
   `input-lock-failsafe.service`. It stops InputLock synchronously before one
   final ordinary suspend attempt, including when the journal is corrupt or
   missing (a missing journal means nothing was pending: no request, no latch).
   Both helper units use `TimeoutStartSec=330s`, the 240-second observation
   budget plus at least 60 seconds for the steps before it. Automatic daemon
   restart is disabled to prevent new grabs and overwritten evidence racing
   recovery.
5. **The OS sleep path is never asked twice.** Neither helper requests a
   suspend, and neither latches, when logind is already `preparing`,
   `systemd-suspend.service` is still running, or it ran within the last
   180 seconds and failed. In recovery, a run within 180 seconds defers even
   when it succeeded, and so does a daemon that ended with `Result=watchdog`
   (it was frozen by the OS sleep freezer or wedged; it is stopped now and
   logind owns the closed lid again). The receipt then says
   `deferred-os-sleep-in-progress` (`transition_status` `DEFERRED`); the
   daemon treats it like a cancellation and leaves `PROTECTIVE_ACTION_PENDING`.
   Nothing is re-armed.

### Receipts and the failure latch

- **Receipt.** Each step of an eligible request writes a receipt (`result`,
  `transition_status`, `dispatch_accepted`, boot and generation). The
  authoritative copy is
  `/var/lib/input-lock-protection/safety-action-result.json` in the root-owned
  directory (`StateDirectory=input-lock-protection`, mode 0755). A copy is
  still written to `/var/lib/input-lock/safety-action-result.json`; the daemon
  reads the root-owned one first and falls back to the copy. Root never
  trusts that daemon-writable copy: recovery and `record-failure` read only
  the root-owned receipt. Both are written with `fchmod` on the temporary file
  before the rename, never through a symbolic link. Root reads the daemon's
  journal (`safety.json`) and the receipt through one
  `O_NOFOLLOW | O_NONBLOCK` descriptor, and only if it is a regular file of at
  most 1 MiB. The tray shows a failure state when the daemon is available
  to receive it.
- **Narrow latching.** `/var/lib/input-lock-protection/failure.json` is written
  only when InputLock's **own** suspend request was accepted by logind and the
  kernel transition then failed or was never confirmed. Refusals before
  dispatch, deferrals and cancellations do not latch.
- **No overwrite.** An existing latch keeps its first error as the headline;
  later failures are appended to `later_failures`. The failsafe unit's
  `ExecStopPost` (`input_lock_safety.py record-failure`, also run after a
  timeout or SIGKILL) appends its `SERVICE_RESULT` to an existing latch. Without
  one it latches only if this boot's root-owned receipt shows an accepted
  request that was never confirmed.
- **Staleness.** A latch records `boot_id` and the kernel
  `suspend_stats/success` counter. After a reboot, or once a later suspend has
  succeeded, it is **stale**: ordinary suspend demonstrably works again, so it
  no longer blocks arming and is reported as information. A new failure
  re-activates a stale latch. A latch without that evidence (older format) or
  one that cannot be read stays active.
- **Inspection.** `input-lock doctor` shows the probe result: a stale latch and
  unit drop-ins as `INFO`, an active latch as `FAIL` with the command below.
  The probe can also be run directly; it prints
  `{"ready": …, "errors": […], "info": […]}`:

  ```bash
  /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py check
  ```

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

`acknowledge-failure` never deletes the latch. It runs only as root and only
while `input-lock-protect.service`, `input-lock-failsafe.service` and
`systemd-suspend.service` are inactive. It reads the latch through one
`O_NOFOLLOW` descriptor (it must be a root-owned regular file), copies it to
`/root/ARCHIVE/InputLock/<UTC time>-failure-latch-<hash>/failure.json`,
verifies the copy's SHA-256, re-checks that the latch did not change meanwhile,
and then renames it in place to `failure-acknowledged-<sha256>.json`.
`input-lock status` and `input-lock doctor` print this hint when arming is
refused because a previous emergency suspend failed. A stale latch does not
need acknowledging to arm again; acknowledging it only archives it.

## Units

- `input-lock.service`: `Type=notify`, `NotifyAccess=main`, `WatchdogSec=60s`
  (extended to 300 s by the daemon from `PrepareForSleep(true)` until logind
  stops preparing; see "Sleep window"), `Restart=no`,
  `OnFailure=input-lock-failsafe.service`, `TimeoutStopSec=5s`. Its
  `ExecStopPost` runs `input-lock-power-helper disable --no-block`: a needed
  UPower restart (only when a legacy UPower drop-in is still present) is
  enqueued without waiting, so it cannot overrun the 5-second stop timeout,
  and the next `ExecStartPre` (which waits) verifies it.
- `input-lock-protect.service` and `input-lock-failsafe.service`: root
  `Type=oneshot`, `TimeoutStartSec=330s`, `StateDirectory=input-lock-protection`
  with `StateDirectoryMode=0755` (root-owned), `PrivateDevices=yes`,
  `ProtectKernelTunables/Modules/Logs=yes`, `ProtectControlGroups=yes`,
  `RestrictNamespaces=yes`, `LockPersonality=yes`,
  `SystemCallArchitectures=native`, `RestrictAddressFamilies=AF_UNIX`. They
  deliberately have no `CapabilityBoundingSet=`: a narrow bounding set cannot
  be verified without a real protective suspend, and a wrong one would
  silently break this last-resort path. `ReadWritePaths=-/var/lib/input-lock`
  carries the `-` prefix, so a missing daemon state directory (fresh install,
  uninstall) does not fail the unit with `226/NAMESPACE`; the readiness probe
  does not inspect `ReadWritePaths`.
- The readiness probe compares these contracts by range, not by text:
  `input-lock.service` watchdog 60–300 s, `Restart=no`, failsafe as
  `OnFailure`, `KillMode=control-group`, `TimeoutStopSec` ≤ 30 s, `Type`
  `simple` or `notify`; each helper's `TimeoutStartSec` between 300 s (budget
  + 60 s) and 30 min. Drop-ins are reported as information. The probe runs on
  its own daemon thread every 30 s, its logind query is bounded to 15 s, and
  telemetry sampling never waits for it; its result gates arming only.

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
(including any latch) to a root-only folder
`/var/backups/input-lock/uninstall-<timestamp>` before removing them, so a
later reinstall does not inherit a latch. It keeps, and names, the installer
backups under `/var/backups/input-lock` and the user's `~/.config/input-lock`.

Isolated tests are not physical acceptance. No lid close, sleep, hibernate,
reboot, hotplug, forced action or AAG Power test is needed for these source checks.
