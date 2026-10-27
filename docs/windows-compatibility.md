# Ubuntu and Windows compatibility map

## Current support

AAG - InputLock 2.0.0-beta.4 targets Ubuntu 26.04, GNOME 50 and Wayland,
and provides a Windows 11 x64 beta backend. The Windows code, build,
installer, agent, secured pipe, service and simulated recovery tests are
implemented. Hardware-dependent Windows features remain experimental until the
target PC completes `windows/test-hardware.ps1` and the manual checklist.

## Portable concepts and code

These parts can be shared after paths and transport are injected instead of
being Linux constants:

- JSON request/response schema and bounded-message validation.
- Lock modes and the mapping between user choices and logical device kinds.
- Settings validation, timeout limits and atomic settings semantics.
- Tray menu wording, settings-dialog behavior and confirmation flows.
- Auto-lock, auto-unlock and cursor-timeout policy as platform-independent
  state machines.
- Versioning, structured status data and most pure unit tests.

The current `input_lock_common.py` is only partly portable: parsing and protocol
validation are reusable, but `/etc`, `/run`, Unix-domain sockets and Unix UID
authorization are not.

## Ubuntu implementation — beta.4 production accepted

The reference Ubuntu deployment is accepted for beta.4. See
[BETA4-QUALIFICATION.md](BETA4-QUALIFICATION.md).
Windows-native hardware acceptance remains separate.

- Device discovery and classification through udev and `/dev/input/event*`.
- Transactional `EVIOCGRAB` locking for keyboard, mouse, touchpad and
  touchscreen.
- Hotplug handling and emergency unlock through the grabbed input stream.
- A restricted systemd daemon account, unit hardening and Unix peer-credential
  checks.
- GNOME fallback shortcuts, GTK/Ayatana tray, notifications and user autostart.
- Session/suspend handling through logind and D-Bus.
- Lid-switch suppression, logind inhibitors, UPower battery monitoring and the
  narrowly scoped Polkit rule.
- Cursor hiding through a GNOME Shell 50 extension on Wayland.
- Bash installation, update and removal flows.

## Windows implementation status

| Area | Current Windows status |
|---|---|
| Privileged process | Implemented Windows Service for recoverable power operations; Windows acceptance pending. |
| IPC and authorization | Implemented local Named Pipes with explicit DACL, remote rejection, client impersonation and SID/administrator checks. |
| Keyboard and mouse | Implemented with dedicated `WH_KEYBOARD_LL`/`WH_MOUSE_LL` message-loop thread; experimental until real-hardware repetition and crash tests pass. |
| Touchscreen | Unsupported and disabled. Documented touch APIs do not provide the required independent global suppression hook. |
| Device discovery | Not required for the system-wide hook beta. Per-device exclusion and stable identifiers remain unsupported on Windows. |
| Global shortcut | Implemented inside the hook so emergency unlock remains visible while input is suppressed. F12 is rejected as the primary shortcut; Secure Attention is untouched. |
| Tray and settings | Implemented native notification-area flow with Tk settings, startup toggle and confirmed Quit. Acceptance pending. |
| Notifications | Errors and confirmation use Windows dialogs; toast notifications remain future work. |
| Start at login | Implemented per user through HKCU Run, separate from the machine service. |
| Session and power | A hidden Win32 monitor unlocks on suspend/resume, session lock/unlock, disconnect and logoff; hardware validation remains pending. |
| Lid close | Opt-in AC/DC `LIDACTION` change uses a durable restoration journal. Normal Windows policy is untouched when disabled. Experimental. |
| Battery threshold | Implemented through `GetSystemPowerStatus`; unknown battery state fails safe to normal policy. Experimental. |
| Cursor hiding | Unsupported: the former thread-local ShowCursor implementation was removed; no cursor-scheme replacement is used. |
| Packaging | PyInstaller bundle and elevated install/uninstall scripts implemented; publisher code signing and MSI remain release work. |
| Diagnostics | `LockLockCli doctor`, CI, safe timeout script and manual hardware checklist implemented. |

## Required architecture direction

Keep shared policy separate from platform integrations:

```text
shared/
  models, validation, protocol schema, policy state machines
platform/linux/
  evdev, udev, logind, UPower, GNOME, systemd
platform/windows/
  service, named pipe, input backend, power/session, tray
```

Platform selection must be explicit and unsupported combinations must fail
closed with a useful error. The Linux behavior and installer must continue to
work while Windows support is developed.

## Acceptance baseline for calling Windows “supported”

- Windows 11 x64 is tested on real hardware; touchscreen stays unsupported
  unless a future documented implementation passes separately.
- Keyboard and mouse/touchpad pointer behavior can be selected independently.
- The configured unlock chord works while every selected class is locked.
- Repeated lock/unlock, suspend/resume, logoff and hotplug do not leave input
  or power policy altered. Cursor hiding remains explicitly unsupported.
- Service or tray crashes cannot leave a persistent lock; forced process stop
  and reboot recovery are tested.
- Standard-user control cannot be forged by another local user.
- Ubuntu's existing test suite remains green and gains platform-independent
  contract tests shared with Windows.
