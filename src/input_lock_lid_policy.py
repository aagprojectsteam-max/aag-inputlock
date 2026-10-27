#!/usr/bin/python3
"""Restoration-only migration of legacy AAG - InputLock GNOME/UPower overrides.

New lid leases never change automatic sleep or critical-battery settings.
Recovery journals remain until each relevant runtime verification succeeds.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Final, Protocol


POWER_SCHEMA: Final = "org.gnome.settings-daemon.plugins.power"
GNOME_POLICY_VALUES: Final[dict[str, str]] = {
    "lid-close-ac-action": "nothing",
    "lid-close-battery-action": "nothing",
    "sleep-inactive-ac-type": "nothing",
    "sleep-inactive-battery-type": "nothing",
}
GNOME_INSPECT_KEYS: Final = (
    *GNOME_POLICY_VALUES,
    "sleep-inactive-ac-timeout",
    "sleep-inactive-battery-timeout",
    "lid-close-suspend-with-external-monitor",
)
BACKUP_PATH: Final = Path.home() / ".config/input-lock/lid-power-policy-backup.json"
POWER_HELPER: Final = Path("/usr/lib/input-lock/input-lock-power-helper")
UPOWER_CONFIG: Final = Path("/etc/UPower/UPower.conf")
UPOWER_DROPIN_DIR: Final = Path("/etc/UPower/UPower.conf.d")
UPOWER_DROPIN_NAME: Final = "99-zzzz-input-lock-no-auto-sleep.conf"
UPOWER_DROPIN_CONTENT: Final = (
    "# Managed by LockLock. Remove by disabling lid-ignore mode.\n"
    "[UPower]\n"
    "AllowRiskyCriticalPowerAction=true\n"
    "CriticalPowerAction=Ignore\n"
)
VALID_UPOWER_DROPIN: Final = re.compile(r"^[0-9][0-9]-[A-Za-z0-9_-]*\.conf$")


class PolicyError(RuntimeError):
    """A power-policy transition or verification failed."""


class SettingsBackend(Protocol):
    def get(self, key: str) -> object: ...
    def set(self, key: str, value: object) -> None: ...
    def sync(self) -> None: ...


class GioPowerSettings:
    """Small lazy wrapper so non-GNOME tests do not need a live session bus."""

    def __init__(self) -> None:
        from gi.repository import Gio

        self._gio = Gio
        self._settings = Gio.Settings.new(POWER_SCHEMA)

    def get(self, key: str) -> object:
        return self._settings.get_value(key).unpack()

    def set(self, key: str, value: object) -> None:
        current = self._settings.get_value(key)
        signature = current.get_type_string()
        if signature == "s" and isinstance(value, str):
            ok = self._settings.set_string(key, value)
        elif signature == "u" and isinstance(value, int) and not isinstance(value, bool):
            ok = self._settings.set_uint(key, value)
        elif signature == "b" and isinstance(value, bool):
            ok = self._settings.set_boolean(key, value)
        else:
            raise PolicyError(f"unsupported value for GNOME power key {key}")
        if not ok:
            raise PolicyError(f"GNOME rejected power key {key}")

    def sync(self) -> None:
        self._gio.Settings.sync()


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, payload: dict[str, object], mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _read_backup(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot read saved GNOME power policy: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise PolicyError("saved GNOME power policy is invalid")
    values = payload.get("values")
    if not isinstance(values, dict):
        raise PolicyError("saved GNOME power policy has no values")
    return payload


def _run_helper(action: str, *, privileged: bool) -> dict[str, object]:
    command = [str(POWER_HELPER), action]
    if privileged:
        command.insert(0, "pkexec")
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120 if privileged else 10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PolicyError(f"UPower helper failed: {exc}") from exc
    if result.returncode != 0:
        raise PolicyError(result.stderr.strip() or "UPower helper rejected the request")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PolicyError("UPower helper returned invalid data") from exc
    if not isinstance(payload, dict):
        raise PolicyError("UPower helper returned a non-object response")
    return payload


class LidNoSuspendPolicy:
    """Transactional user/session policy with a separately privileged UPower step."""

    def __init__(
        self,
        settings: SettingsBackend,
        *,
        backup_path: Path = BACKUP_PATH,
        helper: Callable[[str, bool], dict[str, object]] | None = None,
    ) -> None:
        self.settings = settings
        self.backup_path = backup_path
        self._helper = helper or (lambda action, privileged: _run_helper(action, privileged=privileged))

    @classmethod
    def current_user(cls) -> "LidNoSuspendPolicy":
        return cls(GioPowerSettings())

    def _capture(self) -> dict[str, object]:
        return {key: self.settings.get(key) for key in GNOME_INSPECT_KEYS}

    def _restore_values(self, values: object) -> None:
        if not isinstance(values, dict):
            raise PolicyError("saved GNOME power values are invalid")
        errors = []
        for key in GNOME_POLICY_VALUES:
            try:
                value = values.get(key)
                if not isinstance(value, str):
                    raise PolicyError(f"saved GNOME power value {key} is invalid")
                self.settings.set(key, value)
            except Exception as exc:
                errors.append(str(exc))
        self.settings.sync()
        if errors:
            raise PolicyError("; ".join(errors))

    def enable(self) -> dict[str, object]:
        raise PolicyError("persistent sleep/battery overrides were removed; use the daemon-owned lid lease")


    def disable(self) -> dict[str, object]:
        errors = []
        try:
            result = self._helper("disable", True)
            if result.get("normal_os_policy") is not True and result.get("restoration_required") is not False:
                raise PolicyError("UPower restoration was not verified")
        except Exception as exc:
            errors.append(str(exc))
        # Try independent user restoration even after a root-side failure, but
        # retain its journal until the complete requested restoration succeeds.
        try:
            self.restore_user(remove_backup=not errors)
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            raise PolicyError("; ".join(errors))
        return self.status()

    def restore_user(self, *, remove_backup: bool = True) -> dict[str, object]:
        if not self.backup_path.exists():
            return {"user_restored": True, "backup_present": False}
        import fcntl
        with self.backup_path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            backup = _read_backup(self.backup_path)
            if backup is not None:
                values = backup["values"]
                self._restore_values(values)
                if any(self.settings.get(key) != values[key] for key in GNOME_POLICY_VALUES):
                    raise PolicyError("GNOME restoration verification failed; backup retained")
                if remove_backup:
                    self.backup_path.unlink()
                    _sync_directory(self.backup_path.parent)
        return {"user_restored": True, "backup_present": self.backup_path.exists()}


    def status(self) -> dict[str, object]:
        gnome = self._capture()
        try:
            upower = self._helper("status", False)
        except Exception as exc:
            upower = {"healthy": False, "error": str(exc)}
        gnome_healthy = all(
            gnome.get(key) == expected for key, expected in GNOME_POLICY_VALUES.items()
        )
        return {
            "healthy": gnome_healthy and upower.get("healthy") is True,
            "gnome_healthy": gnome_healthy,
            "gnome": gnome,
            "upower": upower,
            "backup_present": self.backup_path.is_file(),
        }


class UPowerPolicyController:
    """Root-side controller for one dedicated UPower drop-in."""

    def __init__(
        self,
        *,
        config_path: Path = UPOWER_CONFIG,
        dropin_dir: Path = UPOWER_DROPIN_DIR,
        restart: Callable[[], None] | None = None,
        service_active: Callable[[], bool] | None = None,
        runtime_action: Callable[[], str] | None = None,
        restart_nowait: Callable[[], None] | None = None,
    ) -> None:
        self.config_path = config_path
        self.dropin_dir = dropin_dir
        self.dropin_path = dropin_dir / UPOWER_DROPIN_NAME
        self._restart = restart or self._restart_upower
        self._restart_nowait = restart_nowait or self._schedule_upower_restart
        self._service_active = service_active or self._upower_is_active
        self._runtime_action = runtime_action or self._query_runtime_action

    @staticmethod
    def _restart_upower() -> None:
        subprocess.run(
            ["systemctl", "restart", "upower.service"],
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )

    @staticmethod
    def _upower_is_active() -> bool:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", "upower.service"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode == 0

    def _effective(self) -> dict[str, str]:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str  # type: ignore[assignment]
        paths = [self.config_path]
        if self.dropin_dir.is_dir():
            paths.extend(
                path
                for path in sorted(self.dropin_dir.iterdir())
                if path.is_file() and VALID_UPOWER_DROPIN.fullmatch(path.name)
            )
        parser.read([str(path) for path in paths], encoding="utf-8")
        if not parser.has_section("UPower"):
            return {}
        return dict(parser.items("UPower"))

    def status(self) -> dict[str, object]:
        effective = self._effective()
        service_active = self._service_active()
        runtime = self._runtime_action() if service_active else None
        normal = (not self.dropin_path.exists() and service_active
                  and isinstance(runtime, str) and runtime.lower() in {"hybridsleep", "hibernate", "poweroff", "suspend"})
        return {
            "healthy": False, "normal_os_policy": normal,
            "dropin_present": self.dropin_path.exists(),
            "service_active": service_active,
            "configured_critical_power_action": effective.get("CriticalPowerAction"),
            "runtime_critical_power_action": runtime,
            "recovery_pending": (self.dropin_dir / ".input-lock-restore-pending.json").exists(),
        }

    @staticmethod
    def _query_runtime_action() -> str:
        import dbus
        bus = dbus.bus.BusConnection(dbus.bus.BUS_SYSTEM)
        try:
            interface = dbus.Interface(bus.get_object("org.freedesktop.UPower", "/org/freedesktop/UPower"), "org.freedesktop.UPower")
            return str(interface.GetCriticalAction(timeout=5))
        finally:
            bus.close()


    def enable(self) -> dict[str, object]:
        raise PolicyError("critical-battery overrides are no longer supported")


    @staticmethod
    def _schedule_upower_restart() -> None:
        # Enqueue the restart job and return at once (review S6): the
        # daemon unit's ExecStopPost runs under TimeoutStopSec=5s, while a
        # blocking UPower restart may take up to 30 s.
        subprocess.run(
            ["systemctl", "restart", "--no-block", "upower.service"],
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=4,
        )

    def disable(self, *, wait: bool = True) -> dict[str, object]:
        """Remove the legacy UPower drop-in and restore UPower's own policy.

        With ``wait=False`` (the daemon unit's ExecStopPost) the UPower
        restart is only enqueued and not verified; the recovery marker is kept,
        so the next ExecStartPre (``wait=True``) restarts again if needed,
        verifies the runtime policy and removes the marker.
        """
        pending = self.dropin_dir / ".input-lock-restore-pending.json"
        # No owned mutation means there is nothing to restore. In particular,
        # boot must not depend on UPower already answering its runtime API.
        # lexists also prevents dangling recovery symlinks being treated as absent.
        if not os.path.lexists(self.dropin_path) and not os.path.lexists(pending):
            return {"restoration_required": False, "normal_os_policy": None,
                    "dropin_present": False, "recovery_pending": False}
        if pending.is_symlink() or self.dropin_path.is_symlink():
            raise PolicyError("refusing symlink in UPower restoration state")
        if self.dropin_path.exists():
            if self.dropin_path.is_symlink() or self.dropin_path.read_text(encoding="utf-8") != UPOWER_DROPIN_CONTENT:
                raise PolicyError(f"refusing to remove unmanaged {self.dropin_path}")
            _atomic_json(pending, {"version": 1, "action": "restore-normal"}, 0o600)
            self.dropin_path.unlink()
            _sync_directory(self.dropin_dir)
        if not wait:
            if pending.exists():
                self._restart_nowait()
            return {"restoration_required": True, "restart_scheduled": pending.exists(),
                    "normal_os_policy": None, "dropin_present": self.dropin_path.exists(),
                    "recovery_pending": pending.exists()}
        if pending.exists():
            self._restart()
        result = self.status()
        if not result["normal_os_policy"]:
            raise PolicyError("UPower runtime restoration not verified; recovery marker retained")
        if pending.exists():
            pending.unlink()
            _sync_directory(self.dropin_dir)
        result["recovery_pending"] = False
        result["restoration_required"] = True
        return result



def user_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Manage AAG - InputLock GNOME lid policy")
    parser.add_argument("action", choices=("enable-user", "disable-user", "status-user"))
    args = parser.parse_args(argv)
    policy = LidNoSuspendPolicy.current_user()
    try:
        if args.action == "enable-user":
            policy.enable()  # Explicitly rejected; retained only for clear migration errors.
        elif args.action == "disable-user":
            payload = policy.restore_user()
        else:
            payload = policy.status()
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"input-lock lid policy: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(user_main())
