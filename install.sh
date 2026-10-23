#!/usr/bin/env bash
# Input Lock installer. The whole script is parsed before anything runs (see
# main at the end), so editing this file while it runs cannot change it.
set -Eeuo pipefail
umask 022

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
TARGET_USER=${SUDO_USER:-}
SYSTEM_USER_CREATED=0
CHANGES_STARTED=0
INSTALL_COMMITTED=0
WAS_ACTIVE=0
# The tray unit existed and was stopped for the update; NOTIFIER_WAS_ACTIVE
# records whether it was running then, so a rollback restores exactly that
# state (review U-R8). A successful install always starts the tray.
NOTIFIER_STOPPED=0
NOTIFIER_WAS_ACTIVE=0
# 1 when an earlier Input Lock installation exists (an update): the installer
# then never changes whether the cursor-hiding extension is enabled (U-R4).
PREVIOUS_INSTALL=0
STAGE=''
BACKUP_DIR=''
APP_VERSION=''
SOCKET_PATH=''
POST_INSTALL_WARNINGS=0

# Files copied from the checkout. Everything is staged into a root-owned
# directory, checked and compiled there, and installed only from that copy.
PAYLOAD_FIXED=(
    VERSION
    README.md
    LICENSE
    src/input_lock_common.py
    src/input_lock_daemon.py
    src/input_lock_cli.py
    src/input_lock_notifier.py
    src/input_lock_gnome.py
    src/input_lock_lid_policy.py
    src/input_lock_safety.py
    src/input_lock_i18n.py
    src/input_lock_messages.py
    src/input-lock
    src/input-lock-daemon
    src/input-lock-notifier
    src/input-lock-power-helper
    src/input-lock-launch
    config/input-lock.conf
    config/90-input-lock.rules
    assets/input-lock.desktop
    assets/input-lock-locked.svg
    assets/input-lock-unlocked.svg
    assets/input-lock-locked-lid-ignored.svg
    assets/input-lock-unlocked-lid-ignored.svg
    assets/aag-projects-team.png
    gnome-extension/metadata.json
    gnome-extension/extension.js
    systemd/input-lock.service
    systemd/input-lock-protect.service
    systemd/input-lock-failsafe.service
    systemd/input-lock-notifier.service
    integration/input-lock-system-sleep
    tools/po_compile.py
)
# Python modules installed into /usr/lib/input-lock (besides locklock_core/).
LIB_MODULES=(
    input_lock_common.py
    input_lock_daemon.py
    input_lock_cli.py
    input_lock_notifier.py
    input_lock_gnome.py
    input_lock_lid_policy.py
    input_lock_safety.py
    input_lock_i18n.py
    input_lock_messages.py
)
# User-interface translations: po/<lang>.po is compiled in the stage (msgfmt,
# or tools/po_compile.py when msgfmt is missing) and installed as
# /usr/share/locale/<lang>/LC_MESSAGES/input-lock.mo. Each one is also listed
# in BACKUP_ITEMS and RESTORE_ITEMS below.
LOCALES=(
    he
)
# Executables installed into /usr/lib/input-lock.
LIB_PROGRAMS=(
    input-lock-daemon
    input-lock-notifier
    input-lock-power-helper
)
# Extensionless Python programs that must compile before anything changes.
PYTHON_PROGRAMS=(
    src/input-lock
    src/input-lock-daemon
    src/input-lock-notifier
    src/input-lock-power-helper
)
PAYLOAD=()

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
    /usr/share/locale/he/LC_MESSAGES/input-lock.mo
    /etc/UPower/UPower.conf.d/99-zzzz-input-lock-no-auto-sleep.conf
    /etc/UPower/UPower.conf.d/.input-lock-restore-pending.json
)
# Files this installer writes and may therefore restore on failure. Runtime
# state (/var/lib/...) and UPower restore files belong to the daemon and the
# power helper; a rollback never rewrites them.
RESTORE_ITEMS=(
    /usr/lib/input-lock
    /usr/bin/input-lock
    /usr/bin/input-lock-launch
    /etc/modules-load.d/input-lock.conf
    /usr/share/doc/input-lock
    /usr/share/pixmaps/input-lock-about.png
    /etc/input-lock
    /usr/share/polkit-1/rules.d/90-input-lock.rules
    /usr/lib/systemd/system/input-lock.service
    /usr/lib/systemd/system/input-lock-protect.service
    /usr/lib/systemd/system/input-lock-failsafe.service
    /usr/lib/systemd/user/input-lock-notifier.service
    /usr/lib/systemd/system-sleep/input-lock
    /usr/share/icons/hicolor/scalable/status/input-lock-locked.svg
    /usr/share/icons/hicolor/scalable/status/input-lock-unlocked.svg
    /usr/share/icons/hicolor/scalable/status/input-lock-locked-lid-ignored.svg
    /usr/share/icons/hicolor/scalable/status/input-lock-unlocked-lid-ignored.svg
    /usr/share/applications/input-lock.desktop
    /usr/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team
    /usr/share/locale/he/LC_MESSAGES/input-lock.mo
)

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--user LOGIN] [--update]

Installs Input Lock for one local graphical-seat user. The daemon starts in
the UNLOCKED state. The installer never performs a test lock.

The checkout must not be writable by other users. The payload is copied to a
root-owned staging directory, compiled and validated there before the running
service is touched. If a step fails after files were replaced, the previous
installation is restored automatically from the backup.
EOF
}

log() {
    printf '%s\n' "[input-lock] $*"
}

warn() {
    printf '%s\n' "[input-lock] WARNING: $*" >&2
}

fail() {
    printf '%s\n' "[input-lock] ERROR: $*" >&2
    exit 1
}

post_warning() {
    POST_INSTALL_WARNINGS=$((POST_INSTALL_WARNINGS + 1))
    warn "post-install: $*"
}

# Invoked only from the EXIT trap.
# shellcheck disable=SC2317,SC2329
restore_backup() {
    local item failed=0
    for item in "${RESTORE_ITEMS[@]}"; do
        if [[ -n "$BACKUP_DIR" && ( -e "$BACKUP_DIR$item" || -L "$BACKUP_DIR$item" ) ]]; then
            rm -rf -- "$item" || failed=1
            cp -a -- "$BACKUP_DIR$item" "$item" || failed=1
        elif [[ -e "$item" || -L "$item" ]]; then
            # The item did not exist before this run.
            rm -rf -- "$item" || failed=1
        fi
    done
    return "$failed"
}

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2317,SC2329
cleanup() {
    local status=$?
    trap - EXIT
    set +e
    if (( status != 0 && CHANGES_STARTED == 1 && INSTALL_COMMITTED == 0 )); then
        printf '%s\n' '[input-lock] Installation failed; stopping the daemon so input remains free.' >&2
        systemctl stop input-lock.service >/dev/null 2>&1
        if [[ -z "$BACKUP_DIR" || ! -e "$BACKUP_DIR/usr/lib/systemd/system/input-lock.service" ]]; then
            # Fresh installation: do not leave an enablement link behind.
            systemctl disable input-lock.service >/dev/null 2>&1
        fi
        printf '%s\n' '[input-lock] Restoring the previous installation from the backup.' >&2
        if restore_backup; then
            printf '%s\n' '[input-lock] Previous files restored.' >&2
        else
            printf '%s\n' "[input-lock] ERROR: automatic restore was incomplete; the backup is kept at ${BACKUP_DIR:-<none>}" >&2
        fi
        systemctl daemon-reload >/dev/null 2>&1
        if (( WAS_ACTIVE == 1 )) && [[ -f /usr/lib/systemd/system/input-lock.service ]]; then
            # The daemon always starts UNLOCKED, so restarting the old version is safe.
            if systemctl start input-lock.service >/dev/null 2>&1; then
                printf '%s\n' '[input-lock] The previous input-lock.service was started again (unlocked).' >&2
            else
                printf '%s\n' '[input-lock] The previous input-lock.service did not start; input stays free.' >&2
            fi
        fi
        if (( NOTIFIER_STOPPED == 1 )); then
            run_as_target systemctl --user daemon-reload >/dev/null 2>&1
            # Restore the previous state only: a tray that was not running
            # before stays stopped (review U-R8).
            if (( NOTIFIER_WAS_ACTIVE == 1 )); then
                run_as_target systemctl --user start input-lock-notifier.service >/dev/null 2>&1
            fi
        fi
    fi
    if [[ -n "$STAGE" && -d "$STAGE" ]]; then
        rm -rf -- "$STAGE"
    fi
    exit "$status"
}

run_as_target() {
    runuser -u "$TARGET_USER" -- env \
        XDG_RUNTIME_DIR="$TARGET_RUNTIME" \
        DBUS_SESSION_BUS_ADDRESS="$TARGET_BUS" \
        HOME="$TARGET_HOME" \
        PYTHONDONTWRITEBYTECODE=1 \
        "$@"
}

# Root-run Python never imports anything from the checkout: -I ignores the
# environment, the user site and the current directory.
root_python() {
    PYTHONDONTWRITEBYTECODE=1 python3 -I -S "$@"
}

collect_payload() {
    local path lang
    PAYLOAD=("${PAYLOAD_FIXED[@]}")
    for lang in "${LOCALES[@]}"; do
        PAYLOAD+=("po/$lang.po")
    done
    for path in "$PROJECT_DIR"/src/locklock_core/*.py; do
        [[ -e "$path" ]] || fail "package is incomplete: src/locklock_core"
        PAYLOAD+=("${path#"$PROJECT_DIR"/}")
    done
    while IFS= read -r -d '' path; do
        PAYLOAD+=("${path#"$PROJECT_DIR"/}")
    done < <(find "$PROJECT_DIR/docs" \( -type f -o -type l \) -print0 | sort -z)
}

# Refuse a checkout that another account could modify: root would otherwise
# execute and install code that user controls. Group write is accepted only
# for root's group or for the owner's private group (Ubuntu umask 002).
verify_source_tree() {
    root_python - "$PROJECT_DIR" "$TARGET_UID" "${PAYLOAD[@]}" <<'PY' \
        || fail 'the source tree is not safe to install from as root (see above); fix the ownership/permissions or use a root-owned copy'
import grp
import os
import pwd
import stat
import sys

root, target_uid, *relative = sys.argv[1:]
allowed = {0, int(target_uid)}
if os.environ.get("SUDO_UID", "").isdigit():
    allowed.add(int(os.environ["SUDO_UID"]))
problems = []
primary_members = {}
for entry in pwd.getpwall():
    primary_members.setdefault(entry.pw_gid, set()).add(entry.pw_name)


def private_group(st):
    if st.st_gid == 0:
        return True
    try:
        owner = pwd.getpwuid(st.st_uid)
        group = grp.getgrgid(st.st_gid)
    except KeyError:
        return False
    members = set(group.gr_mem) | primary_members.get(st.st_gid, set())
    return owner.pw_gid == st.st_gid and members <= {owner.pw_name}


def check(path, kind):
    try:
        st = os.lstat(path)
    except OSError as exc:
        problems.append(f"{path}: {exc.strerror}")
        return
    if stat.S_ISLNK(st.st_mode):
        problems.append(f"{path}: symbolic links are not accepted in the payload")
        return
    if kind == "file" and not stat.S_ISREG(st.st_mode):
        problems.append(f"{path}: not a regular file")
    if kind == "dir" and not stat.S_ISDIR(st.st_mode):
        problems.append(f"{path}: not a directory")
    if st.st_uid not in allowed:
        problems.append(f"{path}: owned by UID {st.st_uid}")
    sticky_root_dir = stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX and st.st_uid == 0
    if st.st_mode & stat.S_IWOTH and not sticky_root_dir:
        problems.append(f"{path}: writable by every user")
    if st.st_mode & stat.S_IWGRP and not sticky_root_dir and not private_group(st):
        problems.append(f"{path}: writable by shared group GID {st.st_gid}")


directories = set()
current = os.path.abspath(root)
while True:
    directories.add(current)
    parent = os.path.dirname(current)
    if parent == current:
        break
    current = parent
for item in relative:
    parent = os.path.dirname(os.path.join(root, item))
    while len(parent) > len(root):
        directories.add(parent)
        parent = os.path.dirname(parent)
for directory in sorted(directories):
    check(directory, "dir")
for item in relative:
    check(os.path.join(root, item), "file")
for problem in problems:
    print("[input-lock] unsafe source: " + problem, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
}

stage_payload() {
    local item
    STAGE=$(mktemp -d /run/input-lock-install.XXXXXXXX)
    chmod 0700 "$STAGE"
    for item in "${PAYLOAD[@]}"; do
        install -D -m 0644 -- "$PROJECT_DIR/$item" "$STAGE/$item"
    done
}

# Everything that can be checked without touching the system is checked
# before the running service is stopped.
validate_stage() {
    local item
    local python_files=()
    for item in "$STAGE"/src/*.py "$STAGE"/src/locklock_core/*.py; do
        python_files+=("$item")
    done
    for item in "${PYTHON_PROGRAMS[@]}"; do
        python_files+=("$STAGE/$item")
    done
    python_files+=("$STAGE/tools/po_compile.py")
    root_python - "$STAGE/.compiled" "${python_files[@]}" <<'PY' \
        || fail 'the Python payload does not compile; nothing was changed'
import py_compile
import sys
from pathlib import Path

output = Path(sys.argv[1])
output.mkdir(mode=0o700)
for index, source in enumerate(sys.argv[2:]):
    py_compile.compile(source, cfile=str(output / f"{index}.pyc"), doraise=True)
PY
    for item in "${LOCALES[@]}"; do
        compile_catalog "$STAGE/po/$item.po" "$STAGE/locale/$item/LC_MESSAGES/input-lock.mo" \
            || fail "po/$item.po does not compile; nothing was changed"
    done
    root_python - "$STAGE/locale" "${LOCALES[@]}" <<'PY' \
        || fail 'a compiled translation catalogue is invalid; nothing was changed'
import gettext
import sys

root, *languages = sys.argv[1:]
for language in languages:
    with open(f"{root}/{language}/LC_MESSAGES/input-lock.mo", "rb") as handle:
        catalogue = gettext.GNUTranslations(handle)
    if catalogue.info().get("language") != language or catalogue.gettext("Input Lock") == "Input Lock":
        raise SystemExit(f"{language}: catalogue header or content is wrong")
PY
    bash -n "$STAGE/src/input-lock-launch" || fail 'src/input-lock-launch has a shell syntax error'
    sh -n "$STAGE/integration/input-lock-system-sleep" || fail 'the system-sleep hook has a shell syntax error'
    root_python -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' \
        "$STAGE/gnome-extension/metadata.json" || fail 'gnome-extension/metadata.json is not valid JSON'

    APP_VERSION=$(tr -d '\r\n' < "$STAGE/VERSION")
    [[ -n "$APP_VERSION" ]] || fail 'VERSION is empty'

    # The configuration that will be in effect: the preserved one on an
    # update, or the rendered template on a fresh install.
    if [[ -f /etc/input-lock/input-lock.conf ]]; then
        log 'Preserving existing /etc/input-lock/input-lock.conf.'
        cp -- /etc/input-lock/input-lock.conf "$STAGE/effective.conf"
    else
        sed \
            -e "s/@AUTHORIZED_UID@/$TARGET_UID/g" \
            -e "s/@AUTHORIZED_USER@/$TARGET_USER/g" \
            "$STAGE/config/input-lock.conf" > "$STAGE/effective.conf"
    fi
    local summary
    summary=$(root_python -c '
import sys
sys.path.insert(0, sys.argv[1])
from input_lock_common import load_config
config = load_config(sys.argv[2])
print(config.authorized_uid)
print(config.socket_path)
' "$STAGE/src" "$STAGE/effective.conf") \
        || fail 'the configuration is invalid for this version (see above); correct /etc/input-lock/input-lock.conf before updating'
    local effective_uid
    effective_uid=$(sed -n '1p' <<<"$summary")
    SOCKET_PATH=$(sed -n '2p' <<<"$summary")
    [[ "$effective_uid" == "$TARGET_UID" ]] \
        || fail "existing configuration authorizes UID $effective_uid, not target UID $TARGET_UID"
    [[ "$SOCKET_PATH" == /* ]] || fail 'cannot determine the daemon socket path'
}

# Compile one .po catalogue inside the stage. msgfmt --check also validates
# the python-brace-format placeholders; the stdlib fallback keeps hosts
# without the gettext tools installable.
compile_catalog() {
    local source=$1 target=$2
    install -d -m 0755 "$(dirname -- "$target")"
    if command -v msgfmt >/dev/null 2>&1; then
        msgfmt --check -o "$target" "$source"
    else
        root_python "$STAGE/tools/po_compile.py" "$source" "$target"
    fi
}

install_packages() {
    local packages=(
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
    local missing=() package status
    for package in "${packages[@]}"; do
        status=$(dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null || true)
        [[ "$status" == 'ii '* ]] || missing+=("$package")
    done
    if ((${#missing[@]})); then
        log "Installing required Ubuntu packages: ${missing[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    fi
}

backup_existing() {
    local item
    BACKUP_DIR="/var/backups/input-lock/$(date -u +%Y%m%dT%H%M%SZ)"
    for item in "${BACKUP_ITEMS[@]}"; do
        if [[ -e "$item" || -L "$item" ]]; then
            install -d -m 0700 "$BACKUP_DIR"
            cp -a --parents -- "$item" "$BACKUP_DIR/"
        fi
    done
}

install_files() {
    local item
    install -d -m 0755 /usr/lib/input-lock /etc/input-lock /usr/share/doc/input-lock
    install -d -m 0755 /usr/share/polkit-1/rules.d
    install -m 0644 "$STAGE/config/90-input-lock.rules" /usr/share/polkit-1/rules.d/90-input-lock.rules
    for item in "${LIB_MODULES[@]}"; do
        install -m 0644 "$STAGE/src/$item" "/usr/lib/input-lock/$item"
    done
    install -d -m 0755 /usr/lib/input-lock/locklock_core
    install -m 0644 "$STAGE/src/locklock_core/"*.py /usr/lib/input-lock/locklock_core/
    for item in "${LIB_PROGRAMS[@]}"; do
        install -m 0755 "$STAGE/src/$item" "/usr/lib/input-lock/$item"
    done
    install -m 0755 "$STAGE/src/input-lock" /usr/bin/input-lock
    install -m 0755 "$STAGE/src/input-lock-launch" /usr/bin/input-lock-launch
    install -d -m 0755 /usr/share/applications
    install -m 0644 "$STAGE/assets/input-lock.desktop" /usr/share/applications/input-lock.desktop
    install -d -m 0755 /usr/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team
    install -m 0644 \
        "$STAGE/gnome-extension/metadata.json" \
        "$STAGE/gnome-extension/extension.js" \
        /usr/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team/
    install -d -m 0755 /usr/share/icons/hicolor/scalable/status
    install -m 0644 \
        "$STAGE/assets/input-lock-locked.svg" \
        "$STAGE/assets/input-lock-unlocked.svg" \
        "$STAGE/assets/input-lock-locked-lid-ignored.svg" \
        "$STAGE/assets/input-lock-unlocked-lid-ignored.svg" \
        /usr/share/icons/hicolor/scalable/status/
    install -d -m 0755 /usr/share/pixmaps
    install -m 0644 "$STAGE/assets/aag-projects-team.png" /usr/share/pixmaps/input-lock-about.png
    for item in "${LOCALES[@]}"; do
        install -D -m 0644 "$STAGE/locale/$item/LC_MESSAGES/input-lock.mo" \
            "/usr/share/locale/$item/LC_MESSAGES/input-lock.mo"
    done
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache --quiet --force /usr/share/icons/hicolor || true
    fi

    if [[ ! -f /etc/input-lock/input-lock.conf ]]; then
        install -m 0644 "$STAGE/effective.conf" /etc/input-lock/input-lock.conf
    fi
    chown root:root /etc/input-lock/input-lock.conf
    chmod 0644 /etc/input-lock/input-lock.conf

    install -m 0644 "$STAGE/systemd/input-lock.service" /usr/lib/systemd/system/input-lock.service
    install -m 0644 "$STAGE/systemd/input-lock-protect.service" /usr/lib/systemd/system/input-lock-protect.service
    install -m 0644 "$STAGE/systemd/input-lock-failsafe.service" /usr/lib/systemd/system/input-lock-failsafe.service
    install -m 0644 "$STAGE/systemd/input-lock-notifier.service" /usr/lib/systemd/user/input-lock-notifier.service
    install -m 0755 "$STAGE/integration/input-lock-system-sleep" /usr/lib/systemd/system-sleep/input-lock
    # Previous cp -a releases left user-owned documentation descendants. Replace
    # this managed, already-backed-up tree instead of writing through its entries.
    rm -rf -- /usr/share/doc/input-lock
    install -d -m 0755 /usr/share/doc/input-lock
    install -m 0644 "$STAGE/README.md" /usr/share/doc/input-lock/README.md
    install -m 0644 "$STAGE/LICENSE" /usr/share/doc/input-lock/LICENSE
    cp -R --no-preserve=ownership,mode -- "$STAGE/docs/." /usr/share/doc/input-lock/
    chown -R root:root /usr/share/doc/input-lock
    chmod -R u=rwX,go=rX /usr/share/doc/input-lock

    cat > /etc/input-lock/install-state <<EOF
TARGET_USER=$TARGET_USER
TARGET_UID=$TARGET_UID
SYSTEM_USER_CREATED=$SYSTEM_USER_CREATED
VERSION=$APP_VERSION
EOF
    chown root:root /etc/input-lock/install-state
    chmod 0600 /etc/input-lock/install-state
}

# input-lock.service is Type=notify: "systemctl start" returns once the daemon
# reports READY=1. Older units (Type=simple) return earlier, so also wait for
# the control socket before declaring success.
wait_for_daemon() {
    local attempt
    for ((attempt = 0; attempt < 60; attempt++)); do
        if [[ -S "$SOCKET_PATH" ]] && systemctl is-active --quiet input-lock.service; then
            return 0
        fi
        if systemctl is-failed --quiet input-lock.service; then
            break
        fi
        sleep 0.25
    done
    journalctl --no-pager -u input-lock.service -n 50 >&2 || true
    fail "input-lock.service did not become ready (socket $SOCKET_PATH); see: systemctl status input-lock.service"
}

# Remove files that this release does not install (for example editor or
# manual *.bak-* copies). They are still in the backup directory.
prune_unmanaged() {
    local entry name keep
    local -A managed=([locklock_core]=1)
    for name in "${LIB_MODULES[@]}" "${LIB_PROGRAMS[@]}"; do
        managed[$name]=1
    done
    for entry in /usr/lib/input-lock/* /usr/lib/input-lock/.[!.]*; do
        [[ -e "$entry" || -L "$entry" ]] || continue
        name=${entry##*/}
        [[ -z "${managed[$name]:-}" ]] || continue
        log "Removing unmanaged $entry"
        rm -rf -- "$entry"
    done
    for entry in /usr/lib/input-lock/locklock_core/* /usr/lib/input-lock/locklock_core/.[!.]*; do
        [[ -e "$entry" || -L "$entry" ]] || continue
        name=${entry##*/}
        keep="$STAGE/src/locklock_core/$name"
        [[ "$name" == *.py && -f "$keep" ]] && continue
        log "Removing unmanaged $entry"
        rm -rf -- "$entry"
    done
}

setup_user_session() {
    if [[ ! -S "$TARGET_RUNTIME/bus" ]]; then
        post_warning 'notifier, GNOME shortcuts, and cursor hiding need a later ./update.sh while the user is logged in.'
        return 0
    fi
    run_as_target systemctl --user daemon-reload \
        || post_warning 'systemctl --user daemon-reload failed'
    run_as_target systemctl --user enable input-lock-notifier.service \
        || post_warning 'could not enable input-lock-notifier.service'
    # Always (re)start the tray after a successful install, even if it was
    # stopped before: without it Ignore Lid Close cannot be used (U-R8).
    run_as_target systemctl --user restart input-lock-notifier.service \
        || post_warning 'could not start the tray (input-lock-notifier.service)'
    run_as_target python3 /usr/lib/input-lock/input_lock_gnome.py install \
        || post_warning 'GNOME fallback shortcuts were not installed; the daemon hotkeys still work'

    # GNOME Shell 50 on Wayland cannot safely reload extension source in the
    # running process (its ReloadExtension D-Bus method is deprecated). Never
    # disable the live copy during an update: write the new files and let the
    # next login replace the running module atomically.
    #
    # Respect the user's choice (review U-R4): an extension listed in
    # disabled-extensions is never moved out of it, and an update never
    # enables it. Only a fresh installation, with the UUID in neither list,
    # adds it to enabled-extensions.
    local extension_state
    if extension_state=$(run_as_target python3 -I - "$PREVIOUS_INSTALL" <<'PY'
import sys

from gi.repository import Gio

uuid = "input-lock-cursor@aag-projects-team"
fresh_install = sys.argv[1] != "1"
settings = Gio.Settings.new("org.gnome.shell")
enabled = list(settings.get_strv("enabled-extensions"))
disabled = list(settings.get_strv("disabled-extensions"))
if uuid in disabled:
    print("disabled-by-user")
elif uuid in enabled:
    print("enabled")
elif fresh_install:
    settings.set_strv("enabled-extensions", [*enabled, uuid])
    Gio.Settings.sync()
    print("enabled-now")
else:
    print("not-enabled")
PY
    ); then
        case "$extension_state" in
            enabled|enabled-now)
                log 'Cursor hiding extension update will activate at the next login.'
                ;;
            disabled-by-user)
                log 'The cursor-hiding extension stays disabled, as chosen; it can be enabled from the tray Settings.'
                ;;
            *)
                log 'The cursor-hiding extension is not enabled; it can be enabled from the tray Settings.'
                ;;
        esac
    else
        post_warning 'could not read the cursor-hiding extension state'
    fi
}

main() {
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
    [[ "$TARGET_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || fail 'unsupported login-name syntax'
    id "$TARGET_USER" >/dev/null 2>&1 || fail "user does not exist: $TARGET_USER"

    if [[ -r /etc/input-lock/install-state ]]; then
        local saved_created
        saved_created=$(awk -F= '$1=="SYSTEM_USER_CREATED" {print $2}' /etc/input-lock/install-state)
        [[ "$saved_created" != 1 ]] || SYSTEM_USER_CREATED=1
    fi
    if [[ -e /etc/input-lock/install-state || -e /usr/lib/input-lock ]]; then
        PREVIOUS_INSTALL=1
    fi

    TARGET_UID=$(id -u "$TARGET_USER")
    TARGET_HOME=$(getent passwd "$TARGET_USER" | awk -F: '{print $6}')
    [[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]] || fail "home directory is unavailable for $TARGET_USER"
    TARGET_RUNTIME="/run/user/$TARGET_UID"
    TARGET_BUS="unix:path=$TARGET_RUNTIME/bus"

    log "Target desktop user: $TARGET_USER (UID $TARGET_UID)"

    if [[ -r /etc/os-release ]]; then
        # os-release is trusted system data and is used only for this comparison.
        local os_id os_version
        os_id=$(awk -F= '$1=="ID" {gsub(/"/,"",$2); print $2}' /etc/os-release)
        os_version=$(awk -F= '$1=="VERSION_ID" {gsub(/"/,"",$2); print $2}' /etc/os-release)
        if [[ "$os_id" != ubuntu || "$os_version" != 26.04 ]]; then
            warn "designed for Ubuntu 26.04; detected ${os_id:-unknown} ${os_version:-unknown}."
        fi
    fi

    # 1. Verify, stage, compile and validate. Nothing on the system changes yet.
    collect_payload
    verify_source_tree
    stage_payload
    validate_stage
    log "Payload $APP_VERSION staged and compiled."

    # Detect a real GNOME collision before changing the machine. The raw daemon
    # hotkey does not need GNOME, but installing a conflicting fallback is unsafe.
    # This runs unprivileged, as the desktop user, from the user's own checkout.
    if [[ -S "$TARGET_RUNTIME/bus" ]] && command -v gsettings >/dev/null 2>&1; then
        run_as_target python3 "$PROJECT_DIR/src/input_lock_gnome.py" check \
            || fail 'GNOME shortcut collision or inaccessible settings; resolve it before installation'
    else
        warn 'graphical D-Bus is unavailable; GNOME fallback bindings will be installed on the next update/login.'
    fi

    install_packages

    getent group input >/dev/null || groupadd --system input
    if ! id input-lockd >/dev/null 2>&1; then
        log 'Creating restricted input-lockd service account.'
        useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin input-lockd
        SYSTEM_USER_CREATED=1
    fi
    usermod --append --groups input input-lockd

    backup_existing
    if systemctl is-active --quiet input-lock.service; then
        WAS_ACTIVE=1
    fi

    # 2. From here on a failure restores the backup automatically.
    CHANGES_STARTED=1
    /usr/bin/input-lock emergency-unlock >/dev/null 2>&1 || true
    if [[ -S "$TARGET_RUNTIME/bus" ]] && run_as_target systemctl --user cat input-lock-notifier.service >/dev/null 2>&1; then
        if run_as_target systemctl --user is-active --quiet input-lock-notifier.service; then
            NOTIFIER_WAS_ACTIVE=1
        fi
        run_as_target systemctl --user stop input-lock-notifier.service
        NOTIFIER_STOPPED=1
    fi
    if systemctl cat input-lock.service >/dev/null 2>&1; then
        systemctl stop input-lock.service
    fi
    # Use the new restoration-only implementation even when upgrading old code.
    PYTHONDONTWRITEBYTECODE=1 python3 -E -s "$STAGE/src/input-lock-power-helper" disable
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

    install_files
    run_as_target install -d -m 0755 \
        "$TARGET_HOME/.local/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team"
    run_as_target install -m 0644 \
        "$PROJECT_DIR/gnome-extension/metadata.json" \
        "$PROJECT_DIR/gnome-extension/extension.js" \
        "$TARGET_HOME/.local/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team/"

    systemctl daemon-reload
    run_as_target python3 -c 'from pathlib import Path; import os; p=Path.home()/".config/input-lock/cursor-settings.json"; p.parent.mkdir(parents=True, exist_ok=True, mode=0o700); fd=None if p.exists() else os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600); fd is None or (os.write(fd, b"{\"enabled\": false, \"seconds\": 60}\n"), os.close(fd))'
    systemctl enable input-lock.service
    systemctl start input-lock.service
    wait_for_daemon

    # 3. The new daemon runs. Later problems are reported, never rolled back.
    INSTALL_COMMITTED=1
    prune_unmanaged
    setup_user_session

    if ! /usr/bin/input-lock status; then
        post_warning 'input-lock status failed; see: systemctl status input-lock.service'
    fi
    local doctor_status=0
    if [[ -S "$TARGET_RUNTIME/bus" ]]; then
        run_as_target /usr/bin/input-lock doctor || doctor_status=$?
    else
        /usr/bin/input-lock doctor || doctor_status=$?
    fi
    if (( doctor_status != 0 )); then
        post_warning 'input-lock doctor reported FAIL items above. The new version is installed and running unlocked; fix those items before the first lock test.'
    fi

    log "Installation of $APP_VERSION completed in the UNLOCKED state."
    if (( POST_INSTALL_WARNINGS > 0 )); then
        log "There were $POST_INSTALL_WARNINGS post-install warning(s); review them above."
    fi
    log 'Do not run a full lock yet. Follow README section “First safe test” with --timeout 30.'
    if [[ -d "$BACKUP_DIR" ]]; then
        log "Previous Input Lock files were backed up under $BACKUP_DIR"
    fi
    exit 0
}

main "$@"
