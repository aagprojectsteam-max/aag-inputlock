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
4. `input-lock-notifier` is a user service ordered after GNOME's initialized-session target. It owns the English Ayatana AppIndicator menu, holds the authenticated controller subscription and renews its heartbeat and calls `notify-send` with the user's real D-Bus environment. A missing GTK display produces a clean temporary failure and delayed systemd retry, never a GTK abort/core-dump loop.
5. `input_lock_gnome.py` installs two fallback custom keybindings without replacing the user's existing list.
6. A system-sleep hook asks the daemon to release input before suspend. The daemon also listens to login1's `PrepareForSleep` and Session `Active` changes.
7. The tray's confirmed `Quit` action sends an authorized IPC shutdown request. The daemon exits cleanly after replying, its cleanup releases every grab, and then the tray exits. Both units use `Restart=on-failure`, so a deliberate clean exit stays stopped.
8. The Settings dialog stores the selected default lock kinds beside the hotkey and lid preference. Primary hotkey and tray toggles use this exact set; mouse selection deliberately includes both mouse and touchpad nodes.
9. Optional idle auto-lock uses the daemon's existing evdev stream and monotonic clock. Non-synthetic keyboard, pointer, touchpad, and touchscreen events reset the activity timestamp while unlocked. The timer is armed only for the active authorized session and locks the user-selected default kinds; it is disabled by default.
10. The GNOME 50 cursor extension reads the per-user cursor timeout and samples the compositor's actual pointer coordinates and button-modifier state. This deliberately avoids Mutter's general `IdleMonitor`, which can be kept awake indefinitely by remote-desktop and synthetic uinput devices. It balances `CursorTracker.inhibit_cursor_visibility()` with exactly one `uninhibit_cursor_visibility()`; real pointer movement restores the cursor and starts a new interval, while disabling or unloading the extension always restores it.
Lid-ignore is a daemon-owned, generation-checked runtime lease supervised by `input_lock_safety.py`. Thermal, critical battery and bounded telemetry-loss protection remain enforced; there is no closed-lid duration deadline. GNOME session Idle-only inhibition and a separate system D-Bus power-saver cooling hold are kept by the connected user tray only while lid-ignore is effective; private bus connections release both on disable/disconnection, without blocking native safety suspend. InputLock releases its restrictions before requesting ordinary OS suspend. Its own recovery unit stops the daemon before a final attempt; terminal failure latches arming off. No AAG readiness or resume receipt is required. The old compatibility module remains packaged only for historical callers. See [independent lid protection](independent-lid-protection.md).

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

Lid-ignore is a daemon-owned, generation-checked runtime lease supervised by `input_lock_safety.py`. Thermal, critical battery and bounded telemetry-loss protection remain enforced; there is no closed-lid duration deadline. GNOME session Idle-only inhibition and a separate system D-Bus power-saver cooling hold are kept by the connected user tray only while lid-ignore is effective; private bus connections release both on disable/disconnection, without blocking native safety suspend. InputLock releases its restrictions before requesting ordinary OS suspend. Its own recovery unit stops the daemon before a final attempt; terminal failure latches arming off. No AAG readiness or resume receipt is required. The old compatibility module remains packaged only for historical callers. See [independent lid protection](independent-lid-protection.md).

## Hotplug

The process blocks in `selectors.select()`. A udev netlink event causes one rescan; there is no periodic `/dev/input` polling. New matching nodes are grabbed during an active lock unless excluded. Removal unregisters the fd, clears its pressed state and cannot leave a grab behind.

## State semantics

`/run/input-lock/state.json` is an atomic observation, never startup authority. Persistent preference and durable forensic evidence live separately under `/var/lib/input-lock`. Every start leaves input and lid suppression off; an unclean previous record produces RECOVERY. Preference survives but explicit re-arm is required.

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
