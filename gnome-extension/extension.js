import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DEFAULT_TIMEOUT_SECONDS = 60;
const MAX_TIMEOUT_SECONDS = 86399;
// While the cursor is hidden (or hiding was refused because the InputLock
// lease was not valid), re-check the lease at this low rate. No pointer
// polling: Mutter's core idle monitor reports idle/active transitions.
const LEASE_RECHECK_SECONDS = 2;
const LEASE_MAX_AGE_SECONDS = 5;

export default class InputLockCursorHider extends Extension {
    enable() {
        this._cursorTracker = global.backend.get_cursor_tracker();
        this._idleMonitor = global.backend.get_core_idle_monitor();
        this._idleWatchId = 0;
        this._activeWatchId = 0;
        this._recheckSource = 0;
        this._reloadSource = 0;
        this._cursorHidden = false;
        this._timeoutSeconds = DEFAULT_TIMEOUT_SECONDS;
        this._runtimeState = Gio.File.new_for_path(
            '/run/input-lock/state.json');

        this._settingsDirectory = Gio.File.new_for_path(
            `${GLib.get_user_config_dir()}/input-lock`);
        this._settingsFile = this._settingsDirectory.get_child('cursor-settings.json');
        GLib.mkdir_with_parents(this._settingsDirectory.get_path(), 0o700);
        this._settingsMonitor = this._settingsDirectory.monitor_directory(
            Gio.FileMonitorFlags.NONE, null);
        this._settingsChangedId = this._settingsMonitor.connect(
            'changed', (_monitor, file) => {
                if (file?.get_basename() === 'cursor-settings.json')
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
        this._timeoutSeconds = settings.seconds;
        this._idleWatchId = this._idleMonitor.add_idle_watch(
            this._timeoutSeconds * 1000, () => this._onIdle());
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

    _onIdle() {
        // Fired once each time the session has been idle for the timeout.
        this._ensureActiveWatch();
        this._evaluateWhileIdle();
    }

    _evaluateWhileIdle() {
        if (this._leaseValid())
            this._hideCursor();
        else
            this._showCursor();
        this._ensureRecheck();
    }

    _ensureActiveWatch() {
        if (this._activeWatchId || !this._idleMonitor)
            return;
        this._activeWatchId = this._idleMonitor.add_user_active_watch(() => {
            // One-shot watch: Mutter removes it after it fires.
            this._activeWatchId = 0;
            this._stopRecheck();
            this._showCursor();
        });
    }

    _ensureRecheck() {
        if (this._recheckSource)
            return;
        this._recheckSource = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, LEASE_RECHECK_SECONDS, () => {
                if (!this._cursorTracker) {
                    this._recheckSource = 0;
                    return GLib.SOURCE_REMOVE;
                }
                if (this._leaseValid())
                    this._hideCursor();
                else
                    this._showCursor();
                return GLib.SOURCE_CONTINUE;
            });
    }

    _stopRecheck() {
        if (this._recheckSource)
            GLib.Source.remove(this._recheckSource);
        this._recheckSource = 0;
    }

    _stopWatching() {
        this._stopRecheck();
        if (this._idleMonitor) {
            if (this._idleWatchId)
                this._idleMonitor.remove_watch(this._idleWatchId);
            if (this._activeWatchId)
                this._idleMonitor.remove_watch(this._activeWatchId);
        }
        this._idleWatchId = 0;
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
