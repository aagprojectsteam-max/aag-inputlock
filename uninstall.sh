#!/usr/bin/env bash
# Input Lock uninstaller. Safe to run again after a partial run. The whole
# script is parsed before anything runs (see main at the end).
set -Eeuo pipefail
umask 022

SYSTEM_USER_CREATED=0
TARGET_USER=${SUDO_USER:-}
TARGET_UID=''
TARGET_HOME=''
EXTENSION_UUID='input-lock-cursor@aag-projects-team'
ARCHIVE_DIR="/var/backups/input-lock/uninstall-$(date -u +%Y%m%dT%H%M%SZ)"

log() {
    printf '%s\n' "[input-lock] $*"
}

warn() {
    printf '%s\n' "[input-lock] WARNING: $*" >&2
}

die() {
    printf '%s\n' "[input-lock] ERROR: $*" >&2
    exit 1
}

run_as_target() {
    runuser -u "$TARGET_USER" -- env \
        HOME="$TARGET_HOME" PYTHONDONTWRITEBYTECODE=1 \
        XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$TARGET_UID/bus" \
        "$@"
}

# Run a user-session command, starting a private D-Bus session when the user
# is not logged in (dconf writes then still reach the user's database).
run_in_user_session() {
    if [[ -S "/run/user/$TARGET_UID/bus" ]]; then
        run_as_target "$@"
    elif command -v dbus-run-session >/dev/null 2>&1; then
        run_as_target dbus-run-session -- "$@"
    else
        return 1
    fi
}

unit_known() {
    systemctl cat "$1" >/dev/null 2>&1
}

# Stop a unit and confirm it is no longer running before its files go away.
stop_and_verify() {
    local unit=$1 state attempt
    unit_known "$unit" || return 0
    systemctl stop "$unit" || warn "systemctl stop $unit reported an error"
    for ((attempt = 0; attempt < 20; attempt++)); do
        state=$(systemctl is-active "$unit" 2>/dev/null || true)
        case "$state" in
            active|activating|deactivating|reloading|refreshing) sleep 0.5 ;;
            *) return 0 ;;
        esac
    done
    die "$unit is still $state; keeping every file so it can finish. Re-run uninstall.sh afterwards."
}

archive_dir() {
    local source=$1
    [[ -e "$source" || -L "$source" ]] || return 0
    install -d -m 0700 -o root -g root "$ARCHIVE_DIR"
    cp -a -- "$source" "$ARCHIVE_DIR/"
    chown -R root:root "$ARCHIVE_DIR"
    chmod 0700 "$ARCHIVE_DIR"
    rm -rf -- "$source"
    log "Archived $source to $ARCHIVE_DIR (root-only) and removed it."
}

main() {
    (( EUID == 0 )) || {
        printf '%s\n' 'Run with: sudo ./uninstall.sh' >&2
        exit 1
    }

    if [[ -r /etc/input-lock/install-state ]]; then
        local saved_user saved_created
        saved_user=$(awk -F= '$1=="TARGET_USER" {print $2}' /etc/input-lock/install-state)
        saved_created=$(awk -F= '$1=="SYSTEM_USER_CREATED" {print $2}' /etc/input-lock/install-state)
        [[ -z "$saved_user" ]] || TARGET_USER=$saved_user
        [[ "$saved_created" != 1 ]] || SYSTEM_USER_CREATED=1
    fi

    if [[ -n "$TARGET_USER" ]] && id "$TARGET_USER" >/dev/null 2>&1; then
        TARGET_UID=$(id -u "$TARGET_USER")
        TARGET_HOME=$(getent passwd "$TARGET_USER" | awk -F: '{print $6}')
    fi

    # Stop every possible writer first. Any restoration failure aborts removal,
    # preserving helpers, journals and install metadata for another attempt.
    log 'Releasing every input device before removal.'
    if [[ -x /usr/bin/input-lock ]]; then
        /usr/bin/input-lock emergency-unlock >/dev/null 2>&1 || true
    fi
    if [[ -n "$TARGET_UID" && -S "/run/user/$TARGET_UID/bus" ]]; then
        if run_as_target systemctl --user cat input-lock-notifier.service >/dev/null 2>&1; then
            run_as_target systemctl --user stop input-lock-notifier.service \
                || warn 'could not stop input-lock-notifier.service'
        fi
    fi
    stop_and_verify input-lock.service
    # The protective-suspend and fail-safe units run as root from files this
    # script deletes; they must be stopped (and confirmed stopped) first.
    stop_and_verify input-lock-protect.service
    stop_and_verify input-lock-failsafe.service

    if [[ -x /usr/lib/input-lock/input-lock-power-helper ]]; then
        /usr/lib/input-lock/input-lock-power-helper disable
    elif [[ -f /etc/UPower/UPower.conf.d/99-zzzz-input-lock-no-auto-sleep.conf || -f /etc/UPower/UPower.conf.d/.input-lock-restore-pending.json ]]; then
        die 'restore helper is missing; retaining all recovery files.'
    fi
    if [[ -n "$TARGET_UID" && -f /usr/lib/input-lock/input_lock_lid_policy.py ]]; then
        run_in_user_session python3 /usr/lib/input-lock/input_lock_lid_policy.py disable-user \
            || die 'could not restore the user lid policy; retaining helpers for another attempt.'
    fi
    if [[ -z "$TARGET_UID" ]]; then
        die 'target user cannot be resolved; retain user recovery metadata and helpers.'
    fi

    # Re-runs: the unit may already be gone; never abort on that.
    local unit
    for unit in input-lock.service input-lock-protect.service input-lock-failsafe.service; do
        if unit_known "$unit"; then
            systemctl disable "$unit" >/dev/null 2>&1 || warn "could not disable $unit"
        fi
    done
    systemctl reset-failed input-lock.service input-lock-protect.service input-lock-failsafe.service \
        >/dev/null 2>&1 || true

    # User session: tray service, GNOME fallback shortcuts, cursor extension.
    if [[ -S "/run/user/$TARGET_UID/bus" ]]; then
        run_as_target gnome-extensions disable "$EXTENSION_UUID" >/dev/null 2>&1 || true
        run_as_target systemctl --user disable --now input-lock-notifier.service >/dev/null 2>&1 || true
    fi
    if [[ -f /usr/lib/input-lock/input_lock_gnome.py ]]; then
        run_in_user_session python3 /usr/lib/input-lock/input_lock_gnome.py remove \
            || warn 'could not remove the GNOME fallback shortcuts; remove them in Settings > Keyboard'
    fi
    # Remove the extension UUID from enabled-extensions (and disabled-extensions)
    # so GNOME does not keep a dangling entry.
    # shellcheck disable=SC2016
    run_in_user_session python3 -c '
import sys
from gi.repository import Gio
settings = Gio.Settings.new("org.gnome.shell")
uuid = sys.argv[1]
for key in ("enabled-extensions", "disabled-extensions"):
    values = list(settings.get_strv(key))
    if uuid in values:
        settings.set_strv(key, [value for value in values if value != uuid])
Gio.Settings.sync()
' "$EXTENSION_UUID" || warn "could not remove $EXTENSION_UUID from enabled-extensions"

    if [[ -n "$TARGET_HOME" ]]; then
        run_as_target rm -f -- "$TARGET_HOME/.config/systemd/user/graphical-session.target.wants/input-lock-notifier.service" || true
        run_as_target rm -rf -- "$TARGET_HOME/.local/share/gnome-shell/extensions/$EXTENSION_UUID" || true
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
    rm -rf -- "/usr/share/gnome-shell/extensions/$EXTENSION_UUID"
    rm -rf -- /usr/lib/input-lock /etc/input-lock /usr/share/doc/input-lock
    rm -rf -- /run/input-lock

    # Daemon state and the root-owned protection journal (including any
    # failure latch) are archived for review, then removed, so a later
    # reinstall does not start with a stale latch.
    archive_dir /var/lib/input-lock
    archive_dir /var/lib/input-lock-protection

    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache --quiet --force /usr/share/icons/hicolor || true
    fi

    systemctl daemon-reload
    if [[ -S "/run/user/$TARGET_UID/bus" ]]; then
        run_as_target systemctl --user daemon-reload || true
    fi

    if (( SYSTEM_USER_CREATED == 1 )) && id input-lockd >/dev/null 2>&1; then
        userdel input-lockd || warn 'could not remove the input-lockd account'
    fi

    log 'Input Lock was removed. All local input is released.'
    if [[ -d "$ARCHIVE_DIR" ]]; then
        log "State and protection records were archived under $ARCHIVE_DIR (root-only)."
    fi
    if [[ -d /var/backups/input-lock ]]; then
        log 'Installer backups remain under /var/backups/input-lock; delete them with: sudo rm -rf /var/backups/input-lock'
    fi
    if [[ -n "$TARGET_HOME" && -d "$TARGET_HOME/.config/input-lock" ]]; then
        log "Personal preferences remain in $TARGET_HOME/.config/input-lock; delete them with: rm -rf ~/.config/input-lock"
    fi
}

main "$@"
