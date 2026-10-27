import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DEFAULT_TIMEOUT_SECONDS = 60;
const MAX_TIMEOUT_SECONDS = 86399;
// Mutter does not fire idle watches while any GNOME idle inhibitor is active
// (Caffeine, AAG - InputLock's own lid-ignore guard, a video player...), so the
// idle *time* is sampled instead. Sampling the core idle monitor is cheap;
// the runtime lease file is read only once the pointer is already idle.
const IDLE_POLL_MS = 500;
const LEASE_MAX_AGE_SECONDS = 5;

export default class InputLockCursorHider extends Extension {
    enable() {
        this._cursorTracker = global.backend.get_cursor_tracker();
        this._idleMonitor = global.backend.get_core_idle_monitor();
        this._activeWatchId = 0;
        this._pollSource = 0;
        this._reloadSource = 0;
        this._cursorHidden = false;
        this._timeoutMs = DEFAULT_TIMEOUT_SECONDS * 1000;
        this._runtimeState = Gio.File.new_for_path(
            '/run/input-lock/state.json');

        this._settingsDirectory = Gio.File.new_for_path(
            `${GLib.get_user_config_dir()}/input-lock`);
        this._settingsFile = this._settingsDirectory.get_child('cursor-settings.json');
        GLib.mkdir_with_parents(this._settingsDirectory.get_path(), 0o700);
        this._settingsMonitor = this._settingsDirectory.monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, null);
        this._settingsChangedId = this._settingsMonitor.connect(
            'changed', (_monitor, file, otherFile) => {
                if (file?.get_basename() === 'cursor-settings.json' ||
                    otherFile?.get_basename() === 'cursor-settings.json')
                    this._queueReload();
            });
        this._reloadSettings();
    }

    disable() {
        this._stopWatching();
        if (this._reloadSource)
            GLib.Source.remove(this._reloadSource);
        this._reloadSource = 0;
        this._showCursor();
        if (this._settingsMonitor && this._settingsChangedId)
            this._settingsMonitor.disconnect(this._settingsChangedId);
        this._settingsMonitor?.cancel();
        this._settingsChangedId = 0;
        this._settingsMonitor = null;
        this._settingsFile = null;
        this._settingsDirectory = null;
        this._runtimeState = null;
        this._idleMonitor = null;
        this._cursorTracker = null;
    }

    _readSettings() {
        try {
            const [ok, contents] = this._settingsFile.load_contents(null);
            if (!ok)
                throw new Error('settings file could not be read');
            const parsed = JSON.parse(new TextDecoder().decode(contents));
            const enabled = parsed.enabled === true;
            const seconds = Number.isInteger(parsed.seconds)
                ? Math.min(MAX_TIMEOUT_SECONDS, Math.max(1, parsed.seconds))
                : DEFAULT_TIMEOUT_SECONDS;
            return {enabled, seconds};
        } catch (_error) {
            return {enabled: false, seconds: DEFAULT_TIMEOUT_SECONDS};
        }
    }

    _reloadSettings() {
        const settings = this._readSettings();
        this._stopWatching();
        this._showCursor();
        if (!settings.enabled)
            return;
        this._timeoutMs = settings.seconds * 1000;
        this._pollSource = GLib.timeout_add(GLib.PRIORITY_DEFAULT, IDLE_POLL_MS, () => {
            if (!this._cursorTracker || !this._idleMonitor) {
                this._pollSource = 0;
                return GLib.SOURCE_REMOVE;
            }
            this._poll();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _queueReload() {
        if (this._reloadSource)
            return;
        this._reloadSource = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._reloadSource = 0;
            if (this._cursorTracker)
                this._reloadSettings();
            return GLib.SOURCE_REMOVE;
        });
    }

    _leaseValid() {
        // Restore visibility after daemon/tray loss; no persistent hidden
        // cursor survives an expired runtime observation.
        try {
            const [ok, contents] = this._runtimeState.load_contents(null);
            const state = ok ? JSON.parse(new TextDecoder().decode(contents)) : {};
            const age = Date.now() / 1000 - state.timestamp;
            return state.lid_controller_connected === true &&
                state.authorized_session_active === true &&
                age >= 0 && age <= LEASE_MAX_AGE_SECONDS;
        } catch (_error) {
            return false;
        }
    }

    _poll() {
        let idle;
        try {
            idle = this._idleMonitor.get_idletime();
        } catch (_error) {
            this._showCursor();
            return;
        }
        if (idle < this._timeoutMs) {
            this._showCursor();
            return;
        }
        if (this._leaseValid()) {
            this._hideCursor();
            this._ensureActiveWatch();
        } else {
            this._showCursor();
        }
    }

    _ensureActiveWatch() {
        if (this._activeWatchId || !this._idleMonitor)
            return;
        // Shows the pointer on the very first movement (fires even while
        // idle is inhibited); one-shot, Mutter removes it after it fires.
        this._activeWatchId = this._idleMonitor.add_user_active_watch(() => {
            this._activeWatchId = 0;
            this._showCursor();
        });
    }

    _stopWatching() {
        if (this._pollSource)
            GLib.Source.remove(this._pollSource);
        this._pollSource = 0;
        if (this._idleMonitor && this._activeWatchId)
            this._idleMonitor.remove_watch(this._activeWatchId);
        this._activeWatchId = 0;
    }

    _hideCursor() {
        if (!this._cursorHidden && this._cursorTracker) {
            this._cursorTracker.inhibit_cursor_visibility();
            this._cursorHidden = true;
        }
    }

    _showCursor() {
        if (this._cursorHidden) {
            this._cursorTracker?.uninhibit_cursor_visibility();
            this._cursorHidden = false;
        }
    }
}
