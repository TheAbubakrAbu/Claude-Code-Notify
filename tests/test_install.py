"""Tests for install.sh. Every run points at a throwaway HOME, settings.json, and hooks folder,
under NOTIFY_DRY_RUN, so your real ~/.claude is never touched and nothing is posted.

    python3 -m unittest discover -s tests -v
"""
import glob
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.sh")


class Installer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-notify-install-")
        self.home = os.path.join(self.tmp, "home")
        self.dest = os.path.join(self.tmp, "hooks")
        self.settings = os.path.join(self.tmp, "settings.json")
        os.makedirs(self.home)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_install(self, *args, default_paths=False):
        env = dict(os.environ, HOME=self.home, NOTIFY_DRY_RUN="1",
                   CLAUDE_NOTIFY_LOG=os.path.join(self.tmp, "notify.log"))
        for key in ("CLAUDE_NOTIFY_CONFIG", "CLAUDE_NOTIFY_SETTINGS"):
            env.pop(key, None)
        paths = [] if default_paths else ["--dest", self.dest, "--settings", self.settings]
        return subprocess.run(["bash", INSTALL] + paths + list(args), env=env,
                              capture_output=True, text=True, timeout=60)

    def read(self, path=None):
        with open(path or self.settings) as fh:
            return json.load(fh)

    def write(self, data):
        with open(self.settings, "w") as fh:
            json.dump(data, fh)

    def ours(self, data, event):
        return [h for g in data.get("hooks", {}).get(event, [])
                for h in g.get("hooks", []) if "notify.py" in h.get("command", "")]

    def backups(self):
        return glob.glob(self.settings + ".bak-*")

    def test_fresh_install_wires_stop_and_stop_failure(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.read()
        self.assertEqual(sorted(data["hooks"]), ["Stop", "StopFailure"])
        cmd = 'python3 "%s/notify.py"' % self.dest
        for event in ("Stop", "StopFailure"):
            self.assertEqual(data["hooks"][event], [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}])
        installed = os.path.join(self.dest, "notify.py")
        self.assertTrue(os.access(installed, os.X_OK))
        with open(installed) as a, open(os.path.join(ROOT, "notify.py")) as b:
            self.assertEqual(a.read(), b.read())
        with open(os.path.join(self.dest, "notify.json")) as a, open(os.path.join(ROOT, "notify.example.json")) as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(self.backups(), [])

    def test_second_run_changes_nothing_and_keeps_your_config(self):
        self.run_install()
        config = os.path.join(self.dest, "notify.json")
        with open(config, "w") as fh:
            fh.write('{"sound": ""}')
        result = self.run_install()
        self.assertIn("settings already up to date", result.stdout)
        with open(config) as fh:
            self.assertEqual(fh.read(), '{"sound": ""}')
        self.assertEqual(self.backups(), [])

    def test_notification_event_is_opt_in(self):
        self.run_install("--events", "Stop,StopFailure,Notification")
        self.assertEqual(sorted(self.read()["hooks"]), ["Notification", "Stop", "StopFailure"])

    def test_keeps_everything_else_and_backs_up(self):
        other = {"type": "command", "command": "python3 other.py"}
        self.write({"model": "opus", "hooks": {"Stop": [{"hooks": [other]}],
                                               "PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}})
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.read()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"]["PreToolUse"], [{"matcher": "Bash", "hooks": [other]}])
        self.assertIn({"hooks": [other]}, data["hooks"]["Stop"])
        self.assertEqual(len(self.ours(data, "Stop")), 1)
        self.assertEqual(len(self.backups()), 1)

    def test_existing_entry_is_updated_in_place(self):
        old = {"type": "command", "command": "python3 /old/place/notify.py", "timeout": 30}
        self.write({"hooks": {"Stop": [{"hooks": [old]}]}})
        self.run_install("--events", "Stop")
        hooks = self.ours(self.read(), "Stop")
        self.assertEqual(hooks, [{"type": "command", "command": 'python3 "%s/notify.py"' % self.dest, "timeout": 30}])

    def test_default_location_stores_a_portable_command(self):
        result = self.run_install(default_paths=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.read(os.path.join(self.home, ".claude", "settings.json"))
        self.assertEqual(self.ours(data, "Stop")[0]["command"], 'python3 "$HOME/.claude/hooks/notify.py"')

    def test_phone_off_turns_off_every_route(self):
        result = self.run_install("--phone", "off")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.read()
        self.assertIs(data["agentPushNotifEnabled"], False)
        self.assertIs(data["inputNeededNotifEnabled"], False)
        self.assertIs(self.read(os.path.join(self.dest, "notify.json"))["phone"], False)

    def test_phone_rejects_other_values(self):
        result = self.run_install("--phone", "maybe")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(self.settings))

    def test_uninstall_removes_only_ours_and_keeps_files(self):
        other = {"type": "command", "command": "other.sh"}
        self.write({"hooks": {"Stop": [{"hooks": [other]}]}})
        self.run_install()
        result = self.run_install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read()["hooks"], {"Stop": [{"hooks": [other]}]})
        self.assertTrue(os.path.exists(os.path.join(self.dest, "notify.py")))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "notify.json")))

    def test_uninstall_purge_deletes_the_files(self):
        self.run_install()
        open(os.path.join(self.dest, "notify.log"), "w").close()
        result = self.run_install("--uninstall", "--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read(), {})
        for name in ("notify.py", "notify.json", "notify.log"):
            self.assertFalse(os.path.exists(os.path.join(self.dest, name)), name)

    def test_refuses_settings_that_are_not_an_object(self):
        with open(self.settings, "w") as fh:
            fh.write("[]")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        with open(self.settings) as fh:
            self.assertEqual(fh.read(), "[]")

    def test_unknown_option_and_help(self):
        self.assertEqual(self.run_install("--bogus").returncode, 2)
        result = subprocess.run(["bash", INSTALL, "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--uninstall", result.stdout)


if __name__ == "__main__":
    unittest.main()
