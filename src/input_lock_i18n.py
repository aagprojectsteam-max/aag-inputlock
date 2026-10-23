#!/usr/bin/python3
"""User-interface language for Input Lock (gettext text domain ``input-lock``).

Only the stdlib ``gettext`` module is used. Nothing here imports GTK, so the
CLI, the GNOME helper and the tray share the same language choice:

* ``INPUT_LOCK_UI_LANGUAGE`` (``he``/``en``) in the environment, when set,
  wins. It is meant for scripts, tests and English bug reports.
* Otherwise the per-user ``ui_language`` setting (``auto``/``he``/``en``) in
  ``~/.config/input-lock/cursor-settings.json``.
* ``auto`` follows the session locale with the usual gettext precedence
  (``LC_ALL``, ``LC_MESSAGES``, ``LANG``; then the ``LANGUAGE`` priority list).

English is the source language and the fallback: a missing or unreadable
catalogue never breaks the UI, it only leaves it in English. Log lines and
protocol strings are never translated.
"""

from __future__ import annotations

import contextlib
import gettext
import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

DOMAIN: Final = "input-lock"
LANGUAGES: Final = ("auto", "he", "en")
# Languages with a catalogue in po/; every other language uses English.
TRANSLATED_LANGUAGES: Final = ("he",)
RTL_LANGUAGES: Final = frozenset({"he"})
ENV_OVERRIDE: Final = "INPUT_LOCK_UI_LANGUAGE"
SETTING_KEY: Final = "ui_language"
INSTALLED_LIBDIR: Final = Path("/usr/lib/input-lock")
INSTALLED_LOCALEDIR: Final = Path("/usr/share/locale")
SETTINGS_RELATIVE: Final = Path(".config/input-lock/cursor-settings.json")
# Unicode first-strong isolate / pop directional isolate. Hebrew translations
# put them around Latin words and values inside right-to-left text, but NEVER
# around commands, paths or shortcuts: those are copied into a shell or a
# settings field, and the invisible isolates would be copied with them
# (review U-R2). Commands stand on a line of their own instead.
FSI: Final = "⁨"
PDI: Final = "⁩"
LOG = logging.getLogger("input-lock-i18n")

_translations: gettext.NullTranslations = gettext.NullTranslations()
_language = "en"


def _(message: str) -> str:
    """Translate ``message`` with the active catalogue (English if none)."""
    return _translations.gettext(message)


def ngettext(singular: str, plural: str, count: int) -> str:
    return _translations.ngettext(singular, plural, count)


def pgettext(context: str, message: str) -> str:
    """Translate a short ``message`` whose meaning depends on ``context``."""
    return _translations.pgettext(context, message)


def N_(message: str) -> str:
    """Mark a string for extraction; translate it later with ``_()``."""
    return message


def safe_format(message: str, /, **values: object) -> str:
    """``_(message).format(**values)`` that never raises (review U-R9).

    A broken translation (missing catalogue entry, wrong placeholder) falls
    back to the English msgid; if even that cannot be formatted, the msgid is
    returned unformatted. Use it with ``N_("...")`` so the text is extracted.
    """
    try:
        return _(message).format(**values)
    except Exception:  # noqa: BLE001 - display text must never break a caller
        LOG.debug("Translation of %r failed; using English", message, exc_info=True)
    try:
        return message.format(**values)
    except Exception:  # noqa: BLE001
        return message


def isolate(text: object) -> str:
    """Wrap a Latin fragment so it keeps its own direction inside RTL text.

    Never use it for commands, paths or shortcuts (see FSI above).
    """
    return f"{FSI}{text}{PDI}"


def language() -> str:
    """The effective UI language: ``he`` or ``en``."""
    return _language


def is_rtl() -> bool:
    return _language in RTL_LANGUAGES


# ---------------------------------------------------------------------------
# Per-user setting
# ---------------------------------------------------------------------------
def settings_path() -> Path:
    return Path.home() / SETTINGS_RELATIVE


def read_user_settings(path: Path | None = None) -> dict[str, object]:
    """The settings object, or ``{}`` when it is missing or not an object."""
    try:
        payload = json.loads((path or settings_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError, KeyError):
        # RuntimeError/KeyError: no resolvable home directory (system context).
        return {}
    return payload if isinstance(payload, dict) else {}


def read_ui_language(path: Path | None = None) -> str:
    value = read_user_settings(path).get(SETTING_KEY, "auto")
    return value if isinstance(value, str) and value in LANGUAGES else "auto"


class SettingsFileError(ValueError):
    """The existing settings file cannot be merged; it was left unchanged."""


def _settings_for_update(target: Path) -> dict[str, object]:
    """The current settings object, ``{}`` only when the file does not exist.

    Any other problem (unreadable, not JSON, not an object) raises
    SettingsFileError: merging into ``{}`` would silently drop the user's
    other keys (review U-R7).
    """
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SettingsFileError(_problem_text(target, exc)) from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise SettingsFileError(_problem_text(target, exc)) from exc
    if not isinstance(payload, dict):
        raise SettingsFileError(_problem_text(target, "not a JSON object"))
    return payload


def _problem_text(target: Path, error: object) -> str:
    LOG.warning("Settings file %s cannot be merged (%s); it was left unchanged", target, error)
    return safe_format(N_("The settings file {path} cannot be read ({error}), so it was left "
                          "unchanged. Fix or remove it, then save again."),
                       path=target, error=error)


def update_user_settings(path: Path | None = None, **values: object) -> None:
    """Merge ``values`` into the settings file, keeping every other key.

    Refuses (SettingsFileError, file untouched) when an existing file cannot
    be parsed. Written atomically with mode 0600 through a unique temporary
    file in the same directory, so the GNOME extension that watches the
    directory never sees a partial file and two writers never share a name.
    """
    target = path or settings_path()
    payload = _settings_for_update(target)
    payload.update(values)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def write_ui_language(value: str, path: Path | None = None) -> None:
    if value not in LANGUAGES:
        raise ValueError(f"ui_language must be one of: {', '.join(LANGUAGES)}")
    update_user_settings(path, **{SETTING_KEY: value})


# ---------------------------------------------------------------------------
# Language resolution
# ---------------------------------------------------------------------------
def _language_code(value: str) -> str:
    """``he_IL.UTF-8@x`` -> ``he``; ``C.UTF-8`` -> ``c``; ``iw`` -> ``he``."""
    code = value.strip().split(".", 1)[0].split("@", 1)[0].split("_", 1)[0].lower()
    return "he" if code == "iw" else code


def session_language(environ: Mapping[str, str] | None = None) -> str:
    """The language gettext would pick for this session: ``he`` or ``en``."""
    env = os.environ if environ is None else environ
    locale_value = next((env[name] for name in ("LC_ALL", "LC_MESSAGES", "LANG")
                         if env.get(name)), "")
    base = _language_code(locale_value)
    if base in {"", "c", "posix"}:
        return "en"  # gettext ignores LANGUAGE in the C locale
    priority = [_language_code(item) for item in env.get("LANGUAGE", "").split(":") if item.strip()]
    if priority:
        # gettext walks LANGUAGE in order and falls back to the msgid.
        for code in priority:
            if code in TRANSLATED_LANGUAGES or code == "en":
                return code
        return "en"
    return base if base in TRANSLATED_LANGUAGES else "en"


def resolve_language(setting: str | None = "auto",
                     environ: Mapping[str, str] | None = None) -> str:
    """Effective language for a ``ui_language`` setting: ``he`` or ``en``."""
    env = os.environ if environ is None else environ
    override = env.get(ENV_OVERRIDE, "").strip().lower()
    if override in TRANSLATED_LANGUAGES or override == "en":
        return override
    if setting in TRANSLATED_LANGUAGES or setting == "en":
        return str(setting)
    return session_language(env)


def locale_dir(module_dir: Path | None = None) -> Path:
    """Where the compiled catalogues are.

    Installed modules live in /usr/lib/input-lock and use /usr/share/locale.
    A source checkout (``src/`` next to ``po/``) uses the checkout's build
    directory ``po/build``, filled by ``python3 tools/po_compile.py --build``.
    """
    here = Path(__file__).resolve().parent if module_dir is None else Path(module_dir)
    checkout_po = here.parent / "po"
    if here != INSTALLED_LIBDIR and checkout_po.is_dir():
        return checkout_po / "build"
    return INSTALLED_LOCALEDIR


def setup(setting: str | None = None, *, environ: Mapping[str, str] | None = None,
          localedir: Path | None = None, settings_file: Path | None = None) -> str:
    """Activate the catalogue for the user's choice; return ``he`` or ``en``.

    ``setting`` defaults to the stored ``ui_language``. A missing catalogue
    leaves English active and reports ``en``, so callers never switch the
    layout to right-to-left for untranslated text.
    """
    global _translations, _language
    try:
        if setting is None:
            setting = read_ui_language(settings_file)
        wanted = resolve_language(setting, environ)
    except Exception:  # noqa: BLE001 - choosing a language must never fail a command
        wanted = "en"
    translations: gettext.NullTranslations = gettext.NullTranslations()
    effective = "en"
    if wanted != "en":
        try:
            translations = gettext.translation(
                DOMAIN, localedir=str(localedir or locale_dir()), languages=[wanted])
            effective = wanted
        except Exception:  # noqa: BLE001 - a broken catalogue must not break the UI
            translations = gettext.NullTranslations()
    _translations, _language = translations, effective
    return effective


def reset() -> None:
    """Back to untranslated English (tests)."""
    global _translations, _language
    _translations, _language = gettext.NullTranslations(), "en"


def main(argv: list[str] | None = None) -> int:
    """``input_lock_i18n.py gettext MSGID`` prints MSGID in the user's language.

    Used by the shell launcher, which has no Python gettext of its own.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "gettext":
        print("usage: input_lock_i18n.py gettext MSGID", file=sys.stderr)
        return 2
    setup()
    print(_(args[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
