"""Windows command-line client for diagnostics and recovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
import platform

from locklock_windows.ipc import current_user_sid, send_request
from locklock_windows.paths import pipe_name_for_sid


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="locklock")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("toggle")
    sub.add_parser("emergency-unlock")
    lock = sub.add_parser("lock")
    lock.add_argument("mode", choices=("keyboard", "mouse", "touchpad", "touchscreen", "all", "all-input"))
    lock.add_argument("--timeout", type=int, default=0)
    unlock = sub.add_parser("unlock")
    unlock.add_argument("mode", choices=("keyboard", "mouse", "touchpad", "touchscreen", "all", "all-input"), nargs="?", default="all-input")
    sub.add_parser("quit")
    sub.add_parser("doctor")
    return result


def main(arguments: list[str] | None = None) -> int:
    if os.name != "nt":
        print("This client is for Windows.", file=sys.stderr)
        return 2
    args = parser().parse_args(arguments)
    if args.command == "doctor":
        return doctor()
    commands = {
        "status": {"cmd": "status"},
        "toggle": {"cmd": "toggle-default"},
        "emergency-unlock": {"cmd": "emergency-unlock"},
        "quit": {"cmd": "quit"},
    }
    request = commands.get(args.command)
    if request is None:
        request = {"cmd": args.command, "mode": args.mode}
        if args.command == "lock":
            request["timeout"] = args.timeout
    response = send_request(pipe_name_for_sid(current_user_sid()), request)
    print(json.dumps(response, indent=2))
    return 0 if response.get("ok") is True else 1


def doctor() -> int:
    failures = 0
    print("AAG - InputLock Doctor — Windows")
    print("-----------------------------")
    release = platform.platform()
    supported = platform.system() == "Windows" and platform.machine().endswith("64")
    print(f"[{'PASS' if supported else 'FAIL'}] Operating system: {release}")
    failures += 0 if supported else 1
    try:
        response = send_request(
            pipe_name_for_sid(current_user_sid()), {"cmd": "status"}, timeout_ms=2000
        )
        if response.get("ok") is not True:
            raise RuntimeError(str(response.get("error", "agent rejected status")))
        state = response.get("state", {})
        print("[PASS] Interactive agent and authenticated named pipe")
        capabilities = state.get("capabilities", {}) if isinstance(state, dict) else {}
        for name in ("keyboard", "mouse", "touchpad", "touchscreen", "cursor_hiding", "lid_control"):
            print(f"[INFO] {name}: {capabilities.get(name, 'unknown')}")
    except Exception as exc:
        failures += 1
        print(f"[FAIL] Interactive agent: {exc}")
    print("[INFO] Secure Attention Ctrl+Alt+Del is intentionally never blocked")
    print("[INFO] Run test-hardware.ps1 -Armed before accepting experimental features")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
