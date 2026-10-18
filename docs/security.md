# Security and privacy

## Privilege model

The interactive user is not added to `input`. A dedicated system account, `input-lockd`, receives only supplementary membership in that group. It has no password, login shell, home directory or network address family. The systemd unit applies:

- `NoNewPrivileges=yes`
- `DevicePolicy=closed` plus `DeviceAllow=char-input rw`
- `RestrictAddressFamilies=AF_UNIX AF_NETLINK`
- `ProtectSystem=strict`, `ProtectHome=yes`
- kernel/module/control-group/clock/hostname protections
- namespace, realtime, executable-memory and personality restrictions

`AF_NETLINK` is required for udev Hotplug. `AF_UNIX` is required for local IPC and system D-Bus. `PrivateDevices=yes` is deliberately not used because it would hide the physical input nodes. `PrivateNetwork=yes` is deliberately not used for the daemon because it can prevent host udev events in a private network namespace; Internet families are instead excluded explicitly.

## IPC boundary

The socket is mode 0666 so the already-running desktop session does not need a logout to acquire a newly-created group. This does **not** authorize callers. Linux supplies immutable peer PID/UID/GID via `SO_PEERCRED`, and the server accepts only UID 0 or the exact configured UID. Requests are capped at 16 KiB, require one JSON object and one fixed command, and validate every enum, integer, boolean and hotkey string. No field becomes a filename, program or shell command. Admission checks run before client registration. Limits are 64 clients, eight subscribers, 16 KiB per client, five seconds for initial requests and 35 seconds for subscribed heartbeats. Descriptor exhaustion temporarily removes the listener from the selector.

The broad socket mode is therefore a reachability choice, not an authorization choice. A rejected caller can at most open a bounded local connection and receive `unauthorized UID`.

## Key data

To identify a chord, the daemon must observe evdev numeric key codes. It maintains only the currently pressed set per device. It deliberately does not:

- translate key codes through a keyboard layout;
- combine codes into text;
- store event timestamps or sequences;
- log ordinary key/button events;
- expose raw events over IPC;
- send any network traffic or telemetry.

Logs contain state changes, fixed reasons, device node/name/class, exclusion decisions and operational errors. A malicious replacement of the installed daemon could misuse `input` access, so `/usr/lib/input-lock` and `/etc/input-lock` remain root-owned and `ProtectSystem=strict` prevents the service from modifying them.

## Failure behavior

- Invalid startup configuration: daemon exits without grabbing.
- Invalid SIGHUP configuration: all current input is released, the reload is rejected, and the previous validated configuration remains in memory.
- Partial initial grab: newly acquired grabs are rolled back.
- New device cannot be grabbed: the new device remains usable and a warning is logged.
- Python exception/SIGTERM/SIGINT: `finally` releases grabs and closes fds.
- SIGKILL or interpreter crash: kernel closes process fds and releases grabs.
- Restart/reboot: keyboard/pointer state starts unlocked. The selected hotkey is restored, but lid-ignore is always reset to the normal OS lid policy.
- Notification failure: optional user process fails independently; lock safety is unchanged.

## Lid-close warning

Lid suppression is continuously supervised until manually disabled: there is no closed-lid expiry deadline. Thermal/critical-battery telemetry, bounded monitoring-loss grace, an authenticated controller lease and one daemon owner remain mandatory. GNOME idle-only inhibition (flag 8) is scoped to the tray session; a separate temporary power-profiles-daemon `power-saver` hold reduces heat and is cancelled when the tray loses the session or the option is disabled. Neither lease overrides a real system suspend, critical battery protection or hardware thermal controls, and neither persistently edits GNOME power preferences. No logind sleep blocker, persistent GNOME power override or UPower critical-action suppression is introduced. A fixed root systemd adapter requests ordinary OS suspend after release; InputLock-owned recovery stops the daemon before a final attempt. Terminal failure latches arming off; Polkit permits only that named unit and verb. The legacy helper cannot enable a critical-battery override. See [current independent protection](independent-lid-protection.md) and the [historical safety policy](SAFETY-REMEDIATION-20260911.md).

## Trust assumptions

- root controls installed code and configuration.
- `AUTHORIZED_UID` identifies the one intended local user.
- physical possession allows use of the two local chords; the primary chord is disabled while the authorized session is inactive unless input is already locked.
- the Linux input subsystem, udev properties and systemd are trusted.
- this is not an authentication lock and must not replace the GNOME lock screen. It prevents accidental input, not a determined local administrator.
