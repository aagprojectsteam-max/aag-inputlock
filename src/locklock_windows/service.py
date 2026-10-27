"""Windows Service entry point for privileged, recoverable power operations."""

from __future__ import annotations

import json
import os
import sys

from locklock_windows.ipc import NamedPipeServer
from locklock_windows.paths import POWER_JOURNAL_PATH, SERVICE_CONFIG_PATH, pipe_name_for_sid
from locklock_windows.power import PowerPolicyLease, PowerPolicyManager, WindowsPowerApi


def _authorized_sid() -> str:
    try:
        value = json.loads(SERVICE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read service configuration: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"authorized_sid"}:
        raise RuntimeError("service configuration has an unexpected schema")
    sid = value["authorized_sid"]
    if not isinstance(sid, str) or not sid.startswith("S-1-"):
        raise RuntimeError("service configuration has an invalid SID")
    return sid


if os.name == "nt":
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class LockLockPowerService(win32serviceutil.ServiceFramework):
        _svc_name_ = "AAGLockLock"
        _svc_display_name_ = "AAG - InputLock Power Helper"
        _svc_description_ = "Restores temporary AAG - InputLock lid power-policy changes."

        def __init__(self, arguments):
            super().__init__(arguments)
            self.stop_event = win32event.CreateEvent(None, True, False, None)
            self.manager = PowerPolicyManager(WindowsPowerApi(), POWER_JOURNAL_PATH)
            self.lease = PowerPolicyLease(self.manager)
            self.pipe: NamedPipeServer | None = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcShutdown(self):
            self.SvcStop()

        def _dispatch(self, request: dict[str, object]) -> dict[str, object]:
            command = request.get("cmd")
            try:
                if command == "status":
                    pass
                elif command == "apply-lid-do-nothing":
                    self.lease.apply(request.get("lease_seconds", 45))
                elif command == "restore-lid":
                    self.lease.restore()
                elif command == "shutdown":
                    win32event.SetEvent(self.stop_event)
                else:
                    raise ValueError("unknown service command")
                return {
                    "ok": True,
                    "state": {
                        "pending_power_restore": self.manager.pending_restore,
                        "lease_active": self.lease.deadline is not None,
                    },
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def SvcDoRun(self):
            # A prior crash must be repaired before new commands are accepted.
            self.manager.restore()
            sid = _authorized_sid()
            self.pipe = NamedPipeServer(
                pipe_name_for_sid(sid, service=True), sid, self._dispatch
            )
            self.pipe.start()
            servicemanager.LogInfoMsg("AAG - InputLock power helper started")
            try:
                while win32event.WaitForSingleObject(self.stop_event, 5000) == win32event.WAIT_TIMEOUT:
                    if self.pipe.error:
                        raise RuntimeError(self.pipe.error)
                    self.lease.tick()
            finally:
                self.pipe.close()
                self.lease.restore()


def main() -> int:
    if os.name != "nt":
        print("The AAG - InputLock Windows service can run only on Windows.", file=sys.stderr)
        return 2
    import servicemanager
    import win32serviceutil

    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(LockLockPowerService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(LockLockPowerService)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
