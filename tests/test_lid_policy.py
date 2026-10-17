#!/usr/bin/python3
"""Tests for the reversible GNOME/UPower lid no-suspend policy."""

from __future__ import annotations

import tempfile
import json
from unittest.mock import Mock, patch
import unittest
from pathlib import Path

from input_lock_lid_policy import (
    GNOME_POLICY_VALUES,
    LidNoSuspendPolicy,
    PolicyError,
    UPOWER_DROPIN_CONTENT,
    UPowerPolicyController,
)


class MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {
            "lid-close-ac-action": "suspend",
            "lid-close-battery-action": "suspend",
            "sleep-inactive-ac-type": "nothing",
            "sleep-inactive-battery-type": "suspend",
            "sleep-inactive-ac-timeout": 3600,
            "sleep-inactive-battery-timeout": 900,
            "lid-close-suspend-with-external-monitor": False,
        }
        self.sync_count = 0

    def get(self, key: str) -> object:
        return self.values[key]

    def set(self, key: str, value: object) -> None:
        self.values[key] = value

    def sync(self) -> None:
        self.sync_count += 1


class LidNoSuspendPolicyTests(unittest.TestCase):
    def test_verified_noop_allows_independent_user_restore_without_claiming_runtime_health(self):
        policy = LidNoSuspendPolicy(MemorySettings(), helper=Mock(return_value={
            'restoration_required': False, 'normal_os_policy': None}))
        with patch.object(policy, 'restore_user') as restore, patch.object(policy, 'status', return_value={}):
            policy.disable()
        restore.assert_called_once_with(remove_backup=True)

    def test_legacy_user_restoration_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MemorySettings()
            original = dict(settings.values)
            backup = Path(directory) / "policy.json"
            backup.write_text(json.dumps({"version":1,"values": original}))
            settings.values.update(GNOME_POLICY_VALUES)
            helper = Mock(return_value={"normal_os_policy": True})
            policy = LidNoSuspendPolicy(settings, backup_path=backup, helper=helper)
            policy.disable()
            self.assertEqual(settings.values, original)
            self.assertFalse(backup.exists())
            self.assertNotIn("enable", repr(helper.call_args_list))

    def test_root_failure_retains_journal_but_still_restores_user(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MemorySettings()
            original = dict(settings.values)
            backup = Path(directory)/"policy.json"
            backup.write_text(json.dumps({"version":1,"values":original}))
            settings.values.update(GNOME_POLICY_VALUES)
            policy = LidNoSuspendPolicy(settings, backup_path=backup, helper=Mock(side_effect=PolicyError("failed")))
            with self.assertRaises(PolicyError):
                policy.disable()
            self.assertEqual(settings.values, original)
            self.assertTrue(backup.exists())

    def test_enable_is_rejected_without_changes(self):
        settings = MemorySettings()
        helper = Mock()
        with self.assertRaises(PolicyError):
            LidNoSuspendPolicy(settings, helper=helper).enable()
        helper.assert_not_called()
        self.assertEqual(settings.sync_count, 0)


class UPowerPolicyControllerTests(unittest.TestCase):
    def test_no_owned_state_does_not_query_or_restart_unready_upower(self):
        controller = UPowerPolicyController(restart=Mock(), service_active=Mock(side_effect=OSError('not ready')),
                                           runtime_action=Mock())
        with patch('input_lock_lid_policy.os.path.lexists', return_value=False):
            result = controller.disable()
        self.assertIs(result['restoration_required'], False)
        self.assertIsNone(result['normal_os_policy'])  # No invented runtime success.
        controller._service_active.assert_not_called()
        controller._runtime_action.assert_not_called()
        controller._restart.assert_not_called()

    def test_pending_only_unready_upower_fails_and_retains_marker(self):
        controller = UPowerPolicyController(restart=Mock(), service_active=lambda: False)
        with (patch('input_lock_lid_policy.os.path.lexists', side_effect=lambda p: p.name.startswith('.')),
              patch.object(Path, 'exists', autospec=True, side_effect=lambda p: p.name.startswith('.')),
              patch.object(Path, 'is_symlink', return_value=False),
              patch.object(controller, '_effective', return_value={}), patch.object(Path, 'unlink') as unlink):
            with self.assertRaisesRegex(PolicyError, 'not verified'):
                controller.disable()
        controller._restart.assert_called_once()
        unlink.assert_not_called()

    def test_pending_symlink_never_becomes_a_noop(self):
        controller = UPowerPolicyController(restart=Mock())
        with (patch('input_lock_lid_policy.os.path.lexists', return_value=True),
              patch.object(Path, 'is_symlink', return_value=True)):
            with self.assertRaisesRegex(PolicyError, 'symlink'):
                controller.disable()
        controller._restart.assert_not_called()

    def test_owned_override_unready_runtime_retains_new_recovery_marker(self):
        controller = UPowerPolicyController(restart=Mock(), service_active=lambda: False)
        with (patch('input_lock_lid_policy.os.path.lexists', return_value=True),
              patch.object(Path, 'is_symlink', return_value=False),
              patch.object(Path, 'exists', return_value=True),
              patch.object(Path, 'read_text', return_value=UPOWER_DROPIN_CONTENT),
              patch('input_lock_lid_policy._atomic_json') as journal,
              patch('input_lock_lid_policy._sync_directory'),
              patch.object(controller, '_effective', return_value={}),
              patch.object(Path, 'unlink', autospec=True) as unlink):
            with self.assertRaisesRegex(PolicyError, 'not verified'):
                controller.disable()
        journal.assert_called_once()
        self.assertEqual(journal.call_args.args[0].name, '.input-lock-restore-pending.json')
        unlink.assert_called_once_with(controller.dropin_path)
        controller._restart.assert_called_once()

    def test_pending_runtime_bus_failure_retains_marker(self):
        controller = UPowerPolicyController(restart=Mock(), service_active=lambda: True,
                                           runtime_action=Mock(side_effect=OSError('bus unavailable')))
        with (patch('input_lock_lid_policy.os.path.lexists', return_value=True),
              patch.object(Path, 'is_symlink', return_value=False),
              patch.object(Path, 'exists', autospec=True, side_effect=lambda p: p.name.startswith('.')),
              patch.object(controller, '_effective', return_value={}), patch.object(Path, 'unlink') as unlink):
            with self.assertRaisesRegex(OSError, 'bus unavailable'):
                controller.disable()
        unlink.assert_not_called()

    def test_runtime_failure_retains_durable_retry_after_dropin_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/"UPower.conf"
            config.write_text("[UPower]\nCriticalPowerAction=PowerOff\n")
            restart = Mock()
            runtime = Mock(return_value="Ignore")
            controller = UPowerPolicyController(config_path=config, dropin_dir=root, restart=restart,
                                               service_active=lambda:True, runtime_action=runtime)
            controller.dropin_path.write_text(UPOWER_DROPIN_CONTENT)
            with self.assertRaises(PolicyError):
                controller.disable()
            self.assertFalse(controller.dropin_path.exists())
            self.assertTrue((root/".input-lock-restore-pending.json").exists())
            runtime.return_value = "PowerOff"
            self.assertTrue(controller.disable()["normal_os_policy"])
            self.assertEqual(restart.call_count, 2)
            self.assertFalse((root/".input-lock-restore-pending.json").exists())
            controller.disable()
            self.assertEqual(restart.call_count, 2)

    def test_unmanaged_dropin_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = UPowerPolicyController(config_path=Path(directory)/"base", dropin_dir=Path(directory))
            controller.dropin_path.write_text("# administrator custom content")
            with self.assertRaises(PolicyError):
                controller.disable()
            self.assertTrue(controller.dropin_path.exists())


if __name__ == "__main__":
    unittest.main()
