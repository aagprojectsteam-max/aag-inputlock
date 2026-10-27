"""Platform-independent contracts and policy for AAG - InputLock."""

from .contracts import (
    CapabilityLevel,
    CapabilityReport,
    InputBackend,
    UnsupportedCapabilityError,
)
from .hotkeys import Hotkey, HotkeyError, parse_hotkey
from .allowed_keys import (
    ALLOWED_KEY_CATALOG,
    display_key,
    gtk_key_to_canonical,
    validate_allowed_keys,
)
from .policy import PolicyAction, PolicyEngine
from .protocol import MAX_MESSAGE_BYTES, ProtocolError, decode_message, encode_message
from .settings import (
    DEVICE_KINDS,
    LOCK_MODES,
    SettingsError,
    UserSettings,
    load_settings,
    save_settings,
)

VERSION = "2.0.0-beta.5"

__all__ = [
    "CapabilityLevel",
    "ALLOWED_KEY_CATALOG",
    "CapabilityReport",
    "DEVICE_KINDS",
    "Hotkey",
    "HotkeyError",
    "InputBackend",
    "LOCK_MODES",
    "MAX_MESSAGE_BYTES",
    "PolicyAction",
    "PolicyEngine",
    "ProtocolError",
    "SettingsError",
    "UnsupportedCapabilityError",
    "UserSettings",
    "VERSION",
    "decode_message",
    "display_key",
    "encode_message",
    "load_settings",
    "gtk_key_to_canonical",
    "parse_hotkey",
    "save_settings",
    "validate_allowed_keys",
]
