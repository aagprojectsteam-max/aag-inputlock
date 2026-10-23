#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
export PYTHONDONTWRITEBYTECODE=1

python3 -B "$PROJECT_DIR/tools/run_tests.py" all
PROJECT_DIR="$PROJECT_DIR" python3 -B - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["PROJECT_DIR"])
paths = list((root / "src").rglob("*.py")) + [
    root / "src" / name
    for name in (
        "input-lock", "input-lock-daemon", "input-lock-notifier",
        "input-lock-power-helper",
    )
]
for path in paths:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
bash -n \
    "$PROJECT_DIR/install.sh" \
    "$PROJECT_DIR/update.sh" \
    "$PROJECT_DIR/uninstall.sh" \
    "$PROJECT_DIR/src/input-lock-launch"

grep -q '^Exec=/usr/bin/input-lock-launch$' "$PROJECT_DIR/assets/input-lock.desktop"
grep -q '^Terminal=false$' "$PROJECT_DIR/assets/input-lock.desktop"
grep -q '^LOCK_TOUCHSCREEN=yes$' "$PROJECT_DIR/config/input-lock.conf"
grep -q 'fill="#8b7355"' "$PROJECT_DIR/assets/input-lock-locked-lid-ignored.svg"
grep -q 'fill="#8b7355"' "$PROJECT_DIR/assets/input-lock-unlocked-lid-ignored.svg"
grep -q 'org.freedesktop.login1.inhibit-handle-lid-switch' \
    "$PROJECT_DIR/config/90-input-lock.rules"
grep -q 'input-lock-protect.service' \
    "$PROJECT_DIR/config/90-input-lock.rules"
grep -q '99-zzzz-input-lock-no-auto-sleep.conf' "$PROJECT_DIR/install.sh"
grep -Fq '"$STAGE/LICENSE" /usr/share/doc/input-lock/LICENSE' "$PROJECT_DIR/install.sh"
# Root never runs Python from the checkout and never installs unverified files.
grep -q '^verify_source_tree()' "$PROJECT_DIR/install.sh"
grep -q '^prune_unmanaged()' "$PROJECT_DIR/install.sh"
grep -q 'stop_and_verify input-lock-protect.service' "$PROJECT_DIR/uninstall.sh"
grep -q 'After=.*gnome-session-initialized.target' \
    "$PROJECT_DIR/systemd/input-lock-notifier.service"

printf '%s\n' 'PASS: configuration, protocol, Python syntax, and shell syntax'
