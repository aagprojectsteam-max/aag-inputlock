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
    'input_lock_safety.py': 'f59783c22011204f56d4c0eb747520827e5edfa4c0143f6fe15d38730ee21055',
    'input_lock_daemon.py': '05f5cd8f3ae4ab47e474d68cf0d8c25efb9bc03d47fe67eaca19a5eee81a6559',
    'input_lock_notifier.py': 'a486543782421e37fe1841693c2f143022c4cb87c7a20b8e1c96b4d29f24f21c',
    'input-lock.service': 'b62f1712d0fb5746bc054adcf2efff2413603f8e8c0248eea0b645f7d14ba631',
    'input-lock-protect.service': '72faad2d82f1a4e19fefe7dbdacd41834f1844f924aff98f68e8da3d4198dd0d',
    'input-lock-failsafe.service': None,
}


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=15).stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def open_unlocked():
    from input_lock_safety import Telemetry, native_status
    sample = Telemetry().read()
    require(0 <= time.monotonic() - sample.at <= 12 and sample.lid_closed is False
            and not sample.errors and {'cpu', 'chassis'} <= {s.kind for s in sample.sensors}
            and sample.ac is not None and sample.battery is not None and sample.discharging is not None,
            'fresh kernel telemetry and an open lid are required')
    require(not any(s.risk() >= 2 for s in sample.sensors), 'temperature is too high')
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
    native = native_status()
    require(native['supported'] and not native['blocked'] and not native['preparing']
            and not native['inputlock_holds_lid'], 'ordinary suspend capability unavailable or inhibited')


def preflight():
    from input_lock_safety import FAILURE_LATCH, suspend_success_count
    require(not FAILURE_LATCH.exists(), 'unresolved emergency failure latch')
    suspend_success_count()
    for source, target in FILES.items():
        path = Path(target)
        require(not path.is_symlink(), 'refusing symlink: ' + target)
        expected = BASELINE[path.name]
        require((digest(path) if path.exists() else None) == expected, 'installed baseline changed: ' + target)
        if path.exists():
            require(path.stat().st_uid == 0 and path.stat().st_mode & 0o022 == 0, 'installed file ownership/mode is unsafe: ' + target)
        if source.endswith('.py'):
            compile((ROOT / source).read_text(), source, 'exec')
    for unit in ('input-lock.service', 'input-lock-protect.service', 'input-lock-failsafe.service'):
        values = dict(line.split('=', 1) for line in run('/usr/bin/systemctl', 'show', unit,
                      '--property=DropInPaths,FragmentPath,ActiveState').splitlines() if '=' in line)
        require(not values.get('DropInPaths'), 'local unit overrides require review: ' + unit)
        if unit != 'input-lock-failsafe.service':
            require(values.get('FragmentPath') == '/usr/lib/systemd/system/' + unit,
                    'unexpected unit location: ' + unit)
        require(values.get('ActiveState') == ('active' if unit == 'input-lock.service' else 'inactive'),
                'unit is not in the expected idle state: ' + unit)
    open_unlocked()


def snapshot():
    account = pwd.getpwnam(os.environ['SUDO_USER'])
    archive = Path(account.pw_dir) / 'ARCHIVE' / 'InputLock'
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
    for target, expected in manifest.items():
        if expected is not None:
            require(digest(backup / target.lstrip('/')) == expected, 'backup verification failed')
    os.sync()
    return backup


def restore(backup):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / 'src'))
    if args.rollback:
        require(os.geteuid() == 0, 'rollback requires normal sudo')
        # Rollback only on an open lid; do not restore a stale safety journal.
        from input_lock_safety import Telemetry
        require(Telemetry().read().lid_closed is False, 'open the lid before rollback')
        restore(args.rollback.resolve())
        return
    preflight()
    print('Read-only deployment preflight passed; no lid/sleep tests performed.')
    if not args.install:
        return
    require(os.geteuid() == 0 and os.environ.get('SUDO_USER') not in (None, 'root'),
            'installation requires normal sudo from the desktop user')
    backup = snapshot()
    print('Verified root-owned rollback backup:', backup, flush=True)
    open_unlocked()  # Backup time must not make the open-lid gate stale.
    try:
        run('/usr/bin/systemctl', 'stop', 'input-lock.service')
        hashes = json.loads((backup / 'source-sha256.json').read_text())
        for source, target in FILES.items():
            data = (ROOT / source).read_bytes()
            require(hashlib.sha256(data).hexdigest() == hashes[source], 'source changed during installation')
            with open(target, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chown(target, 0, 0)
            os.chmod(target, 0o644)
            require(digest(Path(target)) == hashes[source], 'installed hash mismatch')
        run('/usr/bin/systemctl', 'daemon-reload')
        from input_lock_safety import protection_errors
        errors = protection_errors()
        require(not errors, '; '.join(errors))
        run('/usr/bin/systemctl', 'start', 'input-lock.service')
        # Read-only health wait; never arm lid-ignore as an installation test.
        for _ in range(40):
            time.sleep(1)
            state = json.loads(Path('/run/input-lock/state.json').read_text())
            sample = (state.get('safety') or {}).get('sample') or {}
            if sample.get('stack_ready'):
                open_unlocked()
                break
        else:
            raise RuntimeError('new daemon did not report native readiness')
        account = pwd.getpwnam(os.environ['SUDO_USER'])
        run('/usr/sbin/runuser', '-u', account.pw_name, '--', '/usr/bin/env',
            'XDG_RUNTIME_DIR=/run/user/' + str(account.pw_uid),
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(account.pw_uid) + '/bus',
            '/usr/bin/systemctl', '--user', 'restart', 'input-lock-notifier.service')
        print('Installed and disarmed. No physical acceptance claimed. Backup:', backup)
    except Exception:
        restore(backup)
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit('BLOCKED: ' + str(exc))
