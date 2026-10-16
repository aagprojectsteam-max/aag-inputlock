#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
APP_VERSION=$(tr -d '\r\n' < "$PROJECT_DIR/VERSION")
[[ -n "$APP_VERSION" ]] || {
    printf '%s\n' '[input-lock] ERROR: VERSION is empty' >&2
    exit 1
}
TARGET_USER=${SUDO_USER:-}
SYSTEM_USER_CREATED=0
CHANGES_STARTED=0
if [[ -r /etc/input-lock/install-state ]]; then
    SAVED_CREATED=$(awk -F= '$1=="SYSTEM_USER_CREATED" {print $2}' /etc/input-lock/install-state)
    [[ "$SAVED_CREATED" != 1 ]] || SYSTEM_USER_CREATED=1
fi

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--user LOGIN] [--update]

Installs Input Lock for one local graphical-seat user. The daemon starts in
the UNLOCKED state. The installer never performs a test lock.
EOF
}

log() {
    printf '%s\n' "[input-lock] $*"
}

fail() {
    printf '%s\n' "[input-lock] ERROR: $*" >&2
    exit 1
}

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2317,SC2329
cleanup() {
    local status=$?
    if (( status != 0 && CHANGES_STARTED == 1 )); then
        printf '%s\n' '[input-lock] Installation failed; stopping the daemon so input remains free.' >&2
        systemctl stop input-lock.service >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

while (($#)); do
    case "$1" in
        --user)
            (($# >= 2)) || fail '--user requires a login name'
            TARGET_USER=$2
            shift 2
            ;;
        --update)
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

(( EUID == 0 )) || fail 'run this installer with sudo'
[[ -n "$TARGET_USER" && "$TARGET_USER" != root ]] || fail 'use --user LOGIN (or run through sudo from the desktop user)'
id "$TARGET_USER" >/dev/null 2>&1 || fail "user does not exist: $TARGET_USER"
[[ "$TARGET_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || fail 'unsupported login-name syntax'

TARGET_UID=$(id -u "$TARGET_USER")
TARGET_HOME=$(getent passwd "$TARGET_USER" | awk -F: '{print $6}')
[[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]] || fail "home directory is unavailable for $TARGET_USER"
TARGET_RUNTIME="/run/user/$TARGET_UID"
TARGET_BUS="unix:path=$TARGET_RUNTIME/bus"

run_as_target() {
    runuser -u "$TARGET_USER" -- env \
        XDG_RUNTIME_DIR="$TARGET_RUNTIME" \
        DBUS_SESSION_BUS_ADDRESS="$TARGET_BUS" \
        HOME="$TARGET_HOME" \
        PYTHONDONTWRITEBYTECODE=1 \
        "$@"
}

log "Target desktop user: $TARGET_USER (UID $TARGET_UID)"

if [[ -r /etc/os-release ]]; then
    # os-release is trusted system data and is used only for this comparison.
    OS_ID=$(awk -F= '$1=="ID" {gsub(/"/,"",$2); print $2}' /etc/os-release)
    OS_VERSION=$(awk -F= '$1=="VERSION_ID" {gsub(/"/,"",$2); print $2}' /etc/os-release)
    if [[ "$OS_ID" != ubuntu || "$OS_VERSION" != 26.04 ]]; then
        log "WARNING: designed for Ubuntu 26.04; detected ${OS_ID:-unknown} ${OS_VERSION:-unknown}."
    fi
fi

for required in \
    "$PROJECT_DIR/src/input_lock_daemon.py" \
    "$PROJECT_DIR/src/input_lock_notifier.py" \
    "$PROJECT_DIR/src/input_lock_lid_policy.py" \
    "$PROJECT_DIR/src/input_lock_safety.py" \
    "$PROJECT_DIR/src/aag_sleep_transaction_compat.py" \
    "$PROJECT_DIR/src/input-lock-power-helper" \
    "$PROJECT_DIR/src/input-lock-launch" \
    "$PROJECT_DIR/config/input-lock.conf" \
    "$PROJECT_DIR/config/90-input-lock.rules" \
    "$PROJECT_DIR/assets/input-lock.desktop" \
    "$PROJECT_DIR/gnome-extension/metadata.json" \
    "$PROJECT_DIR/gnome-extension/extension.js" \
    "$PROJECT_DIR/systemd/input-lock.service" \
    "$PROJECT_DIR/systemd/input-lock-protect.service" \
    "$PROJECT_DIR/systemd/input-lock-failsafe.service" \
    "$PROJECT_DIR/assets/input-lock-locked.svg" \
    "$PROJECT_DIR/assets/input-lock-unlocked.svg" \
    "$PROJECT_DIR/assets/input-lock-locked-lid-ignored.svg" \
    "$PROJECT_DIR/assets/input-lock-unlocked-lid-ignored.svg" \
    "$PROJECT_DIR/assets/aag-projects-team.png" \
    "$PROJECT_DIR/LICENSE"; do
    [[ -f "$required" ]] || fail "package is incomplete: $required"
done

# Detect a real GNOME collision before changing the machine. The raw daemon
# hotkey does not need GNOME, but installing a conflicting fallback is unsafe.
if [[ -S "$TARGET_RUNTIME/bus" ]] && command -v gsettings >/dev/null 2>&1; then
    set +e
    run_as_target python3 "$PROJECT_DIR/src/input_lock_gnome.py" check
    GNOME_CHECK=$?
    set -e
    (( GNOME_CHECK == 0 )) || fail 'GNOME shortcut collision or inaccessible settings; resolve it before installation'
else
    log 'WARNING: graphical D-Bus is unavailable; GNOME fallback bindings will be installed on the next update/login.'
fi

if [[ -f /etc/input-lock/input-lock.conf ]]; then
    log 'Preserving existing /etc/input-lock/input-lock.conf.'
    PYTHONPATH="$PROJECT_DIR/src" INPUT_LOCK_CONFIG=/etc/input-lock/input-lock.conf \
        python3 -c 'from input_lock_common import load_config; load_config()' \
        || fail 'the existing configuration is invalid; restore or correct it before updating'
    EXISTING_UID=$(PYTHONPATH="$PROJECT_DIR/src" INPUT_LOCK_CONFIG=/etc/input-lock/input-lock.conf \
        python3 -c 'from input_lock_common import load_config; print(load_config().authorized_uid)')
    [[ "$EXISTING_UID" == "$TARGET_UID" ]] \
        || fail "existing configuration authorizes UID $EXISTING_UID, not target UID $TARGET_UID"
fi

PACKAGES=(
    python3
    python3-evdev
    python3-pyudev
    python3-dbus
    python3-gi
    gir1.2-gtk-3.0
    gir1.2-ayatanaappindicator3-0.1
    libnotify-bin
    pkexec
    evtest
    libinput-tools
)
MISSING=()
for package in "${PACKAGES[@]}"; do
    dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null | grep -q '^ii ' || MISSING+=("$package")
done
if ((${#MISSING[@]})); then
    log "Installing required Ubuntu packages: ${MISSING[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${MISSING[@]}"
fi

getent group input >/dev/null || groupadd --system input
if ! id input-lockd >/dev/null 2>&1; then
    log 'Creating restricted input-lockd service account.'
    useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin input-lockd
    SYSTEM_USER_CREATED=1
fi
usermod --append --groups input input-lockd

BACKUP_DIR="/var/backups/input-lock/$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ITEMS=(
    /usr/lib/input-lock
    /usr/bin/input-lock
    /usr/bin/input-lock-launch
    /etc/modules-load.d/input-lock.conf
    /usr/share/doc/input-lock
    /usr/share/pixmaps/input-lock-about.png
    /etc/input-lock
    /var/lib/input-lock
    /usr/share/polkit-1/rules.d/90-input-lock.rules
    /usr/lib/systemd/system/input-lock.service
    /usr/lib/systemd/system/input-lock-protect.service
    /usr/lib/systemd/system/input-lock-failsafe.service
    /var/lib/input-lock-protection
    /usr/lib/systemd/user/input-lock-notifier.service
    /usr/lib/systemd/system-sleep/input-lock
    /usr/share/icons/hicolor/scalable/status/input-lock-locked.svg
    /usr/share/icons/hicolor/scalable/status/input-lock-unlocked.svg
    /usr/share/icons/hicolor/scalable/status/input-lock-locked-lid-ignored.svg
    /usr/share/icons/hicolor/scalable/status/input-lock-unlocked-lid-ignored.svg
    /usr/share/applications/input-lock.desktop
    /usr/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team
    /etc/UPower/UPower.conf.d/99-zzzz-input-lock-no-auto-sleep.conf
    /etc/UPower/UPower.conf.d/.input-lock-restore-pending.json
)
for item in "${BACKUP_ITEMS[@]}"; do
    if [[ -e "$item" || -L "$item" ]]; then
        install -d -m 0700 "$BACKUP_DIR"
        cp -a --parents -- "$item" "$BACKUP_DIR/"
    fi
done

CHANGES_STARTED=1
/usr/bin/input-lock emergency-unlock >/dev/null 2>&1 || true
if [[ -S "$TARGET_RUNTIME/bus" ]] && run_as_target systemctl --user cat input-lock-notifier.service >/dev/null 2>&1; then
    run_as_target systemctl --user stop input-lock-notifier.service
fi
if systemctl cat input-lock.service >/dev/null 2>&1; then
    systemctl stop input-lock.service
fi
# Use the new restoration-only implementation even when upgrading old code.
PYTHONDONTWRITEBYTECODE=1 python3 "$PROJECT_DIR/src/input-lock-power-helper" disable
if [[ -S "$TARGET_RUNTIME/bus" ]]; then
    run_as_target python3 "$PROJECT_DIR/src/input_lock_lid_policy.py" disable-user
else
    run_as_target dbus-run-session -- python3 "$PROJECT_DIR/src/input_lock_lid_policy.py" disable-user
fi

# uinput supplies the filtered virtual keyboard used by user-approved keys.
modprobe uinput || fail 'cannot load the uinput kernel module'
[[ -c /dev/uinput ]] || fail '/dev/uinput was not created by the kernel'
install -d -m 0755 /etc/modules-load.d
printf '%s\n' uinput > /etc/modules-load.d/input-lock.conf

install -d -m 0755 /usr/lib/input-lock /etc/input-lock /usr/share/doc/input-lock
install -d -m 0755 /usr/share/polkit-1/rules.d
install -m 0644 "$PROJECT_DIR/config/90-input-lock.rules" /usr/share/polkit-1/rules.d/90-input-lock.rules
install -m 0644 \
    "$PROJECT_DIR/src/input_lock_common.py" \
    "$PROJECT_DIR/src/input_lock_daemon.py" \
    "$PROJECT_DIR/src/input_lock_cli.py" \
    "$PROJECT_DIR/src/input_lock_notifier.py" \
    "$PROJECT_DIR/src/input_lock_gnome.py" \
    "$PROJECT_DIR/src/input_lock_lid_policy.py" \
    "$PROJECT_DIR/src/input_lock_safety.py" \
    "$PROJECT_DIR/src/aag_sleep_transaction_compat.py" \
    /usr/lib/input-lock/
install -d -m 0755 /usr/lib/input-lock/locklock_core
install -m 0644 "$PROJECT_DIR/src/locklock_core/"*.py /usr/lib/input-lock/locklock_core/
install -m 0755 \
    "$PROJECT_DIR/src/input-lock-daemon" \
    "$PROJECT_DIR/src/input-lock-notifier" \
    "$PROJECT_DIR/src/input-lock-power-helper" \
    /usr/lib/input-lock/
install -m 0755 "$PROJECT_DIR/src/input-lock" /usr/bin/input-lock
install -m 0755 "$PROJECT_DIR/src/input-lock-launch" /usr/bin/input-lock-launch
install -d -m 0755 /usr/share/applications
install -m 0644 "$PROJECT_DIR/assets/input-lock.desktop" /usr/share/applications/input-lock.desktop
install -d -m 0755 /usr/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team
install -m 0644 \
    "$PROJECT_DIR/gnome-extension/metadata.json" \
    "$PROJECT_DIR/gnome-extension/extension.js" \
    /usr/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team/
run_as_target install -d -m 0755 \
    "$TARGET_HOME/.local/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team"
run_as_target install -m 0644 \
    "$PROJECT_DIR/gnome-extension/metadata.json" \
    "$PROJECT_DIR/gnome-extension/extension.js" \
    "$TARGET_HOME/.local/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team/"
install -d -m 0755 /usr/share/icons/hicolor/scalable/status
install -m 0644 \
    "$PROJECT_DIR/assets/input-lock-locked.svg" \
    "$PROJECT_DIR/assets/input-lock-unlocked.svg" \
    "$PROJECT_DIR/assets/input-lock-locked-lid-ignored.svg" \
    "$PROJECT_DIR/assets/input-lock-unlocked-lid-ignored.svg" \
    /usr/share/icons/hicolor/scalable/status/
install -d -m 0755 /usr/share/pixmaps
install -m 0644 "$PROJECT_DIR/assets/aag-projects-team.png" /usr/share/pixmaps/input-lock-about.png
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache --quiet --force /usr/share/icons/hicolor || true
fi

if [[ ! -f /etc/input-lock/input-lock.conf ]]; then
    sed \
        -e "s/@AUTHORIZED_UID@/$TARGET_UID/g" \
        -e "s/@AUTHORIZED_USER@/$TARGET_USER/g" \
        "$PROJECT_DIR/config/input-lock.conf" > /etc/input-lock/input-lock.conf
fi
chown root:root /etc/input-lock/input-lock.conf
chmod 0644 /etc/input-lock/input-lock.conf

install -m 0644 "$PROJECT_DIR/systemd/input-lock.service" /usr/lib/systemd/system/input-lock.service
install -m 0644 "$PROJECT_DIR/systemd/input-lock-protect.service" /usr/lib/systemd/system/input-lock-protect.service
install -m 0644 "$PROJECT_DIR/systemd/input-lock-failsafe.service" /usr/lib/systemd/system/input-lock-failsafe.service
install -m 0644 "$PROJECT_DIR/systemd/input-lock-notifier.service" /usr/lib/systemd/user/input-lock-notifier.service
install -m 0755 "$PROJECT_DIR/integration/input-lock-system-sleep" /usr/lib/systemd/system-sleep/input-lock
# Previous cp -a releases left user-owned documentation descendants. Replace
# this managed, already-backed-up tree instead of writing through its entries.
rm -rf -- /usr/share/doc/input-lock
install -d -m 0755 /usr/share/doc/input-lock
install -m 0644 "$PROJECT_DIR/README.md" /usr/share/doc/input-lock/README.md
install -m 0644 "$PROJECT_DIR/LICENSE" /usr/share/doc/input-lock/LICENSE
cp -R --no-preserve=ownership -- "$PROJECT_DIR/docs/." /usr/share/doc/input-lock/

cat > /etc/input-lock/install-state <<EOF
TARGET_USER=$TARGET_USER
TARGET_UID=$TARGET_UID
SYSTEM_USER_CREATED=$SYSTEM_USER_CREATED
VERSION=$APP_VERSION
EOF
chown root:root /etc/input-lock/install-state
chmod 0600 /etc/input-lock/install-state

systemctl daemon-reload
run_as_target python3 -c 'from pathlib import Path; import os; p=Path.home()/".config/input-lock/cursor-settings.json"; p.parent.mkdir(parents=True, exist_ok=True, mode=0o700); fd=None if p.exists() else os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600); fd is None or (os.write(fd, b"{\"enabled\": false, \"seconds\": 60}\n"), os.close(fd))'
systemctl enable --now input-lock.service

if [[ -S "$TARGET_RUNTIME/bus" ]]; then
    run_as_target systemctl --user daemon-reload
    run_as_target systemctl --user enable input-lock-notifier.service
    run_as_target systemctl --user restart input-lock-notifier.service
    run_as_target python3 /usr/lib/input-lock/input_lock_gnome.py install

    # GNOME Shell 50 on Wayland cannot safely reload extension source in the
    # running process (its ReloadExtension D-Bus method is deprecated). Never
    # disable the live copy during an update: write the new files, mark the UUID
    # enabled, and let the next login replace the running module atomically.
    run_as_target python3 -c \
        'from gi.repository import Gio; s=Gio.Settings.new("org.gnome.shell"); u="input-lock-cursor@aag-projects-team"; e=list(s.get_strv("enabled-extensions")); d=[x for x in s.get_strv("disabled-extensions") if x != u]; s.set_strv("enabled-extensions", e if u in e else [*e, u]); s.set_strv("disabled-extensions", d); Gio.Settings.sync()'
    log 'Cursor hiding extension update will activate at the next login.'

else
    log 'WARNING: notifier, GNOME shortcuts, and cursor hiding need a later ./update.sh while the user is logged in.'
fi

sleep 1
systemctl is-active --quiet input-lock.service || {
    journalctl --no-pager -u input-lock.service -n 50 >&2 || true
    fail 'input-lock.service did not become active'
}
/usr/bin/input-lock status
if [[ -S "$TARGET_RUNTIME/bus" ]]; then
    run_as_target /usr/bin/input-lock doctor
else
    /usr/bin/input-lock doctor
fi

trap - EXIT
log 'Installation completed in the UNLOCKED state.'
log 'Do not run a full lock yet. Follow README section “First safe test” with --timeout 30.'
if [[ -d "$BACKUP_DIR" ]]; then
    log "Previous Input Lock files were backed up under $BACKUP_DIR"
fi
exit 0
