"""Supervised lid suppression, not a replacement machine power controller.

Pure decisions are separate from telemetry and InputLock's native suspend adapter.
No incident-time thermal evidence established the cause of the 2026-09-11 stop.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

LOG = logging.getLogger("input-lock-safety")
BREADCRUMB = Path("/var/lib/input-lock/safety.json")
PROTECT_UNIT = "input-lock-protect.service"
FAILSAFE_UNIT = "input-lock-failsafe.service"
FAILURE_LATCH = Path("/var/lib/input-lock-protection/failure.json")
THERMAL_MEASUREMENTS = ("NORMAL", "WARNING", "HIGH_RISK", "EMERGENCY")
THERMAL_STATES = ("NORMAL", "WARNING", "HIGH_RISK", "EMERGENCY_SOFTWARE_PROTECTION")


def atomic_json(path: Path, value: dict) -> None:
    """Keep the previous complete record on write failure; sync its directory."""
    fd, temporary = tempfile.mkstemp(prefix=".safety-", dir=path.parent)
    try:
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

    def healthy(self, now: float) -> bool:
        kinds = {sensor.kind for sensor in self.sensors}
        return (0 <= now - self.at <= 12 and self.lid_closed is not None
                and {"cpu", "chassis"} <= kinds and self.ac is not None
                and self.battery is not None and self.discharging is not None
                and self.dependencies_ok and self.stack_ready and not self.errors)


class SafetyPolicy:
    """Monotonic, continuously supervised lid-ignore; never performs power actions.

    No normal closed-lid duration expires this policy. Only a thermal/battery
    risk or loss of reliable supervision can request protective suspend.
    """
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

    def event(self, reason: str) -> None:
        self.events.append(reason)
        LOG.info("event=%s state=%s", reason, self.state)

    def transition(self, state: str, reason: str) -> None:
        if self.state != state or self.reason != reason:
            self.state, self.reason = state, reason
            self.event(reason)

    def arm(self, sample: Sample) -> None:
        if not sample.stack_ready:
            detail = "; ".join(sample.protection_errors) or "native suspend/fail-safe readiness unavailable"
            raise ValueError("Lid-ignore unavailable: " + detail)
        if not sample.healthy(self.clock()) or sample.lid_closed is not False:
            raise ValueError("Open the lid and restore fresh safety telemetry before arming")
        if any(sensor.risk() >= 2 for sensor in sample.sensors):
            raise ValueError("Temperature is already too high to arm lid-ignore")
        # Explicit re-arm with a freshly observed open lid starts a new
        # episode even if no periodic open-lid update ran in between.
        self.release()
        self.last_decision = None
        self.previous_ac = sample.ac
        self.required_sensors = {s.identity for s in sample.sensors}
        self.armed = True
        self.sample = sample
        self.transition("LID_IGNORE_ARMED", "LID_IGNORE_ARMED")

    def release(self, reason: str = "SAFE_STATE_RESTORED") -> None:
        self.armed = False
        self.closed_since = self.deadline = self.missing_since = None
        self.thermal_since = self.critical_since = None
        self.thermal_level = self.thermal_candidate = 0
        self.transition("SAFE_NORMAL", reason)

    def update(self, sample: Sample) -> str | None:
        now = self.clock()
        self.sample = sample
        if not self.armed:
            return None
        if sample.lid_closed is False:
            if self.closed_since is not None:
                self.event("LID_OPENED")
                self.event("CONTINUOUS_MONITORING_ENDED")
            self.closed_since = self.deadline = self.missing_since = None
            self.thermal_since = self.critical_since = None
            self.thermal_level = self.thermal_candidate = 0
            self.transition("LID_IGNORE_ARMED", "LID_IGNORE_ARMED")
            return None
        if self.closed_since is None:
            self.closed_since = now
            self.transition("LID_CLOSED_MONITORED", "LID_CLOSED")
            self.event("CONTINUOUS_MONITORING_STARTED")
        # Unknown lid state is conservatively supervised as closed.
        # Supervision-loss duration is bounded independently of closed-lid time.
        ids = {s.identity for s in sample.sensors}
        healthy = sample.healthy(now) and self.required_sensors <= ids
        if not healthy:
            if self.missing_since is None:
                self.missing_since = now
                self.event("TELEMETRY_LOST")
            if now - self.missing_since >= 15:
                return self.decide("TELEMETRY_LOST")
        else:
            if self.missing_since is not None:
                self.event("TELEMETRY_RESTORED")
            self.missing_since = None
        if self.previous_ac is not None and self.previous_ac != sample.ac:
            self.event("AC_CONNECTED" if sample.ac else "AC_LOST")
        self.previous_ac = sample.ac
        # AC loss and processor load are observations, never normal
        # time-based suspend triggers. Thermal sensors are still supervised.
        raw = max((s.risk() for s in sample.sensors), default=0)
        if raw < self.thermal_level and any(s.risk(3) >= self.thermal_level for s in sample.sensors):
            raw = self.thermal_level
        if raw != self.thermal_candidate:
            self.thermal_candidate, self.thermal_since = raw, now
        if self.thermal_since is None:
            self.thermal_since = now
        dwell = {0: 15, 1: 10, 2: 6, 3: 2}[raw]
        if raw != self.thermal_level and now - self.thermal_since >= dwell:
            self.thermal_level = raw
            self.event(("THERMAL_NORMAL", "THERMAL_WARNING", "THERMAL_HIGH_RISK", "THERMAL_EMERGENCY")[raw])
        critical = ((sample.battery is not None and sample.battery <= 10)
                    or (sample.warning_level is not None and sample.warning_level >= 4))
        if critical:
            if self.critical_since is None:
                self.critical_since = now
                self.event("BATTERY_CRITICAL")
            if now - self.critical_since >= 2:
                return self.decide("BATTERY_CRITICAL")
        else:
            self.critical_since = None
        if self.thermal_level >= 2:
            return self.decide("THERMAL_HIGH_RISK" if self.thermal_level == 2 else "THERMAL_EMERGENCY")
        if (self.thermal_level >= 1 or not healthy
                or (sample.battery is not None and sample.battery <= 20)):
            self.transition(
                "LID_CLOSED_WARNING",
                "BATTERY_WARNING" if sample.battery is not None and sample.battery <= 20
                else "SAFETY_WARNING",
            )
        else:
            self.transition("LID_CLOSED_MONITORED", "MONITORING")
        return None

    def decide(self, reason: str) -> str:
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
            "cpu_busy": sample.cpu_busy if sample is not None else None,
        }
        if reason in {"THERMAL_HIGH_RISK", "THERMAL_EMERGENCY"} and sample is not None:
            # Retain the actual hot readings and software intervention levels.
            # Previously only the generic THERMAL_EMERGENCY reason survived,
            # making false/single-sensor spikes indistinguishable from
            # sustained corroborated heat after the device resumed.
            self.last_decision["thermal_evidence"] = {
                "thermal_level": self.thermal_level,
                "candidate_level": self.thermal_candidate,
                "candidate_seconds": (round(now - self.thermal_since, 2)
                                      if self.thermal_since is not None else None),
                "sensors": [
                    {"identity": s.identity, "kind": s.kind,
                     "celsius": round(s.celsius, 2),
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

    def measured_thermal(self) -> dict:
        """Describe the sample, independently of intervention arming/dwell."""
        sample = self.sample
        fresh = sample is not None and 0 <= self.clock() - sample.at <= 12
        sensors = sample.sensors if sample is not None else []
        valid = [s for s in sensors if math.isfinite(s.celsius)]
        complete = (fresh and {"cpu", "chassis"} <= {s.kind for s in valid}
                    and self.required_sensors <= {s.identity for s in valid}
                    and len(valid) == len(sensors) and not sample.errors)
        risk = max((s.risk() for s in valid), default=0)
        # A missing sensor must not hide a known hot reading. An incomplete
        # normal sample, however, cannot certify a normal thermal condition.
        state = (THERMAL_MEASUREMENTS[risk] if fresh and valid and (complete or risk > 0)
                 else "UNKNOWN")
        return {"state": state, "fresh": bool(fresh), "complete": bool(complete)}

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
                "sample": asdict(self.sample) if self.sample else None}


class Telemetry:
    """Kernel drivers/types identify sensors; numbering and display labels do not."""
    def __init__(self, sys_root=Path("/sys"), proc_root=Path("/proc")):
        self.sys, self.proc = sys_root, proc_root
        self.previous_cpu = None

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
            limits = ((min(85, crit - 25), min(95, crit - 15), min(100, crit - 10))
                      if category == "cpu" else (min(65, crit - 20), min(70, crit - 12), min(73, crit - 7)))
            sensors.append(Sensor(f"thermal:{kind}", category, value, *limits))
        for hwmon in (self.sys / "class/hwmon").glob("hwmon*"):
            try:
                driver = (hwmon / "name").read_text().strip()
            except OSError:
                continue
            if driver not in {"coretemp", "k10temp", "nvme"}:
                continue
            # NVMe composite temp1 has specified critical limits; secondary
            # controller sensors may have invalid firmware max values.
            inputs = [hwmon / "temp1_input"] if driver == "nvme" else list(hwmon.glob("temp*_input"))
            readings, criticals = [], []
            for p in inputs:
                value = self.temperature(p)
                if value is None:
                    errors.append(f"unreadable:{driver}:{p.stem}")
                    continue
                readings.append(value)
                critical = self.temperature(p.with_name(p.name.replace("_input", "_crit")))
                if critical is not None:
                    criticals.append(critical)
            if readings:
                # Summarize per driver/device using the hottest valid channel
                # and lowest valid trip. Identity survives channel reordering.
                identity = f"hwmon:{driver}:{hwmon.resolve().parent}"
                category = "storage" if driver == "nvme" else "cpu"
                crit = min(criticals, default=85 if category == "storage" else 100)
                limits = ((min(70, crit - 15), min(77, crit - 10), min(80, crit - 5))
                          if category == "storage" else (min(85, crit - 25), min(95, crit - 15), min(100, crit - 10)))
                sensors.append(Sensor(identity, category, max(readings), *limits))
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
        return Sample(observed_at, any(lids) if lids else None, sensors,
                      any(ac_values) if ac_values else None,
                      min(batteries) if batteries else None,
                      any(discharging) if discharging else None, warning_level,
                      cpu_busy, dependencies_ok, stack_ready, errors)


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


def unit_properties(run, unit: str) -> dict:
    result = run(["/usr/bin/systemctl", "show", unit,
                  "--property=LoadState,ActiveState,ExecStart,ExecStopPost,OnFailure,WatchdogUSec,Restart,DropInPaths,User,Type,TimeoutStartUSec,TimeoutStopUSec,KillMode,InvocationID"],
                 capture_output=True, text=True, timeout=3, check=False)
    if result.returncode:
        raise RuntimeError("cannot inspect " + unit)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def protection_errors(run=subprocess.run, status=native_status, counter=None) -> list[str]:
    """Effective native prerequisites; retired services and AAG APIs are irrelevant.

    This is a capability check, not proof that a future kernel suspend succeeds.
    A failed final attempt latches arming off until explicit operator recovery.
    """
    try:
        if FAILURE_LATCH.exists():
            return ["previous emergency suspend failed; operator recovery required"]
        state = status()
        if not state["supported"] or state["blocked"] or state["preparing"]:
            return ["ordinary suspend unavailable, blocked, or already in progress"]
        (counter or suspend_success_count)()
        for unit, command in {
            "systemd-suspend.service": "/usr/lib/systemd/systemd-sleep suspend",
            PROTECT_UNIT: "/usr/lib/input-lock/input_lock_safety.py protect",
            FAILSAFE_UNIT: "/usr/lib/input-lock/input_lock_safety.py recover",
            "input-lock.service": "/usr/lib/input-lock/input-lock-daemon",
        }.items():
            props = unit_properties(run, unit)
            if props.get("LoadState") != "loaded" or command not in props.get("ExecStart", ""):
                return ["missing or incompatible " + unit]
            if unit in {PROTECT_UNIT, FAILSAFE_UNIT} and (props.get("User") not in {"", "root"}
                    or props.get("Type") != "oneshot" or props.get("TimeoutStartUSec") != "2min 45s"
                    or props.get("Restart") != "no"):
                return ["InputLock root helper/timeout contract changed: " + unit]
            if unit in {PROTECT_UNIT, "input-lock.service"} and props.get("OnFailure", "").split() != [FAILSAFE_UNIT]:
                return ["missing InputLock failure handler on " + unit]
            if unit == FAILSAFE_UNIT and props.get("OnFailure"):
                return ["unexpected recursive failure handler"]
            if unit == FAILSAFE_UNIT and "/usr/lib/input-lock/input_lock_safety.py record-failure" not in props.get("ExecStopPost", ""):
                return ["missing terminal failure recorder"]
            if unit == "input-lock.service" and (props.get("Restart") != "no" or
                    props.get("WatchdogUSec") != "20s" or props.get("TimeoutStopUSec") != "5s"
                    or props.get("KillMode") != "control-group"):
                return ["InputLock watchdog/restart contract changed"]
        return []
    except Exception as exc:
        return ["native protection probe failed: " + str(exc)]


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
                    sleep=time.sleep, counter=suspend_success_count) -> None:
    """A successful enqueue is not a completed sleep/resume operation."""
    deadline = clock() + 120
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


def protect_main(run=subprocess.run, probe=stack_ready, telemetry=None, path=BREADCRUMB,
                 observe=observe_suspend, counter=suspend_success_count, *, recovery=False,
                 status=native_status, latch=FAILURE_LATCH, dispatch=request_native_suspend) -> int:
    """One ordinary suspend attempt; systemd invokes a separate final recovery.

    Recovery stops the daemon synchronously, so the kernel closes all its evdev
    and inhibitor descriptors even if its event loop or release code is broken.
    No automatic daemon restart may race this operation. Native hooks remain OS
    policy; we neither invoke their private APIs nor bypass them.
    """
    telemetry = telemetry or Telemetry()
    state = {}

    def record(result: str, detail: str = "") -> None:
        receipt = path.with_name("safety-action-result.json")
        atomic_json(receipt, {"schema": 1, "boot_id": state.get("boot_id"),
                    "generation": state.get("generation"), "result": result,
                    "detail": detail, "observed_at": time.time(),
                    "transition_status": {"suspend-requested": "REQUESTED",
                        "suspend-request-accepted": "ACCEPTED", "suspend-transition-confirmed": "CONFIRMED",
                        "recovery-required": "RECOVERY_REQUIRED", "protection-failed": "FAILED"
                        }.get(result, "CANCELLED"),
                    "transition_confirmed": result == "suspend-transition-confirmed"})
        os.chmod(receipt, 0o644)

    try:
        if recovery:
            # Capture before stopping: a pre-hook failure has a new invocation
            # but cannot have written a journal. Old same-boot state is not an
            # authorization to suspend on behalf of that failed startup.
            try:
                invocation = unit_properties(run, "input-lock.service").get("InvocationID")
                state = json.loads(path.read_text())
            finally:
                # Release even when the journal is missing/corrupt. Failure to
                # read evidence must never leave this process's lid suppression.
                result = run(["/usr/bin/systemctl", "stop", "input-lock.service"], timeout=10, check=False)
                if result.returncode or unit_properties(run, "input-lock.service").get("ActiveState") not in {"inactive", "failed"}:
                    raise RuntimeError("cannot confirm InputLock stopped; no sleep dispatched")
        else:
            state = json.loads(path.read_text())
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
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
            try:
                receipt = json.loads(path.with_name("safety-action-result.json").read_text())
                if (receipt.get("boot_id") == boot and receipt.get("generation") == state.get("generation")
                        and receipt.get("transition_confirmed") is True):
                    return 0
            except FileNotFoundError:
                pass
        elif state.get("effective") is not False or state.get("restrictions_released") is not True:
            raise RuntimeError("InputLock restrictions have not been released")
        if telemetry.read().lid_closed is False:
            record("cancelled-lid-open")
            return 0
        if not recovery and not 0 <= time.time() - state.get("updated_at", 0) <= 20:
            raise RuntimeError("pending closed-lid request expired")
        if not probe():
            raise RuntimeError("native suspend/fail-safe prerequisites unavailable")
        if not recovery:
            current = json.loads(path.read_text())
            if (current.get("generation") != state.get("generation") or
                    current.get("safety", {}).get("state") != "PROTECTIVE_ACTION_PENDING"):
                record("cancelled-before-dispatch")
                return 0
        native = status()
        if (not native["supported"] or native["blocked"] or native["preparing"] or native["inputlock_holds_lid"]):
            raise RuntimeError("ordinary suspend unavailable, inhibited, or already preparing")
        if unit_properties(run, "systemd-suspend.service").get("ActiveState") not in {"inactive", "failed"}:
            raise RuntimeError("a suspend transition is already in progress; refusing duplicate")
        if telemetry.read().lid_closed is False:
            record("cancelled-before-dispatch")
            return 0
        before_success = counter()
        requested_at = time.monotonic()
        record("suspend-requested")
        dispatch()
        record("suspend-request-accepted")
        observe(run, requested_at, before_success)
        record("suspend-transition-confirmed")
        return 0
    except Exception as exc:
        LOG.error("event=PROTECTIVE_ACTION_FAILED recovery=%s error=%s", recovery, exc)
        # The final failure stays visible across daemon restarts and boots.
        # There is intentionally no force/hibernate/shutdown escalation.
        if recovery:
            try:
                atomic_json(latch, {"error": str(exc), "observed_at": time.time()})
                os.chmod(latch, 0o644)
            except OSError:
                LOG.exception("Cannot persist emergency failure latch")
        try:
            record("protection-failed" if recovery else "recovery-required", str(exc))
        except OSError:
            LOG.exception("Cannot persist protective result")
        return 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="InputLock ordinary suspend protection")
    parser.add_argument("action", choices=("protect", "recover", "check", "record-failure"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    if args.action == "check":
        errors = protection_errors()
        print(json.dumps({"ready": not errors, "errors": errors}))
        raise SystemExit(bool(errors))
    if os.geteuid() != 0:
        raise SystemExit("protective adapter requires the installed root service")
    if args.action == "record-failure":
        # Includes watchdog/timeout/SIGKILL failures outside Python's handler.
        if os.environ.get("SERVICE_RESULT") != "success":
            atomic_json(FAILURE_LATCH, {"error": "recovery unit failed: " + os.environ.get("SERVICE_RESULT", "unknown"),
                                       "observed_at": time.time()})
            os.chmod(FAILURE_LATCH, 0o644)
        raise SystemExit(0)
    raise SystemExit(protect_main(recovery=args.action == "recover"))
