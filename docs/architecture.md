# Architecture

## Cross-platform boundary

`src/locklock_core/` contains platform-independent settings, hotkey, protocol,
capability and monotonic-time policy contracts. Linux keeps its proven
evdev/udev/logind/GNOME implementation. Windows uses a separate interactive
user agent for input hooks and tray UI plus a narrowly privileged service for
power operations. Platform modules may depend on the core; the core never
imports a platform module.

## Components

1. `input-lock-daemon` runs as the restricted `input-lockd` account with the input group. Processes independently granted input-device access remain outside LockLock isolation.
2. `input-lock` is an unprivileged CLI. It sends one validated JSON command over `/run/input-lock/control.sock`.
3. The daemon checks `SO_PEERCRED`; only root and `AUTHORIZED_UID` may control it.
4. `input-lock-notifier` is a user service ordered after GNOME's initialized-session target. It owns the Ayatana AppIndicator menu (English or Hebrew, see the README's Language section), holds the authenticated controller subscription and renews its heartbeat and calls `notify-send` with the user's real D-Bus environment. A missing GTK display produces a clean temporary failure and delayed systemd retry, never a GTK abort/core-dump loop.
5. `input_lock_gnome.py` installs two fallback custom keybindings without replacing the user's existing list. The emergency binding is generated from `EMERGENCY_HOTKEY` (falling back to Ctrl+Alt+Shift+F12). After changing that setting, run `python3 /usr/lib/input-lock/input_lock_gnome.py install` again (or `./update.sh`); `input-lock doctor` warns while the installed binding differs.
6. A system-sleep hook calls `input-lock prepare-sleep` before suspend (releasing input locks when `UNLOCK_ON_SUSPEND` is on; lid-ignore is kept) and `input-lock resume-sleep` after resume. The daemon also listens to login1's `PrepareForSleep` and Session `Active` changes; either source opens or closes the same sleep window. From `PrepareForSleep(true)` until resume plus one controller timeout (35 s), controller and lease deadlines are paused and the systemd watchdog is extended to 300 s (restored to 20 s afterwards), so a slow kernel freeze no longer kills the daemon or drops lid-ignore. In that window supervision keeps sampling (the safety policy still receives every update and gathers evidence), the policy takes no protective action while the OS reports that a suspend is being prepared (within its own 300 s cap), and status reports `os_sleep_in_progress`. The daemon accepts the `prepare-sleep` and `resume-sleep` IPC commands only from root (the hook). The window closes early on evidence of resume and is capped at about 120 s without a resume signal.
7. The tray's confirmed `Quit` action sends an authorized IPC shutdown request. The daemon exits cleanly after replying, its cleanup releases every grab, and then the tray exits. `input-lock.service` is `Type=notify` with `NotifyAccess=main`: the daemon sends `READY=1` once its socket, first device scan and first state snapshot exist, so `systemctl start` returns only when the CLI can connect. It uses `Restart=no`; a crash or watchdog timeout starts `input-lock-failsafe.service` through `OnFailure=` instead of restarting. The tray unit uses `Restart=on-failure`. A deliberate clean exit of either stays stopped. Stopping the tray runs `input-lock session-ended`, which releases input and ends lid-ignore; this is by design.
8. The Settings dialog stores the selected default lock kinds beside the hotkey and lid preference. Primary hotkey and tray toggles use this exact set; mouse selection deliberately includes both mouse and touchpad nodes.
9. Optional idle auto-lock uses the daemon's existing evdev stream and monotonic clock. Non-synthetic keyboard, pointer, touchpad, and touchscreen events reset the activity timestamp while unlocked. The timer is armed only for the active authorized session and locks the user-selected default kinds; it is disabled by default.
10. The GNOME 50 cursor extension reads the per-user cursor timeout and is driven by Mutter's core idle monitor instead of polling the pointer: any input counts as activity, including input injected by RustDesk or other uinput/virtual devices. The installer enables the extension only on a fresh installation where it is in neither `enabled-extensions` nor `disabled-extensions`; an update never enables it and never moves it out of `disabled-extensions`. It balances `CursorTracker.inhibit_cursor_visibility()` with exactly one `uninhibit_cursor_visibility()`; activity restores the cursor and starts a new interval, while disabling or unloading the extension always restores it.
11. `input_lock_safety.py` is both the daemon's safety policy (imported) and the root helper run by `input-lock-protect.service` (`protect`) and `input-lock-failsafe.service` (`recover`, then `record-failure`). The helper writes its receipt to the root-owned `/var/lib/input-lock-protection/safety-action-result.json` (plus a copy in `/var/lib/input-lock/`); the daemon reads the root-owned one first. `input_lock_safety.py check` prints the readiness probe, and `acknowledge-failure` archives an active failure latch.

## Control protocol (2.0.0-beta.5)

- **Version.** The protocol is version 3 (`input_lock_common.PROTOCOL_VERSION`). The daemon reports `"protocol": 3` in `status` responses and state snapshots. The CLI prints one warning when the running daemon reports another version or none (typically an old daemon still running after an update); `input-lock doctor` reports it as a WARNING. Restart `input-lock.service` to clear it.
- **Hotkeys.** `HOTKEY`, `EMERGENCY_HOTKEY`, IPC `set-hotkey` and `input-lock set-hotkey` share one parser, `input_lock_common.canonical_hotkey`: at least two of CTRL/ALT/SHIFT/SUPER plus exactly one key A–Z, 0–9 or F1–F12, returned in canonical order (for example `CTRL+ALT+SHIFT+F12`). The two shortcuts must differ after canonicalisation. An invalid value makes the configuration invalid, and doctor reports FAIL for an invalid emergency hotkey. `input-lock set-hotkey` fails with a clear error when the GNOME bindings are not installed.
- **Connecting.** The CLI retries a missing (`ENOENT`) or refusing (`ECONNREFUSED`) socket for up to 5 s while `input-lock.service` is activating or active, then names the socket path and suggests `systemctl status input-lock.service`.
- **Internal commands.** `gnome-hotkey`, `gnome-emergency`, `session-ended`, `prepare-sleep` and `resume-sleep` are hidden from `--help`. `prepare-sleep` and `resume-sleep` are sent only by the root-run system-sleep hook; the daemon refuses them from other peers, and the CLI run by hand as a normal user explains this and exits with status 1. They, and `emergency-unlock`, never depend on a parseable configuration: if it cannot be parsed they use the default socket `/run/input-lock/control.sock` and say so on stderr.
- **Lid-ignore disable reasons.** The daemon logs `event=LID_IGNORE_DISABLED reason=… requested_by_user=…` and publishes `last_lid_disable` (`reason`, `requested_by_user`, `user_initiated`, `at`) in its state; `input-lock status` prints it. The tray maps it as follows:
  - `requested_by_user` (`input-lock lid-ignore off`, the tray's own toggle, or a settings change that resets the locks — hotkey, lock kinds, allowed keys, reload — requested by the tray or the CLI): no notification.
  - `user_initiated` without `requested_by_user` (a side effect of another user action: the physical emergency hotkey, the GNOME emergency shortcut, the TTY recovery chord): a short informational notification naming that action. The tray's own Unlock/Quit action is also informational.
  - Neither: a critical, persistent notification naming the reason, with a "Re-enable…" action that goes through the confirmation dialog and needs an open lid.
  - A daemon without `user_initiated` gets the previous mapping (critical unless requested).

  When the daemon drops the controller it uses `controller-lease-expired` (lease expiry) or `controller-disconnected` (closed connection) and first sends the final state event. If that event is lost, the tray reports `daemon-unavailable`; on resubscribe, a newer `last_lid_disable.at` makes it show one corrected notice with the real reason. Lid-ignore is never re-armed automatically.
- **Controller.** One tray controller is accepted; a refused controller claim returns `ok:false` with a reason.
- **Lock cooldown.** After an emergency unlock by the emergency hotkey, the GNOME emergency shortcut (`gnome-emergency`) or the TTY recovery chord, IPC `lock` and `lock-default` are refused for 60 s, and so are `toggle`, `toggle-default` and `gnome-hotkey` while nothing is locked (with something locked they unlock). The refusal logs `event=LOCK_REFUSED_COOLDOWN` with the requester's PID and UID, so other same-user code cannot immediately re-lock. Unlocking is never refused.
- **Sleep commands.** `prepare-sleep` and `resume-sleep` open and close the sleep window; both reply with the state and are refused for non-root peers. Status and the state snapshot carry `system_sleep_in_progress` and, inside `safety`, `os_sleep_in_progress` and `missing_categories`.
- **Removed.** The `lid_power_policy_enabled` / `lid_battery_threshold` user settings and the `set-lid-power-policy` / `set-lid-policy-health` IPC commands no longer exist; an unknown command returns `ok:false`.

## Event flow while unlocked

- libinput/GNOME and the daemon both have an evdev file descriptor.
- Neither holds an exclusive grab, so the kernel delivers each event to every listener.
- The daemon updates only an in-memory set of pressed numeric key codes and compares it with fixed chord definitions.
- Unlocked input is not forwarded. While keyboard input is grabbed, only user-approved allowed keys are forwarded through a filtered virtual keyboard. No typed text is decoded, stored or logged.

## Lock transition

1. A raw chord or authorized IPC request identifies an action and a fixed mode.
2. A chord waits until all chord keys are released.
3. A direct lock waits up to two seconds for a normal Enter/key/button release, then rejects a target event node that still reports an active key/button.
4. The daemon computes every event node that must be acquired.
5. It calls `EVIOCGRAB` on all new nodes. If any call fails, every grab acquired by this transition is released and the old state remains.
6. Only after a successful transaction does it release nodes no longer needed, update `/run/input-lock/state.json`, arm the optional monotonic timer, notify subscribers, and log the state change.

## Unlock transition

The grabbed keyboard still delivers events to the daemon. After the unlock chord is fully released, the daemon calls `EVIOCGRAB(0)` through python-evdev's `ungrab()`. Because the chord's key-down and key-up events were both hidden from the compositor, no modifier remains logically pressed there.

Closing the descriptor also releases the grab. This is the last-resort property used by service stop, process crash, SIGKILL, and reboot.

## Cursor hiding

Cursor hiding is deliberately separate from input locking. It runs inside GNOME Shell because Wayland does not allow an ordinary background client to change the global compositor cursor. Settings are written atomically to `~/.config/input-lock/cursor-settings.json`; the extension monitors that directory so atomic file replacement is detected. The extension never grabs input and cannot prevent the emergency unlock shortcuts.

## Classification

udev properties are authoritative:

- `ID_INPUT_KEYBOARD=1` → keyboard
- `ID_INPUT_MOUSE=1`, `ID_INPUT_TRACKBALL=1`, `ID_INPUT_POINTINGSTICK=1` → mouse
- `ID_INPUT_TOUCHPAD=1` → touchpad
- `ID_INPUT_TOUCHSCREEN=1` → touchscreen
- capability `EV_SW/SW_LID` → lid-switch

For hardware lacking those tags, a conservative capability fallback requires a meaningful alphabetic keyboard, relative X/Y plus left button, or absolute X/Y plus touch/finger and pointer/direct input properties. A lid switch is recognized only by the exact `SW_LID` capability; power buttons, ambient-light sensors, fingerprint readers and arbitrary `ID_INPUT_KEY` nodes do not satisfy these rules.

## Lid-close suppression

Lid-ignore is a daemon-owned, generation-checked runtime lease supervised by `input_lock_safety.py`. Thermal, critical battery and bounded telemetry-loss protection remain enforced; there is no closed-lid duration deadline. GNOME session Idle-only inhibition and a separate system D-Bus power-saver cooling hold are kept by the connected user tray only while lid-ignore is effective; private bus connections release both on disable/disconnection, without blocking native safety suspend. InputLock releases its restrictions before requesting ordinary OS suspend. Its own recovery unit stops the daemon before a final attempt and never requests a second sleep while the OS sleep path is busy or has just failed (`deferred-os-sleep-in-progress`). Only a failure after InputLock's own accepted request latches arming off, until acknowledged; a latch from an earlier boot, or one followed by a successful suspend, is stale and only informational. No AAG readiness or resume receipt is required. The old AAG compatibility module was removed in 2.0.0-beta.5. See [independent lid protection](independent-lid-protection.md).

## Hotplug

The process blocks in `selectors.select()`. A udev netlink event causes one rescan; there is no periodic `/dev/input` polling. New matching nodes are grabbed during an active lock unless excluded. Removal unregisters the fd, clears its pressed state and cannot leave a grab behind.

## State semantics

`/run/input-lock/state.json` is an atomic observation, never startup authority. Persistent preference and durable forensic evidence live separately under `/var/lib/input-lock` (owned by `input-lockd`, mode 0700). The protective receipt and the failure latch live in the root-owned `/var/lib/input-lock-protection` (mode 0755, created by the helper units' `StateDirectory=`), which the daemon can read but not write. Every start leaves input and lid suppression off; an unclean previous record produces RECOVERY. Preference survives but explicit re-arm is required.

## Modes

| Command mode | Internal kinds |
|---|---|
| `keyboard` | keyboard |
| `mouse` | mouse, touchpad |
| `touchpad` | touchpad |
| `touchscreen` | touchscreen |
| `all` | keyboard, mouse, touchpad |
| `all-input` | keyboard, mouse, touchpad, touchscreen |

If one event node has several kinds, it remains grabbed until none of its kinds are locked.

## Multi-user behavior

This release supports one configured user on one local seat. IPC from other UIDs is rejected. login1 tracks local non-remote sessions of the configured UID. When none is active, the daemon unlocks and refuses a new raw primary-hotkey lock; the emergency chord remains unlock-only. New acquisitions also require current graphical eligibility. Multi-seat device isolation is not implemented.

The GNOME cursor extension restores visibility if the controller/session disappears or the runtime observation is older than five seconds. A fresh login is required to load updated extension code.
