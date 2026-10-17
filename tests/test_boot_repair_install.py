"""Recovery installer regressions: every host read/write/process is mocked."""
import importlib.util
import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

import input_lock_safety as safety
import input_lock_lid_policy as policy

SPEC = importlib.util.spec_from_file_location('boot_installer', Path(__file__).resolve().parents[1] / 'tools/install_native_lid.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class RecoveryPreflightTests(unittest.TestCase):
    def preflight(self, *, recovery=True, changed=None, props=None, latch=True):
        def digest(path):
            if path == installer.LATCH:
                return installer.KNOWN_LATCH_SHA256 if latch else 'changed'
            return 'changed' if path.name == changed else installer.FAILED_BASELINE[path.name]

        def run(*args):
            unit = args[2]
            values = dict(DropInPaths='', FragmentPath='/usr/lib/systemd/system/' + unit,
                          ActiveState='failed' if unit == 'input-lock.service' else 'inactive',
                          Result='exit-code', MainPID='0', ControlPID='0', Job='',
                          ExecMainStartTimestampMonotonic='0',
                          ExecStartPre='{ argv[]=/usr/lib/input-lock/input-lock-power-helper disable ; code=exited ; status=1 }')
            values.update(props or {})
            return '\n'.join(k + '=' + v for k, v in values.items())

        with ExitStack() as stack:
            for target, name, options in (
                (Path, 'exists', dict(return_value=True)), (Path, 'is_file', dict(return_value=True)),
                (Path, 'is_symlink', dict(return_value=False)),
                (Path, 'stat', dict(return_value=SimpleNamespace(st_uid=0, st_mode=0o644))),
                (Path, 'read_text', dict(return_value='pass')),
                (installer, 'digest', dict(side_effect=digest)), (installer, 'run', dict(side_effect=run)),
                (safety, 'suspend_success_count', dict(return_value=1)),
            ):
                stack.enter_context(patch.object(target, name, **options))
            gate = stack.enter_context(patch.object(installer, 'failed_daemon_gate'))
            normal = stack.enter_context(patch.object(installer, 'open_unlocked'))
            installer.preflight(recovery=recovery)
            gate.assert_called_once()
            normal.assert_not_called()

    def test_exact_known_failure_can_use_independent_gate_without_daemon_state(self):
        self.preflight()

    def test_general_preflight_still_refuses_latch(self):
        with self.assertRaisesRegex(RuntimeError, 'unresolved emergency'):
            self.preflight(recovery=False)

    def test_unknown_latch_or_any_changed_installed_file_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'latch differs'):
            self.preflight(latch=False)
        for name in installer.FAILED_BASELINE:
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'baseline changed'):
                self.preflight(changed=name)

    def test_live_process_jobs_overrides_wrong_units_and_wrong_failure_refused(self):
        for props in ({'MainPID': '3'}, {'ControlPID': '4'}, {'Job': '9'},
                      {'ActiveState': 'active'}, {'DropInPaths': '/etc/override'},
                      {'FragmentPath': '/etc/other'}, {'Result': 'timeout'},
                      {'ExecMainStartTimestampMonotonic': '100'}, {'ExecStartPre': ''}):
            with self.subTest(props=props), self.assertRaises(RuntimeError):
                self.preflight(props=props)

    def test_source_pins_require_exact_set_and_content(self):
        hashes = dict.fromkeys(set(installer.FILES) | {'tools/install_native_lid.py'}, 'pin')
        with patch.object(Path, 'read_text', return_value=json.dumps(hashes)), patch.object(installer, 'digest', return_value='pin'):
            self.assertEqual(installer.verify_sources(Path('/fixture/pins')), hashes)
        with patch.object(Path, 'read_text', return_value=json.dumps(hashes)), patch.object(installer, 'digest', return_value='changed'):
            with self.assertRaisesRegex(RuntimeError, 'pin mismatch'):
                installer.verify_sources(Path('/fixture/pins'))
        with patch.object(Path, 'read_text', return_value='{}'):
            with self.assertRaisesRegex(RuntimeError, 'unexpected source'):
                installer.verify_sources(Path('/fixture/pins'))

    def test_install_requires_explicit_historical_failure_acknowledgement(self):
        with (patch.object(sys, 'argv', ['installer', '--install', '--recover-known-boot-failure', '--source-manifest', '/fixture/pins']),
              patch.object(installer, 'verify_sources'), patch.object(installer, 'snapshot') as snapshot,
              patch.object(installer, 'preflight') as preflight):
            with self.assertRaisesRegex(RuntimeError, 'acknowledge-failed-protection'):
                installer.main()
        preflight.assert_not_called()
        snapshot.assert_not_called()


class IndependentGateTests(unittest.TestCase):
    def gate(self, *, artifact=False, normal=True, pending=False, session='s1', active='yes'):
        with (patch.object(installer, 'open_telemetry'), patch.object(installer, 'native_available'),
              patch.object(installer.os.path, 'lexists', return_value=artifact),
              patch.object(policy.UPowerPolicyController, 'status', return_value=dict(normal_os_policy=normal, recovery_pending=pending)),
              patch.dict(installer.os.environ, {'SUDO_USER': 'fixture'}),
              patch.object(installer, 'run', side_effect=[session, f'Name=fixture\nActive={active}\nRemote=no\nClass=user\nType=wayland'])):
            installer.failed_daemon_gate()

    def test_verified_open_idle_session_passes(self):
        self.gate()

    def test_runtime_artifacts_policy_failure_or_session_loss_refuse(self):
        for kwargs in ({'artifact': True}, {'normal': False}, {'pending': True}, {'session': ''}, {'active': 'no'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                self.gate(**kwargs)

    def test_closed_hot_or_missing_telemetry_refuses(self):
        sample = safety.Sample(100, False, [safety.Sensor('cpu', 'cpu', 40, 70, 80, 90),
                                          safety.Sensor('chassis', 'chassis', 40, 70, 80, 90)],
                               ac=True, battery=80, discharging=False)
        with patch.object(safety.Telemetry, 'read', return_value=sample), patch.object(installer.time, 'monotonic', return_value=100):
            installer.open_telemetry()
            for key, value in (('lid_closed', True), ('at', 0), ('errors', ['missing']), ('battery', None)):
                old = getattr(sample, key)
                setattr(sample, key, value)
                with self.assertRaises(RuntimeError):
                    installer.open_telemetry()
                setattr(sample, key, old)
            sample.sensors[0] = safety.Sensor('cpu', 'cpu', 85, 70, 80, 90)
            with self.assertRaisesRegex(RuntimeError, 'temperature'):
                installer.open_telemetry()

    def test_logind_live_api_is_introspected_without_power_calls(self):
        for xml, ok in (("<node><interface name='org.freedesktop.login1.Manager'><method name='SuspendWithFlags'><arg type='t' direction='in'/></method></interface></node>", True), ('<node/>', False)):
            bus, interface = Mock(), Mock()
            interface.Introspect.return_value = xml
            dbus = SimpleNamespace(SystemBus=Mock(return_value=bus), Interface=Mock(return_value=interface))
            with patch.dict(sys.modules, dbus=dbus), patch.object(safety, 'native_status', return_value=dict(supported=True, blocked=False, preparing=False, inputlock_holds_lid=False)):
                if ok:
                    installer.native_available()
                else:
                    with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                        installer.native_available()
            self.assertEqual(interface.method_calls, [('Introspect', (), {'timeout': 5})])
            bus.close.assert_called_once()


class EvidenceAndHealthTests(unittest.TestCase):
    def test_acknowledgement_preserves_original_only_after_verified_backup(self):
        for pin, ok in ((installer.KNOWN_LATCH_SHA256, True), ('changed', False)):
            with (patch.object(Path, 'is_symlink', return_value=False),
                  patch.object(installer, 'digest', return_value=pin),
                  patch.object(installer.os.path, 'lexists', return_value=False),
                  patch.object(Path, 'rename') as rename, patch.object(Path, 'unlink') as unlink,
                  patch.object(installer.os, 'sync')):
                if ok:
                    installer.acknowledge_latch(Path('/fixture/backup'))
                    rename.assert_called_once()
                else:
                    with self.assertRaises(RuntimeError):
                        installer.acknowledge_latch(Path('/fixture/backup'))
                    rename.assert_not_called()
                unlink.assert_not_called()

    def test_rollback_restores_old_latch_without_overwriting_new_failure(self):
        for present in (False, True):
            with (patch.object(Path, 'exists', return_value=True),
                  patch.object(installer.os.path, 'lexists', return_value=present),
                  patch.object(installer, 'digest', return_value=installer.KNOWN_LATCH_SHA256),
                  patch.object(installer.shutil, 'copy2') as copy, patch.object(installer.os, 'chown'),
                  patch.object(installer.os, 'sync')):
                installer.restore_latch(Path('/fixture/backup'))
                self.assertEqual(copy.call_count, 0 if present else 1)

    def test_missing_partial_and_not_ready_state_are_retried_without_real_wait(self):
        ready = json.dumps({'safety': {'sample': {'stack_ready': True}}})
        with (patch.object(Path, 'read_text', side_effect=[FileNotFoundError(), '{', '[]', '{}', ready]),
              patch.object(installer.time, 'sleep'), patch.object(installer, 'open_unlocked') as gate):
            installer.wait_ready()
        gate.assert_called_once()

    def test_readiness_timeout_is_failure_without_real_wait(self):
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError()), patch.object(installer.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'did not report'):
                installer.wait_ready()
        self.assertEqual(sleep.call_count, 40)


class InstallTransactionTests(unittest.TestCase):
    def test_recovery_install_success_and_failures_rollback_without_power_actions(self):
        import tempfile
        data = b'fixture source'
        pin = installer.hashlib.sha256(data).hexdigest()
        for failing in (None, 'acknowledge_latch', 'wait_ready', 'verify_sources', 'restore'):
            with self.subTest(failing=failing), ExitStack() as stack:
                stack.enter_context(patch.object(sys, 'argv', ['installer', '--install', '--recover-known-boot-failure',
                    '--acknowledge-failed-protection', '--source-manifest', '/fixture/pins']))
                stack.enter_context(patch.dict(installer.os.environ, {'SUDO_USER': 'fixture'}))
                stack.enter_context(patch.object(installer.os, 'geteuid', return_value=0))
                stack.enter_context(patch.object(installer, 'snapshot', return_value=Path('/fixture/backup')))
                stack.enter_context(patch.object(installer, 'preflight'))
                functions = {}
                for name in ('verify_sources', 'acknowledge_latch', 'wait_ready', 'restore'):
                    functions[name] = stack.enter_context(patch.object(installer, name))
                if failing == 'verify_sources':
                    functions[failing].side_effect = [None, RuntimeError('changed source')]
                elif failing:
                    functions[failing].side_effect = RuntimeError(failing)
                    if failing == 'restore':
                        functions['wait_ready'].side_effect = RuntimeError('health failed')
                run = stack.enter_context(patch.object(installer, 'run'))
                stack.enter_context(patch.object(safety, 'protection_errors', return_value=[]))
                stack.enter_context(patch.object(installer.pwd, 'getpwnam', return_value=SimpleNamespace(pw_name='fixture', pw_uid=123)))
                stack.enter_context(patch.object(Path, 'read_text', return_value=json.dumps(dict.fromkeys(installer.FILES, pin))))
                stack.enter_context(patch.object(Path, 'read_bytes', return_value=data))
                stack.enter_context(patch.object(installer, 'digest', return_value=pin))
                stack.enter_context(patch.object(tempfile, 'mkstemp', return_value=(999, '/fixture/staged')))
                stack.enter_context(patch.object(installer.os, 'fdopen', mock_open()))
                for name in ('fsync', 'fchown', 'fchmod', 'replace', 'sync'):
                    stack.enter_context(patch.object(installer.os, name))
                stack.enter_context(patch.object(Path, 'unlink'))
                stack.enter_context(patch('builtins.print'))
                if failing:
                    with self.assertRaises(RuntimeError) as error:
                        installer.main()
                    if failing == 'restore':
                        self.assertIn('health failed; rollback incomplete: restore', str(error.exception))
                    functions['restore'].assert_called_once_with(Path('/fixture/backup'))
                else:
                    installer.main()
                    functions['restore'].assert_not_called()
                    functions['acknowledge_latch'].assert_called_once_with(Path('/fixture/backup'))
                    functions['wait_ready'].assert_called_once()
                commands = [call.args for call in run.call_args_list]
                self.assertEqual(commands[0], ('/usr/bin/systemctl', 'stop', 'input-lock.service'))
                self.assertFalse(any(word in {'suspend', 'hibernate', 'reboot', 'poweroff'} for cmd in commands for word in cmd))
                if failing == 'verify_sources':
                    functions['acknowledge_latch'].assert_not_called()
