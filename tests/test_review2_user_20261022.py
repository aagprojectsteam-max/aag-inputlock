#!/usr/bin/python3
"""Second review round, user-facing side (U-R1 to U-R10), 2026-10-22.

No test opens a GTK window, sends a notification, runs an installer or
touches the user's real settings: GTK dialogs and notify-send are mocked and
every file lives in a temporary directory.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import tomllib
import types
import unittest
from unittest import mock

os.environ["INPUT_LOCK_UI_LANGUAGE"] = "en"

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tools"))

import input_lock_cli as cli  # noqa: E402
import input_lock_gnome as gnome  # noqa: E402
import input_lock_i18n as i18n  # noqa: E402
import input_lock_messages as messages  # noqa: E402
import input_lock_notifier as notifier  # noqa: E402
import po_compile  # noqa: E402
from input_lock_notifier import TrayNotifier  # noqa: E402
from test_i18n_overhaul_20261022 import HebrewSession  # noqa: E402
from test_integration_overhaul_20261022 import bash_array  # noqa: E402

BIDI_CONTROLS = set("‎‏⁦⁧⁨⁩")


def bare_tray(**state):
    tray = TrayNotifier.__new__(TrayNotifier)
    tray.config = mock.Mock()
    tray.current_state = dict(state)
    tray.previous_locked = None
    for name in ("status_item", "safety_item", "toggle_item", "lid_item", "indicator", "shortcut_item"):
        setattr(tray, name, mock.Mock())
    tray._notify = mock.Mock()
    tray._show_error = mock.Mock(return_value=False)
    tray.idle_guard = mock.Mock()
    tray.power_guard = mock.Mock()
    tray.power_guard.acquire.return_value = False
    return tray


ARMED = {"ignore_lid_close": True, "lid_generation": 4, "lid_switch_devices": 1,
         "lid_policy_healthy": True, "locked_kinds": [], "lid_is_closed": False}


def disarmed(**extra):
    state = dict(ARMED, ignore_lid_close=False, lid_generation=5, lid_policy_healthy=False)
    state.update(extra)
    return state


def event(state, reason="state-changed"):
    return {"type": "event", "event": "state-changed", "reason": reason, "state": state}


def armed_tray(**extra):
    tray = bare_tray(**dict(ARMED, **extra))
    tray.connected = True
    return tray


# ---------------------------------------------------------------------------
# U-R1: the right cause when the daemon drops the controller
# ---------------------------------------------------------------------------
class ControllerDropTests(unittest.TestCase):
    def test_both_keys_have_english_and_hebrew_text(self):
        for key, words, hebrew in (("controller-lease-expired", "did not check in", "לא דיווח"),
                                   ("controller-disconnected", "connection", "החיבור")):
            with self.subTest(key=key):
                self.assertIn(words, messages.describe_lid_disable(key))
                with HebrewSession():
                    self.assertIn(hebrew, messages.describe_lid_disable(key))

    def test_flushed_final_state_names_the_cause_and_eof_adds_nothing(self):
        tray = armed_tray(last_lid_disable={"reason": "x", "requested_by_user": True, "at": 10.0})
        with mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=7):
            tray._handle_message(event(disarmed(last_lid_disable={
                "reason": "controller-lease-expired", "requested_by_user": False,
                "user_initiated": False, "at": 20.0}), "controller-lease-expired"))
            tray._show_disconnected()
        tray._notify.assert_called_once()
        self.assertIn("did not check in", tray._notify.call_args.args[1])
        self.assertFalse(tray.unavailable_reported)

    def lost_event_tray(self):
        tray = armed_tray()
        with mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=7):
            tray._handle_message({"subscribed": True, "state": dict(ARMED, last_lid_disable={
                "reason": "lid-ignore-disabled", "requested_by_user": True, "at": 10.0})})
            tray._show_disconnected()  # the final event was lost: EOF only
        self.assertIn("connection to the Input Lock background service was lost",
                      tray._notify.call_args.args[1])
        self.assertTrue(tray.unavailable_reported)
        self.assertEqual(tray.unavailable_since_at, 10.0)
        tray._notify.reset_mock()
        return tray

    def test_newer_disable_after_resubscribe_corrects_the_cause_once(self):
        tray = self.lost_event_tray()
        state = disarmed(last_lid_disable={"reason": "controller-lease-expired",
                                           "requested_by_user": False, "user_initiated": False, "at": 30.0})
        tray._handle_message({"subscribed": True, "state": state})
        tray._notify.assert_called_once()
        title, body = tray._notify.call_args.args[:2]
        self.assertEqual(title, notifier.LID_LOST_TITLE)
        self.assertTrue(body.startswith("Correction to the previous notice: The Input Lock tray did not check in"))
        self.assertTrue(tray._notify.call_args.kwargs["critical"])
        self.assertTrue(tray._notify.call_args.kwargs["reenable"])
        self.assertFalse(tray.unavailable_reported)
        tray._handle_message(event(state))
        tray._notify.assert_called_once()  # only one corrected notice

    def test_no_newer_disable_keeps_the_connection_lost_cause(self):
        tray = self.lost_event_tray()
        for state in (disarmed(last_lid_disable={"reason": "lid-ignore-disabled",
                                                 "requested_by_user": True, "at": 10.0}),
                      disarmed(last_lid_disable=None)):  # e.g. a restarted daemon
            tray.unavailable_reported, tray.unavailable_since_at = True, 10.0
            tray._handle_message({"subscribed": True, "state": state})
            tray._notify.assert_not_called()
            self.assertFalse(tray.unavailable_reported)

    def test_user_initiated_correction_is_informational(self):
        tray = self.lost_event_tray()
        tray._handle_message({"subscribed": True, "state": disarmed(last_lid_disable={
            "reason": "emergency-hotkey", "requested_by_user": False, "user_initiated": True, "at": 30.0})})
        self.assertNotIn("critical", tray._notify.call_args.kwargs)
        self.assertIn("emergency unlock shortcut", tray._notify.call_args.args[1])


# ---------------------------------------------------------------------------
# U-R5: notice severity follows who caused the loss
# ---------------------------------------------------------------------------
class LossMappingTests(unittest.TestCase):
    def lose(self, info, reason="state-changed"):
        tray = armed_tray()
        with mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=7) as timer:
            tray._handle_message(event(disarmed(last_lid_disable=info), reason))
        return tray, timer

    def test_requested_by_user_is_silent(self):
        tray, _timer = self.lose({"reason": "settings-reset", "requested_by_user": True,
                                  "user_initiated": True, "at": 1.0})
        tray._notify.assert_not_called()

    def test_user_initiated_is_short_and_informational_naming_the_action(self):
        tray, timer = self.lose({"reason": "emergency-hotkey", "requested_by_user": False,
                                 "user_initiated": True, "at": 1.0})
        tray._notify.assert_called_once()
        title, body = tray._notify.call_args.args[:2]
        self.assertEqual(title, notifier.LID_LOST_TITLE)
        self.assertIn("emergency unlock shortcut was used", body)
        self.assertIn(notifier.LID_OFF_INFO_HINT, body)
        self.assertNotIn("critical", tray._notify.call_args.kwargs)
        self.assertNotIn("reenable", tray._notify.call_args.kwargs)
        timer.assert_called_once()  # idle grace kept: the action may have come over SSH

    def test_neither_is_critical_with_reenable(self):
        tray, _timer = self.lose({"reason": "thermal-emergency", "requested_by_user": False,
                                  "user_initiated": False, "at": 1.0})
        self.assertTrue(tray._notify.call_args.kwargs["critical"])
        self.assertTrue(tray._notify.call_args.kwargs["reenable"])

    def test_older_daemon_without_user_initiated_keeps_todays_mapping(self):
        tray, _timer = self.lose({"reason": "emergency-hotkey", "requested_by_user": False, "at": 1.0})
        self.assertTrue(tray._notify.call_args.kwargs["critical"])

    def test_tray_unlock_note_wins_even_when_the_daemon_marks_it_requested(self):
        tray = armed_tray()
        tray._mark_tray_unlock()
        tray._handle_message(event(disarmed(last_lid_disable={
            "reason": "emergency-unlock", "requested_by_user": True, "user_initiated": True})))
        self.assertEqual(tray._notify.call_args.args[1], notifier.TRAY_UNLOCK_LID_NOTE)

    def test_cli_status_names_a_side_effect_of_a_user_action(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.print_status({"last_lid_disable": {"reason": "emergency-hotkey", "requested_by_user": False,
                                                   "user_initiated": True}})
        self.assertIn("Last lid-ignore off: emergency-hotkey (side effect of a user action)\n", output.getvalue())
        self.assertIn("emergency unlock shortcut was used", output.getvalue())


# ---------------------------------------------------------------------------
# U-R6: no lease outlives a failed re-enable
# ---------------------------------------------------------------------------
class FailedEnableTests(unittest.TestCase):
    def test_grace_expiring_during_a_failed_enable_releases_the_idle_inhibitor(self):
        tray = bare_tray(**disarmed())
        tray.connected = True
        tray.grace_until = 1.0
        tray.enable_in_flight = True
        tray._idle_grace_expired()  # in flight: does not release
        tray.idle_guard.release.assert_not_called()
        tray._enable_finished(False, "refused", None)
        tray.idle_guard.release.assert_called_once_with()
        tray.power_guard.release.assert_called_once_with()
        self.assertFalse(tray.enable_in_flight)
        tray._show_error.assert_called_once()

    def test_failed_enable_within_grace_keeps_the_idle_inhibitor(self):
        tray = bare_tray(**disarmed())
        tray.connected = True
        tray.enable_in_flight = True
        tray.grace_until = 99.0
        tray._enable_finished(False, "refused", None)
        tray.idle_guard.release.assert_not_called()
        tray.power_guard.release.assert_called_once_with()


# ---------------------------------------------------------------------------
# U-R7: settings merge never drops keys
# ---------------------------------------------------------------------------
class SettingsMergeTests(unittest.TestCase):
    def test_unparseable_or_non_object_file_is_left_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor-settings.json"
            for content in ('{"enabled": true, "seconds": 90', "[1, 2]", "\udcff"):
                path.write_text(content, encoding="utf-8", errors="surrogateescape")
                before = path.read_bytes()
                with self.subTest(content=content), self.assertLogs("input-lock-i18n", "WARNING"):
                    with self.assertRaises(i18n.SettingsFileError) as caught:
                        i18n.write_ui_language("he", path)
                    self.assertIn(str(path), str(caught.exception))
                self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["cursor-settings.json"])

    def test_merge_keeps_keys_and_writes_atomically_through_a_unique_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor-settings.json"
            path.write_text('{"enabled": true, "seconds": 90, "future": [1]}')
            names = []
            real = tempfile.mkstemp

            def spy(*args, **kwargs):
                result = real(*args, **kwargs)
                names.append(result[1])
                return result

            with mock.patch.object(i18n.tempfile, "mkstemp", side_effect=spy):
                i18n.write_ui_language("en", path)
            self.assertEqual(json.loads(path.read_text()),
                             {"enabled": True, "seconds": 90, "future": [1], "ui_language": "en"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(names), 1)
            self.assertEqual(Path(names[0]).parent, Path(directory))
            self.assertNotEqual(Path(names[0]).name, "cursor-settings.tmp")
            self.assertFalse(Path(names[0]).exists())

    def test_failed_write_removes_the_temporary_and_keeps_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor-settings.json"
            path.write_text('{"enabled": false}')
            with mock.patch.object(i18n.os, "replace", side_effect=OSError("disk full")), \
                    self.assertRaises(OSError):
                i18n.update_user_settings(path, seconds=5)
            self.assertEqual(path.read_text(), '{"enabled": false}')
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["cursor-settings.json"])

    def test_missing_file_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new" / "cursor-settings.json"
            i18n.update_user_settings(path, enabled=True)
            self.assertEqual(json.loads(path.read_text()), {"enabled": True})

    def test_tray_settings_worker_reports_the_refusal(self):
        tray = bare_tray()
        tray.config = mock.Mock(socket_path="/nonexistent")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor-settings.json"
            path.write_text("not json")
            with mock.patch.object(notifier, "CURSOR_SETTINGS_PATH", path), \
                    mock.patch.object(notifier, "send_request", return_value={"ok": True}), \
                    mock.patch.object(notifier.GLib, "idle_add") as idle_add, \
                    self.assertLogs("input-lock-i18n", "WARNING"):
                tray._settings_worker(["keyboard"], False, False, 600, True, 60, [], "he")
            self.assertEqual(path.read_text(), "not json")
        self.assertEqual(idle_add.call_args.args[0], tray._show_error)
        self.assertIn("left unchanged", idle_add.call_args.args[2])


# ---------------------------------------------------------------------------
# U-R9: robust wording, Hebrew fixes, Meta = Super
# ---------------------------------------------------------------------------
class BrokenTranslation:
    """A catalogue whose every entry has a placeholder the code does not pass."""

    def gettext(self, message):
        return message + " {missing}"

    def ngettext(self, singular, plural, count):
        return singular + " {missing}"

    def pgettext(self, context, message):
        return message + " {missing}"


class RobustWordingTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.reset)
        i18n._translations = BrokenTranslation()

    def test_describe_and_daemon_text_fall_back_to_english(self):
        self.assertEqual(messages.describe_lid_disable("something-new", detail="x=1"),
                         "The Input Lock service turned it off (reason: something-new). Details: x=1")
        self.assertEqual(messages.daemon_text("locking is paused for 3 s after an emergency unlock"),
                         "locking is paused for 3 s after an emergency unlock")
        self.assertEqual(i18n.safe_format("Press {shortcut} to unlock.", shortcut="Ctrl+Alt+Z"),
                         "Press Ctrl+Alt+Z to unlock.")
        self.assertEqual(i18n.safe_format("{nothing}"), "{nothing}")

    def test_handle_message_survives_wording_failures(self):
        tray = armed_tray(safety={"state": "LID_IGNORE_ARMED"})
        state = disarmed(safety={"state": "PROTECTIVE_ACTION_FAILED"}, last_lid_disable={
            "reason": "something-new", "requested_by_user": False, "at": 2.0})
        with mock.patch.object(notifier, "describe_lid_disable", side_effect=RuntimeError("boom")), \
                mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=7), \
                self.assertLogs("input-lock-tray", "WARNING"):
            tray._handle_message(event(state, "something-new"))
        # The critical notice still went out (English fallback), and the
        # menu and leases were still updated.
        first = tray._notify.call_args_list[0]
        self.assertTrue(first.kwargs["critical"])
        self.assertIn("something-new", first.args[1])
        tray.lid_item.set_active.assert_called_with(False)
        tray.power_guard.release.assert_called_once_with()
        self.assertEqual(tray.last_disable_at, 2.0)
        self.assertEqual(tray._notify.call_args_list[-1].args[1],
                         "Open the lid. Automatic safety protection failed; see InputLock diagnostics.")


class HebrewWordingTests(unittest.TestCase):
    def test_singular_cooldown_reads_naturally(self):
        with HebrewSession():
            self.assertEqual(messages.daemon_text("locking is paused for 1 s after an emergency unlock"),
                             "הנעילה מושהית לשנייה אחת לאחר שחרור חירום")
            self.assertIn("שניות", messages.daemon_text("locking is paused for 5 s after an emergency unlock"))

    def test_failure_latch_is_not_called_a_lock(self):
        text = (REPO / "po" / "he.po").read_text(encoding="utf-8")
        self.assertNotIn("נעילת כשל", text)
        self.assertNotIn("נעילת הכשל", text)
        entries = {e.msgid: e.msgstr[0] for e in po_compile.parse_po(text) if e.msgid}
        for msgid in ("Protection failure latch", "unreadable failure latch",
                      "failure latch from a previous boot (informational; acknowledge to archive it)"):
            self.assertIn("סימון", entries[msgid])

    def test_new_strings_are_translated(self):
        with HebrewSession():
            self.assertEqual(i18n._(notifier.LID_OFF_INFO_HINT),
                             "אם עדיין צריך אותה, אפשר להפעיל אותה שוב מהתפריט של נעילת הקלט.")
            self.assertTrue(i18n.safe_format(notifier.LID_CORRECTION_TEXT, cause="X").startswith("תיקון"))


class MetaIsSuperTests(unittest.TestCase):
    def test_gnome_and_cli_agree_that_meta_is_super(self):
        self.assertEqual(gnome.canonical_binding("<Meta><Control>z"), "<Control><Super>z")
        self.assertEqual(gnome.canonical_binding("<Meta><Control>z"), gnome.canonical_binding("<Super><Primary>Z"))
        self.assertEqual(gnome.hotkey_to_binding("CTRL+META+Z"), "<Control><Super>z")
        self.assertEqual(cli._binding_hotkey("<Meta><Control>z"), "CTRL+SUPER+Z")
        self.assertTrue(cli.gnome_binding_matches("<Control><Meta>F12", "CTRL+SUPER+F12"))
        self.assertNotEqual(gnome.canonical_binding("<Meta><Control>z"), gnome.canonical_binding("<Alt><Control>z"))


# ---------------------------------------------------------------------------
# Interface 3: root-only sleep commands
# ---------------------------------------------------------------------------
class SleepCommandTests(unittest.TestCase):
    def test_a_user_running_them_by_hand_gets_a_clear_error_and_nothing_is_sent(self):
        for command in ("prepare-sleep", "resume-sleep"):
            stderr = io.StringIO()
            with self.subTest(command=command), \
                    mock.patch.object(cli.os, "geteuid", return_value=1000), \
                    mock.patch.object(cli, "_request") as request, \
                    contextlib.redirect_stderr(stderr):
                self.assertEqual(cli.main([command]), 1)
                request.assert_not_called()
                self.assertEqual(stderr.getvalue(),
                                 f"input-lock: {command} is used only by the system sleep hook, which runs "
                                 "as root; it cannot be run by hand\n")

    def test_the_root_hook_still_sends_them_and_they_stay_hidden(self):
        for command in ("prepare-sleep", "resume-sleep"):
            with self.subTest(command=command), \
                    mock.patch.object(cli.os, "geteuid", return_value=0), \
                    mock.patch.object(cli, "_request", return_value={"ok": True}) as request:
                self.assertEqual(cli.main([command]), 0)
                self.assertEqual(request.call_args.args[0], {"cmd": command})
        help_text = cli.build_parser().format_help()
        self.assertNotIn("prepare-sleep", help_text)
        self.assertNotIn("resume-sleep", help_text)

    def test_hebrew_error(self):
        stderr = io.StringIO()
        with HebrewSession(), mock.patch.object(cli.i18n, "setup"), \
                mock.patch.object(cli.os, "geteuid", return_value=1000), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["prepare-sleep"]), 1)
        self.assertIn("הפקודה prepare-sleep מיועדת רק לרכיב השינה של המערכת", stderr.getvalue())


# ---------------------------------------------------------------------------
# U-R2: copyable commands in real output
# ---------------------------------------------------------------------------
class CopyableOutputTests(unittest.TestCase):
    def assert_own_line(self, text, command):
        lines = text.split("\n")
        self.assertIn(command, lines, text)
        self.assertFalse(BIDI_CONTROLS & set(lines[lines.index(command)]))

    def test_cli_recovery_command_in_hebrew(self):
        stderr = io.StringIO()
        with HebrewSession(), \
                mock.patch.object(cli, "_request", side_effect=cli.ProtocolError("unknown daemon error")), \
                mock.patch.object(cli.os, "geteuid", return_value=1000), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.emergency_unlock(), 2)
        self.assert_own_line(stderr.getvalue().rstrip("\n"), "sudo systemctl stop input-lock.service")

    def test_cannot_connect_error_in_hebrew(self):
        with HebrewSession(), mock.patch.object(
                cli, "send_request", side_effect=cli.ProtocolError("x")) as send:
            send.side_effect.__cause__ = FileNotFoundError(2, "No such file")
            with self.assertRaises(cli.ProtocolError) as caught:
                cli._send_with_retry({"cmd": "status"}, Path("/run/input-lock/control.sock"), 1.0, 0.0,
                                     unit_state=lambda: "inactive")
        text = str(caught.exception)
        self.assert_own_line(text, "systemctl status input-lock.service")
        self.assertIn("‎/run/input-lock/control.sock", text)
        self.assertNotIn("⁨/run", text)

    def test_readiness_list_puts_each_command_on_its_own_line(self):
        reason = ("previous emergency suspend failed; operator recovery required (see 'input-lock doctor'); "
                  + cli.RECOVERY_HINT)
        with HebrewSession():
            text = messages.daemon_text(reason)
        for command in ("input-lock doctor", "journalctl -u input-lock-failsafe.service",
                        "sudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure"):
            self.assert_own_line(text, command)
        # English output is exactly the daemon's text.
        self.assertEqual(messages.daemon_text(reason), reason)

    def test_tray_service_unavailable_dialog(self):
        tray = bare_tray()
        tray._show_disconnected = mock.Mock()
        with HebrewSession():
            tray._action_error("T", FileNotFoundError(2, "missing"))
        self.assert_own_line(tray._show_error.call_args.args[1], "systemctl status input-lock.service")

    def test_hebrew_shortcut_values_are_not_isolated(self):
        with HebrewSession():
            body = i18n.safe_format("Press {shortcut} to unlock.", shortcut="Ctrl+Alt+Z")
        self.assertEqual(body, "לשחרור הנעילה יש ללחוץ על Ctrl+Alt+Z.")


# ---------------------------------------------------------------------------
# U-R3: packaging carries the i18n modules and catalogues
# ---------------------------------------------------------------------------
class PackagingTests(unittest.TestCase):
    def test_install_pyproject_manifest_and_artifact_check_agree_on_runtime_modules(self):
        install = (REPO / "install.sh").read_text()
        lib_modules = bash_array(install, "LIB_MODULES")
        fixed = bash_array(install, "PAYLOAD_FIXED")
        pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
        py_modules = pyproject["tool"]["setuptools"]["py-modules"]
        self.assertEqual(sorted(name.removesuffix(".py") for name in lib_modules), sorted(py_modules))
        for module in lib_modules:
            self.assertIn("src/" + module, fixed)
        self.assertEqual(sorted(path.name for path in (REPO / "src").glob("input_lock_*.py")),
                         sorted(lib_modules))
        manifest = (REPO / "MANIFEST.in").read_text()
        self.assertRegex(manifest, r"(?m)^recursive-include po .*\*\.po\b")
        self.assertRegex(manifest, r"(?m)^recursive-include po .*\*\.pot\b")
        self.assertRegex(manifest, r"(?m)^recursive-include tools \*\.py")
        check = (REPO / "tools" / "check_artifacts.py").read_text()
        for module in lib_modules:
            self.assertIn(f"'{module}'", check)
        for language in bash_array(install, "LOCALES"):
            self.assertIn(f"'po/{language}.po'", check)
        self.assertIn("'tools/po_compile.py'", check)


# ---------------------------------------------------------------------------
# U-R4 and U-R8: installer respects the user's choices
# ---------------------------------------------------------------------------
class FakeSettings:
    def __init__(self, store):
        self.store = store

    def get_strv(self, key):
        return list(self.store.get(key, []))

    def set_strv(self, key, value):
        self.store[key] = list(value)


class ExtensionChoiceTests(unittest.TestCase):
    UUID = "input-lock-cursor@aag-projects-team"

    def run_snippet(self, previous_install, enabled, disabled):
        install = (REPO / "install.sh").read_text()
        body = install.split('extension_state=$(run_as_target python3 -I - "$PREVIOUS_INSTALL" <<\'PY\'\n', 1)[1]
        body = body.split("\nPY\n", 1)[0]
        store = {"enabled-extensions": list(enabled), "disabled-extensions": list(disabled)}
        gio = types.SimpleNamespace(Settings=types.SimpleNamespace(new=lambda schema: FakeSettings(store)),
                                    Settings_sync=None)
        gio.Settings.sync = mock.Mock()
        repository = types.ModuleType("gi.repository")
        repository.Gio = gio
        gi = types.ModuleType("gi")
        gi.repository = repository
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}), \
                mock.patch.object(sys, "argv", ["-", previous_install]), contextlib.redirect_stdout(output):
            exec(compile(body, "install.sh:extension", "exec"), {"__name__": "__main__"})
        return output.getvalue().strip(), store

    def test_disabled_by_the_user_stays_disabled_on_update_and_on_fresh_install(self):
        for previous in ("0", "1"):
            state, store = self.run_snippet(previous, ["other@x"], [self.UUID])
            self.assertEqual(state, "disabled-by-user")
            self.assertEqual(store, {"enabled-extensions": ["other@x"], "disabled-extensions": [self.UUID]})

    def test_fresh_install_in_neither_list_enables(self):
        state, store = self.run_snippet("0", ["other@x"], [])
        self.assertEqual(state, "enabled-now")
        self.assertEqual(store["enabled-extensions"], ["other@x", self.UUID])

    def test_update_never_enables(self):
        state, store = self.run_snippet("1", [], [])
        self.assertEqual(state, "not-enabled")
        self.assertEqual(store["enabled-extensions"], [])
        state, store = self.run_snippet("1", [self.UUID], [])
        self.assertEqual(state, "enabled")

    def test_previous_install_is_detected_before_anything_changes(self):
        install = (REPO / "install.sh").read_text()
        detect = install.index("PREVIOUS_INSTALL=1")
        self.assertLess(detect, install.index("    backup_existing\n"))
        self.assertNotIn('d=[x for x in s.get_strv("disabled-extensions")', install)


class TrayStateRollbackTests(unittest.TestCase):
    def cleanup_block(self):
        install = (REPO / "install.sh").read_text()
        cleanup = install[install.index("cleanup() {"):]
        cleanup = cleanup[:cleanup.index("\n}\n")]
        start = cleanup.index("        if (( NOTIFIER_STOPPED == 1 )); then")
        end = cleanup.index("\n        fi\n", cleanup.index("NOTIFIER_WAS_ACTIVE == 1")) + len("\n        fi\n")
        return cleanup[start:end]

    def run_block(self, stopped, was_active):
        # The block silences each command; record the calls on fd 3.
        script = ("exec 3>&1\nrun_as_target() { printf '%s\\n' \"$*\" >&3; }\n"
                  f"NOTIFIER_STOPPED={stopped}\nNOTIFIER_WAS_ACTIVE={was_active}\n" + self.cleanup_block())
        return subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True,
                              timeout=30).stdout.splitlines()

    def test_rollback_restores_only_the_previous_tray_state(self):
        self.assertEqual(self.run_block(1, 1), ["systemctl --user daemon-reload",
                                                "systemctl --user start input-lock-notifier.service"])
        self.assertEqual(self.run_block(1, 0), ["systemctl --user daemon-reload"])
        self.assertEqual(self.run_block(0, 0), [])

    def test_state_recorded_before_stop_and_success_always_starts(self):
        install = (REPO / "install.sh").read_text()
        recorded = install.index("is-active --quiet input-lock-notifier.service; then\n            NOTIFIER_WAS_ACTIVE=1")
        self.assertLess(recorded, install.index("run_as_target systemctl --user stop input-lock-notifier.service"))
        session = install[install.index("setup_user_session() {"):]
        session = session[:session.index("\n}\n")]
        self.assertIn("run_as_target systemctl --user restart input-lock-notifier.service", session)
        self.assertNotIn("NOTIFIER_WAS_ACTIVE", session)

    def test_installers_parse(self):
        for script in ("install.sh", "update.sh", "uninstall.sh"):
            with self.subTest(script=script):
                subprocess.run(["bash", "-n", str(REPO / script)], check=True, timeout=30)


# ---------------------------------------------------------------------------
# U-R10: documentation matches the code
# ---------------------------------------------------------------------------
class DocumentationTests(unittest.TestCase):
    def test_readme_names_the_current_beta(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        version = (REPO / "VERSION").read_text().strip()
        self.assertIn(f"Version {version} source update", readme)
        self.assertIn(f"unzip locklock-{version}.zip", readme)
        self.assertNotIn("locklock-2.0.0-beta.4", readme)

    def test_relative_links_in_docs_resolve(self):
        for path in [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]:
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
                if "://" in target or target.startswith("mailto:"):
                    continue
                with self.subTest(document=path.name, target=target):
                    self.assertTrue((path.parent / target).exists())

    def test_cursor_extension_behaviour_is_described_consistently(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        architecture = (REPO / "docs" / "architecture.md").read_text(encoding="utf-8")
        self.assertNotIn("ואינו מושפע מפעילות Idle מדומה של RustDesk", readme)
        self.assertIn("RustDesk או של התקני uinput", readme)
        self.assertIn("including input injected by RustDesk", architecture)
        self.assertIn("never moves it out of `disabled-extensions`", architecture)
        self.assertIn("`user_initiated`", architecture)


if __name__ == "__main__":
    unittest.main()
