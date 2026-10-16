"""No files, buses, devices or power operations: all effects are injected."""
import json
import subprocess
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
    return dict(LoadState='loaded', ActiveState='inactive', ExecStart=commands[unit],
                OnFailure=safety.FAILSAFE_UNIT if unit in {'input-lock.service', safety.PROTECT_UNIT} else '',
                Restart='no', WatchdogUSec='20s', TimeoutStopUSec='5s', KillMode='control-group',
                User='', Type='oneshot', TimeoutStartUSec='2min 45s',
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
        state = dict(boot_id='boot', generation=9, updated_at=100., effective=False,
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
        with (patch.object(Path, 'read_text', read), patch.object(safety.time, 'time', return_value=100.),
              patch.object(safety, 'atomic_json') as records, patch.object(safety.os, 'chmod'),
              patch.object(safety, 'unit_properties', side_effect=props)):
            result = safety.protect_main(run, lambda: ready, telemetry, Path('/fixture/request.json'),
                                        observe, lambda: 1, recovery=recovery, status=lambda: status,
                                        latch=Path('/fixture/failure.json'))
        return result, calls, records.call_args_list, observe

    def test_only_ordinary_suspend_after_release(self):
        result, calls, records, observe = self.request()
        self.assertEqual(result, 0)
        self.assertEqual(calls, [['/usr/bin/systemctl', '--no-ask-password', '--check-inhibitors=yes', 'suspend']])
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
                       {'native_changes': {'blocked': True}}, {'native_changes': {'preparing': True}},
                       {'active': 'activating'}):
            result, calls, records, _ = self.request(**kwargs)
            self.assertEqual(result, 1)
            self.assertEqual(calls, [])
            self.assertEqual(records[-1].args[1]['result'], 'recovery-required')

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
        self.assertEqual(calls[1][-1], 'suspend')

    def test_final_failure_latches_and_never_hibernates_or_forces(self):
        result, calls, records, _ = self.request(recovery=True, dispatch=1)
        self.assertEqual(result, 1)
        self.assertTrue(any(call.args[0].name == 'failure.json' for call in records))
        self.assertEqual(records[-1].args[1]['result'], 'protection-failed')
        self.assertEqual(len(calls), 2)

    def test_failed_stop_never_requests_suspend(self):
        result, calls, *_ = self.request(recovery=True, stop=1)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)

    def test_completed_transition_is_not_retried(self):
        result, calls, *_ = self.request(recovery=True, receipt=dict(boot_id='boot', generation=9, transition_confirmed=True))
        self.assertEqual(result, 0)
        self.assertEqual(calls, [['/usr/bin/systemctl', 'stop', 'input-lock.service']])

    def test_corrupt_journal_still_releases_daemon_and_latches(self):
        run = Mock(return_value=SimpleNamespace(returncode=0))
        with (patch.object(Path, 'read_text', side_effect=ValueError('corrupt journal')),
              patch.object(safety, 'unit_properties', return_value={'ActiveState': 'inactive'}),
              patch.object(safety, 'atomic_json') as record, patch.object(safety.os, 'chmod')):
            result = safety.protect_main(run=run, recovery=True)
        self.assertEqual(result, 1)
        run.assert_called_once_with(['/usr/bin/systemctl', 'stop', 'input-lock.service'], timeout=10, check=False)
        self.assertTrue(any(call.args[0] == safety.FAILURE_LATCH for call in record.call_args_list))


if __name__ == '__main__':
    unittest.main()
