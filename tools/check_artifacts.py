#!/usr/bin/env python3
"""Validate wheel import inventory and source release installation payload."""
import argparse
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('folder', type=Path)
parser.add_argument('--linux', action='store_true', help='also import Linux modules (requires distro native dependencies)')
args = parser.parse_args()
folder = args.folder
wheels = list(folder.glob('*.whl'))
sources = list(folder.glob('*.tar.gz'))
assert len(wheels) == len(sources) == 1, 'expected one wheel and one source distribution'
with zipfile.ZipFile(wheels[0]) as archive:
    names = set(archive.namelist())
    for name in ('input_lock_cli.py','input_lock_daemon.py','input_lock_notifier.py',
                 'input_lock_common.py','input_lock_gnome.py','input_lock_lid_policy.py','input_lock_safety.py',
                 'locklock_windows/dispatch.py','locklock_windows/ipc.py','locklock_core/__init__.py'):
        assert name in names, f'missing wheel module {name}'
with tarfile.open(sources[0]) as archive:
    names = {name.partition('/')[2] for name in archive.getnames()}
    for name in ('VERSION','install.sh','uninstall.sh','config/input-lock.conf',
                 'systemd/input-lock.service','systemd/input-lock-protect.service',
                 'src/input-lock-daemon','src/input-lock-power-helper','gnome-extension/extension.js',
                 'windows/common.ps1','windows/install.ps1','tools/run_tests.py',
                 'tools/legacy/install_native_lid.py'):
        assert name in names, f'missing source payload {name}'
modules = ['locklock_core', 'locklock_windows.state', 'locklock_windows.power',
           'locklock_windows.ipc', 'locklock_windows.dispatch', 'input_lock_safety']
if args.linux:
    modules += ['input_lock_cli', 'input_lock_daemon', 'input_lock_notifier',
                'input_lock_common', 'input_lock_gnome', 'input_lock_lid_policy']
# Isolated mode excludes the checkout, cwd and PYTHONPATH. Importing modules
# never instantiates hooks, services, input devices, GUI windows or power APIs.
subprocess.run([sys.executable, '-I', '-B', '-c',
                'import importlib, sys; sys.path.insert(0, sys.argv[1]); '
                '[importlib.import_module(name) for name in sys.argv[2:]]',
                str(wheels[0].resolve()), *modules], check=True)
print(f'PASS: wheel inventory, source payload and {len(modules)} isolated wheel imports')
