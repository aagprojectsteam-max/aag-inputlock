"""Final independent review, lid switch (P2-1).

python3-evdev 1.9.3 on the host has no InputDevice.switches(); the daemon reads
SW_LID with the EVIOCGSW ioctl. Tests use a fake descriptor (a pipe) and a fake
ioctl, never a real input device.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import os  # noqa: E402
import unittest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from test_daemon_logic import daemon_module  # noqa: E402  (installs evdev/pyudev stubs if needed)
from test_daemon_overhaul_20261022 import Clock, armed_daemon, base_daemon, drain  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
_ENVIRONMENT = mock.patch.dict(os.environ)


def setUpModule():
    # Never let a test talk to a real service manager's notify socket.
    _ENVIRONMENT.start()
    os.environ.pop("NOTIFY_SOCKET", None)


def tearDownModule():
    _ENVIRONMENT.stop()


class FakeIoctl:
    """fcntl.ioctl for EVIOCGSW on one fake descriptor."""

    def __init__(self, fd, bits=0x00, error=None):
        self.fd, self.bits, self.error, self.calls = fd, bits, error, []

    def __call__(self, fd, request, buffer, mutate=True):
        self.calls.append((fd, request, len(buffer), mutate))
        if fd != self.fd:
            raise OSError(9, "Bad file descriptor")
        if self.error is not None:
            raise self.error
        buffer[0] = self.bits
        return 0


class LidSwitchIoctlTests(unittest.TestCase):
    def setUp(self):
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, write_end)
        self.fd = read_end

    def test_request_code_is_the_kernel_eviocgsw(self):
        # _IOC(_IOC_READ=2, 'E'=0x45, 0x1b, len): dir<<30 | len<<16 | type<<8 | nr
        self.assertEqual(daemon_module.eviocgsw(3), 0x8003451B)
        self.assertEqual(daemon_module.eviocgsw(1), 0x8001451B)
        self.assertEqual(daemon_module.SW_BITMAP_BYTES, 3)    # SW_CNT = 0x11 bits

    def test_reads_only_the_sw_lid_bit(self):
        device = SimpleNamespace(fd=self.fd)                   # no .switches() at all
        for bits, closed in ((0x01, True), (0x00, False), (0x02, False), (0x03, True)):
            fake = FakeIoctl(self.fd, bits)
            with self.subTest(bits=bits), mock.patch.object(daemon_module.fcntl, "ioctl", fake):
                self.assertIs(daemon_module.read_lid_switch(device), closed)
            self.assertEqual(fake.calls, [(self.fd, daemon_module.eviocgsw(3), 3, True)])

    def test_unreadable_switch_fails_safe(self):
        daemon = base_daemon()
        lid = daemon_module.DeviceRecord("lid", SimpleNamespace(fd=self.fd), "lid", frozenset({"lid-switch"}),
                                         "", (), False, "", grabbed=False)
        daemon.devices["lid"] = lid
        with mock.patch.object(daemon_module.fcntl, "ioctl", FakeIoctl(self.fd, 0x01)):
            self.assertTrue(daemon._lid_switch_readable())
        with mock.patch.object(daemon_module.fcntl, "ioctl", FakeIoctl(self.fd, error=OSError(19, "gone"))):
            self.assertFalse(daemon._lid_switch_readable())
        lid.device = SimpleNamespace()                         # no descriptor: AttributeError
        self.assertFalse(daemon._lid_switch_readable())

    def resync(self, *, ioctl, device=None):
        """Feed SYN_DROPPED + SYN_REPORT to the lid-switch record."""
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        ecodes = daemon_module.ecodes
        events = [SimpleNamespace(type=ecodes.EV_SYN, code=ecodes.SYN_DROPPED, value=0),
                  SimpleNamespace(type=ecodes.EV_SYN, code=ecodes.SYN_REPORT, value=0)]
        record = daemon.devices["lid"]
        record.device = device or SimpleNamespace(fd=self.fd, read=lambda: iter(events), active_keys=lambda: [])
        if device is not None:
            device.read = lambda: iter(events)
            device.active_keys = lambda: []
        daemon._release_forwarded_for_record = mock.Mock()
        daemon._evaluate_hotkeys = mock.Mock()
        daemon._maybe_fire_pending_hotkey = mock.Mock()
        daemon._start_protection = mock.Mock()
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module.fcntl, "ioctl", ioctl):
            daemon._read_device("lid")
        return daemon

    def test_syn_dropped_resync_reads_the_lid_with_eviocgsw(self):
        daemon = self.resync(ioctl=FakeIoctl(self.fd, 0x01))
        self.assertTrue(daemon.lid_is_closed)
        self.assertTrue(daemon.lid_state_from_evdev)
        daemon._start_protection.assert_not_called()
        daemon = self.resync(ioctl=FakeIoctl(self.fd, 0x00))
        self.assertFalse(daemon.lid_is_closed)

    def test_syn_dropped_resync_failure_is_telemetry_loss_not_a_crash(self):
        daemon = self.resync(ioctl=FakeIoctl(self.fd, error=OSError(19, "No such device")))
        daemon._start_protection.assert_called_once_with("LID_TELEMETRY_LOST")
        # A device object without a descriptor (AttributeError) fails safe too.
        bare = SimpleNamespace()
        daemon = self.resync(ioctl=FakeIoctl(self.fd, 0x01), device=bare)
        daemon._start_protection.assert_called_once_with("LID_TELEMETRY_LOST")

    def test_deferred_lid_telemetry_loss_uses_the_ioctl(self):
        # S-R8b with a fake descriptor and ioctl (not a mocked .switches()).
        for readable in (False, True):
            with self.subTest(readable=readable):
                clock = Clock(100.0)
                daemon = armed_daemon(clock)
                daemon.lid_is_closed = True
                daemon.safety_sample.lid_closed = True
                daemon.devices["lid"].device = SimpleNamespace(fd=self.fd)
                fake = (FakeIoctl(self.fd, 0x01) if readable
                        else FakeIoctl(self.fd, error=OSError(5, "I/O error")))
                with mock.patch.object(daemon_module.time, "monotonic", clock), \
                        mock.patch.object(daemon_module, "sd_notify"), \
                        mock.patch.object(daemon_module.fcntl, "ioctl", fake):
                    drain(daemon, "prepare-sleep")
                    daemon._start_protection("LID_TELEMETRY_LOST")
                    self.assertEqual(daemon.deferred_protection, "LID_TELEMETRY_LOST")
                    self.assertIsNone(daemon.protective_result)
                    drain(daemon, "resume-sleep")
                    daemon._start_protection = mock.Mock()
                    daemon.clients[3].deadline = 1e9
                    clock.now = 100.0 + daemon_module.CONTROLLER_TIMEOUT + 1
                    daemon._handle_timers()
                self.assertEqual(fake.calls, [(self.fd, daemon_module.eviocgsw(3), 3, True)])
                if readable:
                    daemon._start_protection.assert_not_called()
                    self.assertTrue(daemon.ignore_lid_close)
                else:
                    daemon._start_protection.assert_called_once_with("LID_TELEMETRY_LOST")

    def test_the_daemon_never_calls_switches(self):
        source = (REPO / "src" / "input_lock_daemon.py").read_text()
        self.assertNotIn("device.switches(", source)

if __name__ == "__main__":
    unittest.main()
