"""No files, buses, devices or power operations: all effects are injected."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import input_lock_safety as safety


def native(**changes):
    return dict(supported=True, blocked=False, preparing=False, inputlock_holds_lid=False, **changes)


def properties(unit):
    commands = {
        'input-lock.service': '/usr/lib/input-lock/input-lock-daemon',
        safety.PROTECT_UNIT: '/usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py protect',
        safety.FAILSAFE_UNIT: '/usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py recover',
        'systemd-suspend.service': '/usr/lib/systemd/systemd-sleep suspend',
    }
    return dict(LoadState='loaded', ActiveState='inactive', ExecStart=commands[unit], InvocationID='invocation',
                OnFailure=safety.FAILSAFE_UNIT if unit in {'input-lock.service', safety.PROTECT_UNIT} else '',
                Restart='no', WatchdogUSec='1min', TimeoutStopUSec='5s', KillMode='control-group',
                User='', Type='notify' if unit == 'input-lock.service' else 'oneshot',
                TimeoutStartUSec='5min 30s',
                ExecStopPost='/usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py record-failure'
                if unit == safety.FAILSAFE_UNIT else '')


class ReadinessTests(unittest.TestCase):
    def probe(self, state=None, props=properties, latch=False):
        with patch.object(Path, 'exists', return_value=latch), patch.object(safety, 'unit_properties', side_effect=lambda _run, unit: props(unit)):
            return safety.protection_errors(Mock(), status=lambda: state or native(), counter=lambda: 1)

    def test_no_aag_service_or_api_is_required(self):
        self.assertEqual(self.probe(), [])

    def test_native_refusal_and_inhibitors_fail_closed(self):
        for key in ('supported', 'blocked', 'preparing'):
            state = native()
            state[key] = key != 'supported'
            self.assertTrue(self.probe(state))

    def test_missing_fallback_wrong_command_or_restart_prevents_arm(self):
        for unit, key, value in ((safety.FAILSAFE_UNIT, 'LoadState', 'not-found'),
                                 (safety.PROTECT_UNIT, 'OnFailure', 'retired.service'),
                                 (safety.FAILSAFE_UNIT, 'ExecStart', '/bin/true'),
                                 (safety.FAILSAFE_UNIT, 'ExecStopPost', ''),
                                 (safety.FAILSAFE_UNIT, 'User', 'nobody'),
                                 (safety.PROTECT_UNIT, 'TimeoutStartUSec', 'infinity'),
                                 ('input-lock.service', 'Restart', 'on-failure'),
                                 ('input-lock.service', 'KillMode', 'process'),
                                 ('input-lock.service', 'WatchdogUSec', '0')):
            def changed(name):
                result = properties(name)
                if name == unit:
                    result[key] = value
                return result
            self.assertTrue(self.probe(props=changed))

    def test_failure_latch_blocks_subsequent_arm(self):
        self.assertTrue(self.probe(latch=True))

    def test_failed_bus_or_counter_is_not_treated_as_ready(self):
        with patch.object(Path, 'exists', return_value=False):
            self.assertTrue(safety.protection_errors(status=Mock(side_effect=RuntimeError('no bus'))))
            self.assertTrue(safety.protection_errors(status=native, counter=Mock(side_effect=OSError('no counter'))))


class ProtectionTests(unittest.TestCase):
    def request(self, *, recovery=False, lid=True, ready=True, state_changes=None,
                native_changes=None, dispatch=0, observe_error=None, stop=0, active='inactive',
                changed_generation=False, receipt=None):
        state = dict(boot_id='boot', invocation_id='invocation', generation=9, updated_at=100., effective=False,
                     restrictions_released=True, safety={'state': 'PROTECTIVE_ACTION_PENDING'})
        state.update(state_changes or {})
        reads = [0]
        def read(path, *args, **kwargs):
            if path.name == 'boot_id':
                return 'boot'
            if path.name == 'safety-action-result.json':
                if receipt is None:
                    raise FileNotFoundError()
                return json.dumps(receipt)
            reads[0] += 1
            return json.dumps({**state, 'generation': 10} if changed_generation and reads[0] > 1 else state)
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == 'stop':
                return SimpleNamespace(returncode=stop)
            if isinstance(dispatch, Exception):
                raise dispatch
            return SimpleNamespace(returncode=dispatch)
        def request_suspend():
            calls.append(['logind', 'SuspendWithFlags', 0])
            if isinstance(dispatch, Exception):
                raise dispatch
            if dispatch:
                raise RuntimeError('logind refused request')
        status = native()
        status.update(native_changes or {})
        telemetry = Mock()
        telemetry.read.return_value = SimpleNamespace(lid_closed=lid)
        observe = Mock(side_effect=observe_error)
        def props(_run, unit):
            result = properties(unit)
            if unit == 'systemd-suspend.service':
                result['ActiveState'] = active
            return result
        # S-R6: root reads the daemon journal and receipt through one bounded
        # O_NOFOLLOW descriptor; the fixture serves the same content there.
        with (patch.object(Path, 'read_text', read), patch.object(safety.time, 'time', return_value=100.),
              patch.object(safety, '_read_bounded_json', side_effect=lambda p, *a, **k: json.loads(read(p))),
              patch.object(safety, 'atomic_json') as records, patch.object(safety.os, 'chmod'),
              patch.object(safety, 'unit_properties', side_effect=props)):
            result = safety.protect_main(run, lambda: ready, telemetry, Path('/fixture/request.json'),
                                        observe, lambda: 1, recovery=recovery, status=lambda: status,
                                        latch=Path('/fixture/failure.json'), dispatch=request_suspend)
        return result, calls, records.call_args_list, observe

    def test_only_ordinary_suspend_after_release(self):
        result, calls, records, observe = self.request()
        self.assertEqual(result, 0)
        self.assertEqual(calls, [['logind', 'SuspendWithFlags', 0]])
        self.assertEqual(records[-1].args[1]['result'], 'suspend-transition-confirmed')
        observe.assert_called_once()

    def test_unreleased_or_stale_request_never_dispatches(self):
        for changes in ({'effective': True}, {'restrictions_released': False}, {'updated_at': 0}, {'boot_id': 'old'}):
            result, calls, *_ = self.request(state_changes=changes)
            self.assertEqual(calls, [])

    def test_open_lid_and_cancelled_generation_do_not_suspend(self):
        for kwargs in ({'lid': False}, {'changed_generation': True}):
            self.assertEqual(self.request(**kwargs)[1], [])

    def test_inhibitor_and_native_pending_transition_never_bypassed(self):
        for kwargs in ({'ready': False}, {'native_changes': {'inputlock_holds_lid': True}},
                       {'native_changes': {'blocked': True}}):
            result, calls, records, _ = self.request(**kwargs)
            self.assertEqual(result, 1)
            self.assertEqual(calls, [])
            self.assertEqual(records[-1].args[1]['result'], 'recovery-required')
            self.assertFalse(any(c.args[0].name == 'failure.json' for c in records))

    def test_os_sleep_already_in_progress_is_deferred_not_failed(self):
        # A9/A10: never a duplicate request, never a failsafe chain or latch.
        for recovery in (False, True):
            for kwargs in ({'native_changes': {'preparing': True}}, {'active': 'activating'}):
                if recovery:
                    kwargs = dict(kwargs, state_changes={'effective': True, 'restrictions_released': False})
                result, calls, records, _ = self.request(recovery=recovery, **kwargs)
                self.assertEqual(result, 0)
                self.assertEqual([c for c in calls if c[0] == 'logind'], [])
                self.assertEqual(records[-1].args[1]['result'], 'deferred-os-sleep-in-progress')
                self.assertEqual(records[-1].args[1]['transition_status'], 'DEFERRED')
                self.assertFalse(any(c.args[0].name == 'failure.json' for c in records))

    def test_failure_and_timeout_request_systemd_recovery_without_recursive_dispatch(self):
        for kwargs in ({'dispatch': 1}, {'dispatch': subprocess.TimeoutExpired('fixture', 10)},
                       {'observe_error': TimeoutError('unconfirmed')}):
            result, calls, records, _ = self.request(**kwargs)
            self.assertEqual(result, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(records[-1].args[1]['result'], 'recovery-required')

    def test_recovery_stops_daemon_before_one_final_native_attempt(self):
        result, calls, *_ = self.request(recovery=True, state_changes={'effective': True, 'restrictions_released': False})
        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ['/usr/bin/systemctl', 'stop', 'input-lock.service'])
        self.assertEqual(calls[1], ['logind', 'SuspendWithFlags', 0])

    def test_startup_failure_cannot_use_old_same_boot_or_previous_boot_journal(self):
        for changes in ({'invocation_id': 'previous', 'effective': True},
                        {'invocation_id': None, 'effective': True},
                        {'boot_id': 'previous-boot', 'effective': True},
                        {'safety': {'state': 'SAFE_NORMAL'}, 'effective': False}):
            result, calls, records, observe = self.request(recovery=True, state_changes=changes)
            self.assertEqual(result, 0)
            self.assertEqual(calls, [['/usr/bin/systemctl', 'stop', 'input-lock.service']])
            observe.assert_not_called()
            self.assertFalse(any(c.args[0].name == 'failure.json' for c in records))

    def test_final_failure_latches_and_never_hibernates_or_forces(self):
        result, calls, records, _ = self.request(recovery=True, observe_error=TimeoutError('unconfirmed'))
        self.assertEqual(result, 1)
        self.assertTrue(any(call.args[0].name == 'failure.json' for call in records))
        self.assertEqual(records[-1].args[1]['result'], 'protection-failed')
        self.assertTrue(records[-1].args[1]['dispatch_accepted'])
        self.assertEqual(len(calls), 2)

    def test_refused_request_is_not_latched(self):
        # A9: latch only when AAG - InputLock's own request was accepted.
        result, calls, records, _ = self.request(recovery=True, dispatch=1)
        self.assertEqual(result, 1)
        self.assertFalse(any(call.args[0].name == 'failure.json' for call in records))
        self.assertEqual(records[-1].args[1]['result'], 'protection-failed')
        self.assertFalse(records[-1].args[1]['dispatch_accepted'])

    def test_failed_stop_never_requests_suspend(self):
        result, calls, *_ = self.request(recovery=True, stop=1)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)

    def test_completed_transition_is_not_retried(self):
        result, calls, *_ = self.request(recovery=True, receipt=dict(boot_id='boot', generation=9, transition_confirmed=True))
        self.assertEqual(result, 0)
        self.assertEqual(calls, [['/usr/bin/systemctl', 'stop', 'input-lock.service']])

    def test_corrupt_journal_still_releases_daemon_without_latching(self):
        run = Mock(return_value=SimpleNamespace(returncode=0))
        with (patch.object(Path, 'read_text', side_effect=ValueError('corrupt journal')),
              patch.object(safety, '_read_bounded_json', side_effect=ValueError('corrupt journal')),
              patch.object(safety, 'unit_properties', return_value={'ActiveState': 'inactive'}),
              patch.object(safety, 'atomic_json') as record, patch.object(safety.os, 'chmod')):
            result = safety.protect_main(run=run, recovery=True)
        self.assertEqual(result, 1)
        run.assert_called_once_with(['/usr/bin/systemctl', 'stop', 'input-lock.service'], timeout=10, check=False)
        self.assertFalse(any(call.args[0] == safety.FAILURE_LATCH for call in record.call_args_list))


class LogindInhibitorSemanticsTests(unittest.TestCase):
    """Read-only mock of the actual logind list; never request real sleep."""

    def inspect(self, rows):
        bus, manager, props = Mock(), Mock(), Mock()
        manager.CanSuspend.return_value = "yes"
        manager.ListInhibitors.return_value = rows
        props.Get.return_value = False
        dbus = SimpleNamespace(SystemBus=Mock(return_value=bus),
                               Interface=Mock(side_effect=[manager, props]))
        with patch.dict(sys.modules, dbus=dbus), patch.object(
                safety, 'root_hard_block_supported', return_value=True):
            state = safety.native_status()
        manager.ListInhibitors.assert_called_once_with(timeout=3)
        bus.close.assert_called_once()
        return state

    def test_weak_gnome_session_sleep_lock_is_advisory_for_root(self):
        status = self.inspect([
            ('sleep', 'gnome-session', 'user session inhibited', 'block-weak', 1000, 8275),
            ('sleep', 'GNOME Shell', 'save session', 'delay', 1000, 8287),
            ('handle-lid-switch', 'AAG Power', 'native lid owner', 'block', 0, 2269),
        ])
        self.assertTrue(status['supported'])
        self.assertFalse(status['blocked'])
        self.assertFalse(status['inputlock_holds_lid'])

    def test_real_hard_sleep_locks_fail_closed_even_with_weak_lock(self):
        for what in ('sleep', 'shutdown:sleep', 'sleep:shutdown'):
            with self.subTest(what=what):
                self.assertTrue(self.inspect([
                    ('sleep', 'gnome-session', 'weak', 'block-weak', 1000, 82),
                    (what, 'APT', 'upgrading', 'block', 0, 123),
                ])['blocked'])

    def test_non_sleep_hard_lock_or_only_delays_do_not_block_sleep(self):
        self.assertFalse(self.inspect([
            ('shutdown', 'APT', 'upgrade', 'block', 0, 123),
            ('sleep', 'AAG Power', 'checkpoint', 'delay', 0, 2269),
        ])['blocked'])

    def test_own_lid_inhibitor_is_detected_independently(self):
        status = self.inspect([
            ('handle-lid-switch', 'Input Lock', 'armed', 'block', 0, 404),
        ])
        self.assertTrue(status['inputlock_holds_lid'])
        self.assertFalse(status['blocked'])


class SystemdVersionSafetyTests(unittest.TestCase):
    def test_hard_sleep_block_semantics_are_never_assumed_on_old_or_unknown_versions(self):
        for first_line, allowed in (
            ('systemd 256 (256.5)', False),
            ('systemd 257 (257.8)', True),
            ('systemd 259 (259.5-0ubuntu3.4)', True),
            ('systemd 999', True),
            ('malformed output', False),
            ('', False),
        ):
            with self.subTest(version=first_line):
                run = Mock(return_value=SimpleNamespace(returncode=0, stdout=first_line + '\n'))
                self.assertEqual(safety.root_hard_block_supported(run=run), allowed)
                run.assert_called_once_with(
                    ['/usr/bin/systemctl', '--version'],
                    capture_output=True, text=True, timeout=3, check=False)
        self.assertFalse(safety.root_hard_block_supported(
            run=Mock(return_value=SimpleNamespace(returncode=1, stdout='systemd 259\n'))))
        self.assertFalse(safety.root_hard_block_supported(
            run=Mock(side_effect=subprocess.TimeoutExpired('systemctl', 3))))
        self.assertFalse(safety.root_hard_block_supported(
            run=Mock(side_effect=FileNotFoundError())))

    def test_old_systemd_refuses_suspend_before_connecting_to_dbus(self):
        dbus = SimpleNamespace(SystemBus=Mock())
        with patch.dict(sys.modules, dbus=dbus), patch.object(
                safety, 'root_hard_block_supported', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '257'):
                safety.request_native_suspend()
        dbus.SystemBus.assert_not_called()


class LogindDispatchTests(unittest.TestCase):
    def test_bounded_noninteractive_hard_inhibitor_checked_request_and_bus_cleanup(self):
        for error in (None, RuntimeError('AccessDenied'), TimeoutError('NoReply'), RuntimeError('UnknownMethod')):
            bus, manager = Mock(), Mock()
            manager.SuspendWithFlags.side_effect = error
            dbus = SimpleNamespace(SystemBus=Mock(return_value=bus), Interface=Mock(return_value=manager), UInt64=int)
            with patch.dict(sys.modules, dbus=dbus), patch.object(
                    safety, 'root_hard_block_supported', return_value=True):
                if error:
                    with self.assertRaises(type(error)):
                        safety.request_native_suspend()
                else:
                    safety.request_native_suspend()
            dbus.SystemBus.assert_called_once_with(private=True)
            bus.get_object.assert_called_once_with('org.freedesktop.login1', '/org/freedesktop/login1')
            manager.SuspendWithFlags.assert_called_once_with(0, timeout=10)
            self.assertEqual(manager.method_calls, [('SuspendWithFlags', (0,), {'timeout': 10})])
            # 0x10 (SD_LOGIND_SKIP_INHIBITORS) must never be set.
            self.assertEqual(manager.SuspendWithFlags.call_args.args[0] & 0x10, 0)
            bus.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
