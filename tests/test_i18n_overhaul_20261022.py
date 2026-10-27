"""Hebrew user interface (review U7): catalogue, language choice, display mapping.

No test here creates a GTK widget or window, sends a notification or touches
the user's real settings: every file lives in a temporary directory.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ast
import contextlib
import gettext
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

# English output regardless of the developer's own language setting.
os.environ["INPUT_LOCK_UI_LANGUAGE"] = "en"

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import input_lock_i18n as i18n  # noqa: E402
import input_lock_messages as messages  # noqa: E402
import po_compile  # noqa: E402
import po_extract  # noqa: E402

FSI, PDI = "⁨", "⁩"
BIDI_CONTROLS = "‎‏⁦⁧⁨⁩"
BRACE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Review U-R2: commands, paths and shortcuts are copied into a shell or a
# settings field, so they are never inside isolates. Fields that can hold one
# (an error or reason text can end with a command line) are never isolated.
UNISOLATED_FIELDS = ("command", "path", "paths", "shortcut", "binding", "hotkey", "unit", "text",
                     "key", "error", "errors", "reason", "detail")
UNISOLATED_VALUE_MSGIDS = {"Primary hotkey:   {value}", "Emergency hotkey: {value}", "  Stable path: {value}"}
COPYABLE = re.compile(
    r"\{(?:" + "|".join(UNISOLATED_FIELDS) + r")\}"
    r"|[\w-]+\.service\b|\b\w+\.py\b|\./update\.sh|\bsystemctl\b|\bjournalctl\b|\bsudo\b|\bpython3\b"
    r"|\binput-lock\b|\bgnome-shell\b|\bgnome-extensions\b|\bgsettings\b|\bnotify-send\b"
    r"|(?<![\w.])/[\w./-]+|--[a-z][a-z-]*|\b[A-Z_]*HOTKEY\b|\bset-primary\b|\bF\d{1,2}\b"
    r"|\bA[-–]Z\b|\b0[-–]9\b|\bCopilot\b")
# A command to run stands on a line of its own: no Hebrew and no bidi
# control characters on that line, so RTL cannot reorder or pollute it.
COMMAND_LINE = re.compile(r"\{command\}|\bsystemctl |\bjournalctl |\bsudo |\bpython3 |\./update\.sh"
                          r"|input_lock_gnome\.py install|input-lock doctor")
HEBREW = re.compile("[֐-׿]")
PERCENT = re.compile(r"%(?:\([A-Za-z_]+\))?[sdif]")
POT = REPO / "po" / "input-lock.pot"
HE_PO = REPO / "po" / "he.po"


def catalogue(path: Path) -> dict:
    return {(entry.msgctxt, entry.msgid): entry
            for entry in po_compile.parse_po(path.read_text(encoding="utf-8"), str(path))
            if entry.msgid and not entry.obsolete}


def hebrew_mo(directory: Path) -> Path:
    """Compile po/he.po into ``directory`` (a gettext locale tree)."""
    target = directory / "he" / "LC_MESSAGES" / "input-lock.mo"
    po_compile.compile_file(HE_PO, target)
    return directory


class HebrewSession(contextlib.ContextDecorator):
    """Activate the Hebrew catalogue for the duration of a test."""

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory()
        hebrew_mo(Path(self.directory.name))
        language = i18n.setup("he", environ={}, localedir=Path(self.directory.name))
        assert language == "he", language
        return self

    def __exit__(self, *exc):
        i18n.reset()
        self.directory.cleanup()
        return False


# ---------------------------------------------------------------------------
# Catalogue completeness and correctness
# ---------------------------------------------------------------------------
class CatalogueTests(unittest.TestCase):
    def test_every_msgid_in_the_code_is_in_the_template(self):
        code = po_extract.extract(REPO)
        template = catalogue(POT)
        self.assertGreater(len(code), 300)
        self.assertEqual(set(code), set(template),
                         "po/input-lock.pot is stale; run: python3 tools/po_extract.py")
        for key, message in code.items():
            self.assertEqual(message.plural, template[key].msgid_plural, key)

    def test_hebrew_covers_the_whole_template_without_fuzzy_entries(self):
        template = catalogue(POT)
        hebrew = catalogue(HE_PO)
        self.assertEqual(set(hebrew), set(template), "he.po has missing or stale entries")
        for key, entry in hebrew.items():
            with self.subTest(key=key):
                self.assertNotIn("fuzzy", entry.flags)
                self.assertTrue(entry.translated)
                self.assertEqual(entry.msgid_plural, template[key].msgid_plural)
                if entry.msgid_plural is not None:
                    self.assertEqual(sorted(entry.msgstr), [0, 1])
                # Hebrew text, isolated Latin, or nothing but a command name
                # and placeholders (for example "input-lock: {error}").
                for text in entry.msgstr.values():
                    bare = BRACE.sub("", COPYABLE.sub("", text))
                    self.assertTrue(re.search("[֐-׿]", text) or FSI in text
                                    or not re.search(r"[A-Za-z]", bare), text)

    def test_header_declares_hebrew_and_its_plural_rule(self):
        header = next(entry for entry in po_compile.parse_po(HE_PO.read_text(encoding="utf-8"))
                      if entry.msgid == "")
        self.assertIn("Language: he\n", header.msgstr[0])
        self.assertIn("Plural-Forms: nplurals=2; plural=(n != 1);\n", header.msgstr[0])
        self.assertIn("charset=UTF-8", header.msgstr[0])

    def test_placeholders_match_between_msgid_and_msgstr(self):
        for key, entry in catalogue(HE_PO).items():
            source = entry.msgid + (entry.msgid_plural or "")
            for index, text in entry.msgstr.items():
                with self.subTest(key=key, form=index):
                    if entry.msgid_plural is not None and index == 0:
                        # The singular form may say "one second" in words
                        # instead of using the count (review U-R9 b).
                        self.assertLessEqual(set(BRACE.findall(text)), set(BRACE.findall(source)))
                    else:
                        self.assertEqual(set(BRACE.findall(text)), set(BRACE.findall(source)))
                    self.assertEqual(sorted(PERCENT.findall(text)), sorted(PERCENT.findall(entry.msgid)))
                    # The text must format with exactly the msgid's fields.
                    text.format(**{name: "x" for name in BRACE.findall(source)})

    @staticmethod
    def _isolate_spans(text: str) -> tuple[list[str], str]:
        """(contents of the outermost isolates, text outside them)."""
        depth, spans, outside, current = 0, [], [], []
        for char in text:
            if char == FSI:
                depth += 1
                if depth == 1:
                    current = []
                    continue
            elif char == PDI:
                depth -= 1
                if depth < 0:
                    raise AssertionError(f"unbalanced PDI in {text!r}")
                if depth == 0:
                    spans.append("".join(current))
                    continue
            (current if depth else outside).append(char)
        if depth:
            raise AssertionError(f"unbalanced FSI in {text!r}")
        return spans, "".join(outside)

    def test_isolates_are_balanced_and_never_wrap_commands_paths_or_shortcuts(self):
        for key, entry in catalogue(HE_PO).items():
            for index, text in entry.msgstr.items():
                with self.subTest(key=key, form=index):
                    spans, outside = self._isolate_spans(text)
                    for span in spans:
                        self.assertIsNone(COPYABLE.search(span), span)
                        if entry.msgid in UNISOLATED_VALUE_MSGIDS:
                            self.assertNotIn("{value}", span)
                    # Other Latin words and values stay isolated: outside the
                    # isolates only Hebrew, digits, punctuation, Hebrew-valued
                    # fields, command lines and copyable items remain.
                    lines = [line for line in outside.split("\n") if not COMMAND_LINE.search(line)]
                    bare = BRACE.sub("", COPYABLE.sub("", "\n".join(lines)))
                    self.assertIsNone(re.search(r"[A-Za-z]", bare), bare)

    def test_commands_stand_on_their_own_line(self):
        for key, entry in catalogue(HE_PO).items():
            for index, text in entry.msgstr.items():
                for line in text.split("\n"):
                    if COMMAND_LINE.search(line):
                        with self.subTest(key=key, form=index, line=line):
                            self.assertIsNone(HEBREW.search(line))
                            self.assertFalse(set(line) & set(BIDI_CONTROLS))

    def test_shortcut_names_and_commands_stay_latin(self):
        hebrew = catalogue(HE_PO)
        for key, needle in {
            (None, "Recovery: {command}"): "\n{command}",
            (None, "TTY recovery: {shortcut}"): " {shortcut}",
            (None, "AAG - InputLock background service unavailable. Open the lid and check "
                   "input-lock.service with systemctl status input-lock.service. After the service "
                   "is repaired, the tray will reconnect automatically."):
                "\nsystemctl status input-lock.service\n",
            (None, "Press a shortcut with at least two modifiers.\nSupported keys: A–Z, 0–9, and F1–F12."):
                " A–Z, 0–9 ו־F1–F12.",
            (None, "review 'journalctl -u input-lock-failsafe.service', then run: "
                   "sudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure"):
                "\nsudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure",
        }.items():
            self.assertIn(needle, hebrew[key].msgstr[0])
        self.assertTrue(hebrew[(None, "Recovery: {command}")].msgstr[0].endswith("\n{command}"))

    def test_status_labels_keep_their_identity_with_the_ignore_lid_close_label(self):
        hebrew = catalogue(HE_PO)
        label = hebrew[(None, "Ignore Lid Close")].msgstr[0]
        for msgid in ("Ignore Lid Close (continuous)",
                      "Ignore Lid Close (service unavailable)", "Ignore Lid Close (safety unavailable)"):
            self.assertTrue(hebrew[(None, msgid)].msgstr[0].startswith(label))


class CompilerTests(unittest.TestCase):
    def test_hebrew_catalogue_compiles_and_round_trips(self):
        entries = po_compile.parse_po(HE_PO.read_text(encoding="utf-8"))
        translations = gettext.GNUTranslations(io.BytesIO(po_compile.compile_entries(entries)))
        self.assertEqual(translations.info()["language"], "he")
        for entry in entries:
            if not entry.msgid:
                continue
            with self.subTest(msgid=entry.msgid):
                if entry.msgid_plural is not None:
                    for count, form in ((1, 0), (2, 1), (7, 1)):
                        self.assertEqual(translations.ngettext(entry.msgid, entry.msgid_plural, count),
                                         entry.msgstr[form])
                elif entry.msgctxt is not None:
                    self.assertEqual(translations.pgettext(entry.msgctxt, entry.msgid), entry.msgstr[0])
                else:
                    self.assertEqual(translations.gettext(entry.msgid), entry.msgstr[0])

    @unittest.skipUnless(shutil.which("msgfmt"), "msgfmt is not installed")
    def test_fallback_compiler_matches_msgfmt(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "msgfmt.mo"
            ours = Path(directory) / "ours.mo"
            subprocess.run(["msgfmt", "--check", "-o", str(reference), str(HE_PO)], check=True)
            po_compile.compile_file(HE_PO, ours)
            with reference.open("rb") as a, ours.open("rb") as b:
                self.assertEqual(gettext.GNUTranslations(a)._catalog, gettext.GNUTranslations(b)._catalog)

    def test_fuzzy_and_untranslated_entries_are_left_out(self):
        text = '''msgid ""
msgstr "Content-Type: text/plain; charset=UTF-8\\n"

#, fuzzy
msgid "Fuzzy"
msgstr "X"

msgid "Empty"
msgstr ""

msgctxt "ctx"
msgid "Esc"
msgstr "a\\tb\\n\\"c\\" \\\\"

msgid "One"
msgid_plural "Many"
msgstr[0] "אחד"
msgstr[1] "רבים"
'''
        translations = gettext.GNUTranslations(io.BytesIO(po_compile.compile_entries(po_compile.parse_po(text))))
        self.assertEqual(translations.gettext("Fuzzy"), "Fuzzy")
        self.assertEqual(translations.gettext("Empty"), "Empty")
        self.assertEqual(translations.pgettext("ctx", "Esc"), 'a\tb\n"c" \\')
        self.assertEqual(translations.ngettext("One", "Many", 1), "אחד")
        self.assertEqual(translations.ngettext("One", "Many", 3), "רבים")

    def test_malformed_po_is_rejected(self):
        for text in ('msgid "a"\n', 'msgid "a\n msgstr "b"\n', 'msgid "a"\nmsgstr "b"\n\nmsgid "a"\nmsgstr "c"\n',
                     'msgid "a\\q"\nmsgstr "b"\n'):
            with self.subTest(text=text), self.assertRaises(po_compile.PoError):
                po_compile.parse_po(text)

    def test_build_writes_the_checkout_locale_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            built = po_compile.build(REPO / "po", Path(directory))
            self.assertEqual([path.relative_to(directory).as_posix() for path in built],
                             ["he/LC_MESSAGES/input-lock.mo"])


# ---------------------------------------------------------------------------
# Language choice
# ---------------------------------------------------------------------------
class LanguageResolutionTests(unittest.TestCase):
    def test_session_locale_rules(self):
        cases = [
            ({"LANG": "he_IL.UTF-8"}, "he"),
            ({"LANG": "en_US.UTF-8"}, "en"),
            ({"LANG": "fr_FR.UTF-8"}, "en"),
            ({}, "en"),
            ({"LANG": "he_IL.UTF-8", "LC_ALL": "C"}, "en"),
            ({"LANG": "he_IL.UTF-8", "LC_ALL": "C.UTF-8"}, "en"),
            ({"LANG": "en_US.UTF-8", "LC_MESSAGES": "he_IL.UTF-8"}, "he"),
            ({"LANG": "en_US.UTF-8", "LC_ALL": "he_IL.UTF-8", "LC_MESSAGES": "en_US.UTF-8"}, "he"),
            ({"LANG": "en_US.UTF-8", "LANGUAGE": "he:en"}, "he"),
            ({"LANG": "he_IL.UTF-8", "LANGUAGE": "en_GB:he"}, "en"),
            ({"LANG": "en_US.UTF-8", "LANGUAGE": "fr:iw"}, "he"),
            ({"LANG": "he_IL.UTF-8", "LANGUAGE": "fr"}, "en"),
            ({"LANG": "C", "LANGUAGE": "he"}, "en"),
            ({"LANG": "he"}, "he"),
        ]
        for environ, expected in cases:
            with self.subTest(environ=environ):
                self.assertEqual(i18n.session_language(environ), expected)
                self.assertEqual(i18n.resolve_language("auto", environ), expected)

    def test_explicit_setting_beats_the_locale_and_the_override_beats_both(self):
        self.assertEqual(i18n.resolve_language("he", {"LANG": "en_US.UTF-8"}), "he")
        self.assertEqual(i18n.resolve_language("en", {"LANG": "he_IL.UTF-8"}), "en")
        self.assertEqual(i18n.resolve_language("bogus", {"LANG": "he_IL.UTF-8"}), "he")
        self.assertEqual(i18n.resolve_language("he", {i18n.ENV_OVERRIDE: "en"}), "en")
        self.assertEqual(i18n.resolve_language("en", {i18n.ENV_OVERRIDE: "HE"}), "he")
        self.assertEqual(i18n.resolve_language("he", {i18n.ENV_OVERRIDE: "fr"}), "he")

    def test_setting_file_is_read_and_merged_without_losing_cursor_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "cursor-settings.json"
            self.assertEqual(i18n.read_ui_language(path), "auto")
            path.parent.mkdir()
            path.write_text('{"enabled": true, "seconds": 90}')
            i18n.write_ui_language("he", path)
            self.assertEqual(json.loads(path.read_text()), {"enabled": True, "seconds": 90, "ui_language": "he"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(i18n.read_ui_language(path), "he")
            for broken in ('{"ui_language": "fr"}', '{"ui_language": 3}', "[]", "not json"):
                path.write_text(broken)
                self.assertEqual(i18n.read_ui_language(path), "auto")
            with self.assertRaises(ValueError):
                i18n.write_ui_language("fr", path)

    def test_tray_cursor_settings_keep_the_language(self):
        import input_lock_notifier as notifier
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor-settings.json"
            path.write_text('{"enabled": false, "seconds": 60, "ui_language": "he"}')
            with mock.patch.object(notifier, "CURSOR_SETTINGS_PATH", path):
                notifier.TrayNotifier._save_cursor_settings(True, 30)
                self.assertEqual(notifier.TrayNotifier._load_cursor_settings(), {"enabled": True, "seconds": 30})
            self.assertEqual(i18n.read_ui_language(path), "he")

    def test_setup_activates_the_catalogue_and_falls_back_to_english(self):
        self.addCleanup(i18n.reset)
        with tempfile.TemporaryDirectory() as directory:
            hebrew_mo(Path(directory))
            self.assertEqual(i18n.setup("he", environ={}, localedir=Path(directory)), "he")
            self.assertTrue(i18n.is_rtl())
            self.assertEqual(i18n._("AAG - InputLock"), "⁨AAG - InputLock⁩")
            self.assertEqual(i18n._("text that is not in the catalogue"), "text that is not in the catalogue")
            self.assertEqual(i18n.setup("auto", environ={"LANG": "en_US.UTF-8"}, localedir=Path(directory)), "en")
            self.assertEqual(i18n._("AAG - InputLock"), "AAG - InputLock")
            self.assertFalse(i18n.is_rtl())
        with tempfile.TemporaryDirectory() as empty:
            # Hebrew requested but no catalogue installed: English, left-to-right.
            self.assertEqual(i18n.setup("he", environ={}, localedir=Path(empty)), "en")
            self.assertFalse(i18n.is_rtl())

    def test_setup_reads_the_stored_setting_and_never_raises(self):
        self.addCleanup(i18n.reset)
        with tempfile.TemporaryDirectory() as directory:
            hebrew_mo(Path(directory))
            settings = Path(directory) / "cursor-settings.json"
            settings.write_text('{"ui_language": "he"}')
            self.assertEqual(i18n.setup(environ={"LANG": "en_US.UTF-8"}, localedir=Path(directory),
                                        settings_file=settings), "he")
            with mock.patch.object(i18n, "read_ui_language", side_effect=RuntimeError("no home")):
                self.assertEqual(i18n.setup(environ={}, localedir=Path(directory)), "en")

    def test_locale_directory_for_installed_and_checkout_modules(self):
        self.assertEqual(i18n.locale_dir(Path("/usr/lib/input-lock")), Path("/usr/share/locale"))
        self.assertEqual(i18n.locale_dir(REPO / "src"), REPO / "po" / "build")
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(i18n.locale_dir(Path(directory) / "lib"), Path("/usr/share/locale"))

    def test_command_line_helper_prints_the_translation(self):
        output = io.StringIO()
        with HebrewSession(), mock.patch.object(i18n, "setup"), contextlib.redirect_stdout(output):
            self.assertEqual(i18n.main(["gettext", "AAG - InputLock"]), 0)
        self.assertEqual(output.getvalue(), "⁨AAG - InputLock⁩\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(i18n.main(["bogus"]), 2)


# ---------------------------------------------------------------------------
# Daemon and safety texts, translated on the display side
# ---------------------------------------------------------------------------
def _source_constants() -> set[str]:
    """Every string literal (implicit concatenation merged) under src/."""
    constants: set[str] = set()
    for path in [*(REPO / "src").glob("*.py"), *(REPO / "src" / "locklock_core").glob("*.py")]:
        if path.name == "input_lock_messages.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                constants.add(node.value)
    return constants


class DaemonTextTests(unittest.TestCase):
    SAMPLES = [
        "Lid-ignore unavailable: ordinary suspend unavailable, blocked, or already in progress",
        "Temperature is already too high to arm lid-ignore (acpitz/temp1 97C)",
        "locking is paused for 1 s after an emergency unlock",
        "locking is paused for 12 s after an emergency unlock",
        "lock refused because a key or button remained pressed on: AT Keyboard, Mouse",
        "safety journal is not writable: [Errno 30] Read-only file system",
        "EVIOCGRAB failed; transition rolled back: [Errno 16] Device or resource busy",
        "input-lock.service has drop-ins: /etc/systemd/system/input-lock.service.d/x.conf",
        "invalid shortcut 'CTRL+Z': shortcut requires at least 2 modifiers",
        "Power-saver hold did not become effective after one retry (active profile: balanced)",
        "previous emergency suspend failed; operator recovery required (see 'input-lock doctor'); "
        "review 'journalctl -u input-lock-failsafe.service', then run: "
        "sudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure",
        "telemetry errors: unreadable:coretemp:temp1",
    ]

    def test_english_display_is_exactly_the_daemon_text(self):
        for text in [*sorted(messages.KNOWN_DAEMON_TEXTS), *self.SAMPLES, "brand-new daemon text", ""]:
            with self.subTest(text=text):
                self.assertEqual(messages.daemon_text(text), text)

    def test_known_texts_still_exist_in_the_sources(self):
        constants = _source_constants()
        formatted = {"shortcut requires at least 2 modifiers"}  # f-string in locklock_core.hotkeys
        for text in sorted(messages.KNOWN_DAEMON_TEXTS - formatted):
            with self.subTest(text=text):
                self.assertIn(text, constants)

    def test_hebrew_display(self):
        with HebrewSession():
            self.assertEqual(messages.daemon_text("Safety telemetry is not ready"),
                             "נתוני הבטיחות מהחיישנים עדיין אינם מוכנים")
            self.assertEqual(messages.daemon_text("locking is paused for 12 s after an emergency unlock"),
                             f"הנעילה מושהית ל־{FSI}12{PDI} שניות לאחר שחרור חירום")
            nested = messages.daemon_text(self.SAMPLES[0])
            self.assertTrue(nested.startswith("התעלמות מסגירת מכסה אינה זמינה: שינה רגילה"), nested)
            joined = messages.daemon_text(self.SAMPLES[10])
            # U-R2: each command on its own line, without isolates; the
            # segment after a command starts on a new line instead of "; ".
            lines = joined.split("\n")
            for command in ("input-lock doctor", "journalctl -u input-lock-failsafe.service",
                            "sudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure"):
                self.assertIn(command, lines)
            self.assertNotIn("input-lock doctor;", joined)
            self.assertIn(FSI + "acpitz/temp1" + PDI, messages.daemon_text(self.SAMPLES[1]))
            shortcut = messages.daemon_text(self.SAMPLES[8])
            self.assertIn("שני מקשי צירוף", shortcut)
            # Unknown text, and unknown parts of a list, are shown unchanged.
            self.assertEqual(messages.daemon_text("brand-new daemon text"), "brand-new daemon text")
            self.assertEqual(messages.daemon_text("x; Safety telemetry is not ready"),
                             "x; נתוני הבטיחות מהחיישנים עדיין אינם מוכנים")

    def test_lid_disable_reasons_and_status_codes(self):
        self.assertEqual(messages.thermal_text("HIGH_RISK"), "HIGH_RISK")
        self.assertEqual(messages.protection_text("SOMETHING"), "SOMETHING")
        with HebrewSession():
            self.assertEqual(messages.thermal_text("HIGH_RISK"), "סיכון גבוה")
            self.assertEqual(messages.protection_text("DISARMED"), "כבויה")
            self.assertEqual(messages.protection_text("SOMETHING"), "SOMETHING")
            text = messages.describe_lid_disable("emergency-unlock")
            self.assertIn("שחרור חירום", text)
            text = messages.describe_lid_disable("THERMAL_EMERGENCY")
            self.assertIn("טמפרטורה מסוכנת", text)
            text = messages.describe_lid_disable("something-new", detail="x=1")
            self.assertIn("something-new", text)
            self.assertTrue(text.endswith("פרטים: x=1"), text)


# ---------------------------------------------------------------------------
# Consumers: CLI, tray and launcher (no windows, no daemon)
# ---------------------------------------------------------------------------
class CliLanguageTests(unittest.TestCase):
    STATE = {"locked": {"keyboard": True}, "primary_hotkey": "CTRL+ALT+Z",
             "safety": {"state": "LID_IGNORE_ARMED", "thermal": "NORMAL", "thermal_protection": "ARMED",
                        "sample": {"protection_errors": [], "errors": [], "stack_ready": True,
                                   "dependencies_ok": True}},
             "last_lid_disable": {"reason": "emergency-unlock", "requested_by_user": False}}

    def status(self) -> str:
        import input_lock_cli as cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.print_status(self.STATE)
        return output.getvalue()

    def test_english_status_is_unchanged(self):
        text = self.status()
        self.assertIn("Keyboard:    Locked\n", text)
        self.assertIn("Lid-ignore ready: yes\n", text)
        self.assertIn("Last lid-ignore off: emergency-unlock (automatic)\n", text)
        self.assertIn("  Input was unlocked with an emergency unlock", text)

    def test_hebrew_status(self):
        with HebrewSession():
            text = self.status()
        self.assertTrue(text.startswith("מצב ⁨AAG - InputLock⁩\n"))
        self.assertIn(FSI + "נעול" + PDI, text)
        # U-R2: a shortcut and a reason key are copyable; never isolated.
        self.assertIn("קיצור ראשי:          CTRL+ALT+Z\n", text)
        self.assertIn("טמפרטורה נמדדת:", text)
        self.assertIn(": emergency-unlock (אוטומטי)\n", text)

    def test_main_sets_up_the_language_before_building_the_parser(self):
        import input_lock_cli as cli
        order = []
        with mock.patch.object(cli.i18n, "setup", side_effect=lambda *a, **k: order.append("setup")), \
                mock.patch.object(cli, "build_parser", side_effect=lambda: order.append("parser") or SystemExit(0)), \
                contextlib.suppress(Exception):
            cli.main(["status"])
        self.assertEqual(order[:2], ["setup", "parser"])

    def test_hebrew_help_and_errors(self):
        import input_lock_cli as cli
        with HebrewSession():
            text = cli.build_parser().format_help()
            self.assertIn("הצגת מצב הנעילה", text)
            stderr = io.StringIO()
            with mock.patch.object(cli.i18n, "setup"), \
                    mock.patch.object(cli, "_request", side_effect=cli.ProtocolError("Safety telemetry is not ready")), \
                    contextlib.redirect_stderr(stderr):
                self.assertEqual(cli.main(["lid-ignore", "on"]), 1)
            self.assertEqual(stderr.getvalue(),
                             "input-lock: נתוני הבטיחות מהחיישנים עדיין אינם מוכנים\n")


class TrayLanguageTests(unittest.TestCase):
    # GTK is replaced by a mock: these tests never call into GObject
    # introspection, so they cannot disturb the GI tests that follow.
    def test_rtl_direction_is_set_for_hebrew_before_the_tray_is_built(self):
        import input_lock_notifier as notifier
        for language, rtl in (("he", True), ("en", False)):
            with self.subTest(language=language), \
                    mock.patch.object(notifier, "Gtk") as gtk, \
                    mock.patch.object(notifier.i18n, "setup", return_value=language), \
                    mock.patch.object(notifier.i18n, "is_rtl", return_value=rtl):
                self.assertEqual(notifier.apply_ui_language(), language)
                gtk.Widget.set_default_direction.assert_called_once_with(
                    gtk.TextDirection.RTL if rtl else gtk.TextDirection.LTR)

    def test_main_applies_the_language_before_constructing_the_tray(self):
        import input_lock_notifier as notifier
        order = []
        # basicConfig would install a global INFO handler for the rest of the
        # run; keep logging exactly as the other test modules expect it.
        with mock.patch.object(notifier.logging, "basicConfig"), \
                mock.patch.object(notifier, "Gtk") as gtk, \
                mock.patch.object(notifier, "apply_ui_language", side_effect=lambda: order.append("language")), \
                mock.patch.object(notifier, "TrayNotifier", side_effect=lambda: order.append("tray") or mock.Mock(
                    run=mock.Mock(return_value=0))):
            gtk.init_check.return_value = (True, [])
            self.assertEqual(notifier.main(), 0)
        self.assertEqual(order, ["language", "tray"])

    def test_language_choices_name_each_language_in_itself(self):
        import input_lock_notifier as notifier
        choices = dict(notifier.TrayNotifier._language_choices())
        self.assertEqual(set(choices), set(i18n.LANGUAGES))
        self.assertEqual((choices["he"], choices["en"]), ("עברית", "English"))
        with HebrewSession():
            self.assertEqual(dict(notifier.TrayNotifier._language_choices())["auto"], "אוטומטית (שפת המערכת)")

    def test_settings_worker_stores_a_changed_language(self):
        import input_lock_notifier as notifier
        tray = notifier.TrayNotifier.__new__(notifier.TrayNotifier)
        tray.config = mock.Mock(socket_path="/nonexistent")
        tray._autostart_enabled = mock.Mock(return_value=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor-settings.json"
            with mock.patch.object(notifier, "CURSOR_SETTINGS_PATH", path), \
                    mock.patch.object(notifier, "send_request", return_value={"ok": True}), \
                    mock.patch.object(notifier.GLib, "idle_add") as idle_add:
                tray._settings_worker(["keyboard"], False, False, 600, False, 60, [], "he")
            self.assertEqual(json.loads(path.read_text()),
                             {"enabled": False, "seconds": 60, "ui_language": "he"})
        self.assertIn(notifier.LANGUAGE_NOTE, idle_add.call_args.args[2])

    def test_hebrew_menu_texts(self):
        import input_lock_notifier as notifier
        tray = notifier.TrayNotifier.__new__(notifier.TrayNotifier)
        tray.current_state = {"safety": {"state": "LID_CLOSED_MONITORED", "thermal": "NORMAL",
                                         "thermal_protection": "ARMED"}, "ignore_lid_close": True}
        tray.cooling_verified = True
        with HebrewSession():
            head, detail, cooling = tray._safety_lines()
            self.assertEqual(head, "בטיחות המכסה: " + FSI + "ניטור מכסה סגור" + PDI)
            self.assertTrue(detail.startswith("טמפרטורה: " + FSI + "תקינה" + PDI), detail)
            self.assertEqual(cooling, "קירור: פעיל")
            command = notifier.TrayNotifier._notify_command("/usr/bin/notify-send", "T", "B", False, True, True)
            self.assertIn("--app-name=⁨AAG - InputLock⁩", command)
            self.assertIn("--action=reenable=הפעלה מחדש…", command)


class LauncherLanguageTests(unittest.TestCase):
    def run_t(self, directory: Path, message: str, language: str) -> str:
        launcher = (REPO / "src" / "input-lock-launch").read_text()
        function = re.search(r"^_t\(\) \{\n.*?^\}\n", launcher, re.M | re.S).group(0)
        (directory / "t.sh").write_text(function)
        env = {**os.environ, i18n.ENV_OVERRIDE: language, "HOME": str(directory)}
        result = subprocess.run(["bash", "-c", f'source "{directory}/t.sh"; _t "$1"', "t", message],
                                check=True, capture_output=True, text=True, env=env, timeout=30)
        return result.stdout

    def test_launcher_messages_are_translated_or_left_in_english(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "src"
            source.mkdir()
            shutil.copy(REPO / "src" / "input_lock_i18n.py", source)
            (root / "po").mkdir()
            hebrew_mo(root / "po" / "build")
            message = "The AAG - InputLock service started, but the tray icon could not be opened."
            self.assertEqual(self.run_t(source, message, "he"),
                             "השירות של ⁨AAG - InputLock⁩ הופעל, אבל לא ניתן היה לפתוח את הסמל שלו.")
            self.assertEqual(self.run_t(source, message, "en"), message)
            # A broken module never hides the message.
            (source / "input_lock_i18n.py").write_text("raise SystemExit(3)\n")
            self.assertEqual(self.run_t(source, message, "he"), message)


class InstallLanguageTests(unittest.TestCase):
    def setUp(self):
        from test_integration_overhaul_20261022 import bash_array
        self.install = (REPO / "install.sh").read_text()
        self.locales = bash_array(self.install, "LOCALES")
        self.modules = bash_array(self.install, "LIB_MODULES")
        self.fixed = bash_array(self.install, "PAYLOAD_FIXED")
        self.backup = bash_array(self.install, "BACKUP_ITEMS")
        self.restore = bash_array(self.install, "RESTORE_ITEMS")

    def test_every_catalogue_is_staged_installed_backed_up_and_removed(self):
        self.assertEqual(self.locales, sorted(path.stem for path in (REPO / "po").glob("*.po")))
        for language in self.locales:
            target = f"/usr/share/locale/{language}/LC_MESSAGES/input-lock.mo"
            self.assertIn(target, self.backup)
            self.assertIn(target, self.restore)
        self.assertIn('PAYLOAD+=("po/$lang.po")', self.install)
        self.assertIn('compile_catalog "$STAGE/po/$item.po" "$STAGE/locale/$item/LC_MESSAGES/input-lock.mo"',
                      self.install)
        self.assertIn('"/usr/share/locale/$item/LC_MESSAGES/input-lock.mo"', self.install)
        self.assertIn("rm -f -- /usr/share/locale/*/LC_MESSAGES/input-lock.mo",
                      (REPO / "uninstall.sh").read_text())

    def test_compiler_choice_and_modules(self):
        body = self.install[self.install.index("compile_catalog() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("msgfmt --check", body)
        self.assertIn('root_python "$STAGE/tools/po_compile.py"', body)
        self.assertIn("tools/po_compile.py", self.fixed)
        for module in ("input_lock_i18n.py", "input_lock_messages.py"):
            self.assertIn(module, self.modules)
            self.assertIn("src/" + module, self.fixed)
        # The catalogue lives outside /usr/lib/input-lock, so prune never sees it.
        prune = self.install[self.install.index("prune_unmanaged() {"):]
        prune = prune[:prune.index("\n}\n")]
        self.assertNotIn("/usr/share/locale", prune)


if __name__ == "__main__":
    unittest.main()
