import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as LoginManager from 'resource:///org/gnome/shell/misc/loginManager.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const TOLERANCE = 3;
const FIT_RETRIES = 6;
const WATCHDOG_INTERVAL_MS = 1000;
const RELEASE_GRACE_MS = 250;
const SYSTEM_REPAIR_DELAY_MS = 400;
const SYSTEM_REPAIR_RETRY_MS = Object.freeze([600, 1200, 2400, 4000]);
const VERIFICATION_RETRY_MS = Object.freeze([100, 250, 500, 1000]);
const DOCK_MISSING_GRACE_MS = 1500;
const BOOTSTRAP_FOCUS_GRACE_MS = 3000;
const WORKSPACE_FOCUS_GRACE_MS = 750;
const SETTLE_DELAYS_MS = Object.freeze([50, 150, 350, 750, 1500]);
const LONG_SETTLE_DELAYS_MS = Object.freeze([
    50,
    150,
    350,
    750,
    1500,
    3000,
]);
const PHASE = Object.freeze({
    BOOTSTRAP: 'BOOTSTRAP',
    USER_ACTIVE: 'USER_ACTIVE',
    OVERVIEW: 'OVERVIEW',
    SESSION_UNAVAILABLE: 'SESSION_UNAVAILABLE',
    DISABLING: 'DISABLING',
});

export default class AAGCleanMaximizeExtension extends Extension {
    enable() {
        console.warn('[AAG Clean Maximize] FULLSCREEN_V6_LOADED');
        const resumeLockState = Boolean(
            this._lockSuspended &&
            this._byWorkspace &&
            this._byWindow
        );

        this._settings = this.getSettings();
        if (!resumeLockState) {
            this._byWorkspace = new Map();
            this._byWindow = new Map();
        }
        this._signals = [];
        this._timers = new Set();
        this._settleTimerIds = new Set();
        this._laters = global.compositor.get_laters();
        this._syncLaterId = 0;
        this._watchdogId = 0;
        this._verificationTimerId = 0;
        this._verificationAttempt = 0;
        this._lastDockWarningMs = 0;
        this._systemRepairTimerId = 0;
        this._focusGraceTimerId = 0;
        this._cleanFocusGraceUntilMs = 0;
        this._eventEpoch = 0;
        this._reconciling = false;
        this._reconcileAgain = false;
        this._applyingChrome = false;
        this._overviewVisible = Boolean(Main.overview.visible);
        this._sleeping = false;
        this._phase = PHASE.BOOTSTRAP;
        this._desiredPanelHidden = false;
        this._coveredMonitors = new Set();
        this._releaseGuards = new Map();
        this._panelRecord = null;
        this._dockRecords = new Map();
        this._dockModule = null;
        this._dockModuleUri = null;
        this._dockManager = null;
        this._dockManagerSignals = [];
        this._dockAdapterToken = 0;
        this._observedWindows = new Map();
        this._sessionSuspended = this._sessionUnavailable();
        this._keybindingsAdded = false;
        this._lockSuspended = false;
        this._disabling = false;

        /*
         * Register the shortcuts even if Shell happens to load us during a
         * lock/unlock transition.  ActionMode.NORMAL keeps them inactive on
         * the lock screen, and the callbacks still reject suspended state.
         * This prevents an ACTIVE extension with no Super+Up binding.
         */
        this._addKeybindings();

        this._connect(
            global.display,
            'notify::focus-window',
            () => {
                this._invalidate('focus-window', true);
                this._scheduleSystemRepair();
            }
        );
        this._connect(
            global.workspace_manager,
            'active-workspace-changed',
            () => {
                this._armCleanFocusGrace(WORKSPACE_FOCUS_GRACE_MS);
                this._invalidate('active-workspace', true);
                this._scheduleSystemRepair();
            }
        );
        this._connect(
            global.workspace_manager,
            'workspace-switched',
            () => {
                this._armCleanFocusGrace(WORKSPACE_FOCUS_GRACE_MS);
                this._invalidate('workspace-switched', true);
            }
        );
        this._connect(
            global.window_manager,
            'switch-workspace',
            () => {
                this._armCleanFocusGrace(WORKSPACE_FOCUS_GRACE_MS);
                this._invalidate('switch-workspace', true);
            }
        );
        this._connect(
            Main.layoutManager,
            'monitors-changed',
            () => this._resetForMonitorChange()
        );
        this._connect(
            Main.layoutManager,
            'startup-complete',
            () => this._invalidate('startup-complete', true, true)
        );
        this._connect(
            Main.layoutManager.uiGroup,
            'child-added',
            (_group, actor) => {
                this._dockActorAdded(actor);
                this._invalidate('chrome-child-added', true, true);
            }
        );
        this._connect(
            Main.layoutManager.uiGroup,
            'child-removed',
            () => this._invalidate('chrome-child-removed', true, true)
        );
        this._connect(
            global.display,
            'in-fullscreen-changed',
            () => this._invalidate('monitor-fullscreen', true)
        );
        this._connect(
            global.display,
            'window-created',
            (_display, window) => {
                this._observeWindow(window);
                this._invalidate('window-created', true);
            }
        );
        this._connect(
            global.display,
            'window-entered-monitor',
            () => this._invalidate('window-entered-monitor', true)
        );
        this._connect(
            global.display,
            'window-left-monitor',
            () => this._invalidate('window-left-monitor', true)
        );
        this._connect(
            global.display,
            'restacked',
            () => this._invalidate('restacked')
        );
        this._connect(
            global.display,
            'workareas-changed',
            () => this._invalidate('workareas-changed', true)
        );
        this._connect(
            Main.sessionMode,
            'updated',
            () => this._sessionModeUpdated()
        );
        this._connect(
            Main.screenShield,
            'active-changed',
            () => this._sessionModeUpdated()
        );
        this._connect(
            Main.screenShield,
            'locked-changed',
            () => this._sessionModeUpdated()
        );
        this._connect(Main.overview, 'showing', () => {
            this._overviewVisible = true;
            this._invalidate('overview-showing', true);
        });
        this._connect(Main.overview, 'shown', () => {
            this._overviewVisible = true;
            this._invalidate('overview-shown');
        });
        this._connect(Main.overview, 'hiding', () => {
            this._invalidate('overview-hiding', true);
        });
        this._connect(Main.overview, 'hidden', () => {
            this._overviewVisible = false;
            this._invalidate('overview-hidden', true);
            this._scheduleSystemRepair();
        });
        this._connect(
            Main.extensionManager,
            'extension-state-changed',
            () => {
                this._refreshDockAdapter();
                this._invalidate(
                    'extension-state-changed',
                    true,
                    true
                );
            }
        );

        try {
            this._loginManager = LoginManager.getLoginManager();
            this._connect(
                this._loginManager,
                'prepare-for-sleep',
                (_manager, aboutToSuspend) => {
                    this._prepareForSleep(Boolean(aboutToSuspend));
                }
            );
        } catch (error) {
            this._loginManager = null;
            console.warn(
                `[AAG Clean Maximize] prepare-for-sleep: ${error}`
            );
        }

        this._refreshDockAdapter();
        this._refreshObservedWindows();

        if (resumeLockState) {
            if (this._sessionSuspended) {
                this._sessionModeUpdated();
            } else {
                this._validateEntriesAfterUnlock();
                this._reconnectEntrySignals();
                this._armCleanFocusGrace(BOOTSTRAP_FOCUS_GRACE_MS);
                this._invalidate('lock-resume-enable', true, true);
            }
        } else if (this._sessionSuspended) {
            this._sessionModeUpdated();
        } else {
            this._armCleanFocusGrace(BOOTSTRAP_FOCUS_GRACE_MS);
            this._invalidate('enable-bootstrap', true, true);
        }
    }

    disable() {
        const preserveForLock = Boolean(
            this._sessionUnavailable() &&
            this._configuredAsEnabled()
        );

        this._disabling = true;
        this._phase = PHASE.DISABLING;
        this._eventEpoch++;
        this._removeKeybindings();
        this._cancelPanelSync();
        this._cancelSettleTimers();
        this._cancelReleaseGuards();
        this._cancelVerificationRetry();
        this._cancelSystemRepair();
        this._cancelCleanFocusGrace();
        this._stopWatchdog();
        this._cancelAllTimers();
        this._releaseChromeOwnership(true);
        this._disconnectChromeRecords();
        this._disconnectObservedWindows();
        this._dockAdapterToken++;
        this._disconnectDockManagerSignals();
        this._dockModule = null;
        this._dockModuleUri = null;
        this._dockManager = null;

        for (const [object, id] of this._signals) {
            try {
                object.disconnect(id);
            } catch {
                // The object may already be gone.
            }
        }
        this._signals = [];
        this._loginManager = null;

        if (preserveForLock) {
            for (const entry of this._byWindow.values()) {
                entry.generation++;
                entry.checkId = 0;
                entry.transitioning = false;
                this._disconnectEntrySignals(entry);
            }

            this._lockSuspended = true;
            this._sessionSuspended = true;
            this._laters = null;
            this._settings = null;
            return;
        }

        this._lockSuspended = false;
        for (const entry of [...this._byWindow.values()])
            this._dropEntry(entry, true);

        this._cancelAllTimers();
        this._byWorkspace.clear();
        this._byWindow.clear();
        this._laters = null;
        this._sessionSuspended = true;
        this._sleeping = false;
        this._settings = null;
    }

    _startClean() {
        if (this._sessionSuspended)
            return;

        const window = this._actionWindow();
        if (!this._usable(window))
            return;

        /*
         * AAG - Zmanim Umidot is a GTK4/Wayland app. Its client-side window
         * refuses the physical monitor move_resize_frame request while the
         * panel is still present (GNOME's workarea limits the frame).  Our
         * standard Clean Maximize therefore keeps retrying "Application
         * refused full size" indefinitely.  Do NOT modify the generic
         * algorithm used by other applications. For this exact application,
         * use Mutter's native fullscreen: it also hides the panel/dock, and
         * avoids the circular workarea/geometry dependency.
         *
         * GTK's fullscreened notification hides its own client titlebar;
         * Escape and F11 continue to work inside that application.
         */
        if (this._usesNativeFullscreen(window)) {
            try {
                console.warn('[AAG Clean Maximize] Using native fullscreen for this application');
                const prior = this._findEntry(window);
                if (prior)
                    this._dropEntry(prior, false);
                window.make_fullscreen();
                this._invalidate('aag-zmanim-native-fullscreen', true, true);
            } catch (error) {
                console.error('[AAG Clean Maximize] AAG Zmanim native fullscreen failed:', error);
            }
            return;
        }

        const workspace = window.get_workspace();
        if (!workspace)
            return;

        const current = this._byWindow.get(window);
        if (current) {
            if (!this._matches(current)) {
                current.needsSystemRepair = false;
                current.systemRepairAttempts = 0;
                current.generation++;
                current.transitioning = true;
                this._fit(current, 0, current.generation);
            }
            this._scheduleSync();
            return;
        }

        const old = this._byWorkspace.get(workspace);
        if (old)
            this._dropEntry(old, true);

        const entry = {
            window,
            workspace,
            restoreRect: this._rect(window.get_frame_rect()),
            wasMaximized: window.is_maximized(),
            restoreReady: !window.is_maximized(),
            targetRect: null,
            signalIds: [],
            checkId: 0,
            mismatchChecks: 0,
            needsSystemRepair: false,
            systemRepairAttempts: 0,
            transitioning: true,
            generation: 1,
        };

        this._byWorkspace.set(workspace, entry);
        this._byWindow.set(window, entry);
        this._trackEntry(entry);
        this._scheduleSync();

        if (entry.wasMaximized) {
            try {
                this._skipEffect(window);
                window.unmaximize();
                this._waitForUnmaximize(entry, 0, entry.generation);
            } catch (error) {
                console.error(`[AAG Clean Maximize] ${error}`);
                this._dropEntry(entry, false);
            }
        } else {
            this._fit(entry, 0, entry.generation);
        }
    }

    _restoreClean() {
        if (this._sessionSuspended)
            return;

        const window = this._actionWindow();

        // Super+Down restores the same AAG window from native fullscreen;
        // other windows keep their existing Clean Maximize restore logic.
        if (window && this._usesNativeFullscreen(window) && window.is_fullscreen()) {
            try {
                window.unmake_fullscreen();
                this._invalidate('aag-zmanim-native-unfullscreen', true, true);
            } catch (error) {
                console.error('[AAG Clean Maximize] AAG Zmanim native restore failed:', error);
            }
            return;
        }

        const entry = this._findEntry(window);

        if (entry) {
            this._dropEntry(entry, true);
            return;
        }

        try {
            if (window?.is_maximized()) {
                this._skipEffect(window);
                window.unmaximize();
            }
        } catch (error) {
            console.warn(`[AAG Clean Maximize] ${error}`);
        }
    }

    _actionWindow() {
        const focused = global.display.get_focus_window();
        // The normal Clean algorithm rejects fullscreen windows by design.
        // Super+Down must still find the *focused* fullscreen AAG window for
        // its dedicated native-fullscreen restoration branch.
        if (this._usesNativeFullscreen(focused) && focused.is_fullscreen())
            return focused;
        if (this._usable(focused))
            return focused;

        try {
            const workspace = global.workspace_manager.get_active_workspace();
            const windows = global.display.get_tab_list(
                Meta.TabList.NORMAL_ALL,
                workspace
            );

            for (const window of windows) {
                if (this._usable(window) && !window.minimized)
                    return window;
            }
        } catch (error) {
            console.warn(`[AAG Clean Maximize] action-window fallback: ${error}`);
        }

        return focused;
    }

    /*
     * GenoPro runs under Wine's native Wayland driver and, like AAG Zmanim,
     * cannot take the physical monitor size while the panel is present.
     * Only this exact application joins the native-fullscreen branch.
     */
    _isGenoPro(window) {
        try {
            return (window?.get_wm_class?.() ?? '').toLowerCase().includes('genopro');
        } catch {
            return false;
        }
    }

    _usesNativeFullscreen(window) {
        return this._isAAGZmanim(window) || this._isGenoPro(window);
    }

    _isAAGZmanim(window) {
        if (!window)
            return false;

        try {
            const applicationId = window.get_gtk_application_id?.() ?? '';
            const windowClass = window.get_wm_class?.() ?? '';
            const title = window.get_title?.() ?? '';
            return applicationId === 'io.aag.zmanim' ||
                windowClass === 'io.aag.zmanim' ||
                title === 'AAG - Zmanim Umidot';
        } catch {
            return false;
        }
    }

    _usable(window) {
        try {
            return Boolean(
                window &&
                window.get_window_type() === Meta.WindowType.NORMAL &&
                !window.is_fullscreen() &&
                !window.is_on_all_workspaces() &&
                (window.is_maximized() || window.allows_resize())
            );
        } catch {
            return false;
        }
    }

    _trackEntry(entry) {
        this._connectEntry(entry, 'unmanaged', () => {
            this._dropEntry(entry, false);
        });
        this._connectEntry(entry, 'workspace-changed', () => {
            this._entryMovedWorkspace(entry);
        });

        for (const signal of [
            'position-changed',
            'size-changed',
            'notify::maximized-horizontally',
            'notify::maximized-vertically',
        ]) {
            this._connectEntry(entry, signal, () => {
                this._geometryChanged(entry);
            });
        }

        for (const signal of [
            'notify::fullscreen',
            'notify::minimized',
            'shown',
        ]) {
            this._connectEntry(entry, signal, () => {
                this._invalidate(`clean-window:${signal}`, true);
                this._scheduleSystemRepair();
            });
        }
    }

    _waitForUnmaximize(entry, attempt, generation) {
        if (!this._current(entry, generation))
            return;

        if (!entry.window.is_maximized()) {
            /*
             * Wayland may clear the maximized state just before the restored
             * frame rectangle settles. Wait one more short frame, then save
             * the real geometry that Super+Down must restore.
             */
            this._timer(25, () => {
                if (!this._current(entry, generation))
                    return;

                if (entry.window.is_maximized()) {
                    this._waitForUnmaximize(
                        entry,
                        attempt + 1,
                        generation
                    );
                    return;
                }

                entry.restoreRect = this._rect(
                    entry.window.get_frame_rect()
                );
                entry.restoreReady = true;
                this._fit(entry, 0, generation);
            });
            return;
        }

        if (attempt >= 24) {
            console.warn(
                '[AAG Clean Maximize] Window did not unmaximize.'
            );
            this._dropEntry(entry, false);
            return;
        }

        this._timer(25, () => {
            this._waitForUnmaximize(entry, attempt + 1, generation);
        });
    }

    _fit(entry, attempt, generation) {
        if (!this._current(entry, generation))
            return;

        if (!this._isEntryCurrentContext(entry)) {
            /*
             * Remember the workspace, but never start/retry geometry while
             * the user is moving to another workspace or window.
             */
            entry.transitioning = false;
            this._scheduleSync();
            return;
        }

        try {
            const monitorIndex = entry.window.get_monitor();
            const monitor = global.display.get_monitor_geometry(
                monitorIndex
            );
            entry.targetRect = {
                x: monitor.x,
                y: monitor.y,
                width: monitor.width,
                height: monitor.height,
            };
            entry.transitioning = true;

            const r = entry.targetRect;
            entry.window.move_resize_frame(
                true,
                r.x,
                r.y,
                r.width,
                r.height
            );
        } catch (error) {
            console.error(`[AAG Clean Maximize] Resize failed: ${error}`);
            this._dropEntry(entry, true);
            return;
        }

        this._timer(70, () => {
            if (!this._current(entry, generation))
                return;

            if (this._matches(entry)) {
                entry.transitioning = false;
                this._scheduleSync();
            } else if (!this._isEntryCurrentContext(entry)) {
                /*
                 * The user switched workspace/window while the initial
                 * Wayland configure was settling. Never retry geometry
                 * during that transition.
                 */
                entry.transitioning = false;
                this._scheduleSync();
            } else if (attempt + 1 < FIT_RETRIES) {
                this._fit(entry, attempt + 1, generation);
            } else {
                console.warn(
                    '[AAG Clean Maximize] Application refused full size.'
                );
                entry.transitioning = false;
                this._dropEntry(entry, true);
            }
        });
    }

    _geometryChanged(entry) {
        if (
            this._sessionSuspended ||
            !this._byWindow.has(entry.window) ||
            entry.transitioning
        ) {
            return;
        }

        if (entry.needsSystemRepair) {
            this._scheduleSystemRepair();
            this._invalidate('pending-system-geometry', true);
            return;
        }

        this._cancelTimer(entry.checkId);
        entry.mismatchChecks = 0;
        const generation = entry.generation;

        entry.checkId = this._timer(180, () => {
            entry.checkId = 0;
            this._verifyGeometry(entry, 0, generation);
        });
        this._invalidate('clean-geometry', true);
    }

    _verifyGeometry(entry, attempt, generation) {
        if (
            !this._current(entry, generation) ||
            entry.transitioning ||
            this._sessionSuspended
        ) {
            return;
        }

        try {
            /*
             * Minimize and native fullscreen are reversible presentation
             * states. They must never erase the remembered Clean intent.
             */
            if (entry.window.minimized || entry.window.is_fullscreen()) {
                entry.mismatchChecks = 0;
                this._invalidate('clean-presentation-state', true);
                return;
            }

            if (
                global.workspace_manager.get_active_workspace() !==
                    entry.workspace
            ) {
                entry.mismatchChecks = 0;
                this._scheduleSync();
                return;
            }

            if (!entry.window.is_maximized() && this._matches(entry)) {
                entry.mismatchChecks = 0;
                this._scheduleSync();
                return;
            }

            entry.mismatchChecks = attempt + 1;
            if (attempt + 1 < 8) {
                entry.checkId = this._timer(180, () => {
                    entry.checkId = 0;
                    this._verifyGeometry(
                        entry,
                        attempt + 1,
                        generation
                    );
                });
                return;
            }

            /* A stable manual resize/maximize exits Clean mode. */
            this._dropEntry(entry, false);
        } catch {
            this._dropEntry(entry, false);
        }
    }

    _entryMovedWorkspace(entry) {
        if (!this._byWindow.has(entry.window))
            return;

        let workspace = null;
        try {
            if (!entry.window.is_on_all_workspaces())
                workspace = entry.window.get_workspace();
        } catch {
            // Treat it as no longer trackable.
        }

        if (!workspace) {
            this._dropEntry(entry, false);
            return;
        }

        if (this._byWorkspace.get(entry.workspace) === entry)
            this._byWorkspace.delete(entry.workspace);

        const displaced = this._byWorkspace.get(workspace);
        if (displaced && displaced !== entry)
            this._dropEntry(displaced, true);

        entry.workspace = workspace;
        this._byWorkspace.set(workspace, entry);

        // Intentionally: no resize, maximize or strut change here.
        this._scheduleSync();
    }

    _resetForMonitorChange() {
        if (
            this._sessionSuspended ||
            this._sessionUnavailable()
        ) {
            /*
             * Display power changes around lock/unlock can emit this signal.
             * Preserve the model and validate it read-only after unlock.
             */
            this._sessionModeUpdated();
            return;
        }

        /*
         * DPMS, unlock and resume can emit monitors-changed even when the
         * physical layout is unchanged. Never erase intent here and never
         * resize a background window. The reconciler validates the observed
         * geometry after Mutter has settled.
         */
        for (const entry of this._byWindow.values()) {
            entry.generation++;
            this._cancelTimer(entry.checkId);
            entry.checkId = 0;
            entry.mismatchChecks = 0;
            entry.transitioning = false;
            entry.needsSystemRepair = true;
            entry.systemRepairAttempts = 0;
        }

        this._refreshObservedWindows();
        this._armCleanFocusGrace(BOOTSTRAP_FOCUS_GRACE_MS);
        this._invalidate('monitors-changed', true, true);
        this._scheduleSystemRepair();
    }

    _scheduleSystemRepair(delay = SYSTEM_REPAIR_DELAY_MS) {
        if (
            this._disabling ||
            this._sessionSuspended ||
            this._systemRepairTimerId ||
            ![...this._byWindow.values()].some(
                entry => entry.needsSystemRepair
            )
        ) {
            return;
        }

        this._systemRepairTimerId = this._timer(delay, () => {
            this._systemRepairTimerId = 0;
            this._repairActiveCleanEntry();
        });
    }

    _cancelSystemRepair() {
        if (!this._systemRepairTimerId)
            return;

        this._cancelTimer(this._systemRepairTimerId);
        this._systemRepairTimerId = 0;
    }

    _rearmSystemRepair(entry) {
        if (
            !entry?.needsSystemRepair ||
            entry.systemRepairAttempts >=
                SYSTEM_REPAIR_RETRY_MS.length
        ) {
            return;
        }

        const delay = SYSTEM_REPAIR_RETRY_MS[
            entry.systemRepairAttempts++
        ];
        this._scheduleSystemRepair(delay);
    }

    _repairActiveCleanEntry() {
        if (
            this._disabling ||
            this._sessionUnavailable() ||
            this._overviewVisible
        ) {
            return;
        }

        const workspace = global.workspace_manager.get_active_workspace();
        const entry = this._byWorkspace.get(workspace);
        if (!entry?.needsSystemRepair || entry.transitioning)
            return;

        try {
            const focus = global.display.get_focus_window();
            const focusMatches = Boolean(
                focus &&
                (focus === entry.window ||
                    entry.window.is_ancestor_of_transient(focus))
            );

            if (
                !focusMatches ||
                entry.window.minimized ||
                entry.window.is_fullscreen() ||
                entry.window.is_maximized()
            ) {
                this._rearmSystemRepair(entry);
                return;
            }

            const monitor = global.display.get_monitor_geometry(
                entry.window.get_monitor()
            );
            entry.targetRect = {
                x: monitor.x,
                y: monitor.y,
                width: monitor.width,
                height: monitor.height,
            };

            if (this._matches(entry)) {
                entry.needsSystemRepair = false;
                entry.systemRepairAttempts = 0;
                this._invalidate('system-geometry-already-correct', true);
                return;
            }

            entry.generation++;
            entry.transitioning = true;
            this._cancelTimer(entry.checkId);
            entry.checkId = 0;
            this._repairEntryGeometry(entry, 0, entry.generation);
        } catch (error) {
            console.warn(
                `[AAG Clean Maximize] System repair: ${error}`
            );
            this._rearmSystemRepair(entry);
        }
    }

    _repairEntryGeometry(entry, attempt, generation) {
        if (!this._current(entry, generation))
            return;

        if (
            this._sessionUnavailable() ||
            global.workspace_manager.get_active_workspace() !==
                entry.workspace
        ) {
            entry.transitioning = false;
            entry.needsSystemRepair = true;
            return;
        }

        try {
            const focus = global.display.get_focus_window();
            if (
                !focus ||
                !(
                    focus === entry.window ||
                    entry.window.is_ancestor_of_transient(focus)
                ) ||
                entry.window.minimized ||
                entry.window.is_fullscreen() ||
                entry.window.is_maximized()
            ) {
                entry.transitioning = false;
                entry.needsSystemRepair = true;
                this._rearmSystemRepair(entry);
                return;
            }

            const monitor = global.display.get_monitor_geometry(
                entry.window.get_monitor()
            );
            entry.targetRect = {
                x: monitor.x,
                y: monitor.y,
                width: monitor.width,
                height: monitor.height,
            };
            const r = entry.targetRect;
            entry.window.move_resize_frame(
                true,
                r.x,
                r.y,
                r.width,
                r.height
            );
        } catch (error) {
            entry.transitioning = false;
            entry.needsSystemRepair = false;
            console.warn(
                `[AAG Clean Maximize] Repair resize failed: ${error}`
            );
            this._dropEntry(entry, false);
            return;
        }

        this._timer(90, () => {
            if (!this._current(entry, generation))
                return;

            if (this._matches(entry)) {
                entry.transitioning = false;
                entry.needsSystemRepair = false;
                entry.systemRepairAttempts = 0;
                this._invalidate('system-geometry-repaired', true);
            } else if (attempt + 1 < FIT_RETRIES) {
                this._repairEntryGeometry(
                    entry,
                    attempt + 1,
                    generation
                );
            } else {
                entry.transitioning = false;
                entry.needsSystemRepair = false;
                console.warn(
                    '[AAG Clean Maximize] System repair was refused.'
                );
                this._dropEntry(entry, false);
            }
        });
    }

    _dropEntry(entry, restore) {
        if (!entry)
            return;

        entry.generation++;
        this._cancelTimer(entry.checkId);
        entry.checkId = 0;

        if (this._byWorkspace.get(entry.workspace) === entry)
            this._byWorkspace.delete(entry.workspace);
        if (this._byWindow.get(entry.window) === entry)
            this._byWindow.delete(entry.window);

        this._disconnectEntrySignals(entry);

        if (restore)
            this._restoreGeometry(entry);

        this._scheduleSync();
    }

    _disconnectEntrySignals(entry) {
        for (const id of entry.signalIds) {
            try {
                entry.window.disconnect(id);
            } catch {
                // The window may already be closing.
            }
        }
        entry.signalIds = [];
    }

    _restoreGeometry(entry) {
        try {
            if (!entry.restoreReady || !entry.restoreRect) {
                if (entry.window.is_maximized()) {
                    this._skipEffect(entry.window);
                    entry.window.unmaximize();
                }
                return;
            }

            const r = this._safeRestoreRect(entry);

            if (entry.window.is_maximized()) {
                this._skipEffect(entry.window);
                entry.window.unmaximize();

                if (!this._disabling) {
                    this._restoreWhenUnmaximized(entry.window, r, 0);
                    return;
                }
            }

            this._moveToRestoreRect(entry.window, r);
        } catch (error) {
            console.warn(`[AAG Clean Maximize] Restore failed: ${error}`);
        }
    }

    _restoreWhenUnmaximized(window, rect, attempt) {
        if (this._disabling || this._byWindow.has(window))
            return;

        try {
            if (!window.is_maximized()) {
                if (!this._byWindow.has(window))
                    this._moveToRestoreRect(window, rect);
                return;
            }

            if (attempt >= 24) {
                console.warn(
                    '[AAG Clean Maximize] Restore unmaximize timed out.'
                );
                return;
            }

            this._timer(
                25,
                () => this._restoreWhenUnmaximized(
                    window,
                    rect,
                    attempt + 1
                )
            );
        } catch (error) {
            console.warn(`[AAG Clean Maximize] Restore failed: ${error}`);
        }
    }

    _moveToRestoreRect(window, rect) {
        window.move_resize_frame(
            false,
            rect.x,
            rect.y,
            rect.width,
            rect.height
        );
    }

    _safeRestoreRect(entry) {
        const r = this._rect(entry.restoreRect);

        try {
            const monitorIndex = entry.window.get_monitor();
            const workspace = entry.window.get_workspace() ?? entry.workspace;
            const area = workspace.get_work_area_for_monitor(monitorIndex);

            r.width = Math.max(1, Math.min(r.width, area.width));
            r.height = Math.max(1, Math.min(r.height, area.height));
            r.x = Math.max(
                area.x,
                Math.min(r.x, area.x + area.width - r.width)
            );
            r.y = Math.max(
                area.y,
                Math.min(r.y, area.y + area.height - r.height)
            );
        } catch {
            // Keep the saved rectangle if monitor data is unavailable.
        }

        return r;
    }

    _findEntry(window) {
        if (!window)
            return null;

        const direct = this._byWindow.get(window);
        if (direct)
            return direct;

        for (const entry of this._byWindow.values()) {
            try {
                if (entry.window.is_ancestor_of_transient(window))
                    return entry;
            } catch {
                // Ignore a closing window.
            }
        }
        return null;
    }

    _scheduleSync() {
        this._requestReconcile();
    }

    _invalidate(_reason, settle = false, longSettle = false) {
        if (this._disabling)
            return;

        this._requestReconcile();

        if (settle) {
            if (
                !this._verificationTimerId &&
                this._verificationAttempt >=
                    VERIFICATION_RETRY_MS.length
            ) {
                this._verificationAttempt = 0;
            }
            this._eventEpoch++;
            this._scheduleSettling(longSettle);
        }
    }

    _requestReconcile() {
        if (this._disabling || this._syncLaterId || !this._laters)
            return;

        this._syncLaterId = this._laters.add(
            Meta.LaterType.BEFORE_REDRAW,
            () => {
                this._syncLaterId = 0;
                this._reconcile();
                return GLib.SOURCE_REMOVE;
            }
        );
    }

    _scheduleSettling(longSettle = false) {
        this._cancelSettleTimers();

        const epoch = this._eventEpoch;
        const delays = longSettle
            ? LONG_SETTLE_DELAYS_MS
            : SETTLE_DELAYS_MS;

        for (const delay of delays) {
            let id = 0;
            id = this._timer(delay, () => {
                this._settleTimerIds.delete(id);
                if (this._disabling || epoch !== this._eventEpoch)
                    return;

                if (!this._sessionUnavailable())
                    this._refreshObservedWindows();
                this._requestReconcile();
            });
            this._settleTimerIds.add(id);
        }
    }

    _cancelSettleTimers() {
        if (!this._settleTimerIds)
            return;

        for (const id of [...this._settleTimerIds])
            this._cancelTimer(id);
        this._settleTimerIds.clear();
    }

    _cancelPanelSync() {
        if (!this._syncLaterId || !this._laters)
            return;

        try {
            this._laters.remove(this._syncLaterId);
        } catch {
            // It may have just completed.
        }
        this._syncLaterId = 0;
    }

    _reconcile() {
        if (this._disabling)
            return;

        if (this._reconciling) {
            this._reconcileAgain = true;
            return;
        }

        this._reconciling = true;
        let needsDeferredRetry = false;
        let passes = 0;

        try {
            do {
                passes++;
                this._reconcileAgain = false;

                const snapshot = this._takeSnapshot();
                const rawDesired = this._deriveDesired(snapshot);
                const desired = this._stabilizeDesired(
                    rawDesired,
                    snapshot
                );

                if (snapshot.phase === PHASE.SESSION_UNAVAILABLE) {
                    this._cancelReleaseGuards();
                    this._cancelVerificationRetry();
                    this._releaseChromeOwnership(true);
                    this._disconnectChromeRecords();
                    this._stopWatchdog();
                    this._phase = snapshot.phase;
                    this._desiredPanelHidden = false;
                    this._coveredMonitors = new Set();
                    continue;
                }

                this._refreshChromeRecords(snapshot);
                this._applyDesiredChrome(desired, snapshot);

                this._phase = snapshot.phase;
                this._desiredPanelHidden = desired.panelHidden;
                this._coveredMonitors = desired.coveredMonitors;
            } while (
                this._reconcileAgain &&
                !this._disabling &&
                passes < 4
            );

            needsDeferredRetry = Boolean(
                this._reconcileAgain && !this._disabling
            );
        } catch (error) {
            console.error(`[AAG Clean Maximize] Reconcile: ${error}`);
            needsDeferredRetry = !this._disabling;
        } finally {
            this._reconciling = false;
            if (needsDeferredRetry)
                this._scheduleVerificationRetry();
        }
    }

    _takeSnapshot() {
        const overviewVisible = Boolean(
            this._overviewVisible || Main.overview.visible
        );
        const sessionUnavailable = this._sessionUnavailable();

        let phase = PHASE.USER_ACTIVE;
        if (sessionUnavailable)
            phase = PHASE.SESSION_UNAVAILABLE;
        else if (overviewVisible)
            phase = PHASE.OVERVIEW;

        const monitors = Main.layoutManager.monitors ?? [];
        const primaryIndex = Number.isInteger(
            Main.layoutManager.primaryIndex
        )
            ? Main.layoutManager.primaryIndex
            : 0;
        let activeWorkspace = null;
        let focusWindow = null;
        try {
            activeWorkspace =
                global.workspace_manager.get_active_workspace();
            focusWindow = global.display.get_focus_window();
        } catch {
            phase = PHASE.SESSION_UNAVAILABLE;
        }
        const coveredMonitors = new Set();

        if (phase === PHASE.USER_ACTIVE) {
            for (let index = 0; index < monitors.length; index++) {
                try {
                    if (global.display.get_monitor_in_fullscreen(index))
                        coveredMonitors.add(index);
                } catch {
                    // GNOME 50 normally provides this for every monitor.
                }
            }

            const entry = this._byWorkspace.get(activeWorkspace);
            if (
                entry &&
                !entry.transitioning &&
                this._matches(entry) &&
                this._isEntryCurrentContext(entry, focusWindow)
            ) {
                try {
                    const monitorIndex = entry.window.get_monitor();
                    if (Number.isInteger(monitorIndex))
                        coveredMonitors.add(monitorIndex);
                } catch {
                    // A closing window simply contributes no coverage.
                }
            }
        }

        return {
            phase,
            monitors,
            primaryIndex,
            activeWorkspace,
            focusWindow,
            coveredMonitors,
        };
    }

    _deriveDesired(snapshot) {
        const active = snapshot.phase === PHASE.USER_ACTIVE;

        return {
            panelHidden: active &&
                snapshot.coveredMonitors.has(snapshot.primaryIndex),
            coveredMonitors: new Set(snapshot.coveredMonitors),
            chromeActive: active,
        };
    }

    _stabilizeDesired(rawDesired, snapshot) {
        if (snapshot.phase !== PHASE.USER_ACTIVE) {
            this._cancelReleaseGuards();
            return rawDesired;
        }

        const covered = new Set(rawDesired.coveredMonitors);
        const nowMs = GLib.get_monotonic_time() / 1000;

        for (const monitorIndex of covered)
            this._clearReleaseGuard(monitorIndex);

        for (const monitorIndex of this._coveredMonitors) {
            if (covered.has(monitorIndex))
                continue;

            let guard = this._releaseGuards.get(monitorIndex);
            if (!guard) {
                guard = {
                    startedMs: nowMs,
                    timerId: 0,
                };
                guard.timerId = this._timer(
                    RELEASE_GRACE_MS,
                    () => {
                        guard.timerId = 0;
                        this._requestReconcile();
                    }
                );
                this._releaseGuards.set(monitorIndex, guard);
            }

            if (nowMs - guard.startedMs < RELEASE_GRACE_MS) {
                covered.add(monitorIndex);
            } else {
                this._clearReleaseGuard(monitorIndex);
            }
        }

        return {
            panelHidden: covered.has(snapshot.primaryIndex),
            coveredMonitors: covered,
            chromeActive: true,
        };
    }

    _clearReleaseGuard(monitorIndex) {
        const guard = this._releaseGuards?.get(monitorIndex);
        if (!guard)
            return;

        if (guard.timerId)
            this._cancelTimer(guard.timerId);
        this._releaseGuards.delete(monitorIndex);
    }

    _cancelReleaseGuards() {
        if (!this._releaseGuards)
            return;

        for (const monitorIndex of [...this._releaseGuards.keys()])
            this._clearReleaseGuard(monitorIndex);
        this._releaseGuards.clear();
    }

    _armCleanFocusGrace(durationMs) {
        if (this._disabling)
            return;

        const nowMs = GLib.get_monotonic_time() / 1000;
        this._cleanFocusGraceUntilMs = Math.max(
            this._cleanFocusGraceUntilMs,
            nowMs + durationMs
        );

        if (this._focusGraceTimerId)
            this._cancelTimer(this._focusGraceTimerId);

        const delay = Math.max(
            1,
            Math.ceil(this._cleanFocusGraceUntilMs - nowMs)
        );
        this._focusGraceTimerId = this._timer(delay, () => {
            this._focusGraceTimerId = 0;
            this._cleanFocusGraceUntilMs = 0;
            this._requestReconcile();
        });
    }

    _cancelCleanFocusGrace() {
        if (this._focusGraceTimerId)
            this._cancelTimer(this._focusGraceTimerId);
        this._focusGraceTimerId = 0;
        this._cleanFocusGraceUntilMs = 0;
    }

    _isEntryCurrentContext(entry, focusWindow = undefined) {
        if (this._sessionUnavailable() || this._overviewVisible)
            return false;

        /*
         * Geometry operations call this helper directly, outside a state
         * snapshot.  Resolve the real focus window in that case.  Keeping
         * an explicitly supplied null distinct is important: null may be a
         * short-lived workspace/unlock transition and is handled by the
         * bounded focus grace below.
         */
        if (focusWindow === undefined) {
            try {
                focusWindow = global.display.get_focus_window();
            } catch {
                return false;
            }
        }

        try {
            if (entry.window.minimized)
                return false;

            if (
                typeof entry.window.showing_on_its_workspace ===
                    'function' &&
                !entry.window.showing_on_its_workspace()
            )
                return false;
        } catch {
            return false;
        }

        if (
            global.workspace_manager.get_active_workspace() !==
            entry.workspace
        ) {
            return false;
        }

        if (!focusWindow) {
            const nowMs = GLib.get_monotonic_time() / 1000;
            return nowMs < this._cleanFocusGraceUntilMs;
        }

        try {
            return focusWindow === entry.window ||
                entry.window.is_ancestor_of_transient(focusWindow);
        } catch {
            return false;
        }
    }

    _refreshChromeRecords(snapshot) {
        const panel = Main.layoutManager.panelBox;
        if (!this._panelRecord || this._panelRecord.actor !== panel) {
            if (this._panelRecord) {
                this._releaseActorRecord(this._panelRecord);
                this._disconnectActorRecord(this._panelRecord);
            }
            this._panelRecord = panel
                ? this._createActorRecord(panel, 'panel')
                : null;
        }

        const currentDocks = new Set(this._findDockActors());
        const nowMs = GLib.get_monotonic_time() / 1000;

        for (const actor of currentDocks) {
            if (!this._dockRecords.has(actor)) {
                this._dockRecords.set(
                    actor,
                    this._createActorRecord(actor, 'dock')
                );
            }

            this._dockRecords.get(actor).missingSinceMs = 0;
        }

        for (const [actor, record] of [...this._dockRecords]) {
            if (currentDocks.has(actor))
                continue;

            if (!record.missingSinceMs) {
                record.missingSinceMs = nowMs;
                continue;
            }

            if (
                nowMs - record.missingSinceMs <
                    DOCK_MISSING_GRACE_MS
            ) {
                continue;
            }

            if (this._actorAttachedToUiGroup(actor)) {
                record.missingSinceMs = nowMs;
                continue;
            }

            this._releaseActorRecord(record);
            this._disconnectActorRecord(record);
            this._dockRecords.delete(actor);
        }

        for (const record of this._dockRecords.values()) {
            record.monitorIndex = this._dockMonitorIndex(
                record.actor,
                snapshot
            );
        }
    }

    _actorAttachedToUiGroup(actor) {
        const root = Main.layoutManager.uiGroup;
        let current = actor;

        for (let depth = 0; current && depth < 64; depth++) {
            if (current === root)
                return true;

            try {
                current = current.get_parent?.() ?? null;
            } catch {
                return false;
            }
        }

        return false;
    }

    _isDockRootActor(actor) {
        if (!actor)
            return false;

        try {
            const name = actor.get_name?.() ?? actor.name ?? '';
            const typeName =
                actor.constructor?.$gtype?.name ??
                actor.constructor?.name ??
                '';
            return Boolean(
                actor.get_parent?.() === Main.layoutManager.uiGroup &&
                (
                    name === 'dashtodockContainer' ||
                    String(typeName).includes('DashToDock')
                )
            );
        } catch {
            return false;
        }
    }

    _dockActorAdded(actor) {
        if (
            this._disabling ||
            this._sessionUnavailable() ||
            !this._isDockRootActor(actor)
        ) {
            return;
        }

        let record = this._dockRecords.get(actor);
        if (!record) {
            record = this._createActorRecord(actor, 'dock');
            this._dockRecords.set(actor, record);
        }

        const snapshot = {
            monitors: Main.layoutManager.monitors ?? [],
        };
        record.monitorIndex = this._dockMonitorIndex(actor, snapshot);

        if (
            record.monitorIndex >= 0 &&
            this._coveredMonitors.has(record.monitorIndex)
        ) {
            this._applyingChrome = true;
            try {
                this._applyActorHidden(record, true);
            } finally {
                this._applyingChrome = false;
            }
        }
    }

    _refreshDockAdapter() {
        if (this._disabling)
            return;

        let uri = null;
        try {
            const candidates = [
                Main.extensionManager.lookup?.(
                    'ubuntu-dock@ubuntu.com'
                ),
                Main.extensionManager.lookup?.(
                    'dash-to-dock@micxgx.gmail.com'
                ),
            ].filter(Boolean);
            /* Prefer the provider that Shell has actually instantiated. */
            const extension =
                candidates.find(candidate => candidate?.stateObj) ??
                candidates[0] ??
                null;
            uri = extension?.dir
                ?.get_child('extension.js')
                ?.get_uri?.() ?? null;
        } catch {
            uri = null;
        }

        if (!uri) {
            this._dockAdapterToken++;
            this._disconnectDockManagerSignals();
            this._dockModule = null;
            this._dockModuleUri = null;
            this._dockManager = null;
            return;
        }

        if (this._dockModuleUri === uri && this._dockModule) {
            this._bindDockManager(this._dockModule.dockManager ?? null);
            return;
        }

        const token = ++this._dockAdapterToken;
        import(uri).then(module => {
            if (this._disabling || token !== this._dockAdapterToken)
                return;

            this._dockModule = module;
            this._dockModuleUri = uri;
            this._bindDockManager(module.dockManager ?? null);
            this._invalidate('dock-adapter-ready', true, true);
        }).catch(error => {
            if (this._disabling || token !== this._dockAdapterToken)
                return;

            this._disconnectDockManagerSignals();
            this._dockModule = null;
            this._dockModuleUri = null;
            this._dockManager = null;
            console.warn(
                `[AAG Clean Maximize] Ubuntu Dock adapter: ${error}`
            );
            this._invalidate('dock-adapter-fallback', true, true);
        });
    }

    _bindDockManager(manager) {
        if (this._dockManager === manager)
            return;

        this._disconnectDockManagerSignals();
        this._dockManager = manager;

        if (!manager || typeof manager.connect !== 'function')
            return;

        for (const signal of [
            'docks-destroyed',
            'docks-ready',
            'toggled',
        ]) {
            try {
                const id = manager.connect(signal, () => {
                    this._invalidate(`ubuntu-dock:${signal}`, true, true);
                });
                this._dockManagerSignals.push([manager, id]);
            } catch {
                // A point release may omit an optional manager signal.
            }
        }
    }

    _disconnectDockManagerSignals() {
        for (const [manager, id] of this._dockManagerSignals ?? []) {
            try {
                manager.disconnect(id);
            } catch {
                // Ubuntu Dock may already have destroyed the manager.
            }
        }
        this._dockManagerSignals = [];
    }

    _findDockActors() {
        try {
            const result = [];
            const found = new Set();
            const root = Main.layoutManager.uiGroup;

            const addDock = actor => {
                if (!actor || found.has(actor))
                    return false;

                try {
                    if (!this._isDockRootActor(actor))
                        return false;

                    found.add(actor);
                    result.push(actor);
                    return true;
                } catch {
                    return false;
                }
            };

            /* Prefer the live manager explicitly exported for consumers. */
            try {
                const manager =
                    this._dockModule?.dockManager ??
                    this._dockManager;
                for (const actor of manager?._allDocks ?? [])
                    addDock(actor);
            } catch {
                // Direct LayoutManager discovery below is authoritative too.
            }

            /* Every Ubuntu Dock root is a direct LayoutManager chrome child. */
            for (const actor of root?.get_children?.() ?? [])
                addDock(actor);

            /* LayoutManager's tracked chrome is a useful second source. */
            try {
                const tracked = Main.layoutManager._trackedActors;
                if (Array.isArray(tracked)) {
                    for (const item of tracked)
                        addDock(item?.actor ?? item);
                } else if (typeof tracked?.keys === 'function') {
                    for (const actor of tracked.keys())
                        addDock(actor);
                } else if (tracked?.[Symbol.iterator]) {
                    for (const item of tracked) {
                        addDock(item?.actor ?? item?.[0] ?? item);
                    }
                }
            } catch {
                // Private fallback; manager/direct-child discovery remains.
            }

            return result;
        } catch {
            return [];
        }
    }

    _dockMonitorIndex(actor, snapshot) {
        try {
            const property = actor.get_property?.('monitor-index');
            if (
                Number.isInteger(property) &&
                property >= 0 &&
                property < snapshot.monitors.length
            ) {
                return property;
            }
        } catch {
            // Try JavaScript aliases and layout detection below.
        }

        try {
            const direct = actor.monitorIndex ?? actor.monitor_index;
            if (
                Number.isInteger(direct) &&
                direct >= 0 &&
                direct < snapshot.monitors.length
            ) {
                return direct;
            }
        } catch {
            // Try layout-based detection below.
        }

        try {
            const monitor = Main.layoutManager.findMonitorForActor(actor);
            const index = snapshot.monitors.indexOf(monitor);
            if (index >= 0)
                return index;
        } catch {
            // Try the actor's transformed allocation below.
        }

        try {
            const [x, y] = actor.get_transformed_position();
            const [width, height] = actor.get_transformed_size();
            const centerX = x + width / 2;
            const centerY = y + height / 2;

            for (let index = 0; index < snapshot.monitors.length; index++) {
                const monitor = snapshot.monitors[index];
                if (
                    centerX >= monitor.x &&
                    centerX < monitor.x + monitor.width &&
                    centerY >= monitor.y &&
                    centerY < monitor.y + monitor.height
                ) {
                    return index;
                }
            }
        } catch {
            // The Dock may not yet have an allocation.
        }

        return snapshot.monitors.length === 1 ? 0 : -1;
    }

    _createActorRecord(actor, kind) {
        const record = {
            actor,
            kind,
            monitorIndex: -1,
            desiredHidden: false,
            forcedHidden: false,
            restoreVisible: false,
            visibleId: 0,
            destroyId: 0,
            destroyed: false,
            missingSinceMs: 0,
        };

        try {
            record.visibleId = actor.connect('notify::visible', () => {
                this._actorVisibilityChanged(record);
            });
            record.destroyId = actor.connect('destroy', () => {
                this._actorDestroyed(record);
            });
        } catch (error) {
            console.warn(
                `[AAG Clean Maximize] Observe ${kind}: ${error}`
            );
        }

        return record;
    }

    _actorVisibilityChanged(record) {
        if (this._disabling || record.destroyed)
            return;

        /*
         * LayoutManager and Ubuntu Dock may show tracked chrome after our
         * reconcile pass.  Re-hide an owned Dock immediately, then perform
         * the normal authoritative reconciliation as verification.
         */
        if (
            record.kind === 'dock' &&
            record.desiredHidden &&
            record.actor.visible &&
            !this._applyingChrome
        ) {
            this._applyingChrome = true;
            try {
                this._applyActorHidden(record, true);
            } finally {
                this._applyingChrome = false;
            }
        }

        if (this._applyingChrome || this._reconciling) {
            this._reconcileAgain = true;
            return;
        }

        this._invalidate(`${record.kind}-visibility`);
    }

    _actorDestroyed(record) {
        record.destroyed = true;
        record.forcedHidden = false;

        if (this._panelRecord === record)
            this._panelRecord = null;
        if (record.kind === 'dock')
            this._dockRecords?.delete(record.actor);

        this._invalidate(`${record.kind}-destroyed`, true, true);
    }

    _disconnectActorRecord(record) {
        if (!record || record.destroyed)
            return;

        for (const id of [record.visibleId, record.destroyId]) {
            if (!id)
                continue;
            try {
                record.actor.disconnect(id);
            } catch {
                // The actor may already be disappearing.
            }
        }

        record.visibleId = 0;
        record.destroyId = 0;
    }

    _applyDesiredChrome(desired, snapshot) {
        this._applyingChrome = true;

        try {
            if (this._panelRecord) {
                this._applyActorHidden(
                    this._panelRecord,
                    desired.panelHidden
                );
            }

            for (const record of this._dockRecords.values()) {
                const hide = Boolean(
                    desired.chromeActive &&
                    record.monitorIndex >= 0 &&
                    desired.coveredMonitors.has(record.monitorIndex)
                );
                this._applyActorHidden(record, hide);
            }
        } finally {
            this._applyingChrome = false;
        }

        const owned = Boolean(
            this._panelRecord?.forcedHidden ||
            [...this._dockRecords.values()].some(
                record => record.forcedHidden
            )
        );
        const hiding = Boolean(
            desired.chromeActive &&
            desired.coveredMonitors.size > 0
        );

        if (hiding || owned)
            this._ensureWatchdog();
        else
            this._stopWatchdog();

        if (!desired.chromeActive) {
            try {
                Main.layoutManager._updateVisibility?.();
            } catch {
                // GNOME will refresh on its next layout event as well.
            }
        }

        if (this._verifyDesiredChrome(desired))
            this._cancelVerificationRetry();
        else
            this._scheduleVerificationRetry();
    }

    _verifyDesiredChrome(desired) {
        try {
            if (
                desired.panelHidden &&
                (!this._panelRecord || this._panelRecord.actor.visible)
            ) {
                return false;
            }

            if (
                desired.chromeActive &&
                desired.coveredMonitors.size > 0 &&
                this._dockExpected() &&
                this._dockRecords.size === 0
            ) {
                this._warnDockDiscovery(
                    'Ubuntu Dock is enabled but no Dock actor was found.'
                );
                return false;
            }

            if (
                desired.chromeActive &&
                desired.coveredMonitors.size > 0 &&
                this._dockExpected() &&
                [...this._dockRecords.values()].some(
                    record => record.monitorIndex < 0
                ) &&
                ![...this._dockRecords.values()].some(
                    record =>
                        record.monitorIndex >= 0 &&
                        desired.coveredMonitors.has(record.monitorIndex)
                )
            ) {
                this._warnDockDiscovery(
                    'Ubuntu Dock was found but its monitor is not ready.'
                );
                return false;
            }

            for (const record of this._dockRecords.values()) {
                const shouldHide = Boolean(
                    desired.chromeActive &&
                    record.monitorIndex >= 0 &&
                    desired.coveredMonitors.has(record.monitorIndex)
                );
                if (shouldHide && record.actor.visible)
                    return false;
            }
        } catch {
            return false;
        }

        return true;
    }

    _dockExpected() {
        try {
            const enabled = global.settings.get_strv(
                'enabled-extensions'
            );
            return enabled.includes('ubuntu-dock@ubuntu.com') ||
                enabled.includes('dash-to-dock@micxgx.gmail.com');
        } catch {
            return false;
        }
    }

    _warnDockDiscovery(message) {
        const nowMs = GLib.get_monotonic_time() / 1000;
        if (
            this._lastDockWarningMs &&
            nowMs - this._lastDockWarningMs < 5000
        ) {
            return;
        }

        this._lastDockWarningMs = nowMs;
        console.warn(`[AAG Clean Maximize] ${message}`);
    }

    _scheduleVerificationRetry() {
        if (
            this._verificationTimerId ||
            this._disabling ||
            this._verificationAttempt >=
                VERIFICATION_RETRY_MS.length
        ) {
            return;
        }

        const delay =
            VERIFICATION_RETRY_MS[this._verificationAttempt++];

        this._verificationTimerId = this._timer(delay, () => {
            this._verificationTimerId = 0;
            this._requestReconcile();
        });
    }

    _cancelVerificationRetry() {
        if (this._verificationTimerId)
            this._cancelTimer(this._verificationTimerId);
        this._verificationTimerId = 0;
        this._verificationAttempt = 0;
    }

    _applyActorHidden(record, hidden) {
        if (!record || record.destroyed)
            return;

        try {
            if (hidden) {
                record.desiredHidden = true;

                if (record.actor.visible) {
                    if (!record.forcedHidden)
                        record.restoreVisible = true;

                    record.actor.hide();
                    record.forcedHidden = true;
                }
                return;
            }

            this._releaseActorRecord(record);
        } catch (error) {
            console.warn(
                `[AAG Clean Maximize] Apply ${record.kind}: ${error}`
            );
        }
    }

    _releaseActorRecord(record) {
        if (!record || record.destroyed)
            return;

        const changedByUs = record.forcedHidden;
        record.desiredHidden = false;

        try {
            if (
                changedByUs &&
                record.restoreVisible &&
                !record.actor.visible
            ) {
                record.actor.show();
                if (record.kind === 'dock')
                    record.actor._updateDashVisibility?.();
            }
        } catch {
            // A rebuilt actor no longer needs restoration from us.
        }

        record.forcedHidden = false;
        record.restoreVisible = false;
    }

    _releaseChromeOwnership(refreshLayout = false) {
        this._applyingChrome = true;

        try {
            this._releaseActorRecord(this._panelRecord);
            for (const record of this._dockRecords?.values?.() ?? [])
                this._releaseActorRecord(record);
        } finally {
            this._applyingChrome = false;
        }

        this._desiredPanelHidden = false;
        this._coveredMonitors = new Set();

        if (refreshLayout) {
            try {
                Main.layoutManager._updateVisibility?.();
            } catch {
                // Best-effort refresh during Shell transitions.
            }
        }
    }

    _disconnectChromeRecords() {
        this._disconnectActorRecord(this._panelRecord);
        this._panelRecord = null;

        for (const record of this._dockRecords?.values?.() ?? [])
            this._disconnectActorRecord(record);
        this._dockRecords?.clear?.();
    }

    _ensureWatchdog() {
        if (this._watchdogId || this._disabling)
            return;

        this._watchdogId = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT,
            WATCHDOG_INTERVAL_MS,
            () => {
                if (this._disabling) {
                    this._watchdogId = 0;
                    return GLib.SOURCE_REMOVE;
                }

                this._refreshObservedWindows();
                this._requestReconcile();
                return GLib.SOURCE_CONTINUE;
            }
        );
    }

    _stopWatchdog() {
        if (!this._watchdogId)
            return;

        try {
            GLib.Source.remove(this._watchdogId);
        } catch {
            // It may have just completed.
        }
        this._watchdogId = 0;
    }

    _refreshObservedWindows() {
        if (
            this._disabling ||
            !this._observedWindows ||
            this._sessionUnavailable()
        ) {
            return;
        }

        try {
            for (const actor of global.get_window_actors())
                this._observeWindow(actor.meta_window);
        } catch {
            // Window actors can be rebuilt during lock/resume.
        }
    }

    _observeWindow(window) {
        if (
            !window ||
            this._sessionUnavailable() ||
            this._observedWindows?.has(window)
        ) {
            return;
        }

        const ids = [];
        const connect = (signal, callback) => {
            try {
                ids.push(window.connect(signal, callback));
            } catch {
                // Feature-detect signals across Mutter point releases.
            }
        };

        for (const signal of [
            'notify::fullscreen',
            'notify::minimized',
            'workspace-changed',
            'shown',
        ]) {
            connect(signal, () => {
                this._invalidate(`window:${signal}`, true);
            });
        }

        connect('unmanaged', () => {
            this._forgetObservedWindow(window, false);
            this._invalidate('window:unmanaged', true);
        });

        this._observedWindows.set(window, ids);
    }

    _forgetObservedWindow(window, disconnect = true) {
        const ids = this._observedWindows?.get(window);
        if (!ids)
            return;

        if (disconnect) {
            for (const id of ids) {
                try {
                    window.disconnect(id);
                } catch {
                    // The window may already be unmanaged.
                }
            }
        }

        this._observedWindows.delete(window);
    }

    _disconnectObservedWindows() {
        const windows = this._observedWindows
            ? [...this._observedWindows.keys()]
            : [];

        for (const window of windows)
            this._forgetObservedWindow(window, true);
        this._observedWindows?.clear?.();
    }

    _sessionUnavailable() {
        try {
            return Boolean(
                this._sleeping ||
                Main.sessionMode.currentMode === 'unlock-dialog' ||
                Main.sessionMode.isLocked ||
                Main.screenShield?.active ||
                Main.screenShield?.locked
            );
        } catch {
            /*
             * If Shell is between modes, release our visual ownership until
             * the next session signal supplies a stable state.
             */
            return true;
        }
    }

    _sessionModeUpdated() {
        if (this._disabling)
            return;

        const suspended = this._sessionUnavailable();

        if (suspended) {
            this._sessionSuspended = true;
            this._phase = PHASE.SESSION_UNAVAILABLE;
            this._cancelPanelSync();
            this._cancelSettleTimers();
            this._cancelReleaseGuards();
            this._cancelVerificationRetry();
            this._cancelSystemRepair();
            this._cancelCleanFocusGrace();
            this._stopWatchdog();

            for (const entry of this._byWindow.values()) {
                entry.generation++;
                this._cancelTimer(entry.checkId);
                entry.checkId = 0;
                entry.transitioning = false;
                this._disconnectEntrySignals(entry);
            }

            this._cancelAllTimers();
            this._disconnectObservedWindows();

            /*
             * Never own desktop chrome on the lock screen. This also drops
             * references to Ubuntu Dock actors that may be rebuilt there.
             */
            this._releaseChromeOwnership(true);
            this._disconnectChromeRecords();
            return;
        }

        if (!this._sessionSuspended) {
            this._invalidate('session-updated', true, true);
            return;
        }

        this._sessionSuspended = false;
        this._phase = PHASE.BOOTSTRAP;
        this._overviewVisible = Boolean(Main.overview.visible);
        this._laters = global.compositor.get_laters();

        /*
         * First validate read-only. A delayed repair may resize only the
         * focused Clean window on the active workspace after Shell settles.
         */
        this._validateEntriesAfterUnlock();
        this._reconnectEntrySignals();
        this._refreshObservedWindows();

        for (const entry of this._byWindow.values()) {
            entry.needsSystemRepair = true;
            entry.systemRepairAttempts = 0;
        }

        this._addKeybindings();
        this._armCleanFocusGrace(BOOTSTRAP_FOCUS_GRACE_MS);
        this._invalidate('session-resumed', true, true);
        this._scheduleSystemRepair();
    }

    _prepareForSleep(aboutToSuspend) {
        if (this._disabling)
            return;

        this._sleeping = aboutToSuspend;
        this._sessionModeUpdated();

        if (!aboutToSuspend)
            this._invalidate('resume-from-suspend', true, true);
    }

    _validateEntriesAfterUnlock() {
        for (const entry of [...this._byWindow.values()]) {
            let workspace = null;
            let valid = false;

            try {
                workspace = entry.window.get_workspace();
                valid = Boolean(
                    workspace &&
                    !entry.window.is_on_all_workspaces() &&
                    entry.restoreReady &&
                    entry.targetRect &&
                    entry.window.get_window_type() ===
                        Meta.WindowType.NORMAL
                );
            } catch {
                valid = false;
            }

            if (!valid) {
                this._dropEntry(entry, false);
                continue;
            }

            entry.transitioning = false;
            entry.checkId = 0;
            entry.mismatchChecks = 0;

            if (workspace === entry.workspace)
                continue;

            if (this._byWorkspace.get(entry.workspace) === entry)
                this._byWorkspace.delete(entry.workspace);

            const displaced = this._byWorkspace.get(workspace);
            if (displaced && displaced !== entry)
                this._dropEntry(displaced, false);

            entry.workspace = workspace;
            this._byWorkspace.set(workspace, entry);
        }
    }

    _targetMatchesCurrentMonitor(entry) {
        try {
            const monitor = global.display.get_monitor_geometry(
                entry.window.get_monitor()
            );
            const target = entry.targetRect;

            return Math.abs(target.x - monitor.x) <= TOLERANCE &&
                Math.abs(target.y - monitor.y) <= TOLERANCE &&
                Math.abs(target.width - monitor.width) <= TOLERANCE &&
                Math.abs(target.height - monitor.height) <= TOLERANCE;
        } catch {
            return false;
        }
    }

    _reconnectEntrySignals() {
        for (const entry of this._byWindow.values()) {
            if (entry.signalIds.length === 0)
                this._trackEntry(entry);
        }
    }

    _addKeybindings() {
        if (
            this._keybindingsAdded ||
            !this._settings
        ) {
            return;
        }

        Main.wm.addKeybinding(
            'maximize-clean',
            this._settings,
            Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.NORMAL,
            () => this._startClean()
        );

        Main.wm.addKeybinding(
            'restore-clean',
            this._settings,
            Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.NORMAL,
            () => this._restoreClean()
        );

        this._keybindingsAdded = true;
    }

    _removeKeybindings() {
        if (!this._keybindingsAdded)
            return;

        Main.wm.removeKeybinding('maximize-clean');
        Main.wm.removeKeybinding('restore-clean');
        this._keybindingsAdded = false;
    }

    _configuredAsEnabled() {
        try {
            const enabled = global.settings.get_strv(
                'enabled-extensions'
            );
            const disabled = global.settings.get_strv(
                'disabled-extensions'
            );

            return enabled.includes(this.uuid) &&
                !disabled.includes(this.uuid);
        } catch {
            /*
             * During a Shell mode transition, preserving inert state is safer
             * than moving windows behind the lock screen.
             */
            return true;
        }
    }

    _matches(entry) {
        if (!entry.targetRect)
            return false;

        try {
            const a = entry.window.get_frame_rect();
            const b = entry.targetRect;
            return Math.abs(a.x - b.x) <= TOLERANCE &&
                Math.abs(a.y - b.y) <= TOLERANCE &&
                Math.abs(a.width - b.width) <= TOLERANCE &&
                Math.abs(a.height - b.height) <= TOLERANCE;
        } catch {
            return false;
        }
    }

    _current(entry, generation) {
        return this._byWindow.get(entry.window) === entry &&
            entry.generation === generation;
    }

    _rect(rect) {
        return {
            x: rect.x,
            y: rect.y,
            width: rect.width,
            height: rect.height,
        };
    }

    _connect(object, signal, callback) {
        try {
            const id = object.connect(signal, callback);
            this._signals.push([object, id]);
        } catch (error) {
            console.warn(`[AAG Clean Maximize] ${signal}: ${error}`);
        }
    }

    _connectEntry(entry, signal, callback) {
        try {
            entry.signalIds.push(entry.window.connect(signal, callback));
        } catch (error) {
            console.warn(`[AAG Clean Maximize] ${signal}: ${error}`);
        }
    }

    _skipEffect(window) {
        try {
            const actor = window.get_compositor_private();
            if (actor && typeof Main.wm.skipNextEffect === 'function')
                Main.wm.skipNextEffect(actor);
        } catch {
            // Optional cosmetic optimization.
        }
    }

    _timer(delay, callback) {
        let id = 0;
        id = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT,
            Math.max(1, delay),
            () => {
                this._timers.delete(id);
                try {
                    callback();
                } catch (error) {
                    console.error(`[AAG Clean Maximize] Timer: ${error}`);
                }
                return GLib.SOURCE_REMOVE;
            }
        );
        this._timers.add(id);
        return id;
    }

    _cancelTimer(id) {
        if (!id || !this._timers.has(id))
            return;
        try {
            GLib.Source.remove(id);
        } catch {
            // It may have just completed.
        }
        this._timers.delete(id);
    }

    _cancelAllTimers() {
        for (const id of this._timers) {
            try {
                GLib.Source.remove(id);
            } catch {
                // It may have just completed.
            }
        }
        this._timers.clear();
    }
}
