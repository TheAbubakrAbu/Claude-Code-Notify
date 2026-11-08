"""Tests for notify.py. Everything runs under NOTIFY_DRY_RUN, so nothing is
posted; the hook and its detached children write what they would have sent
to a temporary log, and the tests read that.

    python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "notify.py")


def iso(unix):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(unix)) + ".000Z"


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def load_module():
    spec = importlib.util.spec_from_file_location("notify", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-notify-")
        self.log = os.path.join(self.tmp, "notify.log")
        session = os.path.join(self.tmp, "sess")
        self.tasks = os.path.join(session, "tasks")
        self.scratch = os.path.join(session, "scratchpad")
        os.makedirs(self.tasks)
        os.makedirs(self.scratch)
        write(os.path.join(self.tasks, "run1.output"), "building...\n")
        write(os.path.join(self.tasks, "done1.output"), "ok\n\n[exited with code 0]\n")
        now = time.time()
        # A transcript whose newest entry predates the Stop: Claude never resumed.
        self.old_transcript = os.path.join(self.tmp, "old.jsonl")
        write(self.old_transcript, json.dumps({"type": "assistant", "timestamp": iso(now - 600)}) + "\n"
              + json.dumps({"type": "api-request-blob"}) + "\n")
        # A transcript with an entry well after the Stop: Claude resumed.
        self.new_transcript = os.path.join(self.tmp, "new.jsonl")
        write(self.new_transcript, json.dumps({"type": "assistant", "timestamp": iso(now - 600)}) + "\n"
              + json.dumps({"type": "user", "timestamp": iso(now + 120)}) + "\n")
        self.config = os.path.join(self.tmp, "notify.json")
        # "true" as the custom command guarantees one channel on every OS.
        write(self.config, json.dumps({"channels": {"command": ["true"]}}))
        self.settings = os.path.join(self.tmp, "settings.json")
        self.env = dict(
            os.environ,
            NOTIFY_DRY_RUN="1",
            NOTIFY_WATCH_SECONDS="1",
            CLAUDE_NOTIFY_LOG=self.log,
            CLAUDE_NOTIFY_CONFIG=self.config,
            CLAUDE_NOTIFY_SETTINGS=self.settings,
            CLAUDE_PROJECT_DIR=os.path.join(os.path.expanduser("~"), "code", "demo-project"),
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers -------------------------------------------------------------

    def payload(self, **fields):
        base = {
            "hook_event_name": "Stop",
            "session_id": "test",
            "cwd": self.tmp,
            "scratchpad_dir": self.scratch,
            "transcript_path": self.old_transcript,
            "background_tasks": [],
        }
        base.update(fields)
        return base

    def run_hook(self, payload, raw=None):
        started = time.time()
        subprocess.run([sys.executable, SCRIPT], input=raw if raw is not None else json.dumps(payload),
                       text=True, env=self.env, timeout=15, check=True)
        return time.time() - started

    def entries(self):
        try:
            with open(self.log) as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []

    def posted(self):
        return [e for e in self.entries() if e.get("dry_run")]

    def wait_for(self, predicate, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def shell(self, task_id, description, status="running"):
        return {"id": task_id, "type": "shell", "status": status, "description": description}

    # tests ---------------------------------------------------------------

    def test_plain_stop_posts_completed_with_project(self):
        self.run_hook(self.payload())
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertEqual(self.posted()[0]["message"], "Completed in ~/code/demo-project")
        self.assertIn("command", self.posted()[0]["channels"])

    def test_hook_returns_without_waiting_for_delivery(self):
        elapsed = self.run_hook(self.payload(background_tasks=[self.shell("run1", "Build the app")]))
        self.assertLess(elapsed, 1.5)

    def test_running_shell_is_silent_then_watcher_speaks(self):
        self.run_hook(self.payload(background_tasks=[
            self.shell("run1", "Build the Debug app"), self.shell("done1", "Earlier step", "completed")]))
        time.sleep(0.4)
        self.assertEqual(self.posted(), [], "a yield must not post")
        self.assertTrue(any("deferred" in e for e in self.entries()))
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        message = self.posted()[0]["message"]
        self.assertTrue(message.startswith("Turn ended"), message)
        self.assertIn("still waiting on: Build the Debug app", message)

    def test_exited_shell_without_resume_says_finished(self):
        self.run_hook(self.payload(background_tasks=[self.shell("done1", "Already exited")]))
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertTrue(self.posted()[0]["message"].endswith("its background work has finished"))

    def test_resumed_transcript_silences_the_watcher(self):
        self.run_hook(self.payload(transcript_path=self.new_transcript, background_tasks=[
            {"id": "ag1", "type": "subagent", "status": "running", "description": "Explore agent"}]))
        self.assertTrue(self.wait_for(lambda: any(e.get("watcher") == "resumed" for e in self.entries())))
        time.sleep(0.3)
        self.assertEqual(self.posted(), [])

    def test_two_pending_tasks_are_counted(self):
        self.run_hook(self.payload(background_tasks=[
            self.shell("run1", "Profile the scroll"), self.shell("zzz", "Second one", "pending")]))
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertIn("Profile the scroll (+1 more)", self.posted()[0]["message"])

    def test_stop_failure_names_the_error(self):
        self.run_hook(self.payload(hook_event_name="StopFailure", error="rate_limit"))
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertEqual(self.posted()[0]["message"], "Stopped with an error: rate limit")

    def test_stop_failure_ignores_pending_tasks(self):
        self.run_hook(self.payload(hook_event_name="StopFailure", error="api_error",
                                   background_tasks=[self.shell("run1", "Build")]))
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertEqual(self.posted()[0]["message"], "Stopped with an error: api error")

    def test_notification_question_is_reworded(self):
        self.run_hook(self.payload(hook_event_name="Notification",
                                   message="Claude needs your permission to use AskUserQuestion"))
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertEqual(self.posted()[0]["message"], "Claude has a question for you")

    def test_include_last_message(self):
        write(self.config, json.dumps({"include_last_message": True, "channels": {"command": ["true"]}}))
        self.run_hook(self.payload(last_assistant_message="Build green.\n\nAll tests pass."))
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertEqual(self.posted()[0]["message"],
                         "Completed in ~/code/demo-project: Build green. All tests pass.")

    def test_garbage_stdin_still_notifies(self):
        self.run_hook(None, raw="this is not json")
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertTrue(self.posted()[0]["message"].startswith("Completed in"))

    def test_config_command_reports_paths_and_channels(self):
        out = subprocess.run([sys.executable, SCRIPT, "--config"], env=self.env, text=True,
                             capture_output=True, timeout=15, check=True).stdout
        info = json.loads(out)
        self.assertEqual(info["config_path"], self.config)
        self.assertEqual(info["log_path"], self.log)
        self.assertIn("command", info["channels_in_use"])
        self.assertEqual(info["config"]["wait_minutes"], 15)

    def test_test_command_dry_run(self):
        result = subprocess.run([sys.executable, SCRIPT, "--test"], env=self.env, text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("command", result.stdout)
        self.assertIn("dry run", result.stdout)

    # phone switch ----------------------------------------------------------

    def run_cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT] + list(args), env=self.env, text=True,
                              capture_output=True, timeout=15)

    def read(self, path):
        with open(path) as fh:
            return json.load(fh)

    def test_phone_off_skips_phone_channels_only(self):
        write(self.config, json.dumps({"phone": False, "channels": {
            "command": ["true"], "ntfy": {"topic": "claude-test"}}}))
        self.run_hook(self.payload())
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        channels = self.posted()[0]["channels"]
        self.assertNotIn("ntfy", channels)
        self.assertIn("command", channels)

    def test_phone_on_by_default_keeps_ntfy(self):
        write(self.config, json.dumps({"channels": {"command": ["true"], "ntfy": {"topic": "claude-test"}}}))
        self.run_hook(self.payload())
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertIn("ntfy", self.posted()[0]["channels"])

    def test_phone_off_writes_config_and_claude_settings(self):
        write(self.settings, json.dumps({"theme": "dark", "agentPushNotifEnabled": True,
                                         "inputNeededNotifEnabled": True}))
        result = self.run_cli("--phone", "off")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIs(self.read(self.config)["phone"], False)
        self.assertEqual(self.read(self.config)["channels"], {"command": ["true"]}, "other keys kept")
        settings = self.read(self.settings)
        self.assertIs(settings["agentPushNotifEnabled"], False)
        self.assertIs(settings["inputNeededNotifEnabled"], False)
        self.assertEqual(settings["theme"], "dark", "other settings kept")
        backups = [n for n in os.listdir(self.tmp) if n.startswith("settings.json.bak-")]
        self.assertEqual(len(backups), 1, "settings.json is backed up before it is changed")

    def test_phone_on_turns_everything_back_on(self):
        write(self.settings, json.dumps({"agentPushNotifEnabled": False}))
        self.run_cli("--phone", "off")
        result = self.run_cli("--phone", "on")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIs(self.read(self.config)["phone"], True)
        self.assertIs(self.read(self.settings)["agentPushNotifEnabled"], True)
        self.assertIs(self.read(self.settings)["inputNeededNotifEnabled"], True)

    def test_phone_switch_twice_is_a_no_op(self):
        self.run_cli("--phone", "off")
        before = sorted(os.listdir(self.tmp))
        result = self.run_cli("--phone", "off")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(os.listdir(self.tmp)), before, "no new backup when nothing changes")

    def test_phone_without_value_reports_and_changes_nothing(self):
        write(self.settings, json.dumps({"agentPushNotifEnabled": False}))
        result = self.run_cli("--phone")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("agentPushNotifEnabled", result.stdout)
        self.assertEqual(self.read(self.config), {"channels": {"command": ["true"]}})
        self.assertEqual(self.read(self.settings), {"agentPushNotifEnabled": False})

    def test_phone_rejects_unknown_value(self):
        result = self.run_cli("--phone", "maybe")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.read(self.config), {"channels": {"command": ["true"]}})

    def test_phone_refuses_to_overwrite_broken_settings(self):
        write(self.settings, "{ not json")
        result = self.run_cli("--phone", "off")
        self.assertEqual(result.returncode, 1)
        with open(self.settings) as fh:
            self.assertEqual(fh.read(), "{ not json")

    def test_config_command_reports_phone(self):
        write(self.config, json.dumps({"phone": False, "channels": {"command": ["true"], "ntfy": {"topic": "t"}}}))
        info = json.loads(self.run_cli("--config").stdout)
        self.assertIs(info["config"]["phone"], False)
        self.assertNotIn("ntfy", info["channels_in_use"])


class UnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["CLAUDE_NOTIFY_LOG"] = os.path.join(tempfile.mkdtemp(prefix="claude-notify-unit-"), "notify.log")
        cls.notify = load_module()

    def name_for(self, path):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        return self.notify.project_name({"cwd": path})

    def test_project_name_keeps_two_segments_under_home(self):
        home = os.path.realpath(os.path.expanduser("~"))
        self.assertEqual(self.name_for(home), "~")
        self.assertEqual(self.name_for(os.path.join(home, "code")), "~/code")
        self.assertEqual(self.name_for(os.path.join(home, "code", "app")), "~/code/app")
        self.assertEqual(self.name_for(os.path.join(home, "a", "b", "c")), "~/b/c")

    def test_project_name_outside_home_keeps_leading_slash(self):
        self.assertEqual(self.name_for("/usr/local/share/thing"), "/share/thing")

    def test_deep_merge_overrides_nested_keys_only(self):
        merged = self.notify.deep_merge(self.notify.DEFAULTS, {"channels": {"ntfy": {"topic": "abc"}}})
        self.assertEqual(merged["channels"]["ntfy"]["topic"], "abc")
        self.assertEqual(merged["channels"]["ntfy"]["server"], "https://ntfy.sh")
        self.assertTrue(merged["channels"]["macos"])
        self.assertEqual(merged["wait_minutes"], 15)

    def test_channels_in_use_needs_complete_settings(self):
        channels = json.loads(json.dumps(self.notify.DEFAULTS["channels"]))
        channels["macos"] = channels["linux"] = False
        self.assertEqual(self.notify.channels_in_use(channels), [])
        channels["pushover"]["token"] = "t"
        self.assertEqual(self.notify.channels_in_use(channels), [])
        channels["pushover"]["user"] = "u"
        channels["ntfy"]["topic"] = "x"
        channels["webhook"]["url"] = "https://example.invalid/hook"
        self.assertEqual(self.notify.channels_in_use(channels), ["ntfy", "pushover", "webhook"])
        self.assertEqual(self.notify.channels_in_use(channels, phone=False), ["webhook"])

    def test_switch_reads_config_and_command_line_values(self):
        switch = self.notify.switch
        for on in (True, "on", "ON", "true", "yes", "1", 1):
            self.assertIs(switch(on), True, on)
        for off in (False, "off", "false", "no", "0", 0):
            self.assertIs(switch(off), False, off)
        self.assertIs(switch(None), True, "missing means the default")
        self.assertIsNone(switch("maybe", None))

    def test_pending_tasks_filters_by_status(self):
        payload = {"background_tasks": [
            {"id": "a", "status": "running"}, {"id": "b", "status": "completed"},
            {"id": "c", "status": "pending"}, "junk"]}
        self.assertEqual([t["id"] for t in self.notify.pending_tasks(payload)], ["a", "c"])


if __name__ == "__main__":
    unittest.main()
