#!/usr/bin/python3
"""Shared configuration and protocol helpers for AAG - InputLock.

This module deliberately has no evdev/udev dependency, so configuration and
CLI tests can run without access to physical input devices.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from locklock_core import (
    LOCK_MODES,
    MAX_MESSAGE_BYTES,
    VERSION as CORE_VERSION,
    HotkeyError,
    parse_hotkey,
)

VERSION: Final = CORE_VERSION
# Version of the JSON control protocol between the CLI/tray and the daemon.
# The daemon reports it as "protocol" in status responses and state snapshots.
PROTOCOL_VERSION: Final = 3

DEFAULT_CONFIG_PATH: Final = Path("/etc/input-lock/input-lock.conf")
DEFAULT_SOCKET_PATH: Final = Path("/run/input-lock/control.sock")
DEFAULT_STATE_PATH: Final = Path("/run/input-lock/state.json")

VALID_MODES: Final = set(LOCK_MODES)
VALID_DEFAULT_MODES: Final = VALID_MODES


class ConfigError(ValueError):
    """Raised when the configuration is unsafe or malformed."""


class ProtocolError(RuntimeError):
    """Raised for an invalid or failed daemon protocol exchange."""


@dataclass(frozen=True, slots=True)
class Config:
    authorized_uid: int
    authorized_user: str
    hotkey: str = "CTRL+ALT+Z"
    emergency_hotkey: str = "CTRL+ALT+SHIFT+F12"
    default_mode: str = "all"
    auto_unlock_seconds: int = 0
    lock_touchscreen: bool = True
    show_notifications: bool = True
    play_sound: bool = False
    debounce_ms: int = 700
    unlock_on_suspend: bool = True
    unlock_on_session_switch: bool = True
    unlock_on_vt_hotkey: bool = True
    exclude_devices: tuple[str, ...] = ()
    exclude_vendor_product: tuple[str, ...] = ()
    socket_path: Path = DEFAULT_SOCKET_PATH
    state_path: Path = DEFAULT_STATE_PATH


_KNOWN_KEYS: Final = {
    "AUTHORIZED_UID",
    "AUTHORIZED_USER",
    "HOTKEY",
    "EMERGENCY_HOTKEY",
    "DEFAULT_MODE",
    "AUTO_UNLOCK_SECONDS",
    "LOCK_TOUCHSCREEN",
    "SHOW_NOTIFICATIONS",
    "PLAY_SOUND",
    "DEBOUNCE_MS",
    "UNLOCK_ON_SUSPEND",
    "UNLOCK_ON_SESSION_SWITCH",
    "UNLOCK_ON_VT_HOTKEY",
    "EXCLUDE_DEVICES",
    "EXCLUDE_VENDOR_PRODUCT",
    "SOCKET_PATH",
    "STATE_PATH",
}


def canonical_hotkey(text: str) -> str:
    """Validate a shortcut and return its canonical text, e.g. ``CTRL+ALT+Z``.

    A shortcut needs at least two modifiers (CTRL, ALT, SHIFT, SUPER) and
    exactly one key from A-Z, 0-9 or F1-F12. This is the single parser shared
    by the configuration, the CLI, the daemon IPC and the GNOME fallback.
    """

    try:
        return parse_hotkey(text, minimum_modifiers=2).text
    except HotkeyError as exc:
        raise ConfigError(f"invalid shortcut {text!r}: {exc}") from exc


def _parse_bool(key: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"yes", "true", "1", "on"}:
        return True
    if normalized in {"no", "false", "0", "off"}:
        return False
    raise ConfigError(f"{key} must be yes/no, true/false, 1/0, or on/off")


def _parse_int(key: str, value: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _parse_list(value: str) -> tuple[str, ...]:
    # Commas delimit entries; spaces inside an entry are preserved.
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_vendor_products(value: str) -> tuple[str, ...]:
    result: list[str] = []
    for item in _parse_list(value):
        normalized = item.lower()
        parts = normalized.split(":")
        if len(parts) != 2 or any(len(part) != 4 for part in parts):
            raise ConfigError(
                "EXCLUDE_VENDOR_PRODUCT entries must look like 046d:c31c"
            )
        try:
            int(parts[0], 16)
            int(parts[1], 16)
        except ValueError as exc:
            raise ConfigError(
                "EXCLUDE_VENDOR_PRODUCT contains a non-hexadecimal entry"
            ) from exc
        result.append(normalized)
    return tuple(result)


def _parse_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in _KNOWN_KEYS:
            raise ConfigError(f"{path}:{line_number}: unknown setting {key}")
        if key in values:
            raise ConfigError(f"{path}:{line_number}: duplicate setting {key}")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ConfigError(f"{path}:{line_number}: invalid control character")
        values[key] = value
    return values


def load_config(path: Path | str | None = None) -> Config:
    config_path = Path(path or os.environ.get("INPUT_LOCK_CONFIG", DEFAULT_CONFIG_PATH))
    values = _parse_file(config_path)

    if "AUTHORIZED_UID" not in values or "AUTHORIZED_USER" not in values:
        raise ConfigError("AUTHORIZED_UID and AUTHORIZED_USER are required")
    authorized_uid = _parse_int("AUTHORIZED_UID", values["AUTHORIZED_UID"], 0, 2**31 - 1)
    authorized_user = values["AUTHORIZED_USER"]
    if not authorized_user or any(ch.isspace() for ch in authorized_user):
        raise ConfigError("AUTHORIZED_USER must be a non-empty login name")

    default_mode = values.get("DEFAULT_MODE", "all").lower()
    if default_mode not in VALID_DEFAULT_MODES:
        raise ConfigError(f"DEFAULT_MODE must be one of: {', '.join(sorted(VALID_DEFAULT_MODES))}")

    socket_path = Path(values.get("SOCKET_PATH", str(DEFAULT_SOCKET_PATH)))
    state_path = Path(values.get("STATE_PATH", str(DEFAULT_STATE_PATH)))
    for key, candidate in (("SOCKET_PATH", socket_path), ("STATE_PATH", state_path)):
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError(f"{key} must be an absolute path without '..'")

    canonical: dict[str, str] = {}
    for key, default in (("HOTKEY", "CTRL+ALT+Z"), ("EMERGENCY_HOTKEY", "CTRL+ALT+SHIFT+F12")):
        try:
            canonical[key] = canonical_hotkey(values.get(key, default))
        except ConfigError as exc:
            raise ConfigError(
                f"{key}: {exc} (use at least two of CTRL/ALT/SHIFT/SUPER plus one key A-Z, 0-9 or F1-F12)"
            ) from exc
    hotkey = canonical["HOTKEY"]
    emergency_hotkey = canonical["EMERGENCY_HOTKEY"]
    if hotkey == emergency_hotkey:
        raise ConfigError("HOTKEY and EMERGENCY_HOTKEY must be different")

    return Config(
        authorized_uid=authorized_uid,
        authorized_user=authorized_user,
        hotkey=hotkey,
        emergency_hotkey=emergency_hotkey,
        default_mode=default_mode,
        auto_unlock_seconds=_parse_int(
            "AUTO_UNLOCK_SECONDS", values.get("AUTO_UNLOCK_SECONDS", "0"), 0, 86_400
        ),
        lock_touchscreen=_parse_bool(
            "LOCK_TOUCHSCREEN", values.get("LOCK_TOUCHSCREEN", "yes")
        ),
        show_notifications=_parse_bool(
            "SHOW_NOTIFICATIONS", values.get("SHOW_NOTIFICATIONS", "yes")
        ),
        play_sound=_parse_bool("PLAY_SOUND", values.get("PLAY_SOUND", "no")),
        debounce_ms=_parse_int("DEBOUNCE_MS", values.get("DEBOUNCE_MS", "700"), 250, 5000),
        unlock_on_suspend=_parse_bool(
            "UNLOCK_ON_SUSPEND", values.get("UNLOCK_ON_SUSPEND", "yes")
        ),
        unlock_on_session_switch=_parse_bool(
            "UNLOCK_ON_SESSION_SWITCH",
            values.get("UNLOCK_ON_SESSION_SWITCH", "yes"),
        ),
        unlock_on_vt_hotkey=_parse_bool(
            "UNLOCK_ON_VT_HOTKEY", values.get("UNLOCK_ON_VT_HOTKEY", "yes")
        ),
        exclude_devices=_parse_list(values.get("EXCLUDE_DEVICES", "")),
        exclude_vendor_product=_parse_vendor_products(
            values.get("EXCLUDE_VENDOR_PRODUCT", "")
        ),
        socket_path=socket_path,
        state_path=state_path,
    )


def send_request(
    request: dict[str, object],
    *,
    socket_path: Path | str = DEFAULT_SOCKET_PATH,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Send one bounded JSON request and return one bounded JSON response."""

    encoded = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError("request is too large")

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(encoded)
        chunks = bytearray()
        while b"\n" not in chunks:
            chunk = client.recv(4096)
            if not chunk:
                raise ProtocolError("daemon closed the connection without a response")
            chunks.extend(chunk)
            if len(chunks) > MAX_MESSAGE_BYTES:
                raise ProtocolError("daemon response is too large")
        raw = bytes(chunks).split(b"\n", 1)[0]
        response = json.loads(raw.decode("utf-8"))
        if not isinstance(response, dict):
            raise ProtocolError("daemon returned a non-object response")
        return response
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(str(exc)) from exc
    finally:
        client.close()


def response_ok(response: dict[str, object]) -> dict[str, object]:
    if response.get("ok") is not True:
        message = response.get("error", "unknown daemon error")
        raise ProtocolError(str(message))
    return response
