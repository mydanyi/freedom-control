import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))

from armada_control import home_power
from armada_control.home_power import HomePower, HomePowerError, Session

SETTINGS_NAME = "home-power.json"


def default_props():
    return {
        home_power.PROP_SUPPORTED: 60,
        home_power.PROP_FEEDBACK: 60,
        home_power.PROP_FOCUSED: 769,
    }


class FakeX:
    """Minimal stand-in for the root window property store xprop talks to."""

    def __init__(self, props=None, fail_set=False, raw=None):
        self.props = default_props() if props is None else dict(props)
        self.fail_set = fail_set
        self.raw = dict(raw or {})
        self.commands = []

    def __call__(self, cmd, timeout=None):
        cmd = list(cmd)
        self.commands.append(cmd)
        try:
            index = cmd.index(home_power.XPROP)
        except ValueError:
            raise AssertionError(f"unexpected command: {cmd}")
        tokens = cmd[index + 1:]
        if len(tokens) < 3 or tokens[0] != "-display" or tokens[2] != "-root":
            raise AssertionError(f"unexpected xprop argv: {cmd}")
        args = tokens[3:]

        if args[:1] == ["-f"]:
            # -f NAME 32c -set NAME VALUE
            name, value = args[4], args[5]
            if self.fail_set:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            self.props[name] = int(value)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if args[:1] == ["-remove"]:
            if self.fail_set:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            self.props.pop(args[1], None)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if len(args) != 1:
            raise AssertionError(f"unexpected xprop read argv: {cmd}")

        name = args[0]
        if name in self.raw:
            return subprocess.CompletedProcess(cmd, 0, self.raw[name], "")
        if name not in self.props:
            return subprocess.CompletedProcess(cmd, 1, f"{name}:  not found.\n", "")
        return subprocess.CompletedProcess(cmd, 0, f"{name}(CARDINAL) = {self.props[name]}\n", "")

    def mutations(self):
        found = []
        for cmd in self.commands:
            index = cmd.index(home_power.XPROP)
            args = cmd[index + 1:][3:]
            if args[:1] in (["-f"], ["-remove"]):
                found.append(args)
        return found

    def touched(self):
        names = []
        for args in self.mutations():
            names.append(args[1] if args[0] == "-remove" else args[-2])
        return names


SESSION = Session(user="deck", uid=0, display=":0", key="100:1")


class HomePowerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = tmp.name
        self.session = SESSION
        self.x = FakeX()

    def build(self, **kwargs):
        kwargs.setdefault("run", self.x)
        kwargs.setdefault("session", lambda: self.session)
        kwargs.setdefault("settings_dir", self.settings)
        return HomePower(**kwargs)

    def settings_path(self):
        return Path(self.settings) / SETTINGS_NAME

    def saved_preference(self):
        return json.loads(self.settings_path().read_text(encoding="utf-8"))

    def save_preference(self, enabled):
        self.settings_path().write_text(json.dumps({"enabled": enabled}), encoding="utf-8")


class DefaultStateTests(HomePowerTest):
    def test_default_on_is_read_only(self):
        power = self.build()

        state = power.get_state()

        self.assertEqual(
            state,
            {"enabled": True, "supported": True, "active": False, "reason": "not-requested"},
        )
        self.assertEqual(self.x.mutations(), [])
        self.assertFalse(self.settings_path().exists())

    def test_stored_off_survives_new_instance_and_sync(self):
        self.save_preference(False)

        self.assertFalse(self.build().get_state()["enabled"])

        power = self.build()
        self.assertFalse(power.get_state()["enabled"])
        state = power.sync()

        self.assertEqual(
            state,
            {"enabled": False, "supported": True, "active": False, "reason": "off"},
        )
        self.assertEqual(self.x.mutations(), [])
        self.assertEqual(self.saved_preference(), {"enabled": False})

    def test_missing_settings_dir_reports_settings_missing(self):
        with mock.patch.dict(os.environ, {"DECKY_PLUGIN_SETTINGS_DIR": ""}):
            power = HomePower(run=self.x, session=lambda: self.session, settings_dir=None)
            state = power.get_state()

        self.assertEqual(state["reason"], "settings-missing")
        self.assertFalse(state["enabled"])
        self.assertEqual(self.x.mutations(), [])


class EnableViewTests(HomePowerTest):
    def test_enable_requests_60_and_persists_preference(self):
        power = self.build()

        state = power.set_enabled(True)

        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)
        self.assertEqual(self.x.touched(), [home_power.PROP_REQUEST])
        self.assertEqual(self.saved_preference(), {"enabled": True})
        self.assertEqual(
            state,
            {"enabled": True, "supported": True, "active": True, "reason": "active"},
        )

    def test_repeated_enable_does_not_add_mutations(self):
        power = self.build()
        power.set_enabled(True)
        mutations = len(self.x.mutations())

        state = power.set_enabled(True)

        self.assertEqual(len(self.x.mutations()), mutations)
        self.assertTrue(state["active"])

    def test_active_requires_feedback_and_steam_focus(self):
        power = self.build()
        power.set_enabled(True)
        self.assertEqual(power.get_state()["reason"], "active")

        self.x.props[home_power.PROP_FOCUSED] = 0
        state = power.get_state()
        self.assertEqual((state["active"], state["reason"]), (False, "focus-mismatch"))

        self.x.props[home_power.PROP_FOCUSED] = 769
        self.x.props[home_power.PROP_FEEDBACK] = 61
        state = power.get_state()
        self.assertEqual((state["active"], state["reason"]), (False, "feedback-mismatch"))

        self.x.props[home_power.PROP_FEEDBACK] = 60
        self.x.props.pop(home_power.PROP_REQUEST)
        state = power.get_state()
        self.assertEqual((state["active"], state["reason"]), (False, "not-requested"))

    def test_feedback_up_to_target_is_active(self):
        for value in (40, 50, 60):
            with self.subTest(feedback=value):
                self.x = FakeX()
                power = self.build()
                power.set_enabled(True)
                self.x.props[home_power.PROP_FEEDBACK] = value

                state = power.get_state()

                self.assertEqual(
                    state,
                    {"enabled": True, "supported": True, "active": True, "reason": "active"},
                )
                self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)

    def test_feedback_outside_range_or_unparsable_is_inactive(self):
        for value in (0, 61):
            with self.subTest(feedback=value):
                self.x = FakeX()
                power = self.build()
                power.set_enabled(True)
                self.x.props[home_power.PROP_FEEDBACK] = value

                state = power.get_state()

                self.assertEqual((state["active"], state["reason"]), (False, "feedback-mismatch"))
                self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)

        with self.subTest(feedback="unparsable"):
            payload = f"{home_power.PROP_FEEDBACK}(CARDINAL) = sixty\n"
            self.x = FakeX(raw={home_power.PROP_FEEDBACK: payload})
            power = self.build()
            power.set_enabled(True)

            state = power.get_state()

            self.assertEqual((state["active"], state["reason"]), (False, "feedback-mismatch"))
            self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)

    def test_request_alone_is_never_active(self):
        power = self.build()
        power.set_enabled(False)
        self.x.props[home_power.PROP_REQUEST] = 60

        state = power.get_state()

        self.assertEqual(
            state,
            {"enabled": False, "supported": True, "active": False, "reason": "off"},
        )

    def test_enable_without_support_raises_and_changes_nothing(self):
        self.x.props.pop(home_power.PROP_SUPPORTED)
        power = self.build()

        with self.assertRaises(HomePowerError) as raised:
            power.set_enabled(True)

        self.assertEqual(raised.exception.reason, "unsupported")
        self.assertNotIn(home_power.PROP_REQUEST, self.x.props)
        self.assertEqual(self.x.mutations(), [])
        self.assertFalse(self.settings_path().exists())

    def test_enable_with_zero_support_raises(self):
        self.x.props[home_power.PROP_SUPPORTED] = 0
        power = self.build()

        with self.assertRaises(HomePowerError) as raised:
            power.set_enabled(True)

        self.assertEqual(raised.exception.reason, "unsupported")
        self.assertEqual(self.x.mutations(), [])

    def test_enable_without_compositor_raises(self):
        self.session = None
        power = self.build()

        with self.assertRaises(HomePowerError) as raised:
            power.set_enabled(True)

        self.assertEqual(raised.exception.reason, "no-compositor")
        self.assertEqual(self.x.commands, [])
        self.assertFalse(self.settings_path().exists())

    def test_enable_write_failure_keeps_preference(self):
        self.x.fail_set = True
        power = self.build()

        with self.assertRaises(HomePowerError) as raised:
            power.set_enabled(True)

        self.assertEqual(raised.exception.reason, "request-failed")
        self.assertNotIn(home_power.PROP_REQUEST, self.x.props)
        self.assertFalse(self.settings_path().exists())
        self.assertFalse(power.get_state()["active"])

    def test_enable_without_settings_dir_raises_before_touching_property(self):
        with mock.patch.dict(os.environ, {"DECKY_PLUGIN_SETTINGS_DIR": ""}):
            power = HomePower(run=self.x, session=lambda: self.session, settings_dir=None)
            with self.assertRaises(HomePowerError) as raised:
                power.set_enabled(True)

        self.assertEqual(raised.exception.reason, "settings-missing")
        self.assertEqual(self.x.commands, [])

    def test_enable_never_touches_game_or_global_properties(self):
        power = self.build()
        power.set_enabled(True)
        power.set_enabled(False)
        power.restore()

        touched = self.x.touched()
        self.assertTrue(touched)
        self.assertEqual(set(touched), {home_power.PROP_REQUEST})

    def test_commands_are_argv_and_switch_user_when_needed(self):
        with mock.patch.object(home_power, "current_uid", return_value=1000):
            self.build().set_enabled(True)

        first = self.x.commands[0]
        self.assertEqual(
            first[:5],
            [home_power.RUNUSER, "-u", "deck", "--", home_power.XPROP],
        )
        for cmd in self.x.commands:
            self.assertIsInstance(cmd, list)
            self.assertTrue(all(isinstance(part, str) for part in cmd))

    def test_commands_skip_runuser_for_same_user(self):
        with mock.patch.object(home_power, "current_uid", return_value=0):
            self.build().set_enabled(True)

        self.assertEqual(self.x.commands[0][0], home_power.XPROP)


class DisableRestoreTests(HomePowerTest):
    def test_restore_reinstates_original_value(self):
        self.x.props[home_power.PROP_REQUEST] = 120
        power = self.build()
        power.set_enabled(True)
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)

        power.restore()

        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 120)
        self.assertEqual(self.saved_preference(), {"enabled": True})

    def test_restore_removes_property_that_was_absent(self):
        power = self.build()
        power.set_enabled(True)

        power.restore()

        self.assertNotIn(home_power.PROP_REQUEST, self.x.props)

    def test_repeated_enable_keeps_first_snapshot(self):
        self.x.props[home_power.PROP_REQUEST] = 120
        power = self.build()
        power.set_enabled(True)
        self.x.props[home_power.PROP_REQUEST] = 60

        power.set_enabled(True)
        power.restore()

        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 120)

    def test_disable_reinstates_original_and_persists_off(self):
        self.x.props[home_power.PROP_REQUEST] = 120
        power = self.build()
        power.set_enabled(True)

        state = power.set_enabled(False)

        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 120)
        self.assertEqual(self.saved_preference(), {"enabled": False})
        self.assertEqual(
            state,
            {"enabled": False, "supported": True, "active": False, "reason": "off"},
        )

    def test_restore_is_idempotent_and_keeps_preference(self):
        power = self.build()
        power.set_enabled(True)

        power.restore()
        mutations = len(self.x.mutations())
        power.restore()

        self.assertEqual(len(self.x.mutations()), mutations)
        self.assertEqual(self.saved_preference(), {"enabled": True})

    def test_disable_without_compositor_persists_off(self):
        self.save_preference(True)
        self.session = None
        power = self.build()

        state = power.set_enabled(False)

        self.assertEqual(self.saved_preference(), {"enabled": False})
        self.assertFalse(state["enabled"])
        self.assertFalse(state["active"])
        self.assertEqual(self.x.commands, [])


class CompositorRestartTests(HomePowerTest):
    def test_restart_does_not_replay_old_snapshot(self):
        self.x.props[home_power.PROP_REQUEST] = 120
        power = self.build()
        power.set_enabled(True)

        self.session = Session(user="deck", uid=0, display=":0", key="200:9")
        self.x.props.pop(home_power.PROP_REQUEST)

        state = power.get_state()
        self.assertEqual((state["active"], state["reason"]), (False, "not-requested"))

        power.sync()
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)

        power.restore()
        self.assertNotIn(home_power.PROP_REQUEST, self.x.props)

    def test_sync_reapplies_saved_preference_after_restart(self):
        self.save_preference(True)
        power = self.build()
        self.assertFalse(power.get_state()["active"])

        state = power.sync()

        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)
        self.assertEqual(
            state,
            {"enabled": True, "supported": True, "active": True, "reason": "active"},
        )

    def test_sync_without_compositor_reports_pending(self):
        self.save_preference(True)
        self.session = None
        power = self.build()

        state = power.sync()

        self.assertEqual(
            state,
            {"enabled": True, "supported": False, "active": False, "reason": "no-compositor"},
        )
        self.assertEqual(self.x.commands, [])
        self.assertEqual(self.saved_preference(), {"enabled": True})


class InvalidRequestPropertyTests(HomePowerTest):
    INVALID_PAYLOADS = (
        f"{home_power.PROP_REQUEST}(CARDINAL) = sixty\n",
        f"{home_power.PROP_REQUEST}(CARDINAL) = 60, 61\n",
        f"{home_power.PROP_REQUEST}(CARDINAL) = -1\n",
        f"{home_power.PROP_REQUEST}(CARDINAL) = 4294967296\n",
        f'{home_power.PROP_REQUEST}(STRING) = "60"\n',
    )

    def test_present_but_unrestorable_request_rejects_before_mutation(self):
        for payload in self.INVALID_PAYLOADS:
            with self.subTest(payload=payload):
                self.x = FakeX(raw={home_power.PROP_REQUEST: payload})
                power = self.build()

                with self.assertRaises(HomePowerError) as raised:
                    power.set_enabled(True)

                self.assertEqual(raised.exception.reason, "invalid-request-property")
                self.assertEqual(self.x.mutations(), [])
                self.assertEqual(self.x.props, default_props())
                self.assertFalse(self.settings_path().exists())


class WriteFailureRollbackTests(HomePowerTest):
    def fail_preference_write(self, fail_restore=False):
        def writer(path, enabled):
            if fail_restore:
                self.x.fail_set = True
            raise HomePowerError("settings-write-failed")

        return mock.patch.object(
            home_power.HomePower, "_write_preference", side_effect=writer
        )

    def test_save_failure_with_successful_rollback_keeps_original(self):
        self.x.props[home_power.PROP_REQUEST] = 0
        self.save_preference(False)
        power = self.build()

        with self.fail_preference_write():
            with self.assertRaises(HomePowerError) as raised:
                power.set_enabled(True)

        self.assertEqual(raised.exception.reason, "settings-write-failed")
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 0)
        self.assertEqual(self.saved_preference(), {"enabled": False})

    def test_save_failure_with_failed_rollback_stays_retryable(self):
        self.x.props[home_power.PROP_REQUEST] = 0
        self.save_preference(False)
        power = self.build()

        with self.fail_preference_write(fail_restore=True):
            with self.assertRaises(HomePowerError) as raised:
                power.set_enabled(True)

        self.assertEqual(raised.exception.reason, "restore-failed")
        self.assertIsInstance(raised.exception.__cause__, HomePowerError)
        self.assertEqual(raised.exception.__cause__.reason, "settings-write-failed")
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)
        self.assertEqual(self.saved_preference(), {"enabled": False})

        state = power.get_state()
        self.assertEqual(set(state), {"enabled", "supported", "active", "reason"})
        self.assertFalse(state["enabled"])
        self.assertFalse(state["active"])
        self.assertEqual(state["reason"], "restore-failed")

        self.x.fail_set = False
        power.restore()
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 0)
        self.assertEqual(power.get_state()["reason"], "off")


class RestoreFailureTests(HomePowerTest):
    def test_disable_restore_failure_keeps_preference_until_retry(self):
        self.x.props[home_power.PROP_REQUEST] = 120
        power = self.build()
        power.set_enabled(True)
        self.x.fail_set = True

        with self.assertRaises(HomePowerError) as raised:
            power.set_enabled(False)

        self.assertEqual(raised.exception.reason, "restore-failed")
        self.assertEqual(self.saved_preference(), {"enabled": True})
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)

        self.x.fail_set = False
        state = power.set_enabled(False)

        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 120)
        self.assertEqual(self.saved_preference(), {"enabled": False})
        self.assertEqual(
            state,
            {"enabled": False, "supported": True, "active": False, "reason": "off"},
        )

    def test_public_restore_failure_is_observable_and_retryable(self):
        self.x.props[home_power.PROP_REQUEST] = 0
        power = self.build()
        power.set_enabled(True)
        self.x.fail_set = True

        with self.assertRaises(HomePowerError) as raised:
            power.restore()

        self.assertEqual(raised.exception.reason, "restore-failed")
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)

        self.x.fail_set = False
        power.restore()
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 0)

        mutations = len(self.x.mutations())
        power.restore()
        self.assertEqual(len(self.x.mutations()), mutations)
        self.assertEqual(self.saved_preference(), {"enabled": True})

    def test_unresolved_session_retains_snapshot_for_same_session(self):
        self.x.props[home_power.PROP_REQUEST] = 0
        power = self.build()
        power.set_enabled(True)
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)

        def ambiguous():
            raise HomePowerError("ambiguous-compositor")

        power._session = ambiguous
        state = power.restore()

        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)
        self.assertEqual(state["reason"], "ambiguous-compositor")
        self.assertEqual(self.saved_preference(), {"enabled": True})

        with self.assertRaises(HomePowerError) as raised:
            power.set_enabled(False)

        self.assertEqual(raised.exception.reason, "ambiguous-compositor")
        self.assertEqual(self.saved_preference(), {"enabled": True})
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 60)

        power._session = lambda: self.session
        power.restore()
        self.assertEqual(self.x.props[home_power.PROP_REQUEST], 0)


if __name__ == "__main__":
    unittest.main()
