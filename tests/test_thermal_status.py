"""Exercise CLI, tray and durable status consumers without a display or daemon."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_daemon_logic import daemon_module
from input_lock_safety import SafetyPolicy, Sensor
from test_safety import sample
from input_lock_cli import print_status
from input_lock_notifier import TrayNotifier


def hot_policy():
    policy=SafetyPolicy(lambda:100.)
    policy.update(sample(sensors=[Sensor('cpu','cpu',95.05,85,95,100),sample().sensors[1]]))
    return policy


class ThermalConsumerTests(unittest.TestCase):
    def test_cli_reports_measurement_and_disarmed_protection_separately(self):
        output=io.StringIO()
        with redirect_stdout(output):
            print_status({'safety':hot_policy().snapshot()})
        self.assertIn('Measured thermal: HIGH_RISK',output.getvalue())
        self.assertIn('Lid protection:   DISARMED',output.getvalue())

    def test_tray_does_not_hide_hot_sample_behind_normal_os_behavior(self):
        tray=TrayNotifier.__new__(TrayNotifier)
        tray.current_state={'safety':hot_policy().snapshot()}
        for name in ('safety_item','status_item','toggle_item','indicator','shortcut_item','lid_item'):
            setattr(tray,name,Mock())
        tray._update_menu(frozenset(),'CTRL+ALT+Z')
        label=tray.safety_item.set_label.call_args.args[0]
        self.assertIn('Temperature: HIGH_RISK',label)
        self.assertIn('Protection: DISARMED',label)

    def test_breadcrumb_preserves_hot_raw_measurement_and_protection_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'safety.json'
            daemon=SimpleNamespace(safety=hot_policy(),ignore_lid_close=False,
                _saved_lid_ignore_close=False,protective_result=None,
                safety_signature=None,safety_last_write=0,boot_id='fixture',
                daemon_started_at=1,lid_generation=2,clean_shutdown=False,safety_previous_run=None,
                lid_inhibitor_fd=None,sleep_inhibitor_fd=None,devices={})
            with patch.object(daemon_module,'BREADCRUMB',path):
                daemon_module.InputLockDaemon._write_safety(daemon,force=True)
            value=json.loads(path.read_text())
            self.assertEqual(value['safety']['thermal'],'HIGH_RISK')
            self.assertFalse(value['safety']['protection_armed'])
            self.assertEqual(value['safety']['sample']['sensors'][0]['celsius'],95.05)
            self.assertEqual(daemon.thermal_measurement_signature,('HIGH_RISK',True,True,False))
