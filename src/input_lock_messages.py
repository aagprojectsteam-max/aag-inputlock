#!/usr/bin/python3
"""Display-side wording for texts that come from the root daemon.

The daemon and the safety module run as system services without the user's
locale, so their IPC errors, readiness reasons and ``last_lid_disable`` keys
stay English on the wire (and in the journal). The tray and the CLI turn the
known ones into translatable text here; anything unknown is shown unchanged.
Every lookup uses the English text as the gettext msgid, so with the English
UI the result is exactly the daemon's text.

No GTK import: the CLI uses this module too.
"""

from __future__ import annotations

import re
from typing import Final

from input_lock_i18n import N_, _, ngettext, pgettext

# ---------------------------------------------------------------------------
# Ignore Lid Close loss reasons (review A8, interface 3)
# ---------------------------------------------------------------------------
SAFETY_REASON_TEXT: Final = {
    "THERMAL_EMERGENCY": N_("the computer reached a dangerous temperature"),
    "THERMAL_HIGH_RISK": N_("the computer stayed too hot for too long"),
    "THERMAL_WARNING": N_("the computer was running hot"),
    "BATTERY_CRITICAL": N_("the battery is critically low"),
    "TELEMETRY_LOST": N_("temperature or battery readings were unavailable for too long"),
    "CONTROLLER_LOST": N_("the tray lost contact with the service while the lid may have been closed"),
}
LID_DISABLE_REASON_TEXT: Final = {
    # Emitted by the daemon when it drops the controller (review U-R1): lease
    # expiry, and a connection the tray side closed or lost.
    "controller-lease-expired": N_(
        "The AAG - InputLock tray did not check in with the background service in time "
        "(for example, the tray stopped responding, or the computer was going to sleep "
        "or waking up)."),
    "controller-disconnected": N_(
        "The AAG - InputLock tray's connection to the background service was closed "
        "(the tray was stopped, restarted or crashed)."),
    "session-inactive": N_("Your desktop session stopped being active (user switch, logout or a session problem)."),
    "authorized-session-inactive": N_(
        "Your desktop session stopped being active (user switch, logout or a session problem)."),
    "user-session-ended": N_("The AAG - InputLock tray was stopped or your desktop session ended."),
    "daemon-shutdown": N_("The AAG - InputLock background service was stopped or restarted."),
    "daemon-unavailable": N_(
        "The connection to the AAG - InputLock background service was lost (the service stopped or crashed)."),
    "daemon-restarted": N_(
        "The AAG - InputLock background service restarted; Ignore Lid Close always starts off "
        "after a restart."),
    "ipc-emergency-unlock": N_(
        "Input was unlocked with an emergency unlock, which also turns Ignore Lid Close off."),
    "emergency-hotkey": N_("The emergency unlock shortcut was used, which also turns Ignore Lid Close off."),
    "gnome-emergency-hotkey": N_(
        "The emergency unlock shortcut was used, which also turns Ignore Lid Close off."),
    "tty-recovery-hotkey": N_(
        "A text-console recovery shortcut was used, which also turns Ignore Lid Close off."),
    "hotkey-change": N_("The lock shortcut was changed, which resets all locks."),
    "lock-kinds-change": N_("The devices to lock were changed, which resets all locks."),
    "allowed-keys-change": N_("The keys allowed while locked were changed, which resets all locks."),
    "configuration-reload": N_("The AAG - InputLock configuration was reloaded, which resets all locks."),
    "invalid-config-reload": N_("The AAG - InputLock configuration could not be reloaded, so all locks were reset."),
    "release-all": N_("All locks were released."),
    # Normalised keys published by the daemon in last_lid_disable.reason.
    "emergency-unlock": N_(
        "Input was unlocked with an emergency unlock, which also turns Ignore Lid Close off."),
    "settings-reset": N_("AAG - InputLock settings were changed or reloaded, which resets all locks."),
    "auto-unlock-timer": N_(
        "The auto-unlock safety timer released input, which also turns Ignore Lid Close off."),
    "safety-journal-failed": N_(
        "AAG - InputLock could not save its safety record, so it stopped ignoring the lid."),
    "power-saver-ineffective": N_("The power-saver cooling hold could not be kept."),
    "lease-unavailable": N_("GNOME idle protection or power-saver cooling stopped working."),
}


def _safety_cause(reason: str, state: dict[str, object]) -> str:
    upper = reason.upper()
    for part in (upper, *upper.replace(":", "=").split("=")[1:]):
        if part in SAFETY_REASON_TEXT:
            return part
    safety = state.get("safety")
    decision = safety.get("last_decision") if isinstance(safety, dict) else None
    if isinstance(decision, dict) and isinstance(decision.get("reason"), str):
        return decision["reason"].upper()
    result = state.get("protective_result")
    if isinstance(result, dict) and isinstance(result.get("reason"), str):
        return result["reason"].upper()
    return ""


def _english(message: str) -> str:
    return message


def _describe(reason: str, state: dict[str, object], detail: str, tr) -> str:
    reason = (reason or "").strip()
    lowered = reason.lower()
    known = LID_DISABLE_REASON_TEXT.get(lowered)
    if known is not None:
        text = tr(known)
    else:
        cause = _safety_cause(reason, state) if (
            lowered.startswith("safety") or reason.upper() in SAFETY_REASON_TEXT
            or ":" in reason or "=" in reason) else ""
        if cause in SAFETY_REASON_TEXT:
            text = tr(N_("Safety protection stepped in: {cause}.")).format(
                cause=tr(SAFETY_REASON_TEXT[cause]))
        elif lowered.startswith("safety"):
            text = (tr(N_("Safety protection stepped in ({cause}).")).format(
                cause=cause.lower().replace("_", " "))
                if cause else tr(N_("Safety protection stepped in.")))
        elif "thermal" in lowered or "temperature" in lowered or "heat" in lowered:
            text = tr(N_("Safety protection stepped in because of heat."))
        elif "battery" in lowered:
            text = tr(N_("Safety protection stepped in because of the battery level."))
        elif "telemetry" in lowered or "supervision" in lowered or "monitor" in lowered:
            text = tr(N_("Safety supervision could not keep monitoring the computer."))
        elif "sleep" in lowered or "suspend" in lowered or "resume" in lowered:
            text = tr(N_("It was lost around a sleep or wake-up of the computer."))
        elif "restart" in lowered or "shutdown" in lowered:
            text = tr(LID_DISABLE_REASON_TEXT["daemon-shutdown"])
        elif reason:
            text = tr(N_("The AAG - InputLock service turned it off (reason: {reason}).")).format(reason=reason)
        else:
            text = tr(N_("The AAG - InputLock service turned it off without giving a reason."))
    if detail:
        text = tr(N_("{text} Details: {detail}")).format(text=text.rstrip(), detail=detail)
    return text


def describe_lid_disable(reason: str, state: dict[str, object] | None = None,
                         detail: str = "") -> str:
    """Plain-words explanation for an Ignore Lid Close loss (review A8).

    Never raises: a broken translation falls back to the English text, and a
    malformed state to the bare reason (review U-R9).
    """
    state = state if isinstance(state, dict) else {}
    for tr in (_, _english):
        try:
            return _describe(str(reason or ""), state, str(detail or ""), tr)
        except Exception:  # noqa: BLE001 - display text must never break a caller
            continue
    return str(reason or "")


# ---------------------------------------------------------------------------
# Status codes from the safety snapshot
# ---------------------------------------------------------------------------
def thermal_text(value: object) -> str:
    """Display text for a measured thermal level (unknown codes unchanged)."""
    names = {
        "NORMAL": pgettext("thermal level", "NORMAL"),
        "WARNING": pgettext("thermal level", "WARNING"),
        "HIGH_RISK": pgettext("thermal level", "HIGH_RISK"),
        "EMERGENCY": pgettext("thermal level", "EMERGENCY"),
        "UNKNOWN": pgettext("thermal level", "UNKNOWN"),
    }
    return names.get(str(value), str(value))


def protection_text(value: object) -> str:
    """Display text for the lid thermal-protection state (ARMED/DISARMED)."""
    names = {
        "ARMED": pgettext("lid protection", "ARMED"),
        "DISARMED": pgettext("lid protection", "DISARMED"),
        "UNKNOWN": pgettext("lid protection", "UNKNOWN"),
    }
    return names.get(str(value), str(value))


# ---------------------------------------------------------------------------
# Daemon / safety texts shown to the user
# ---------------------------------------------------------------------------
# Exact texts. The msgid is the daemon's own English text.
KNOWN_DAEMON_TEXTS: Final = frozenset({
    # Lid-ignore arm refusals and readiness (daemon and SafetyPolicy.arm).
    N_("stale lid request; refresh status before enabling"),
    N_("lid-ignore requires an active graphical session and connected tray"),
    N_("no eligible lid switch input device was detected"),
    N_("Safety telemetry is not ready"),
    N_("Open the lid and restore fresh safety telemetry before arming"),
    N_("native suspend/fail-safe readiness unavailable"),
    N_("previous emergency suspend failed; operator recovery required (see 'input-lock doctor')"),
    N_("ordinary suspend unavailable, blocked, or already in progress"),
    N_("unexpected recursive failure handler"),
    N_("missing terminal failure recorder"),
    N_("AAG - InputLock watchdog/restart contract changed"),
    N_("unreadable failure latch"),
    N_("failure latch from a previous boot (informational; acknowledge to archive it)"),
    N_("failure latch predates a later successful suspend (informational; acknowledge to archive it)"),
    # Readiness reasons the CLI derives from the safety sample.
    N_("the daemon reports lid-ignore is not ready"),
    N_("safety telemetry not reported yet"),
    N_("native suspend readiness unavailable"),
    N_("power/logind supervision is not fresh"),
    N_("review 'journalctl -u input-lock-failsafe.service', then run: "
       "sudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure"),
    # Locking and settings.
    N_("device has a pressed key or button"),
    N_("locking requires the authorized active graphical session"),
    N_("primary hotkey must differ from the emergency hotkey"),
    N_("primary and emergency hotkeys must be different"),
    N_("the shortcut key cannot also be an allowed key"),
    N_("Copilot and F23 represent the same physical key"),
    N_("recovery shortcut keys (including F1-F12) cannot be added"),
    N_("allowed keys need /dev/uinput; run the AAG - InputLock updater and restart"),
    N_("mouse and touchpad must be configured together"),
    N_("mode must be all, all-input, keyboard, mouse, touchpad, or touchscreen"),
    N_("timeout must be between 0 and 86400 seconds"),
    N_("seconds must be between 60 and 86400"),
    # Connection and controller.
    N_("a tray controller is already connected"),
    N_("subscriber limit reached"),
    N_("the tray controller must run as the authorized user"),
    N_("the tray controller must run in the local graphical session"),
    N_("unauthorized UID"),
    N_("daemon closed the connection without a response"),
    N_("unknown daemon error"),
    N_("reload scheduled"),
    N_("shutdown scheduled"),
    # Shortcut validation (locklock_core.hotkeys).
    N_("shortcut contains a duplicate key"),
    N_("shortcut key must be A-Z, 0-9, or F1-F12"),
    N_("shortcut contains an unsupported modifier"),
    N_("shortcut requires at least 2 modifiers"),
    # GNOME fallback shortcuts (input_lock_gnome, used by set-hotkey).
    N_("shortcut must contain at least two modifiers and one key"),
    N_("GNOME custom-keybindings has an unexpected value"),
    N_("gsettings failed"),
    # Tray lease errors: logged in English, shown translated.
    N_("Power Profiles service is unavailable"),
    N_("Power Profiles service disappeared"),
    N_("GNOME SessionManager is unavailable"),
})



def _lock_cooldown(values: dict[str, str]) -> str:
    return ngettext("locking is paused for {seconds} s after an emergency unlock",
                    "locking is paused for {seconds} s after an emergency unlock",
                    int(values["seconds"])).format(**values)


# (pattern, msgid or formatter, groups that are translated recursively)
_PATTERNS: Final = (
    (re.compile(r"Lid-ignore unavailable: (?P<detail>.+)", re.S),
     N_("Lid-ignore unavailable: {detail}"), ("detail",)),
    (re.compile(r"Temperature is already too high to arm lid-ignore \((?P<sensor>.+) (?P<celsius>-?\d+)C\)"),
     N_("Temperature is already too high to arm lid-ignore ({sensor} {celsius}C)"), ()),
    (re.compile(r"locking is paused for (?P<seconds>\d+) s after an emergency unlock"),
     _lock_cooldown, ()),
    (re.compile(r"lock refused because a key or button remained pressed on: (?P<names>.+)", re.S),
     N_("lock refused because a key or button remained pressed on: {names}"), ()),
    (re.compile(r"cannot verify released-key state for (?P<path>\S+): (?P<error>.+)", re.S),
     N_("cannot verify released-key state for {path}: {error}"), ()),
    (re.compile(r"safety journal is not writable: (?P<error>.+)", re.S),
     N_("safety journal is not writable: {error}"), ()),
    (re.compile(r"cannot acquire logind lid inhibitor: (?P<error>.+)", re.S),
     N_("cannot acquire logind lid inhibitor: {error}"), ()),
    (re.compile(r"EVIOCGRAB failed; transition rolled back: (?P<error>.+)", re.S),
     N_("EVIOCGRAB failed; transition rolled back: {error}"), ()),
    (re.compile(r"cannot save user settings: (?P<error>.+)", re.S),
     N_("cannot save user settings: {error}"), ()),
    (re.compile(r"restart required for: (?P<settings>.+)"),
     N_("restart required for: {settings}"), ()),
    (re.compile(r"missing or incompatible (?P<unit>\S+)"),
     N_("missing or incompatible {unit}"), ()),
    (re.compile(r"AAG - InputLock root helper/timeout contract changed: (?P<unit>\S+)"),
     N_("AAG - InputLock root helper/timeout contract changed: {unit}"), ()),
    (re.compile(r"missing AAG - InputLock failure handler on (?P<unit>\S+)"),
     N_("missing AAG - InputLock failure handler on {unit}"), ()),
    (re.compile(r"native protection probe failed: (?P<error>.+)", re.S),
     N_("native protection probe failed: {error}"), ()),
    (re.compile(r"(?P<unit>\S+\.service) has drop-ins: (?P<paths>.+)"),
     N_("{unit} has drop-ins: {paths}"), ()),
    (re.compile(r"telemetry errors: (?P<errors>.+)", re.S),
     N_("telemetry errors: {errors}"), ()),
    (re.compile(r"invalid shortcut (?P<text>'.*'|\".*\"): (?P<error>.+)", re.S),
     N_("invalid shortcut {text}: {error}"), ("error",)),
    (re.compile(r"unsupported allowed key: (?P<key>.+)"),
     N_("unsupported allowed key: {key}"), ()),
    (re.compile(r"Power-saver hold did not become effective after one retry "
                r"\(active profile: (?P<profile>.*)\)"),
     N_("Power-saver hold did not become effective after one retry (active profile: {profile})"), ()),
    (re.compile(r"internal error: (?P<error>.+)", re.S),
     N_("internal error: {error}"), ()),
)


def _translate_whole(text: str) -> str | None:
    if text in KNOWN_DAEMON_TEXTS:
        return _(text)
    for pattern, wording, nested in _PATTERNS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        values = {name: (daemon_text(value) if name in nested else value)
                  for name, value in match.groupdict().items()}
        if callable(wording):
            return wording(values)
        return _(wording).format(**values)
    return None


def daemon_text(text: object) -> str:
    """Translate a daemon/safety message for display; unknown text is unchanged.

    Several reasons may be joined with "; " (readiness lists). Known runs of
    segments are translated, the rest is kept; the longest known run wins
    because some single messages contain "; " themselves.

    A translated segment that ends with a command on its own line (review
    U-R2) is followed by a line break instead of "; ", so nothing is appended
    to the command's line. English text has no line breaks and is returned
    exactly as the daemon sent it. Never raises: on any translation problem
    the daemon's English text is returned (review U-R9).
    """
    try:
        raw = str(text)
    except Exception:  # noqa: BLE001
        return ""
    try:
        return _daemon_text(raw)
    except Exception:  # noqa: BLE001 - display text must never break a caller
        return raw


def _daemon_text(raw: str) -> str:
    whole = _translate_whole(raw)
    if whole is not None:
        return whole
    parts = raw.split("; ")
    if len(parts) < 2:
        return raw
    result: list[str] = []
    index = 0
    while index < len(parts):
        for end in range(len(parts), index, -1):
            translated = _translate_whole("; ".join(parts[index:end]))
            if translated is not None:
                result.append(translated)
                index = end
                break
        else:
            result.append(parts[index])
            index += 1
    joined = result[0]
    for previous, part in zip(result, result[1:]):
        joined += ("\n" if "\n" in previous else "; ") + part
    return joined
