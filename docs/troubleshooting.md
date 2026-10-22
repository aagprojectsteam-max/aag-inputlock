> Current lid and power behavior is documented in [independent lid protection](independent-lid-protection.md). Persistent GNOME/UPower suppression is removed; do not recreate it.

# Troubleshooting

Start every investigation while input is released:

```bash
sudo input-lock emergency-unlock
input-lock doctor
input-lock status --json
journalctl -u input-lock.service -b --no-pager -n 200
```

Each section gives diagnosis, expected meaning, repair, and rollback.

## Shortcut does not act

Diagnosis:

```bash
python3 /usr/lib/input-lock/input_lock_gnome.py status
input-lock logs -n 100
```

Expected: both paths and bindings are printed; the journal shows `reason=primary-hotkey` or `reason=gnome-primary-hotkey`. If the raw listener missed the node, the GNOME fallback should still call IPC.

Repair:

```bash
python3 /usr/lib/input-lock/input_lock_gnome.py check
python3 /usr/lib/input-lock/input_lock_gnome.py install
sudo systemctl restart input-lock.service
```

Rollback:

```bash
python3 /usr/lib/input-lock/input_lock_gnome.py remove
sudo systemctl restart input-lock.service
```

## Keyboard locks but mouse does not, or Touchpad is absent

Diagnosis:

```bash
input-lock list-devices
sudo libinput list-devices
udevadm info --query=property --name=/dev/input/eventX | grep '^ID_INPUT'
```

Expected: the relevant event node has class `mouse`/`touchpad`. A node absent from the daemon list lacks a safe udev tag/capability match.

Repair: update the device's normal Ubuntu/libinput/udev support; do not change the daemon to grab every `event*`. As a temporary investigation, record the complete `udevadm` and `evtest` identification and report it before changing classification code.

Rollback: remove any locally-added udev rule and run:

```bash
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=input
sudo systemctl restart input-lock.service
```

## Bluetooth device is not locked

Diagnosis:

```bash
sudo udevadm monitor --udev --property --subsystem-match=input
input-lock logs -n 100
```

Expected: reconnect creates one or more `event` nodes and journal logs `Input device detected`. A grab failure intentionally leaves the new device usable.

Repair:

```bash
sudo systemctl restart systemd-udevd.service
sudo systemctl restart input-lock.service
```

Rollback: restart is reversible; no persistent setting changed.

## Notification is not displayed

Diagnosis:

```bash
systemctl --user status input-lock-notifier.service --no-pager
journalctl --user -u input-lock-notifier.service -b --no-pager -n 100
echo "$DBUS_SESSION_BUS_ADDRESS"
notify-send 'Input Lock test'
```

Expected: user service active and direct notification visible.

The unit must show `After=... gnome-session-initialized.target`. Older units
that start only after `graphical-session-pre.target` can run before GNOME has
published `DISPLAY`/`WAYLAND_DISPLAY`, producing `Can't create a
GtkStyleContext without a display connection` and repeated core dumps.

Repair:

```bash
systemctl --user daemon-reload
systemctl --user enable --now input-lock-notifier.service
```

Rollback:

```bash
systemctl --user disable --now input-lock-notifier.service
```

The lock mechanism continues without notifications.

## Cursor does not hide or return

Diagnosis:

```bash
gnome-extensions info input-lock-cursor@aag-projects-team
cat ~/.config/input-lock/cursor-settings.json
journalctl --user -b --no-pager | grep -F input-lock-cursor
```

Expected: the extension is enabled, the JSON contains an enabled boolean and a timeout from 1 through 86399 seconds, and moving the pointer or changing a pointer button restores the cursor immediately.

Repair:

```bash
gnome-extensions enable input-lock-cursor@aag-projects-team
sudo ./update.sh
```

Rollback always restores the pointer:

```bash
gnome-extensions disable input-lock-cursor@aag-projects-team
```

## Permission denied on `/dev/input`

Diagnosis:

```bash
id input-lockd
ls -l /dev/input/event*
systemctl show input-lock.service -p SupplementaryGroups -p DevicePolicy -p DeviceAllow
```

Expected: `input-lockd` includes group `input`; event nodes normally belong to `root:input`; unit allows `char-input rw`.

Repair:

```bash
sudo usermod -a -G input input-lockd
sudo systemctl restart input-lock.service
```

If the journal proves that this systemd version does not recognize `char-input`, create a unit override only after keeping SSH open:

```bash
sudo systemctl edit input-lock.service
```

Enter:

```ini
[Service]
DevicePolicy=auto
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart input-lock.service
```

Rollback:

```bash
sudo systemctl revert input-lock.service
sudo systemctl daemon-reload
sudo systemctl restart input-lock.service
```

## Daemon/service fails to start

Diagnosis:

```bash
systemctl status input-lock.service --no-pager
journalctl -u input-lock.service -b --no-pager -n 200
python3 -m py_compile /usr/lib/input-lock/*.py
```

Expected: an invalid configuration is named explicitly and no input is grabbed.

Repair:

```bash
sudoedit /etc/input-lock/input-lock.conf
sudo systemctl restart input-lock.service
input-lock doctor
```

Rollback: restore the dated copy under `/var/backups/input-lock/` or rerun the package's `update.sh`.

## `cannot connect to the Input Lock daemon at …`

The CLI retried for up to 5 seconds while `input-lock.service` was activating or active, or the unit is not running at all. The message names the socket path and the unit state.

```bash
systemctl status input-lock.service --no-pager
journalctl -u input-lock.service -b --no-pager -n 100
```

Repair: start the service (`sudo systemctl start input-lock.service`). With `Type=notify`, `systemctl start` returns once the daemon is ready. If it fails, read the journal; the daemon names an invalid configuration explicitly.

## `configuration cannot be parsed …; using the default socket`

Printed by the internal commands (GNOME shortcuts, the sleep hook, the tray's stop hook) and by `input-lock emergency-unlock`. They still reach the daemon on `/run/input-lock/control.sock`, so the emergency path keeps working, but the configuration needs repair:

```bash
sudoedit /etc/input-lock/input-lock.conf
input-lock doctor
sudo systemctl reload input-lock.service
```

## Invalid `HOTKEY` or `EMERGENCY_HOTKEY`

Since 2.0.0-beta.5 both shortcuts must have at least two of CTRL, ALT, SHIFT, SUPER plus exactly one key A–Z, 0–9 or F1–F12, and they must differ. Values such as `CTRL+ALT+SHIFT+F21`, `A+B` or `SHIFT+A` make the configuration invalid; `input-lock doctor` reports FAIL and names the setting. Correct the value, then reinstall the GNOME fallback binding so it follows `EMERGENCY_HOTKEY`:

```bash
sudoedit /etc/input-lock/input-lock.conf
sudo systemctl reload input-lock.service
python3 /usr/lib/input-lock/input_lock_gnome.py install
input-lock doctor
```

## Warning: the running daemon reports another protocol

The CLI and the running daemon come from different releases, usually because the daemon was not restarted after an update. Restart it while input is released:

```bash
sudo systemctl restart input-lock.service
input-lock doctor
```

## Installer refused the source tree, or restored the backup

`unsafe source: …` means another account could modify the checkout (other-writable files, a shared writable group, a foreign owner or a symbolic link). Fix the permissions (for example `chmod -R go-w <checkout>`) or install from a copy you own.

If the installer prints `Restoring the previous installation from the backup`, a step failed after it started replacing files. The previous files were restored from `/var/backups/input-lock/<timestamp>` and the previous service was started again if it had been running. Read the error above that line, fix it, and run `sudo ./update.sh` again.

`post-install:` warnings are printed after the new daemon is running. The installation succeeded; resolve the named item (often a doctor FAIL item, or a GNOME session that was not available) before the first lock test.

## Lid-ignore turned itself off

```bash
input-lock status
journalctl -u input-lock.service -b --no-pager | grep LID_IGNORE_DISABLED
```

`Last lid-ignore off:` and the journal show the reason and whether you requested it. An automatic disable also produced a critical notification with a "Re-enable…" action; the tray's own Unlock/Quit action only shows an informational note. Lid-ignore is never re-armed automatically; re-enable it with the lid open after checking the reason.

Typical reasons: `controller-disconnected` (the tray stopped or did not check in, also after a long resume), `session-inactive` (logout, user switch), `supervision-logind-unavailable` (logind failed for about 15 s), `emergency-unlock`, `settings-reset`, `thermal-*`, `battery-critical`, `telemetry-lost`, `supervision-journal-unwritable` (`/var/lib/input-lock` full or read-only). A manual Power Mode change does **not** turn it off: the tray restores the power-saver hold and notifies you; only a hold that is still ineffective after one retry ends lid-ignore.

## A protective suspend was deferred

```bash
journalctl -u input-lock.service -u input-lock-protect.service -u input-lock-failsafe.service -b --no-pager | grep -E 'PROTECTIVE_ACTION_DEFERRED|SLEEP_WINDOW'
```

`event=PROTECTIVE_ACTION_DEFERRED` means the root helper found the OS sleep path busy (logind `preparing`, `systemd-suspend.service` running, or it ran within the last 180 seconds and failed; in recovery also a recent success or a daemon that ended by watchdog). It requested nothing and wrote no latch, to avoid driving a second attempt into a stuck freezer. The daemon leaves `PROTECTIVE_ACTION_PENDING` (safety reason `PROTECTIVE_ACTION_DEFERRED`), lid-ignore stays off and logind's own lid policy applies. Nothing is re-armed. Open the lid, check why the OS suspend did not complete (`journalctl -u systemd-suspend.service -b`), and re-enable lid-ignore if you still need it.

While a suspend is being prepared, `input-lock status` shows `OS sleep: in progress`; supervision takes no protective action until resume plus 35 s.

## Arming refused: previous emergency suspend failed

A failure latch is written only when InputLock's own suspend request was accepted and the kernel transition then failed or was never confirmed. While it is active, arming stays blocked until an administrator reviews and acknowledges it:

```bash
input-lock doctor
/usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py check
journalctl -u input-lock-failsafe.service -b --no-pager
sudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure
```

`doctor` shows an active latch as `FAIL` (`Protection failure latch`) and a stale one as `INFO`. A latch is stale after a reboot or after a later successful suspend; it no longer blocks arming and needs no action, although acknowledging it archives it. `check` prints `{"ready": …, "errors": […], "info": […]}`. Later failures are appended to the latch, never overwritten. `acknowledge-failure` refuses while a protect, failsafe or system suspend unit is still running; it copies the latch to `/root/ARCHIVE/InputLock/` and renames it to `failure-acknowledged-<sha256>.json`, never deleting it.

## GNOME binding is not created or collides

Diagnosis:

```bash
python3 /usr/lib/input-lock/input_lock_gnome.py check
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings
```

Expected: `check` exits 0. On collision it prints the exact existing path and binding.

Repair: change or remove the conflicting shortcut in GNOME Settings, then run `install`. The helper never overwrites a foreign binding.

Rollback: run the helper's `remove` action; it preserves all foreign paths.

## New device is detected but remains usable while locked

Diagnosis:

```bash
input-lock list-devices
input-lock logs -n 100
```

Expected: journal identifies either `excluded=...`, a pressed button, permission error or `EVIOCGRAB` failure. The safe policy is to leave that new device usable.

Repair: release its buttons, verify exclusions, then unlock/relock. Do not broaden permissions to `chmod 666 /dev/input/event*`.

Rollback: restore the previous exclusion line and reload; reload itself unlocks first.

## Service does not work after Resume

Diagnosis:

```bash
input-lock status
journalctl -u input-lock.service -b --since '-10 min' --no-pager
```

Expected: a `system-suspend` or `system-sleep-hook` unlock, followed by device rediscovery. State remains unlocked by design.

Repair:

```bash
sudo systemctl restart input-lock.service
```

Rollback: no persistent change. Do not configure automatic re-lock on Resume; that would weaken recovery safety.

## Ctrl, Alt, Shift, mouse button, or drag appears stuck

First recovery:

```bash
sudo input-lock emergency-unlock
sudo systemctl restart input-lock.service
```

Expected: normal code waits for release and rejects a direct lock while a target key/button is down. A stuck state suggests an event overflow, external remapper, or compositor issue.

Repair: disconnect/reconnect the affected external device or log out after input is released. Inspect for other grabbers:

```bash
sudo fuser -v /dev/input/eventX
```

Rollback: stop Input Lock and verify the symptom without it:

```bash
sudo systemctl stop input-lock.service
```

## Cannot switch to TTY

While locked: press and fully release `Ctrl+Alt+F3`; this first unlocks. Press it a second time to switch. Diagnose:

```bash
grep '^UNLOCK_ON_VT_HOTKEY=' /etc/input-lock/input-lock.conf
```

Expected: `yes`. Repair it with `sudoedit`, then reload. Rollback by setting `no`, but doing so removes this recovery path.

## SSH is inaccessible

Input Lock cannot filter SSH. Diagnose the independent service/network:

```bash
systemctl status ssh --no-pager
ss -ltn | grep ':22 '
```

Expected: ssh active and listening. Repair with the normal OpenSSH configuration. No Input Lock rollback is relevant; stopping Input Lock can confirm this:

```bash
sudo systemctl stop input-lock.service
```

## Neither unlock chord is recognized

Use SSH, then:

```bash
sudo input-lock emergency-unlock
sudo systemctl stop input-lock.service
```

Expected: stopping closes every daemon fd and the kernel releases every grab. Keep the service stopped and collect:

```bash
journalctl -u input-lock.service -b --no-pager -n 300
sudo evtest
```

Do not restart or relock until the relevant keyboard node is present in `input-lock list-devices`. Rollback of the entire installation is `sudo ./uninstall.sh` from the package directory.

## Tray icon is missing

Diagnosis:

```bash
systemctl --user status input-lock-notifier.service --no-pager
journalctl --user -u input-lock-notifier.service -b --no-pager -n 100
gnome-extensions info ubuntu-appindicators@ubuntu.com
```

Expected: the user service is active, Ayatana AppIndicator is installed, and Ubuntu AppIndicators is enabled. Repair:

```bash
sudo apt install gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
gnome-extensions enable ubuntu-appindicators@ubuntu.com
systemctl --user restart input-lock-notifier.service
```

Rollback: `systemctl --user disable --now input-lock-notifier.service`. The core keyboard unlock chords remain available without the tray.

## Lid-close option is unavailable or does not change state

Diagnosis:

```bash
input-lock status
input-lock list-devices
input-lock logs -n 100
```

Expected: at least one node named `Lid Switch` with class `lid-switch`. If none exists, the menu item stays disabled and the daemon safely refuses `input-lock lid-ignore on`. Do not substitute an arbitrary power-button or tablet-mode node.

When enabled, status must report an active lid inhibitor, a connected controller and a supervised safety state with fresh telemetry. `input-lock doctor` checks the installed InputLock units, dependencies, the daemon protocol version, the GNOME emergency binding and the native protection readiness (unit contracts, unit drop-ins and the failure latch). There is no tray policy-repair writer. If monitoring is unavailable, reopen the lid and inspect the journals. Return to normal OS behavior with:

```bash
input-lock lid-ignore off
```

If IPC is unavailable, stopping the daemon closes the descriptor and releases the lid switch:

```bash
sudo systemctl stop input-lock.service
```
