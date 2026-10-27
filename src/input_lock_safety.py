"""Supervised lid suppression, not a replacement machine power controller.

Pure decisions are separate from telemetry and AAG - InputLock's native suspend adapter.
No incident-time thermal evidence established the cause of the 2026-09-11 stop.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

LOG = logging.getLogger("input-lock-safety")
BREADCRUMB = Path("/var/lib/input-lock/safety.json")
PROTECT_UNIT = "input-lock-protect.service"
FAILSAFE_UNIT = "input-lock-failsafe.service"
PROTECTION_DIR = Path("/var/lib/input-lock-protection")
FAILURE_LATCH = PROTECTION_DIR / "failure.json"
RESULT_NAME = "safety-action-result.json"
# Authoritative protective receipt, in the root-owned state directory. A copy
# is still published next to the daemon journal for the unprivileged daemon.
ROOT_RECEIPT = PROTECTION_DIR / RESULT_NAME
ACK_ARCHIVE = Path("/root/ARCHIVE/InputLock")
LATCH_ERROR = "previous emergency suspend failed; operator recovery required (see 'input-lock doctor')"
ACK_COMMAND = "sudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure"
THERMAL_MEASUREMENTS = ("NORMAL", "WARNING", "HIGH_RISK", "EMERGENCY")
THERMAL_STATES = ("NORMAL", "WARNING", "HIGH_RISK", "EMERGENCY_SOFTWARE_PROTECTION")

# Protective helper timing. observe_suspend() may wait this long for the
# kernel transition; the protect/failsafe units must allow at least one more
# minute for the steps before it (daemon stop, probe, logind request).
OBSERVE_BUDGET_SECONDS = 240
HELPER_START_TIMEOUT_MIN_SECONDS = OBSERVE_BUDGET_SECONDS + 60
HELPER_START_TIMEOUT_MAX_SECONDS = 1800
# input-lock.service WatchdogUSec: 60 s from the unit (longer than one 20 s
# kernel freezer timeout), up to 300 s while the daemon has extended it for a
# system sleep with sd_notify WATCHDOG_USEC.
DAEMON_WATCHDOG_MIN_SECONDS = 60
DAEMON_WATCHDOG_MAX_SECONDS = 300
# A systemd-suspend activation this recent means the OS sleep path was just
# used. After a failed attempt the freezer is likely still stuck; a second
# request would only repeat that failure (2026-10-21 19:20 -> 19:21).
OS_SLEEP_RECENT_SECONDS = 180
DEFERRED_OS_SLEEP = "deferred-os-sleep-in-progress"


def atomic_json(path: Path, value: dict, *, mode: int | None = None) -> None:
    """Keep the previous complete record on write failure; sync its directory.

    ``mode`` is applied with fchmod to the private temporary file before the
    rename, so a symlink planted at ``path`` is replaced, never followed.
    """
    fd, temporary = tempfile.mkstemp(prefix=".safety-", dir=path.parent)
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class Sensor:
    """One temperature source.

    kind "cpu" = a CPU decision source (TCPU, x86_pkg_temp, coretemp package
    or k10temp Tctl); "cpu-core" = hottest per-core reading, kept for logging
    and evidence only; "chassis" = SEN1/SEN2; "storage" = NVMe composite.
    """
    identity: str
    kind: str
    celsius: float
    warning: float
    high: float
    emergency: float

    def risk(self, hysteresis: float = 0) -> int:
        for level, threshold in ((3, self.emergency), (2, self.high), (1, self.warning)):
            if self.celsius >= threshold - hysteresis:
                return level
        return 0


@dataclass
class Sample:
    at: float
    lid_closed: bool | None
    sensors: list[Sensor] = field(default_factory=list)
    ac: bool | None = None
    battery: float | None = None
    discharging: bool | None = None
    warning_level: int | None = None
    cpu_busy: float | None = None
    dependencies_ok: bool = False
    stack_ready: bool = False
    errors: list[str] = field(default_factory=list)
    protection_errors: list[str] = field(default_factory=list)
    # logind PreparingForSleep at read time; None when it could not be read.
    preparing: bool | None = None

    def healthy(self, now: float) -> bool:
        """Strict arm gate. Closed-lid supervision uses per-category loss instead."""
        kinds = {sensor.kind for sensor in self.sensors}
        return (0 <= now - self.at <= 12 and self.lid_closed is not None
                and {"cpu", "chassis"} <= kinds and self.ac is not None
                and self.battery is not None and self.discharging is not None
                and self.dependencies_ok and self.stack_ready and not self.errors)


@dataclass(frozen=True)
class DwellRule:
    """Time at or above ``limit + offset`` before a level counts as met.

    Once started, the timer keeps running while the value stays at or above
    the threshold minus ``hysteresis``; ``tolerated`` further samples below
    that may interrupt it without resetting. Rules use the per-sensor median
    of the last five samples unless ``raw`` is set.
    """
    name: str
    level: int
    kind: str
    limit: str
    dwell: float
    hysteresis: float
    tolerated: int = 0
    offset: float = 0.0
    raw: bool = False


# Review 2026-10-22 section F. Thresholds come from the Sensor (Telemetry
# derives them from firmware trips: 95/104/107 CPU, 65/70/73 chassis and
# 77/80 NVMe on the EliteBook); dwell, hysteresis and tolerance live here.
THERMAL_RULES = (
    DwellRule("cpu-warning", 1, "cpu", "warning", 30, 3),
    DwellRule("cpu-protective", 2, "cpu", "high", 60, 3, tolerated=2),
    DwellRule("cpu-single-source-extreme", 3, "cpu", "emergency", 10, 0, offset=1, raw=True),
    DwellRule("chassis-warning", 1, "chassis", "warning", 60, 2),
    DwellRule("chassis-protective", 2, "chassis", "high", 60, 2),
    DwellRule("chassis-emergency", 3, "chassis", "emergency", 10, 2),
    DwellRule("storage-warning", 1, "storage", "warning", 20, 2),
    DwellRule("storage-protective", 2, "storage", "high", 20, 2),
)
MEASURED_HYSTERESIS = {"cpu": 3.0, "chassis": 2.0, "storage": 2.0}
CPU_CORROBORATED_SAMPLES = 3
# Physical origin groups (S-R4). TCPU, x86_pkg_temp and the coretemp/k10temp
# package channel (with its per-core readings) are one sensor: the CPU package.
# Every other sensor (each chassis zone, the NVMe drive) is its own origin.
CPU_PACKAGE_ORIGIN = "cpu-package"
INDEPENDENT_WITNESS_KINDS = frozenset({"chassis", "storage"})


def sensor_origin(sensor: Sensor) -> str:
    """The physical sensor behind a reading; same origin never corroborates."""
    if sensor.kind in {"cpu", "cpu-core"}:
        return CPU_PACKAGE_ORIGIN
    return sensor.identity


MEDIAN_SAMPLES = 5
MIN_DWELL_SAMPLES = 3  # Never act on a single (or a pair of) samples.
SAMPLE_GAP_LIMIT = 10.0  # Evidence older than this gap does not continue a timer.
TELEMETRY_LOSS_SECONDS = 30.0
REQUIRED_CATEGORIES = ("cpu", "chassis", "power")
TICK_GAP_SECONDS = 5.0  # The supervising process itself was paused (freezer/suspend).
SLEEP_WINDOW_MAX_SECONDS = 300.0  # Never trust "preparing" longer than this.


@dataclass
class _Timer:
    start: float
    last: float
    samples: int = 1
    drops: int = 0
    window: list = field(default_factory=list)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _categories(sample: Sample) -> set[str]:
    present = {s.kind for s in sample.sensors if math.isfinite(s.celsius)} & {"cpu", "chassis"}
    if sample.battery is not None or sample.ac is True:
        present.add("power")
    return present


def _battery_critical(sample: Sample) -> bool:
    """A low percentage is critical only while the battery is really draining."""
    draining = sample.discharging is True or sample.ac is False
    return ((sample.battery is not None and sample.battery <= 10 and draining)
            or (sample.warning_level is not None and sample.warning_level >= 4))


def _battery_low(sample: Sample) -> bool:
    return (sample.battery is not None and sample.battery <= 20
            and (sample.discharging is True or sample.ac is False))


class SafetyPolicy:
    """Monotonic, continuously supervised lid-ignore; never performs power actions.

    No normal closed-lid duration expires this policy. Only a thermal/battery
    risk or loss of reliable supervision can request protective suspend.

    The daemon calls update() every second with the latest Sample, but a new
    Sample arrives only every 2 s or more. Every timer therefore advances only
    on a new ``Sample.at``; repeated ticks on the same sample never add
    evidence. The readiness probe (``stack_ready``) gates arming only and is
    never treated as telemetry loss. While logind reports that a sleep is
    already being prepared, supervision takes no action.
    """
    ARM_WINDOW_SECONDS = 10.0

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state = "SAFE_NORMAL"
        self.armed = False
        self.closed_since = None
        self.deadline = None  # Compatibility only: permanent absence of a lid timer.
        self.missing_since = None
        self.thermal_level = 0
        self.thermal_candidate = 0
        self.thermal_since = None
        self.critical_since = None
        self.sample: Sample | None = None
        self.reason = "SAFE_STATE_RESTORED"
        self.events: list[str] = []
        self.required_sensors: set[str] = set()
        self.previous_ac = None
        self.last_decision: dict | None = None
        # Recent distinct samples: (at, {identity: celsius}). Used for the
        # per-sensor medians, the arm gate and the measured thermal view.
        self.recent: deque = deque(maxlen=64)
        self.timers: dict[tuple[str, str], _Timer] = {}
        self.corroborated: list[tuple[float, dict]] = []
        self.category_missing: dict[str, float] = {}
        self.critical_samples = 0
        self.sleep_window_since = None  # Set by note_sleep_window() (daemon hook).
        self.os_sleep_since = None
        self.os_sleep_expired = False
        self.grace_at = None
        self.last_tick = None
        self.evaluated_at = None
        self.thermal_evidence: dict | None = None
        self.degraded = False
        self.measured_level = 0
        self.measured_previous = 0
        self.measured_at = None

    def event(self, reason: str) -> None:
        self.events.append(reason)
        LOG.info("event=%s state=%s", reason, self.state)

    def transition(self, state: str, reason: str) -> None:
        if self.state != state or self.reason != reason:
            self.state, self.reason = state, reason
            self.event(reason)

    # ------------------------------------------------------------ arming

    def arm(self, sample: Sample) -> None:
        if not sample.stack_ready:
            detail = "; ".join(sample.protection_errors) or "native suspend/fail-safe readiness unavailable"
            raise ValueError("Lid-ignore unavailable: " + detail)
        if not sample.healthy(self.clock()) or sample.lid_closed is not False:
            raise ValueError("Open the lid and restore fresh safety telemetry before arming")
        smoothed = self._arming_sensors(sample)
        hot = [s for s in smoothed if s.kind != "cpu-core" and s.risk() >= 2]
        # A single raw reading well above the emergency limit still refuses.
        hot += [s for s in sample.sensors if s.kind != "cpu-core" and s.celsius >= s.emergency + 5]
        if hot:
            worst = max(hot, key=lambda s: s.celsius)
            raise ValueError("Temperature is already too high to arm lid-ignore "
                             f"({worst.identity} {worst.celsius:.0f}C)")
        # Explicit re-arm with a freshly observed open lid starts a new
        # episode even if no periodic open-lid update ran in between.
        self.release()
        self.last_decision = None
        self.previous_ac = sample.ac
        self.required_sensors = {s.identity for s in sample.sensors}
        self.armed = True
        self.sample = sample
        self.transition("LID_IGNORE_ARMED", "LID_IGNORE_ARMED")

    def _remember(self, sample: Sample) -> bool:
        """Record a sample once; return True only for a new ``Sample.at``.

        A different object with the same timestamp (the daemon mutates the
        current sample between ticks) refreshes the stored row for display,
        but is not new evidence for any timer.
        """
        row = {s.identity: s.celsius for s in sample.sensors}
        if self.recent and self.recent[-1][0] == sample.at:
            self.recent[-1] = (sample.at, row)
            self._measure(sample, new=False)
            return False
        if self.recent and sample.at < self.recent[-1][0]:
            return False  # Out-of-order (older) sample: never rewinds evidence.
        self.recent.append((sample.at, row))
        self._measure(sample, new=True)
        return True

    def _arming_sensors(self, sample: Sample) -> list[Sensor]:
        """Per-sensor median of the last few seconds, used only to arm.

        CPU package sensors on this hardware class jump for single 2-second
        samples (for example 87 -> 101 -> 91C) while the sustained level is
        normal. Arming refuses sustained heat, not one transient sample.
        With fewer than three recent samples the raw reading is used, so the
        gate is never looser than before on no data.
        """
        self._remember(sample)
        horizon = sample.at - self.ARM_WINDOW_SECONDS
        window = [rows for at, rows in self.recent if horizon <= at <= sample.at]
        result = []
        for sensor in sample.sensors:
            values = [rows[sensor.identity] for rows in window
                      if sensor.identity in rows and math.isfinite(rows[sensor.identity])]
            if len(values) < 3 or not math.isfinite(sensor.celsius):
                result.append(sensor)
                continue
            result.append(replace(sensor, celsius=_median(values)))
        return result

    def median(self, identity: str) -> float | None:
        """Median of this sensor over the last MEDIAN_SAMPLES distinct samples.

        Only the latest contiguous run counts: rows from before a gap larger
        than SAMPLE_GAP_LIMIT (a suspend/resume, a frozen telemetry thread or
        a sleep window in which the daemon stopped calling update()) describe
        a different situation and are never mixed into the current median.
        """
        run: list[dict] = []
        later = None
        for at, rows in reversed(self.recent):
            if later is not None and later - at > SAMPLE_GAP_LIMIT:
                break
            run.append(rows)
            later = at
            if len(run) == MEDIAN_SAMPLES:
                break
        values = [rows[identity] for rows in run
                  if identity in rows and math.isfinite(rows[identity])]
        return _median(values) if values else None

    # --------------------------------------------------- measured view (G1)

    def _measure(self, sample: Sample, *, new: bool) -> None:
        """Smoothed, hysteresized thermal condition for status and logging.

        Uses the same per-sensor medians as supervision, so one spiky sample
        or a reading hovering at a threshold does not flap the reported state.
        """
        if new:
            self.measured_previous = self.measured_level
        previous = self.measured_previous
        level = 0
        holding = False
        for sensor in sample.sensors:
            if sensor.kind not in MEASURED_HYSTERESIS or not math.isfinite(sensor.celsius):
                continue
            value = self.median(sensor.identity)
            if value is None:
                continue
            level = max(level, replace(sensor, celsius=value).risk())
            if previous and replace(sensor, celsius=value).risk(MEASURED_HYSTERESIS[sensor.kind]) >= previous:
                holding = True
        self.measured_level = previous if level < previous and holding else level
        self.measured_at = sample.at

    def measured_thermal(self) -> dict:
        """Describe the sample, independently of intervention arming/dwell."""
        sample = self.sample
        fresh = sample is not None and 0 <= self.clock() - sample.at <= 12
        complete = bool(fresh and {"cpu", "chassis"} <= _categories(sample))
        known = (fresh and self.measured_at == sample.at
                 and any(s.kind in MEASURED_HYSTERESIS and math.isfinite(s.celsius)
                         for s in sample.sensors))
        level = self.measured_level if known else 0
        # A missing sensor must not hide a known hot reading. An incomplete
        # normal sample, however, cannot certify a normal thermal condition.
        state = THERMAL_MEASUREMENTS[level] if known and (complete or level > 0) else "UNKNOWN"
        return {"state": state, "fresh": bool(fresh), "complete": complete}

    # ------------------------------------------------------- supervision

    def release(self, reason: str = "SAFE_STATE_RESTORED") -> None:
        self.armed = False
        self.closed_since = self.deadline = None
        self._reset_episode()
        self.transition("SAFE_NORMAL", reason)

    def _reset_episode(self) -> None:
        self.missing_since = None
        self.thermal_since = self.critical_since = None
        self.thermal_level = self.thermal_candidate = 0
        self.timers.clear()
        self.corroborated = []
        self.category_missing.clear()
        self.critical_samples = 0
        self.thermal_evidence = None
        self.degraded = False

    def note_sleep_window(self, active: bool) -> None:
        """Daemon hook for logind PrepareForSleep(true/false) and the sleep hook.

        Optional: Telemetry also reports logind's PreparingForSleep in every
        sample. Either source opens the window; it is bounded in time.
        """
        if not active:
            self.sleep_window_since = None
        elif self.sleep_window_since is None:
            self.sleep_window_since = self.clock()

    def os_sleep_reported(self) -> bool:
        """Status view: an OS sleep is in progress (seen in samples or via the
        daemon hook), within the same SLEEP_WINDOW_MAX_SECONDS bound."""
        if self.os_sleep_since is not None and not self.os_sleep_expired:
            return True
        return (self.sleep_window_since is not None
                and self.clock() - self.sleep_window_since <= SLEEP_WINDOW_MAX_SECONDS)

    def _in_sleep_window(self, sample: Sample, now: float) -> bool:
        preparing = sample.preparing is True or self.sleep_window_since is not None
        if not preparing:
            if self.os_sleep_since is not None:
                self.os_sleep_since = None
                self.os_sleep_expired = False
                # Restart loss timers: the gap belonged to the OS sleep path.
                self.grace_at = now
                self.category_missing.clear()
                self.missing_since = None
                self.event("OS_SLEEP_WINDOW_ENDED")
            return False
        if self.os_sleep_since is None:
            self.os_sleep_since = now
            self.event("OS_SLEEP_IN_PROGRESS")
        if now - self.os_sleep_since <= SLEEP_WINDOW_MAX_SECONDS:
            return True
        if not self.os_sleep_expired:
            self.os_sleep_expired = True
            self.event("OS_SLEEP_WINDOW_EXPIRED")
        return False

    def update(self, sample: Sample) -> str | None:
        now = self.clock()
        self.sample = sample
        new = self._remember(sample)
        if self.last_tick is not None and now - self.last_tick > TICK_GAP_SECONDS:
            # This process was frozen or the machine slept: the gap is not
            # evidence of telemetry loss. Restart the loss clocks from now.
            self.grace_at = now
            self.category_missing.clear()
            if self.armed and self.closed_since is not None:
                self.event("SUPERVISION_PAUSE_DETECTED")
        self.last_tick = now
        if not self.armed:
            return None
        if sample.lid_closed is False:
            if self.closed_since is not None:
                self.event("LID_OPENED")
                self.event("CONTINUOUS_MONITORING_ENDED")
            self.closed_since = self.deadline = None
            self._reset_episode()
            self.transition("LID_IGNORE_ARMED", "LID_IGNORE_ARMED")
            return None
        if self.closed_since is None:
            self.closed_since = now
            self.evaluated_at = None
            self.transition("LID_CLOSED_MONITORED", "LID_CLOSED")
            self.event("CONTINUOUS_MONITORING_STARTED")
        # Unknown lid state is conservatively supervised as closed.
        if self._in_sleep_window(sample, now):
            # A suspend from another source is already in progress. Requesting
            # one more would at best duplicate it and at worst drive a second
            # attempt into a stuck freezer. Keep collecting evidence only.
            if new:
                self._evaluate_thermal(sample)
            return None
        # Supervision-loss duration is bounded independently of closed-lid time.
        reference = max(sample.at, self.grace_at if self.grace_at is not None else -math.inf)
        if now - reference >= TELEMETRY_LOSS_SECONDS:
            self.missing_since = self.missing_since if self.missing_since is not None else reference
            return self.decide("TELEMETRY_LOST", {"stale_seconds": round(now - sample.at, 1)})
        new = new and self.evaluated_at != sample.at
        if not new:
            return None
        if self.evaluated_at is not None and sample.at - self.evaluated_at > SAMPLE_GAP_LIMIT:
            # Across a suspend/resume or a window in which update() was not
            # called, the gap is neither dwell time nor telemetry-loss time.
            # Thermal timers reset themselves on the same gap.
            self.category_missing.clear()
            self.missing_since = None
            self.critical_since = None
            self.critical_samples = 0
            self.event("SAMPLE_GAP_RESET")
        self.evaluated_at = sample.at
        lost = self._track_categories(sample)
        if lost:
            return self.decide("TELEMETRY_LOST", {"missing_categories": sorted(lost)})
        if self.previous_ac is not None and self.previous_ac != sample.ac and sample.ac is not None:
            self.event("AC_CONNECTED" if sample.ac else "AC_LOST")
        if sample.ac is not None:
            self.previous_ac = sample.ac
        # AC loss and processor load are observations, never normal
        # time-based suspend triggers. Thermal sensors are still supervised.
        level = self._evaluate_thermal(sample)
        if level != self.thermal_level:
            self.thermal_level = level
            self.event(("THERMAL_NORMAL", "THERMAL_WARNING", "THERMAL_HIGH_RISK", "THERMAL_EMERGENCY")[level])
        if _battery_critical(sample):
            if self.critical_since is None:
                self.critical_since = sample.at
                self.critical_samples = 0
                self.event("BATTERY_CRITICAL")
            self.critical_samples += 1
            if self.critical_samples >= 2 and sample.at - self.critical_since >= 2:
                return self.decide("BATTERY_CRITICAL")
        else:
            self.critical_since = None
            self.critical_samples = 0
        if self.thermal_level >= 2:
            return self.decide("THERMAL_HIGH_RISK" if self.thermal_level == 2 else "THERMAL_EMERGENCY")
        degraded = bool(self.category_missing or not sample.dependencies_ok or not sample.stack_ready
                        or sample.protection_errors or sample.errors or sample.lid_closed is None
                        or sample.ac is None or sample.battery is None or sample.discharging is None)
        if degraded != self.degraded:
            self.degraded = degraded
            # A readiness-probe failure or a stale logind/UPower heartbeat is a
            # warning while supervising; it never requests a suspend by itself.
            self.event("SUPERVISION_DEGRADED" if degraded else "SUPERVISION_RESTORED")
        if self.thermal_level >= 1 or degraded or _battery_low(sample):
            self.transition("LID_CLOSED_WARNING",
                            "BATTERY_WARNING" if _battery_low(sample) else "SAFETY_WARNING")
        else:
            self.transition("LID_CLOSED_MONITORED", "MONITORING")
        return None

    def _track_categories(self, sample: Sample) -> list[str]:
        """Telemetry is lost only when a whole category is missing for 30 s.

        One unreadable channel, one vanished CPU source or a stale readiness
        probe is not loss: the remaining sources of that category still
        supervise. Measured in sample time, over distinct samples.
        """
        present = _categories(sample)
        for category in REQUIRED_CATEGORIES:
            if category in present:
                if self.category_missing.pop(category, None) is not None and not self.category_missing:
                    self.event("TELEMETRY_RESTORED")
            elif category not in self.category_missing:
                if not self.category_missing:
                    self.event("TELEMETRY_CATEGORY_MISSING")
                self.category_missing[category] = sample.at
        self.missing_since = min(self.category_missing.values(), default=None)
        return [c for c, since in self.category_missing.items()
                if sample.at - since >= TELEMETRY_LOSS_SECONDS]

    def _evaluate_thermal(self, sample: Sample) -> int:
        """Advance every per-sensor, per-level timer with one new sample."""
        at = sample.at
        met: list[tuple] = []
        seen = set()
        for rule in THERMAL_RULES:
            for sensor in sample.sensors:
                if sensor.kind != rule.kind or not math.isfinite(sensor.celsius):
                    continue
                value = sensor.celsius if rule.raw else self.median(sensor.identity)
                if value is None:
                    continue
                threshold = getattr(sensor, rule.limit) + rule.offset
                key = (rule.name, sensor.identity)
                seen.add(key)
                timer = self.timers.get(key)
                if timer is not None and at - timer.last > SAMPLE_GAP_LIMIT:
                    timer = None
                holding = False
                if timer is None:
                    if value >= threshold:
                        timer = _Timer(at, at, window=[(at, value)])
                        holding = True
                elif value >= threshold - rule.hysteresis:
                    holding = True
                elif timer.drops < rule.tolerated:
                    timer.drops += 1
                else:
                    timer = None
                if timer is None:
                    self.timers.pop(key, None)
                    continue
                if timer.last != at:
                    timer.last = at
                    timer.samples += 1
                    timer.window.append((at, value))
                    del timer.window[:-40]
                self.timers[key] = timer
                if (holding and timer.samples >= MIN_DWELL_SAMPLES
                        and at - timer.start >= rule.dwell):
                    met.append((rule.level, rule, sensor, timer, threshold))
        # A sensor absent from this sample keeps its timer only until the
        # evidence gap exceeds SAMPLE_GAP_LIMIT.
        for key in [k for k, t in self.timers.items() if k not in seen and at - t.last > SAMPLE_GAP_LIMIT]:
            del self.timers[key]
        corroborated = self._corroborated_cpu(sample)
        if corroborated is not None:
            if self.corroborated and at - self.corroborated[-1][0] > SAMPLE_GAP_LIMIT:
                self.corroborated = []
            if not self.corroborated or self.corroborated[-1][0] != at:
                self.corroborated.append((at, corroborated))
            if len(self.corroborated) >= CPU_CORROBORATED_SAMPLES:
                met.append((3, None, None, None, None))
        else:
            self.corroborated = []
        active = [(rule.level, timer.start) for (_name, _id), timer in self.timers.items()
                  for rule in THERMAL_RULES if rule.name == _name]
        if self.corroborated:
            active.append((3, self.corroborated[0][0]))
        if active:
            top = max(level for level, _ in active)
            self.thermal_candidate = top
            self.thermal_since = min(start for level, start in active if level == top)
        else:
            self.thermal_candidate, self.thermal_since = 0, None
        if not met:
            self.thermal_evidence = None
            return 0
        level = max(item[0] for item in met)
        level_rule, rule, sensor, timer, threshold = next(item for item in met if item[0] == level)
        if rule is None:
            self.thermal_evidence = {
                "rule": "cpu-corroborated-emergency", "level": 3,
                "samples": len(self.corroborated),
                "elapsed_seconds": round(at - self.corroborated[0][0], 2),
                "witness_rule": "independent origin (chassis or NVMe) at or above its warning level",
                "window": [{"at_offset": round(t - self.corroborated[0][0], 2), **row}
                           for t, row in self.corroborated[-CPU_CORROBORATED_SAMPLES:]],
            }
        else:
            self.thermal_evidence = {
                "rule": rule.name, "level": rule.level, "sensor": sensor.identity,
                "threshold_c": round(threshold, 2), "dwell_seconds": rule.dwell,
                "hysteresis_c": rule.hysteresis, "tolerated_samples": rule.tolerated,
                "basis": "raw" if rule.raw else f"median-of-{MEDIAN_SAMPLES}",
                "elapsed_seconds": round(at - timer.start, 2),
                "samples": timer.samples, "drops": timer.drops,
                "window": [{"at_offset": round(t - timer.start, 2), "celsius": round(v, 2)}
                           for t, v in timer.window],
            }
        return level

    def _corroborated_cpu(self, sample: Sample) -> dict | None:
        """A raw CPU reading at/above emergency, confirmed by an independent witness.

        TCPU, x86_pkg_temp and the coretemp/k10temp package channel (and its
        per-core readings) all read the same CPU package sensor, so none of
        them can confirm another (S-R4). The witness must come from a
        different physical origin (a chassis zone or the NVMe drive) and be
        at or above its own warning level. Without such a witness only the
        single-source rule (emergency + 1 C for 10 s) applies.
        """
        cpus = [s for s in sample.sensors if s.kind == "cpu" and math.isfinite(s.celsius)]
        for hot in sorted(cpus, key=lambda s: -s.celsius):
            if hot.celsius < hot.emergency:
                break
            origin = sensor_origin(hot)
            witnesses = [o for o in sample.sensors
                         if o.kind in INDEPENDENT_WITNESS_KINDS and math.isfinite(o.celsius)
                         and sensor_origin(o) != origin and o.celsius >= o.warning]
            if witnesses:
                witness = max(witnesses, key=lambda o: o.celsius - o.warning)
                return {"sensor": hot.identity, "celsius": round(hot.celsius, 2),
                        "witness": witness.identity, "witness_celsius": round(witness.celsius, 2),
                        "witness_warning_c": round(witness.warning, 2)}
        return None

    def decide(self, reason: str, detail: dict | None = None) -> str:
        now = self.clock()
        sample = self.sample
        self.last_decision = {
            "reason": reason,
            "closed_seconds": round(now - self.closed_since, 1) if self.closed_since is not None else None,
            # Legacy fields are explicit None/empty, never a finite timeout.
            "time_limit_seconds": None,
            "deadline_shortened_by": [],
            "ac": sample.ac if sample is not None else None,
            "battery": sample.battery if sample is not None else None,
            "discharging": sample.discharging if sample is not None else None,
            "cpu_busy": sample.cpu_busy if sample is not None else None,
        }
        if detail:
            self.last_decision["detail"] = detail
        if reason in {"THERMAL_HIGH_RISK", "THERMAL_EMERGENCY"} and sample is not None:
            # Retain the actual hot readings, their medians, the rule that
            # fired and the samples inside its dwell window, so a spike and
            # sustained corroborated heat stay distinguishable after resume.
            self.last_decision["thermal_evidence"] = {
                "thermal_level": self.thermal_level,
                "candidate_level": self.thermal_candidate,
                "candidate_seconds": (round(sample.at - self.thermal_since, 2)
                                      if self.thermal_since is not None else None),
                "rule": dict(self.thermal_evidence or {}),
                "sensors": [
                    {"identity": s.identity, "kind": s.kind,
                     "celsius": round(s.celsius, 2),
                     "median_c": (round(self.median(s.identity), 2)
                                  if self.median(s.identity) is not None else None),
                     "warning_c": round(s.warning, 2),
                     "high_c": round(s.high, 2),
                     "emergency_c": round(s.emergency, 2),
                     "risk": s.risk(),
                     "risk_hysteresis_3c": s.risk(3)}
                    for s in sorted(sample.sensors, key=lambda s: (-s.risk(), -s.celsius))
                ],
            }
        LOG.info("protective_decision_evidence=%s", json.dumps(self.last_decision, sort_keys=True))
        self.armed = False  # One dispatch per explicit arm.
        self.transition("PROTECTIVE_ACTION_PENDING", "PROTECTIVE_ACTION_DECIDED")
        self.reason = reason
        return reason

    def snapshot(self) -> dict:
        measured = self.measured_thermal()
        return {"state": self.state, "reason": self.reason, "closed_since": self.closed_since,
                "lid_ignore_mode": "continuous_until_disabled",
                "closed_seconds": round(self.clock() - self.closed_since, 1)
                if self.armed and self.closed_since is not None else None,
                "deadline": None,  # Legacy wire field, no longer scheduled.
                "deadline_shortened_by": [],  # Legacy wire field.
                "last_decision": self.last_decision,
                "thermal": measured["state"],
                "thermal_measurement": measured,
                "protection_armed": self.armed,
                "thermal_protection": "ARMED" if self.armed else "DISARMED",
                "thermal_intervention": THERMAL_STATES[self.thermal_level] if self.armed else "DISARMED",
                "os_sleep_in_progress": self.os_sleep_reported(),
                "missing_categories": sorted(self.category_missing),
                "sample": asdict(self.sample) if self.sample else None}


def cpu_limits(critical: float) -> tuple[float, float, float]:
    """Warning / protective / emergency; 95/104/107C at a 110C firmware trip."""
    return min(95, critical - 15), min(104, critical - 6), min(107, critical - 3)


def chassis_limits(critical: float) -> tuple[float, float, float]:
    """65/70/73C at the EliteBook's 80C SEN1/SEN2 critical trip."""
    return min(65, critical - 15), min(70, critical - 10), min(73, critical - 7)


def storage_limits(critical: float) -> tuple[float, float, float]:
    """77/80C at the NVMe composite's 87.85C critical; no emergency rule."""
    return min(77, critical - 10), min(80, critical - 7), critical


def bounded_call(call, timeout: float, what: str):
    """Run ``call()`` on a helper thread and wait at most ``timeout`` seconds.

    libdbus connects (and says Hello) with its default 25-second timeout,
    which no caller can shorten. A caller that must stay responsive waits
    here instead; on timeout the helper thread is abandoned (it is a daemon
    thread and ends when the call finally returns) and TimeoutError is raised.
    """
    result: dict = {}

    def target() -> None:
        try:
            result["value"] = call()
        except BaseException as exc:  # Re-raised in the caller's thread.
            result["error"] = exc

    worker = threading.Thread(target=target, name="input-lock-bounded-call", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"{what} did not answer within {timeout:g} s")
    if "error" in result:
        raise result["error"]
    return result.get("value")


# The readiness probe's logind status (bus connect, systemctl --version and
# three 3-second calls) must finish within this time (S-R2).
NATIVE_STATUS_TIMEOUT_SECONDS = 15.0


class _LogindSleepProbe:
    """Read-only PreparingForSleep reader on one cached private connection.

    Runs on the telemetry thread, which must never wait for a bus connect
    (S-R2): the connection is opened on a helper thread, and until it is
    ready the probe answers None ("unknown"). The Get call itself is bounded
    to 1 s.
    """

    def __init__(self, connect=None) -> None:
        self.bus = None
        self.properties = None
        self._lock = threading.Lock()
        self._connecting = False
        self._connect = connect or self._system_bus_properties

    @staticmethod
    def _system_bus_properties():
        import dbus
        bus = dbus.SystemBus(private=True)
        try:
            return bus, dbus.Interface(
                bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1", introspect=False),
                "org.freedesktop.DBus.Properties")
        except Exception:
            bus.close()
            raise

    def close(self) -> None:
        with self._lock:
            bus, self.bus, self.properties = self.bus, None, None
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

    def _start_connect(self) -> None:
        with self._lock:
            if self._connecting or self.properties is not None:
                return
            self._connecting = True

        def connect() -> None:
            try:
                bus, properties = self._connect()
            except Exception:
                bus = properties = None
            with self._lock:
                self._connecting = False
                if properties is not None:
                    self.bus, self.properties = bus, properties
                    return
            if bus is not None:
                try:
                    bus.close()
                except Exception:
                    pass

        threading.Thread(target=connect, name="input-lock-sleep-probe-connect", daemon=True).start()

    def __call__(self) -> bool | None:
        properties = self.properties
        if properties is None:
            self._start_connect()
            return None
        try:
            return bool(properties.Get("org.freedesktop.login1.Manager",
                                       "PreparingForSleep", timeout=1))
        except Exception:
            self.close()
            return None


class Telemetry:
    """Kernel drivers/types identify sensors; numbering and display labels do not.

    CPU decisions use TCPU, x86_pkg_temp and the coretemp package (or k10temp
    Tctl) channel. The hottest per-core reading is reported separately, as
    kind "cpu-core", for corroboration in logs and evidence only: per-core
    readings are the spikiest and must not decide alone.
    """
    def __init__(self, sys_root=Path("/sys"), proc_root=Path("/proc"), *, sleep_probe="auto"):
        self.sys, self.proc = sys_root, proc_root
        self.previous_cpu = None
        if sleep_probe == "auto":
            # Only the live system has a logind to ask; fixtures never do.
            sleep_probe = _LogindSleepProbe() if Path(sys_root) == Path("/sys") else None
        self.sleep_probe = sleep_probe

    @staticmethod
    def number(path: Path) -> float | None:
        try:
            value = float(path.read_text().strip())
            return value if math.isfinite(value) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def temperature(path: Path) -> float | None:
        value = Telemetry.number(path)
        return value / 1000 if value is not None and 1000 <= value <= 150000 else None

    @staticmethod
    def _label(path: Path) -> str:
        try:
            return path.with_name(path.name.replace("_input", "_label")).read_text().strip()
        except OSError:
            return ""

    def _cpu_hwmon(self, hwmon: Path, driver: str, sensors: list, errors: list) -> None:
        inputs = sorted(hwmon.glob("temp*_input"))
        package = next((p for p in inputs if self._label(p).startswith(("Package id", "Tctl"))), None)
        if package is None:
            package = hwmon / "temp1_input"
        identity = f"hwmon:{driver}:{hwmon.resolve().parent}"
        critical = self.temperature(package.with_name(package.name.replace("_input", "_crit")))
        limits = cpu_limits(critical if critical is not None else 100)
        value = self.temperature(package)
        if value is None:
            errors.append(f"unreadable:{driver}:{package.stem}")
        else:
            sensors.append(Sensor(identity, "cpu", value, *limits))
        cores = [v for v in (self.temperature(p) for p in inputs if p != package) if v is not None]
        if cores:
            # Unreadable per-core channels are not errors: they never decide.
            sensors.append(Sensor(identity + ":cores", "cpu-core", max(cores), *limits))

    def read(self, *, dependencies_ok=False, stack_ready=False, warning_level=None) -> Sample:
        observed_at = time.monotonic()
        sensors = []
        errors = []
        for zone in (self.sys / "class/thermal").glob("thermal_zone*"):
            try:
                kind = (zone / "type").read_text().strip()
            except OSError:
                continue
            category = "cpu" if kind in {"TCPU", "x86_pkg_temp"} else "chassis" if kind in {"SEN1", "SEN2"} else None
            if category is None:
                continue
            value = self.temperature(zone / "temp")
            trips = {}
            for p in zone.glob("trip_point_*_type"):
                try:
                    trip_kind = p.read_text().strip()
                    temperature = self.temperature(p.with_name(p.name.replace("_type", "_temp")))
                    if temperature is not None:
                        trips[trip_kind] = min(trips.get(trip_kind, 150), temperature)
                except OSError:
                    continue
            if value is None:
                errors.append(f"unreadable:{kind}")
                continue
            crit = trips.get("critical", 110 if category == "cpu" else 80)
            limits = cpu_limits(crit) if category == "cpu" else chassis_limits(crit)
            sensors.append(Sensor(f"thermal:{kind}", category, value, *limits))
        for hwmon in (self.sys / "class/hwmon").glob("hwmon*"):
            try:
                driver = (hwmon / "name").read_text().strip()
            except OSError:
                continue
            if driver in {"coretemp", "k10temp"}:
                self._cpu_hwmon(hwmon, driver, sensors, errors)
                continue
            if driver != "nvme":
                continue
            # NVMe composite temp1 has specified critical limits; secondary
            # controller sensors may have invalid firmware max values.
            path = hwmon / "temp1_input"
            value = self.temperature(path)
            if value is None:
                errors.append(f"unreadable:{driver}:{path.stem}")
                continue
            critical = self.temperature(hwmon / "temp1_crit")
            sensors.append(Sensor(f"hwmon:{driver}:{hwmon.resolve().parent}", "storage", value,
                                  *storage_limits(critical if critical is not None else 85)))
        if not {"thermal:SEN1", "thermal:SEN2"} <= {sensor.identity for sensor in sensors}:
            errors.append("required-firmware-chassis-zones-unavailable")
        ac_values, batteries, discharging = [], [], []
        for p in (self.sys / "class/power_supply").glob("*"):
            try:
                kind = (p / "type").read_text().strip()
                if kind == "Mains":
                    value = self.number(p / "online")
                    if value in (0, 1):
                        ac_values.append(bool(value))
                elif kind == "Battery":
                    value = self.number(p / "capacity")
                    if value is not None and 0 <= value <= 100:
                        batteries.append(value)
                    discharging.append((p / "status").read_text().strip() == "Discharging")
            except OSError:
                errors.append("power-supply-unreadable")
        lids = []
        for p in (self.proc / "acpi/button/lid").glob("*/state"):
            try:
                value = p.read_text().lower().split()[-1]
                if value in {"open", "closed"}:
                    lids.append(value == "closed")
            except (OSError, IndexError):
                pass
        cpu_busy = None
        try:
            values = [int(v) for v in (self.proc / "stat").read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(values), values[3] + values[4]
            if self.previous_cpu is not None:
                delta = total - self.previous_cpu[0]
                if delta > 0:
                    cpu_busy = max(0., min(1., 1 - (idle - self.previous_cpu[1]) / delta))
            self.previous_cpu = total, idle
        except (OSError, ValueError, IndexError):
            pass  # Optional CPU usage observation; no lid-close time limit.
        preparing = None
        if self.sleep_probe is not None:
            try:
                preparing = self.sleep_probe()
            except Exception:
                preparing = None
        return Sample(observed_at, any(lids) if lids else None, sensors,
                      any(ac_values) if ac_values else None,
                      min(batteries) if batteries else None,
                      any(discharging) if discharging else None, warning_level,
                      cpu_busy, dependencies_ok, stack_ready, errors, preparing=preparing)


def root_hard_block_supported(run=subprocess.run) -> bool:
    """Require systemd >= 257 before relying on default root block semantics.

    Versions before 257 could ignore ordinary inhibitors for privileged callers
    unless explicitly asked to check them. Deny unknown/older versions rather
    than ever silently suspending through a hard sleep blocker.
    """
    try:
        result = run(["/usr/bin/systemctl", "--version"], capture_output=True,
                     text=True, timeout=3, check=False)
        first = result.stdout.splitlines()[0] if result.returncode == 0 else ""
        match = re.fullmatch(r"systemd\s+(\d+)(?:\s+.*)?", first)
        return bool(match and int(match.group(1)) >= 257)
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        return False


def native_status() -> dict:
    """Read logind only; never acquire an inhibitor or request sleep here."""
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        obj = bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1")
        manager = dbus.Interface(obj, "org.freedesktop.login1.Manager")
        properties = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        inhibitors = list(manager.ListInhibitors(timeout=3))
        return {
            # The unprivileged monitor cannot suspend itself. "challenge"
            # still means supported; the fixed root service performs the action
            # without interactive authorization and handles any actual refusal.
            "supported": (root_hard_block_supported()
                          and str(manager.CanSuspend(timeout=3)) in {"yes", "challenge"}),
            "preparing": bool(properties.Get("org.freedesktop.login1.Manager", "PreparingForSleep", timeout=3)),
            # systemd >= 257 always enforces hard "block" sleep locks for
            # privileged callers. "block-weak" intentionally does not block
            # the root helper with SuspendWithFlags(0). Keep both the native
            # dispatch and this readiness probe in exact agreement.
            "blocked": any("sleep" in str(row[0]).split(":") and str(row[3]) == "block"
                           for row in inhibitors),
            "inputlock_holds_lid": any(str(row[1]) == "Input Lock" and
                                      "handle-lid-switch" in str(row[0]).split(":") for row in inhibitors),
        }
    finally:
        bus.close()


UNIT_PROPERTIES = ("LoadState", "ActiveState", "Result", "ExecStart", "ExecStopPost", "OnFailure",
                   "WatchdogUSec", "Restart", "DropInPaths", "User", "Type", "TimeoutStartUSec",
                   "TimeoutStopUSec", "KillMode", "InvocationID", "InactiveExitTimestampMonotonic")


def unit_properties(run, unit: str) -> dict:
    result = run(["/usr/bin/systemctl", "show", unit, "--property=" + ",".join(UNIT_PROPERTIES)],
                 capture_output=True, text=True, timeout=3, check=False)
    if result.returncode:
        raise RuntimeError("cannot inspect " + unit)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


_TIMESPAN_UNITS = {
    "us": 1, "usec": 1, "µs": 1, "μs": 1, "ms": 1_000, "msec": 1_000,
    "s": 1_000_000, "sec": 1_000_000, "second": 1_000_000, "seconds": 1_000_000,
    "m": 60_000_000, "min": 60_000_000, "minute": 60_000_000, "minutes": 60_000_000,
    "h": 3_600_000_000, "hr": 3_600_000_000, "hour": 3_600_000_000, "hours": 3_600_000_000,
    "d": 86_400_000_000, "day": 86_400_000_000, "days": 86_400_000_000,
    "w": 604_800_000_000, "week": 604_800_000_000, "weeks": 604_800_000_000,
    "M": 2_629_800_000_000, "month": 2_629_800_000_000, "months": 2_629_800_000_000,
    "y": 31_557_600_000_000, "year": 31_557_600_000_000, "years": 31_557_600_000_000,
}


def parse_usec(text: str | None) -> float | None:
    """Parse a systemd timespan ("2min 45s", "20s", "infinity", "0") into µs.

    A bare integer is taken as microseconds, as busctl reports USec values.
    Returns None for anything unparseable, so callers fail closed.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text == "infinity":
        return math.inf
    if text.isdigit():
        return int(text)
    total, position = 0.0, 0
    for match in re.finditer(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-zµμ]+)", text):
        if match.start() != position or match.group(2) not in _TIMESPAN_UNITS:
            return None
        total += float(match.group(1)) * _TIMESPAN_UNITS[match.group(2)]
        position = match.end()
    return total if position == len(text) and position else None


def _seconds(text: str | None) -> float | None:
    value = parse_usec(text)
    return None if value is None else value / 1_000_000


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def latch_state(latch: Path | None = None, *, counter=None, boot=None) -> tuple[str, str]:
    """("absent" | "active" | "stale", detail) for the terminal failure latch.

    A latch records the boot and the kernel suspend success counter. It is
    stale once the machine rebooted or a later suspend completed: ordinary
    suspend then demonstrably works again. Stale is information, not an
    error. A latch without that evidence (older format) or one that cannot
    be read stays active until an operator acknowledges it.
    """
    latch = FAILURE_LATCH if latch is None else latch
    if not latch.exists():
        return "absent", ""
    try:
        record = json.loads(latch.read_text())
        recorded_boot = record.get("boot_id")
        recorded_success = record.get("suspend_success")
    except (OSError, ValueError, AttributeError):
        return "active", "unreadable failure latch"
    try:
        current_boot = boot() if boot is not None else boot_id()
    except OSError:
        current_boot = None
    if isinstance(recorded_boot, str) and current_boot and recorded_boot != current_boot:
        return "stale", "failure latch from a previous boot (informational; acknowledge to archive it)"
    if isinstance(recorded_success, int) and not isinstance(recorded_success, bool):
        try:
            if (counter or suspend_success_count)() > recorded_success:
                return "stale", ("failure latch predates a later successful suspend "
                                 "(informational; acknowledge to archive it)")
        except (OSError, ValueError):
            pass
    return "active", str(record.get("error", ""))


def protection_report(run=subprocess.run, status=native_status, counter=None) -> dict:
    """Effective native prerequisites, as {"errors": [...], "info": [...]}.

    This is a capability check, not proof that a future kernel suspend
    succeeds. Unit contracts are compared by range, not by exact text, so a
    harmless reformatting or an allowed runtime watchdog extension does not
    refuse arming. Drop-ins are reported as information.
    """
    errors: list[str] = []
    info: list[str] = []
    try:
        latch, detail = latch_state(counter=counter)
        if latch == "active":
            return {"errors": [LATCH_ERROR], "info": info}
        if latch == "stale":
            info.append(detail)
        # Bounded (S-R2): a sluggish bus makes the probe fail, never hang.
        state = bounded_call(status, NATIVE_STATUS_TIMEOUT_SECONDS, "logind readiness status")
        if not state["supported"] or state["blocked"] or state["preparing"]:
            return {"errors": ["ordinary suspend unavailable, blocked, or already in progress"], "info": info}
        (counter or suspend_success_count)()
        for unit, command in {
            "systemd-suspend.service": "/usr/lib/systemd/systemd-sleep suspend",
            PROTECT_UNIT: "/usr/lib/input-lock/input_lock_safety.py protect",
            FAILSAFE_UNIT: "/usr/lib/input-lock/input_lock_safety.py recover",
            "input-lock.service": "/usr/lib/input-lock/input-lock-daemon",
        }.items():
            props = unit_properties(run, unit)
            if props.get("LoadState") != "loaded" or command not in props.get("ExecStart", ""):
                return {"errors": ["missing or incompatible " + unit], "info": info}
            if props.get("DropInPaths", "").strip():
                info.append(f"{unit} has drop-ins: {props['DropInPaths'].strip()}")
            if unit in {PROTECT_UNIT, FAILSAFE_UNIT}:
                timeout = _seconds(props.get("TimeoutStartUSec"))
                if (props.get("User") not in {"", "root"} or props.get("Type") != "oneshot"
                        or props.get("Restart") != "no" or timeout is None
                        or not HELPER_START_TIMEOUT_MIN_SECONDS <= timeout <= HELPER_START_TIMEOUT_MAX_SECONDS):
                    return {"errors": ["AAG - InputLock root helper/timeout contract changed: " + unit], "info": info}
            if unit in {PROTECT_UNIT, "input-lock.service"} and props.get("OnFailure", "").split() != [FAILSAFE_UNIT]:
                return {"errors": ["missing AAG - InputLock failure handler on " + unit], "info": info}
            if unit == FAILSAFE_UNIT and props.get("OnFailure"):
                return {"errors": ["unexpected recursive failure handler"], "info": info}
            if unit == FAILSAFE_UNIT and "/usr/lib/input-lock/input_lock_safety.py record-failure" not in props.get("ExecStopPost", ""):
                return {"errors": ["missing terminal failure recorder"], "info": info}
            if unit == "input-lock.service":
                watchdog = _seconds(props.get("WatchdogUSec"))
                stop = _seconds(props.get("TimeoutStopUSec"))
                # 60 s normally (longer than one 20 s freezer timeout); up to
                # 300 s while the daemon has extended it for an OS sleep
                # transition (frozen tasks cannot ping).
                if (props.get("Restart") != "no" or props.get("KillMode") != "control-group"
                        or props.get("Type", "simple") not in {"simple", "notify"}
                        or watchdog is None
                        or not DAEMON_WATCHDOG_MIN_SECONDS <= watchdog <= DAEMON_WATCHDOG_MAX_SECONDS
                        or stop is None or not 0 < stop <= 30):
                    return {"errors": ["AAG - InputLock watchdog/restart contract changed"], "info": info}
        return {"errors": errors, "info": info}
    except Exception as exc:
        return {"errors": ["native protection probe failed: " + str(exc)], "info": info}


def protection_errors(run=subprocess.run, status=native_status, counter=None) -> list[str]:
    """Blocking readiness errors only; see protection_report() for information."""
    return protection_report(run, status, counter)["errors"]


def stack_ready(run=subprocess.run) -> bool:
    """Compatibility name for the wire field; now checks native prerequisites."""
    return not protection_errors(run)


def request_service() -> str:
    """Narrow, authorized systemd request; the daemon never prepares storage."""
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        manager = dbus.Interface(bus.get_object("org.freedesktop.systemd1", "/org/freedesktop/systemd1"), "org.freedesktop.systemd1.Manager")
        return str(manager.StartUnit(PROTECT_UNIT, "replace", timeout=5))
    finally:
        bus.close()


def suspend_success_count() -> int:
    value = int(Path("/sys/power/suspend_stats/success").read_text().strip())
    if value < 0:
        raise ValueError("invalid kernel suspend success counter")
    return value


def observe_suspend(run, requested_at: float, before_success: int, *, clock=time.monotonic,
                    sleep=time.sleep, counter=suspend_success_count,
                    budget: float = OBSERVE_BUDGET_SECONDS) -> None:
    """A successful enqueue is not a completed sleep/resume operation."""
    deadline = clock() + budget
    while clock() < deadline:
        result = run(["/usr/bin/systemctl", "show", "systemd-suspend.service",
                      "--property=ActiveState,Result,ExecMainStatus,InactiveExitTimestampMonotonic,ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic"],
                     capture_output=True, text=True, check=False, timeout=3)
        if result.returncode:
            raise RuntimeError("cannot observe the existing suspend service")
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        activation = int(values.get("InactiveExitTimestampMonotonic", "0")) / 1_000_000
        if activation >= requested_at:
            if values.get("Result") not in {None, "success"} or values.get("ActiveState") == "failed":
                raise RuntimeError("existing suspend service failed: " + values.get("Result", "unknown"))
            start = int(values.get("ExecMainStartTimestampMonotonic", "0"))
            end = int(values.get("ExecMainExitTimestampMonotonic", "0"))
            if (start / 1_000_000 >= requested_at and end >= start
                    and values.get("ActiveState") == "inactive" and values.get("Result") == "success"
                    and values.get("ExecMainStatus") == "0" and counter() > before_success):
                return
        sleep(1)
    raise TimeoutError("suspend completion was not observed within the awake-time budget")


def os_sleep_in_progress(run, native: dict, *, recovery: bool, now=None) -> str | None:
    """Why the OS sleep path must not get another request now, or None.

    logind preparing, or systemd-suspend still running, means a sleep is
    already underway. A recent failed systemd-suspend means the freezer is
    probably still stuck. In recovery even a recent successful one defers:
    the daemon just failed around that sleep (watchdog during the freeze).
    """
    if native.get("preparing"):
        return "logind is already preparing for sleep"
    props = unit_properties(run, "systemd-suspend.service")
    state = props.get("ActiveState")
    if state not in {"inactive", "failed"}:
        return f"systemd-suspend.service is {state}"
    try:
        started = int(props.get("InactiveExitTimestampMonotonic") or 0) / 1_000_000
    except ValueError:
        started = 0
    age = (now or time.monotonic)() - started
    if started > 0 and 0 <= age <= OS_SLEEP_RECENT_SECONDS:
        result = props.get("Result") or "unknown"
        if recovery or result != "success" or state == "failed":
            return f"systemd-suspend.service ran {age:.0f}s ago (result {result})"
    return None


def request_native_suspend() -> None:
    """Ask logind once, non-interactively, honoring hard sleep inhibitors.

    On systemd >= 257, regular "block" inhibitors apply to root regardless
    of flags. SuspendWithFlags(0) follows that native policy, where weak
    "block-weak" session inhibitors do not stop a root-owned protective helper.
    Do NOT set SD_LOGIND_SKIP_INHIBITORS (0x10): it would bypass hard blocks.
    Unsupported APIs/refusals/timeouts fail closed; do not retry or escalate.
    A method reply means accepted, not a completed kernel transition.
    """
    if not root_hard_block_supported():
        raise RuntimeError("systemd 257+ hard inhibitor enforcement is required")
    import dbus
    bus = dbus.SystemBus(private=True)
    try:
        manager = dbus.Interface(bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1"),
                                 "org.freedesktop.login1.Manager")
        manager.SuspendWithFlags(dbus.UInt64(0), timeout=10)
    finally:
        bus.close()


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _read_regular(path: Path, expected_uid: int | None = 0) -> tuple[bytes, os.stat_result]:
    """Read through one O_NOFOLLOW descriptor and check what was opened.

    Raises FileNotFoundError when absent and RuntimeError for a symlink,
    a non-regular file, a hard-linked file or the wrong owner.
    """
    refused = RuntimeError("failure latch is not a root-owned regular file; review manually")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO}:  # Symlink; FIFO/socket without peer.
            raise refused from exc
        raise
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (expected_uid is not None and info.st_uid != expected_uid)):
            raise refused
        return _read_all(fd), info
    finally:
        os.close(fd)


def record_latch(latch: Path, error: str, *, counter=suspend_success_count, boot=boot_id,
                 now=time.time, extra: dict | None = None) -> str:
    """Create the terminal failure latch, or append to the existing one.

    An existing latch is never overwritten: its first error stays the
    headline and later failures are appended. A stale latch that receives a
    new failure is re-armed with the current boot and success counter.
    """
    entry = {"error": error, "observed_at": now(), **(extra or {})}
    try:
        success = counter()
    except (OSError, ValueError):
        success = None
    try:
        current_boot = boot()
    except OSError:
        current_boot = None
    if os.path.lexists(latch):
        try:
            data, _info = _read_regular(latch, expected_uid=None)
            record = json.loads(data)
            if not isinstance(record, dict):
                raise ValueError("failure latch is not an object")
        except (OSError, ValueError, RuntimeError):
            LOG.error("event=FAILURE_LATCH_KEPT reason=existing latch unreadable; new failure: %s", error)
            return "kept"
        if latch_state(latch, counter=counter, boot=boot)[0] == "stale":
            record.update(boot_id=current_boot, suspend_success=success, reactivated_at=now())
        record.setdefault("later_failures", []).append(entry)
        atomic_json(latch, record, mode=0o644)
        return "appended"
    atomic_json(latch, {"schema": 2, "error": error, "observed_at": entry["observed_at"],
                        "boot_id": current_boot, "suspend_success": success, **(extra or {})},
                mode=0o644)
    return "created"


def _write_receipt(path: Path, payload: dict) -> None:
    """Root-owned authoritative receipt first, then the daemon-visible copy."""
    targets = [ROOT_RECEIPT, path.with_name(RESULT_NAME)]
    if targets[0] == targets[1]:
        targets.pop()
    failure = None
    written = 0
    for target in targets:
        try:
            atomic_json(target, payload, mode=0o644)
            written += 1
        except OSError as exc:
            failure = exc
            LOG.warning("Cannot write protective receipt %s: %s", target, exc)
    if not written and failure is not None:
        raise failure


# Largest journal or receipt root will parse (S-R6); real ones are a few KiB.
MAX_STATE_FILE_BYTES = 1 << 20


def _read_bounded_json(path: Path, limit: int = MAX_STATE_FILE_BYTES):
    """Parse a JSON file that a less privileged process may control (S-R6).

    Root reads the daemon's journal through one O_NOFOLLOW | O_NONBLOCK
    descriptor: a symbolic link, FIFO, device or other non-regular file, or
    anything larger than ``limit`` bytes, is refused with ValueError instead
    of being followed, blocking on, or read without bound. Raises
    FileNotFoundError when the file is absent.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO}:  # Symlink; FIFO/socket without peer.
            raise ValueError(f"{path} is not a regular file") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path} is not a regular file")
        if info.st_size > limit:
            raise ValueError(f"{path} is larger than {limit} bytes")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError(f"{path} is larger than {limit} bytes")
            chunks.append(chunk)
    finally:
        os.close(fd)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _read_receipt(path: Path, *, trust_daemon_copy: bool = False) -> dict | None:
    """The protective receipt. Recovery decisions use only the root-owned
    one; the daemon-writable copy next to ``path`` is never trusted there
    (S-R6)."""
    candidates = [ROOT_RECEIPT] + ([path.with_name(RESULT_NAME)] if trust_daemon_copy else [])
    for candidate in candidates:
        try:
            value = _read_bounded_json(candidate)
            return value if isinstance(value, dict) else None
        except FileNotFoundError:
            continue
    return None


def protect_main(run=subprocess.run, probe=stack_ready, telemetry=None, path=BREADCRUMB,
                 observe=observe_suspend, counter=suspend_success_count, *, recovery=False,
                 status=native_status, latch=None, dispatch=request_native_suspend) -> int:
    """One ordinary suspend attempt; systemd invokes a separate final recovery.

    Recovery stops the daemon synchronously, so the kernel closes all its evdev
    and inhibitor descriptors even if its event loop or release code is broken.
    With the lid inhibitor gone, logind's own lid policy owns a closed lid
    again. No automatic daemon restart may race this operation. Native hooks
    remain OS policy; we neither invoke their private APIs nor bypass them.

    Never requests a sleep while the OS sleep path is already busy or has
    just failed (see os_sleep_in_progress). The terminal failure latch is
    written only when AAG - InputLock's own request was accepted by logind and the
    kernel transition then failed or was never confirmed.
    """
    latch = FAILURE_LATCH if latch is None else latch
    telemetry = telemetry or Telemetry(sleep_probe=None)
    state = {}
    progress = {"accepted": False}

    def record(result: str, detail: str = "") -> None:
        _write_receipt(path, {"schema": 2, "boot_id": state.get("boot_id"),
                    "generation": state.get("generation"), "result": result,
                    "detail": detail, "observed_at": time.time(), "recovery": recovery,
                    "dispatch_accepted": progress["accepted"],
                    "transition_status": {"suspend-requested": "REQUESTED",
                        "suspend-request-accepted": "ACCEPTED", "suspend-transition-confirmed": "CONFIRMED",
                        "recovery-required": "RECOVERY_REQUIRED", "protection-failed": "FAILED",
                        DEFERRED_OS_SLEEP: "DEFERRED",
                        }.get(result, "CANCELLED"),
                    "transition_confirmed": result == "suspend-transition-confirmed"})

    try:
        daemon_result = invocation = None
        if recovery:
            # Capture before stopping: a pre-hook failure has a new invocation
            # but cannot have written a journal. Old same-boot state is not an
            # authorization to suspend on behalf of that failed startup.
            try:
                props = unit_properties(run, "input-lock.service")
                invocation, daemon_result = props.get("InvocationID"), props.get("Result")
                try:
                    state = _read_bounded_json(path)
                except FileNotFoundError:
                    # Fresh install or uninstall: nothing was pending, so
                    # nothing is eligible and nothing is latched.
                    state = {}
            finally:
                # Release even when the journal is missing/corrupt. Failure to
                # read evidence must never leave this process's lid suppression.
                result = run(["/usr/bin/systemctl", "stop", "input-lock.service"], timeout=10, check=False)
                if result.returncode or unit_properties(run, "input-lock.service").get("ActiveState") not in {"inactive", "failed"}:
                    raise RuntimeError("cannot confirm AAG - InputLock stopped; no sleep dispatched")
        else:
            state = _read_bounded_json(path)
        if not isinstance(state, dict):
            raise ValueError("invalid safety journal object")
        boot = boot_id()
        eligible = state.get("safety", {}).get("state") == "PROTECTIVE_ACTION_PENDING"
        if recovery:
            eligible = eligible or state.get("effective") is True
        if state.get("boot_id") != boot or not eligible:
            return 0
        if recovery:
            if not invocation or state.get("invocation_id") != invocation:
                record("cancelled-stale-invocation")
                return 0
            # Do not dispatch a second sleep after a completed first transition.
            receipt = _read_receipt(path)
            if (receipt is not None and receipt.get("boot_id") == boot
                    and receipt.get("generation") == state.get("generation")
                    and receipt.get("transition_confirmed") is True):
                return 0
            if daemon_result == "watchdog":
                # The daemon was frozen (OS sleep freezer) or wedged; either
                # way it is stopped now and logind owns the closed lid again.
                LOG.warning("event=PROTECTIVE_ACTION_DEFERRED reason=input-lock.service Result=watchdog")
                record(DEFERRED_OS_SLEEP, "input-lock.service Result=watchdog")
                return 0
        elif state.get("effective") is not False or state.get("restrictions_released") is not True:
            raise RuntimeError("AAG - InputLock restrictions have not been released")
        if telemetry.read().lid_closed is False:
            record("cancelled-lid-open")
            return 0
        if not recovery and not 0 <= time.time() - state.get("updated_at", 0) <= 20:
            raise RuntimeError("pending closed-lid request expired")
        busy = os_sleep_in_progress(run, status(), recovery=recovery)
        if busy:
            LOG.warning("event=PROTECTIVE_ACTION_DEFERRED reason=%s", busy)
            record(DEFERRED_OS_SLEEP, busy)
            return 0
        if not probe():
            raise RuntimeError("native suspend/fail-safe prerequisites unavailable")
        if not recovery:
            current = _read_bounded_json(path)
            if not isinstance(current, dict) or (current.get("generation") != state.get("generation") or
                    current.get("safety", {}).get("state") != "PROTECTIVE_ACTION_PENDING"):
                record("cancelled-before-dispatch")
                return 0
        native = status()
        if not native["supported"] or native["blocked"] or native["inputlock_holds_lid"]:
            raise RuntimeError("ordinary suspend unavailable or inhibited")
        if native["preparing"]:
            record(DEFERRED_OS_SLEEP, "logind is already preparing for sleep")
            return 0
        if unit_properties(run, "systemd-suspend.service").get("ActiveState") not in {"inactive", "failed"}:
            record(DEFERRED_OS_SLEEP, "a suspend transition is already in progress")
            return 0
        if telemetry.read().lid_closed is False:
            record("cancelled-before-dispatch")
            return 0
        before_success = counter()
        requested_at = time.monotonic()
        record("suspend-requested")
        dispatch()
        progress["accepted"] = True
        record("suspend-request-accepted")
        observe(run, requested_at, before_success)
        record("suspend-transition-confirmed")
        return 0
    except Exception as exc:
        LOG.error("event=PROTECTIVE_ACTION_FAILED recovery=%s accepted=%s error=%s",
                  recovery, progress["accepted"], exc)
        # The final failure stays visible across daemon restarts until a
        # later successful suspend or a reboot. There is intentionally no
        # force/hibernate/shutdown escalation.
        if progress["accepted"]:
            try:
                record_latch(latch, str(exc), counter=counter,
                             extra={"generation": state.get("generation"), "recovery": recovery})
            except OSError:
                LOG.exception("Cannot persist emergency failure latch")
        try:
            record("protection-failed" if recovery else "recovery-required", str(exc))
        except OSError:
            LOG.exception("Cannot persist protective result")
        return 1


def record_failure(service_result: str, *, latch: Path | None = None, receipt: Path | None = None,
                   counter=suspend_success_count, boot=boot_id, now=time.time) -> str:
    """ExecStopPost of the failsafe unit: also covers watchdog/timeout/SIGKILL.

    Appends the SERVICE_RESULT to an existing latch, never overwriting it.
    Without one, it latches only if this boot's root-owned receipt shows an
    accepted AAG - InputLock suspend request that was never confirmed.
    """
    if service_result == "success":
        return "no-failure"
    latch = FAILURE_LATCH if latch is None else latch
    receipt = ROOT_RECEIPT if receipt is None else receipt
    if os.path.lexists(latch):
        return record_latch(latch, "recovery unit failed: " + service_result, counter=counter,
                            boot=boot, now=now, extra={"service_result": service_result})
    try:
        evidence = _read_bounded_json(receipt)
    except (OSError, ValueError):
        evidence = None
    try:
        current_boot = boot()
    except OSError:
        current_boot = None
    if (isinstance(evidence, dict) and current_boot and evidence.get("boot_id") == current_boot
            and evidence.get("dispatch_accepted") is True and evidence.get("transition_confirmed") is not True):
        return record_latch(latch, "recovery unit failed after an accepted suspend request: " + service_result,
                            counter=counter, boot=boot, now=now,
                            extra={"service_result": service_result, "generation": evidence.get("generation")})
    LOG.warning("event=RECOVERY_UNIT_FAILED result=%s latched=no "
                "reason=no accepted AAG - InputLock suspend request", service_result)
    return "not-latched"


def _prepare_archive(archive: Path, expected_uid: int) -> None:
    if any(p.is_symlink() for p in (archive, *archive.parents)):
        raise RuntimeError("archive path contains a symlink")
    missing = [p for p in (archive, *archive.parents) if not os.path.lexists(p)]
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    for directory in (archive, archive.parent):
        info = os.lstat(directory)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid
                or info.st_mode & 0o022):
            raise RuntimeError(f"archive directory {directory} must be owned by root "
                               "and not writable by group or others")


def acknowledge_failure(run=subprocess.run, latch=None, archive=None,
                        euid=os.geteuid, now=time.time, *, expected_uid: int = 0) -> dict:
    """Explicit operator acknowledgement of a failed final protective suspend.

    This never claims the failed attempt succeeded. The latch is copied to a
    root-only, hash-verified archive and kept in place, renamed to
    failure-acknowledged-<sha256>.json, exactly as the 2026-10-17 recovery did.
    The latch is read through one O_NOFOLLOW descriptor and re-hashed just
    before the rename, so a failure recorded meanwhile is never renamed
    without having been archived. Raises FileNotFoundError when no latch is
    present.
    """
    latch = FAILURE_LATCH if latch is None else latch
    archive = ACK_ARCHIVE if archive is None else archive
    if euid() != 0:
        raise PermissionError("acknowledge-failure must run as root")
    data, info = _read_regular(latch, expected_uid)
    for unit in (PROTECT_UNIT, FAILSAFE_UNIT, "systemd-suspend.service"):
        state = unit_properties(run, unit).get("ActiveState")
        if state not in {"inactive", "failed"}:
            raise RuntimeError(f"{unit} is {state}; wait until it finishes")
    try:
        record = json.loads(data)
    except ValueError as exc:
        raise RuntimeError("failure latch is not valid JSON; review manually") from exc
    if not isinstance(record, dict):
        raise RuntimeError("failure latch is not a JSON object; review manually")
    digest = hashlib.sha256(data).hexdigest()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now()))
    _prepare_archive(archive, expected_uid)
    for attempt in range(100):
        target = archive / (f"{stamp}-failure-latch-{digest[:12]}" + (f"-{attempt}" if attempt else ""))
        try:
            target.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("cannot allocate a unique archive directory")
    saved = target / "failure.json"
    fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if hashlib.sha256(_read_regular(saved, expected_uid)[0]).hexdigest() != digest:
        raise RuntimeError("archive verification failed; latch left in place")
    current, current_info = _read_regular(latch, expected_uid)
    if ((current_info.st_dev, current_info.st_ino) != (info.st_dev, info.st_ino)
            or hashlib.sha256(current).hexdigest() != digest):
        raise RuntimeError("failure latch changed during acknowledgement; nothing was renamed. "
                           "Run acknowledge-failure again")
    retained = latch.with_name(f"failure-acknowledged-{digest}.json")
    if os.path.lexists(retained):
        retained = latch.with_name(f"failure-acknowledged-{digest}-{stamp}.json")
    os.rename(latch, retained)
    os.sync()
    LOG.info("event=PROTECTION_FAILURE_ACKNOWLEDGED sha256=%s archive=%s", digest, saved)
    return {"acknowledged": True, "sha256": digest, "error": record.get("error"),
            "archive": str(saved), "retained_as": str(retained)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AAG - InputLock ordinary suspend protection")
    parser.add_argument("action", choices=("protect", "recover", "check", "record-failure", "acknowledge-failure"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    if args.action == "check":
        report = protection_report()
        print(json.dumps({"ready": not report["errors"], "errors": report["errors"], "info": report["info"]}))
        raise SystemExit(bool(report["errors"]))
    if args.action == "acknowledge-failure":
        try:
            print(json.dumps(acknowledge_failure()))
        except FileNotFoundError:
            print(json.dumps({"acknowledged": False, "reason": "no failure latch present"}))
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit("acknowledge-failure refused: " + str(exc))
        raise SystemExit(0)
    if os.geteuid() != 0:
        raise SystemExit("protective adapter requires the installed root service")
    if args.action == "record-failure":
        try:
            outcome = record_failure(os.environ.get("SERVICE_RESULT", "unknown"))
            LOG.info("event=RECORD_FAILURE outcome=%s", outcome)
        except Exception:
            LOG.exception("Cannot record the failsafe unit result")
        raise SystemExit(0)
    raise SystemExit(protect_main(recovery=args.action == "recover"))
