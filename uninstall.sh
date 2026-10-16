#!/usr/bin/env bash
set -Eeuo pipefail

SYSTEM_USER_CREATED=0
TARGET_USER=${SUDO_USER:-}

log() {
    printf '%s\n' "[input-lock] $*"
}

(( EUID == 0 )) || {
    printf '%s\n' 'Run with: sudo ./uninstall.sh' >&2
    exit 1
}

if [[ -r /etc/input-lock/install-state ]]; then
    SAVED_USER=$(awk -F= '$1=="TARGET_USER" {print $2}' /etc/input-lock/install-state)
    SAVED_CREATED=$(awk -F= '$1=="SYSTEM_USER_CREATED" {print $2}' /etc/input-lock/install-state)
    [[ -n "$SAVED_USER" ]] && TARGET_USER=$SAVED_USER
    [[ "$SAVED_CREATED" == 1 ]] && SYSTEM_USER_CREATED=1
fi

TARGET_UID=''
TARGET_HOME=''
if [[ -n "$TARGET_USER" ]] && id "$TARGET_USER" >/dev/null 2>&1; then
    TARGET_UID=$(id -u "$TARGET_USER")
    TARGET_HOME=$(getent passwd "$TARGET_USER" | awk -F: '{print $6}')
fi

run_as_target() {
    runuser -u "$TARGET_USER" -- env \
        HOME="$TARGET_HOME" PYTHONDONTWRITEBYTECODE=1 \
        XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" \
        "$@"
}

# Stop every possible writer first. Any restoration failure aborts removal,
# preserving helpers, journals and install metadata for another attempt.
log 'Releasing every input device before removal.'
/usr/bin/input-lock emergency-unlock >/dev/null 2>&1 || true
if [[ -n "$TARGET_UID" && -S "/run/user/$TARGET_UID/bus" ]]; then
    if run_as_target systemctl --user cat input-lock-notifier.service >/dev/null 2>&1; then
        run_as_target systemctl --user stop input-lock-notifier.service
    fi
fi
if systemctl cat input-lock.service >/dev/null 2>&1; then
    systemctl stop input-lock.service
fi
if [[ -x /usr/lib/input-lock/input-lock-power-helper ]]; then
    /usr/lib/input-lock/input-lock-power-helper disable
elif [[ -f /etc/UPower/UPower.conf.d/99-zzzz-input-lock-no-auto-sleep.conf || -f /etc/UPower/UPower.conf.d/.input-lock-restore-pending.json ]]; then
    log 'ERROR: restore helper is missing; retaining all recovery files.'
    exit 1
fi
if [[ -n "$TARGET_UID" && -f /usr/lib/input-lock/input_lock_lid_policy.py ]]; then
    if [[ -S "/run/user/$TARGET_UID/bus" ]]; then
        run_as_target python3 /usr/lib/input-lock/input_lock_lid_policy.py disable-user
    else
        run_as_target dbus-run-session -- python3 /usr/lib/input-lock/input_lock_lid_policy.py disable-user
    fi
fi
if [[ -z "$TARGET_UID" ]]; then
    log 'ERROR: target user cannot be resolved; retain user recovery metadata and helpers.'
    exit 1
fi
systemctl disable input-lock.service

if [[ -n "$TARGET_UID" && -S "/run/user/$TARGET_UID/bus" ]]; then
    run_as_target gnome-extensions disable input-lock-cursor@aag-projects-team >/dev/null 2>&1 || true
    run_as_target systemctl --user disable --now input-lock-notifier.service >/dev/null 2>&1 || true
    if [[ -f /usr/lib/input-lock/input_lock_gnome.py ]]; then
        run_as_target python3 /usr/lib/input-lock/input_lock_gnome.py remove || true
    fi
elif [[ -n "$TARGET_UID" && -f /usr/lib/input-lock/input_lock_gnome.py ]] && command -v dbus-run-session >/dev/null 2>&1; then
    runuser -u "$TARGET_USER" -- dbus-run-session -- \
        python3 /usr/lib/input-lock/input_lock_gnome.py remove || true
fi

if [[ -n "$TARGET_USER" ]] && id "$TARGET_USER" >/dev/null 2>&1; then
    TARGET_HOME=$(getent passwd "$TARGET_USER" | awk -F: '{print $6}')
    if [[ -n "$TARGET_HOME" ]]; then
        run_as_target rm -f -- "$TARGET_HOME/.config/systemd/user/graphical-session.target.wants/input-lock-notifier.service"
        run_as_target rm -rf -- "$TARGET_HOME/.local/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team"
    fi
fi

rm -f -- \
    /usr/bin/input-lock \
    /usr/bin/input-lock-launch \
    /usr/share/polkit-1/rules.d/90-input-lock.rules \
    /usr/share/applications/input-lock.desktop \
    /usr/share/pixmaps/input-lock-about.png \
    /usr/lib/systemd/system/input-lock.service \
    /usr/lib/systemd/system/input-lock-protect.service \
    /usr/lib/systemd/system/input-lock-failsafe.service \
    /usr/lib/systemd/user/input-lock-notifier.service \
    /usr/lib/systemd/system-sleep/input-lock \
    /etc/modules-load.d/input-lock.conf \
    /usr/share/icons/hicolor/scalable/status/input-lock-locked.svg \
    /usr/share/icons/hicolor/scalable/status/input-lock-unlocked.svg \
    /usr/share/icons/hicolor/scalable/status/input-lock-locked-lid-ignored.svg \
    /usr/share/icons/hicolor/scalable/status/input-lock-unlocked-lid-ignored.svg \
    /etc/UPower/UPower.conf.d/99-zzzz-input-lock-no-auto-sleep.conf
rm -rf -- /usr/share/gnome-shell/extensions/input-lock-cursor@aag-projects-team
rm -rf -- /usr/lib/input-lock /etc/input-lock /usr/share/doc/input-lock
rm -rf -- /run/input-lock
if [[ -d /var/lib/input-lock ]]; then
    RECOVERY_ARCHIVE="/var/backups/input-lock/uninstall-$(date -u +%Y%m%dT%H%M%SZ)"
    install -d -m 0700 "$RECOVERY_ARCHIVE"
    cp -a /var/lib/input-lock "$RECOVERY_ARCHIVE/"
    chown -R root:root "$RECOVERY_ARCHIVE"
    rm -rf -- /var/lib/input-lock
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache --quiet --force /usr/share/icons/hicolor || true
fi

systemctl daemon-reload
if [[ -n "$TARGET_UID" && -S "/run/user/$TARGET_UID/bus" ]]; then
    run_as_target systemctl --user daemon-reload || true
fi

if (( SYSTEM_USER_CREATED == 1 )) && id input-lockd >/dev/null 2>&1; then
    userdel input-lockd
fi

log 'Input Lock was removed. All local input is released.'
