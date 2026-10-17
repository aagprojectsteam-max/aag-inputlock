#!/usr/bin/python3
"""Narrow, guarded upgrade for the inspected beta.4 installation; no power tests.

Default: read-only preflight. --install requires normal interactive sudo.
Unknown installed versions or local unit overrides require a fresh review.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'src/input_lock_lid_policy.py': '/usr/lib/input-lock/input_lock_lid_policy.py',
    'src/input_lock_safety.py': '/usr/lib/input-lock/input_lock_safety.py',
    'src/input_lock_daemon.py': '/usr/lib/input-lock/input_lock_daemon.py',
    'src/input_lock_notifier.py': '/usr/lib/input-lock/input_lock_notifier.py',
    'systemd/input-lock.service': '/usr/lib/systemd/system/input-lock.service',
    'systemd/input-lock-protect.service': '/usr/lib/systemd/system/input-lock-protect.service',
    'systemd/input-lock-failsafe.service': '/usr/lib/systemd/system/input-lock-failsafe.service',
}
# Reviewed installed baseline, including the redundant legacy safety-adapter
# branches present on this beta.4 deployment. Never overwrite unknown edits.
BASELINE = {
    'input_lock_lid_policy.py': '94afe4672b6bd0797a16a17a3a992fd901d6460348b6d94e34e6bcc5689eb49c',
    'input_lock_safety.py': 'f59783c22011204f56d4c0eb747520827e5edfa4c0143f6fe15d38730ee21055',
    'input_lock_daemon.py': '05f5cd8f3ae4ab47e474d68cf0d8c25efb9bc03d47fe67eaca19a5eee81a6559',
    'input_lock_notifier.py': 'a486543782421e37fe1841693c2f143022c4cb87c7a20b8e1c96b4d29f24f21c',
    'input-lock.service': 'b62f1712d0fb5746bc054adcf2efff2413603f8e8c0248eea0b645f7d14ba631',
    'input-lock-protect.service': '72faad2d82f1a4e19fefe7dbdacd41834f1844f924aff98f68e8da3d4198dd0d',
    'input-lock-failsafe.service': None,
}
FAILED_BASELINE = {
    'input_lock_lid_policy.py': '94afe4672b6bd0797a16a17a3a992fd901d6460348b6d94e34e6bcc5689eb49c',
    'input_lock_safety.py': '8e88c9d9dd0fa7a363ab7ea67d2667c10e6cc738c52366c10247852bfdfe6a75',
    'input_lock_daemon.py': '9fda4954267067daf9f05dbfa38bbfb1ce663860e37308b930e7231bdcc7be77',
    'input_lock_notifier.py': 'aa8f7b2711a741bbabce91d6db64ba0c20f25c8fd172bf29603911858395bed4',
    'input-lock.service': '62bb12ca1055b7d97e5d42b8743a9c0cda75f3d81d86fb18955a4c73c0c52740',
    'input-lock-protect.service': 'a4273d16bb2bc1e0f68faf9c407719bbf17fd873914d281151514532e06751bc',
    'input-lock-failsafe.service': '967d4cf06e66c1b288c192787d1eeaf6469c06b282f227e62e20bf44d989f69b',
}
# Exact reviewed active/running InputLock release from the 2026-10-17
# installation. All seven hashes must match this ONE version; never combine
# files from different baselines or accept an unknown unit override.
POST_BOOT_REPAIR_BASELINE = {
    'input_lock_lid_policy.py': 'd6cc8671226852ad85e0ac6f6a9767e4dfff66a13daef06a8b74bfc8734e22f4',
    'input_lock_safety.py': 'cf0851bbd01f54cb7edb27b1fe0472540a8c5f2077248b207f2f994f23275437',
    'input_lock_daemon.py': '3f675f3acd0c8de5c67a659eb1796f87192fbe372f57daab5325a38184882287',
    'input_lock_notifier.py': '4828c02c96b32f6bb163cc9d179d02356e786911f0164d2eab3f39a244235b92',
    'input-lock.service': '69bb6695fb0cb11272a44bc32dd67a18feacb6f384fd88ddeced60e2fd094a25',
    'input-lock-protect.service': 'a4273d16bb2bc1e0f68faf9c407719bbf17fd873914d281151514532e06751bc',
    'input-lock-failsafe.service': '967d4cf06e66c1b288c192787d1eeaf6469c06b282f227e62e20bf44d989f69b',
}
KNOWN_LATCH_SHA256 = 'abd072afb9fd98cc19c4f1dc096c946ec07008f5ccb4cd7a99af7462c688a7f8'
LATCH = Path('/var/lib/input-lock-protection/failure.json')


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=15).stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def open_telemetry():
    from input_lock_safety import Telemetry
    sample = Telemetry().read()
    require(0 <= time.monotonic() - sample.at <= 12 and sample.lid_closed is False
            and not sample.errors and {'cpu', 'chassis'} <= {s.kind for s in sample.sensors}
            and sample.ac is not None and sample.battery is not None and sample.discharging is not None,
            'fresh kernel telemetry and an open lid are required')
    require(not any(s.risk() >= 2 for s in sample.sensors), 'temperature is too high')


def native_available():
    from input_lock_safety import native_status
    native = native_status()
    require(native['supported'] and not native['blocked'] and not native['preparing']
            and not native['inputlock_holds_lid'], 'ordinary suspend capability unavailable or inhibited')
    # Inspect the live API without calling any power method. An old logind must
    # fail deployment rather than falling back to weaker privileged semantics.
    import dbus
    import xml.etree.ElementTree as ET
    bus = dbus.SystemBus(private=True)
    try:
        obj = bus.get_object('org.freedesktop.login1', '/org/freedesktop/login1')
        xml = dbus.Interface(obj, 'org.freedesktop.DBus.Introspectable').Introspect(timeout=5)
        node = ET.fromstring(str(xml))
        method = node.find("./interface[@name='org.freedesktop.login1.Manager']/method[@name='SuspendWithFlags']")
        require(method is not None and [(a.get('type'), a.get('direction', 'in')) for a in method.findall('arg')]
                == [('t', 'in')], 'logind SuspendWithFlags(uint64) is unavailable')
    finally:
        bus.close()


def open_unlocked():
    open_telemetry()
    # Type=simple may be active before the first fresh state file is written.
    state = None
    for _ in range(50):
        try:
            observed = json.loads(Path('/run/input-lock/state.json').read_text())
            telemetry = ((observed.get('safety') or {}).get('sample') or {}) if isinstance(observed, dict) else {}
            if (isinstance(observed, dict) and
                    0 <= time.time() - observed.get('timestamp', 0) <= 12 and
                    telemetry.get('dependencies_ok') is True):
                state = observed
                break
        except (OSError, ValueError, TypeError):
            pass
        time.sleep(0.2)
    require(state is not None, 'fresh InputLock daemon state is unavailable')
    require(state.get('ignore_lid_close') is False and state.get('grabbed_devices') == 0
            and state.get('lid_inhibitor_active') is False
            and state.get('authorized_session_active') is True, 'InputLock must be unlocked, disarmed, in an active session')
    require(state.get('safety', {}).get('sample', {}).get('dependencies_ok') is True,
            'daemon session/logind/UPower supervision is unavailable')
    native_available()


def verify_sources(manifest):
    hashes = json.loads(manifest.read_text())
    require(set(hashes) == set(FILES) | {'tools/install_native_lid.py'}, 'unexpected source pin manifest')
    for source, expected in hashes.items():
        require(digest(ROOT / source) == expected, 'source pin mismatch: ' + source)
    return hashes


def failed_daemon_gate():
    """No daemon-state exception unless all independent evidence agrees."""
    from input_lock_lid_policy import UPowerPolicyController
    open_telemetry()
    native_available()
    for name in ('state.json', 'control.sock'):
        require(not os.path.lexists('/run/input-lock/' + name), 'unexpected daemon runtime artifact: ' + name)
    policy = UPowerPolicyController().status()
    require(policy.get('normal_os_policy') is True and not policy.get('recovery_pending')
            and not policy.get('dropin_present'), 'UPower restoration is not verified')
    user = os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name
    session = run('/usr/bin/loginctl', 'show-user', user, '--property=Display', '--value').strip()
    require(bool(session), 'no desktop session for deployment user')
    # loginctl (unlike systemctl show) does not split a comma-separated
    # --property argument. Supply one flag per property or it returns nothing.
    values = dict(line.split('=', 1) for line in run('/usr/bin/loginctl', 'show-session', session,
                  '--property=Active', '--property=Remote', '--property=Class',
                  '--property=Type', '--property=Name').splitlines() if '=' in line)
    require(values.get('Name') == user and values.get('Active') == 'yes' and values.get('Remote') == 'no'
            and values.get('Class') == 'user' and values.get('Type') in {'wayland', 'x11'},
            'deployment requires the active local graphical session')


def approved_baseline(installed: dict, *, recovery=False) -> bool:
    """Refuse mixed or unknown deployments even when each file is recognizable."""
    variants = (FAILED_BASELINE,) if recovery else (BASELINE, POST_BOOT_REPAIR_BASELINE)
    return any(installed == candidate for candidate in variants)


def preflight(*, recovery=False):
    from input_lock_safety import FAILURE_LATCH, suspend_success_count
    if recovery:
        require(not LATCH.is_symlink() and LATCH.is_file() and digest(LATCH) == KNOWN_LATCH_SHA256,
                'historical failure latch differs; fresh review required')
        require(LATCH.stat().st_uid == 0 and LATCH.stat().st_mode & 0o022 == 0, 'unsafe latch ownership/mode')
    else:
        require(not FAILURE_LATCH.exists(), 'unresolved emergency failure latch')
    suspend_success_count()
    observed_baseline = {}
    for source, target in FILES.items():
        path = Path(target)
        require(not path.is_symlink(), 'refusing symlink: ' + target)
        observed_baseline[path.name] = digest(path) if path.exists() else None
        if path.exists():
            require(path.stat().st_uid == 0 and path.stat().st_mode & 0o022 == 0, 'installed file ownership/mode is unsafe: ' + target)
        if source.endswith('.py'):
            compile((ROOT / source).read_text(), source, 'exec')
    require(approved_baseline(observed_baseline, recovery=recovery),
            'installed baseline changed: unknown or mixed installed release')
    for unit in ('input-lock.service', 'input-lock-protect.service', 'input-lock-failsafe.service'):
        values = dict(line.split('=', 1) for line in run('/usr/bin/systemctl', 'show', unit,
                      '--property=DropInPaths,FragmentPath,ActiveState,SubState,Result,MainPID,ControlPID,Job,ExecMainStartTimestampMonotonic,ExecStartPre').splitlines() if '=' in line)
        require(not values.get('DropInPaths'), 'local unit overrides require review: ' + unit)
        if recovery or unit != 'input-lock-failsafe.service':
            require(values.get('FragmentPath') == '/usr/lib/systemd/system/' + unit,
                    'unexpected unit location: ' + unit)
        if recovery:
            allowed = {'failed'} if unit == 'input-lock.service' else {'inactive', 'failed'}
            require(values.get('ActiveState') in allowed and values.get('MainPID') == '0'
                    and values.get('ControlPID') == '0' and not values.get('Job'),
                    'known failed installation is not quiescent: ' + unit)
            if unit == 'input-lock.service':
                require(values.get('Result') == 'exit-code', 'unexpected daemon failure result')
                require(values.get('ExecMainStartTimestampMonotonic') == '0'
                        and '/usr/lib/input-lock/input-lock-power-helper disable' in values.get('ExecStartPre', '')
                        and 'code=exited' in values.get('ExecStartPre', '')
                        and 'status=1 ' in values.get('ExecStartPre', ''),
                        'failure is not the reviewed pre-start helper failure')
        else:
            require(values.get('ActiveState') == ('active' if unit == 'input-lock.service' else 'inactive'),
                    'unit is not in the expected idle state: ' + unit)
    failed_daemon_gate() if recovery else open_unlocked()


def snapshot():
    archive = Path(pwd.getpwuid(0).pw_dir) / 'ARCHIVE' / 'InputLock'
    require(not any(p.is_symlink() for p in (archive, *archive.parents)), 'symlink in root archive path')
    archive.mkdir(parents=True, exist_ok=True)
    backup = archive / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    # Preserve all installed application code, configuration and recovery state,
    # not just the files replaced by this upgrade.
    roots = ['/usr/lib/input-lock', '/etc/input-lock', '/var/lib/input-lock',
             '/var/lib/input-lock-protection', '/usr/share/doc/input-lock',
             '/usr/share/polkit-1/rules.d/90-input-lock.rules',
             '/usr/lib/systemd/user/input-lock-notifier.service',
             *[path for path in FILES.values() if path.endswith('.service')]]
    for name in roots:
        if Path(name).exists():
            run('/usr/bin/cp', '-a', '--parents', '--', name, str(backup))
    manifest = {target: digest(Path(target)) if Path(target).exists() else None for target in FILES.values()}
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (backup / 'source-sha256.json').write_text(json.dumps({source: digest(ROOT / source) for source in FILES}, indent=2) + '\n')
    (backup / 'systemd-state.txt').write_text(run('/usr/bin/systemctl', 'show', 'input-lock.service',
        'input-lock-protect.service', 'input-lock-failsafe.service'))
    for target, expected in manifest.items():
        if expected is not None:
            require(digest(backup / target.lstrip('/')) == expected, 'backup verification failed')
    if LATCH.exists():
        require(digest(backup / str(LATCH).lstrip('/')) == digest(LATCH), 'latch backup verification failed')
    for target in ('/var/lib/input-lock/safety.json', '/var/lib/input-lock/safety-action-result.json'):
        if Path(target).exists():
            require(digest(backup / target.lstrip('/')) == digest(Path(target)), 'journal backup verification failed')
    os.sync()
    return backup


def acknowledge_latch(backup):
    saved = backup / str(LATCH).lstrip('/')
    require(not LATCH.is_symlink() and digest(LATCH) == KNOWN_LATCH_SHA256
            and digest(saved) == KNOWN_LATCH_SHA256, 'latch changed or backup missing')
    historical = LATCH.with_name('failure-acknowledged-' + KNOWN_LATCH_SHA256 + '.json')
    require(not os.path.lexists(historical), 'historical acknowledgement already exists; review required')
    # Preserve the original inode alongside the verified root archive. This is
    # explicit operator acknowledgement of failure, never a claim of success.
    LATCH.rename(historical)
    os.sync()


def restore_latch(backup):
    saved = backup / str(LATCH).lstrip('/')
    if saved.exists() and not os.path.lexists(LATCH):
        require(digest(saved) == KNOWN_LATCH_SHA256, 'rollback latch hash mismatch')
        shutil.copy2(saved, LATCH)
        os.chown(LATCH, 0, 0)
        os.sync()


def restore(backup):
    require(backup.is_dir() and backup.stat().st_uid == 0 and backup.stat().st_mode & 0o077 == 0
            and not any(p.is_symlink() for p in (backup, *backup.parents)), 'unsafe rollback directory')
    restore_latch(backup)  # Re-latch even if later file verification/stop/reload fails.
    manifest = json.loads((backup / 'manifest.json').read_text())
    require(set(manifest) == set(FILES.values()), 'unexpected rollback manifest')
    for target, expected in manifest.items():
        if expected is not None:
            require(digest(backup / target.lstrip('/')) == expected, 'rollback backup hash mismatch')
    run('/usr/bin/systemctl', 'stop', 'input-lock.service')
    for target, expected in manifest.items():
        if expected is None:
            # Only the new, managed unit can be absent from the old install.
            require(target.endswith('/input-lock-failsafe.service'), 'invalid absent entry')
            Path(target).unlink(missing_ok=True)
        else:
            shutil.copy2(backup / target.lstrip('/'), target)
            os.chown(target, 0, 0)
    run('/usr/bin/systemctl', 'daemon-reload')
    # Retain recovery evidence, including any failure latch. Never auto-clear it.
    print('Previous files restored; InputLock remains stopped. Backup:', backup)


def wait_ready():
    for _ in range(40):
        time.sleep(1)
        try:
            state = json.loads(Path('/run/input-lock/state.json').read_text())
            sample = (state.get('safety') or {}).get('sample') or {}
            if sample.get('stack_ready') is True:
                open_unlocked()
                return
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    raise RuntimeError('new daemon did not report native readiness')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--rollback', type=Path)
    parser.add_argument('--recover-known-boot-failure', action='store_true')
    parser.add_argument('--acknowledge-failed-protection', action='store_true')
    parser.add_argument('--source-manifest', type=Path)
    args = parser.parse_args()
    require(not args.rollback or not (args.install or args.recover_known_boot_failure
            or args.acknowledge_failed_protection or args.source_manifest), 'rollback cannot be combined with install options')
    sys.path.insert(0, str(ROOT / 'src'))
    if args.rollback:
        require(os.geteuid() == 0, 'rollback requires normal sudo')
        # Rollback only on an open lid; do not restore a stale safety journal.
        from input_lock_safety import Telemetry
        require(Telemetry().read().lid_closed is False, 'open the lid before rollback')
        restore(args.rollback.resolve())
        return
    require(not args.acknowledge_failed_protection or args.recover_known_boot_failure,
            'failure acknowledgement is only valid for known boot recovery')
    if args.recover_known_boot_failure:
        require(args.source_manifest is not None, 'known recovery requires the reviewed source pin manifest')
        verify_sources(args.source_manifest)
        require(not args.install or args.acknowledge_failed_protection,
                'installation requires explicit --acknowledge-failed-protection; historical protection FAILED')
    elif args.source_manifest:
        verify_sources(args.source_manifest)
    preflight(recovery=args.recover_known_boot_failure)
    print('Read-only deployment preflight passed; no lid/sleep tests performed.')
    if not args.install:
        return
    require(os.geteuid() == 0 and os.environ.get('SUDO_USER') not in (None, 'root'),
            'installation requires normal sudo from the desktop user')
    backup = snapshot()
    print('Verified root-owned rollback backup:', backup, flush=True)
    # Recheck all baseline, quiescence and telemetry gates after backup.
    preflight(recovery=args.recover_known_boot_failure)
    try:
        run('/usr/bin/systemctl', 'stop', 'input-lock.service')
        if args.source_manifest:
            verify_sources(args.source_manifest)
        hashes = json.loads((backup / 'source-sha256.json').read_text())
        for source, target in FILES.items():
            data = (ROOT / source).read_bytes()
            require(hashlib.sha256(data).hexdigest() == hashes[source], 'source changed during installation')
            import tempfile
            fd, staged = tempfile.mkstemp(prefix='.input-lock-install-', dir=Path(target).parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchown(stream.fileno(), 0, 0)
                    os.fchmod(stream.fileno(), 0o644)
                os.replace(staged, target)
            finally:
                Path(staged).unlink(missing_ok=True)
            require(digest(Path(target)) == hashes[source], 'installed hash mismatch')
        run('/usr/bin/systemctl', 'daemon-reload')
        os.sync()
        if args.recover_known_boot_failure:
            acknowledge_latch(backup)
            run('/usr/bin/systemctl', 'reset-failed', 'input-lock.service',
                'input-lock-protect.service', 'input-lock-failsafe.service')
        from input_lock_safety import protection_errors
        errors = protection_errors()
        require(not errors, '; '.join(errors))
        run('/usr/bin/systemctl', 'start', 'input-lock.service')
        # Read-only health wait; never arm lid-ignore as an installation test.
        wait_ready()
        account = pwd.getpwnam(os.environ['SUDO_USER'])
        run('/usr/sbin/runuser', '-u', account.pw_name, '--', '/usr/bin/env',
            'XDG_RUNTIME_DIR=/run/user/' + str(account.pw_uid),
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(account.pw_uid) + '/bus',
            '/usr/bin/systemctl', '--user', 'restart', 'input-lock-notifier.service')
        print('Installed and disarmed. Historical protection FAILED; acknowledgement is not physical acceptance. Backup:', backup)
    except Exception as install_error:
        try:
            restore(backup)
        except Exception as rollback_error:
            raise RuntimeError(f'installation failed: {install_error}; rollback incomplete: {rollback_error}; '
                               f'keep the lid open and review backup {backup}') from rollback_error
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit('BLOCKED: ' + str(exc))
