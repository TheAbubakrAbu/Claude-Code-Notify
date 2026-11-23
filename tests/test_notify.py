"""Tests for notify.py. Nothing is ever posted. The hook tests run under
NOTIFY_DRY_RUN: the hook and its detached children write what they would have
sent to a temporary log, and the tests read that. The sending code a dry run
skips is tested with both desktop channels off, a command that only writes to
a file, and the banner and network calls recorded instead of made.

    python3 -m unittest discover -s tests -v
"""
import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from unittest import mock

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

    def test_an_entry_inside_the_grace_period_is_not_claude_resuming(self):
        # Claude Code appends a stop_hook_summary entry a moment after the hook runs. That is not
        # Claude coming back, so an entry within resume_grace_seconds must not silence the watcher.
        transcript = os.path.join(self.tmp, "summary.jsonl")
        write(transcript, json.dumps({"type": "system", "timestamp": iso(time.time() + 10)}) + "\n")
        self.run_hook(self.payload(transcript_path=transcript,
                                   background_tasks=[self.shell("run1", "Build the app")]))
        self.assertTrue(self.wait_for(lambda: len(self.posted()) == 1))
        self.assertIn("still waiting on: Build the app", self.posted()[0]["message"])

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

    def test_phone_refuses_settings_that_are_not_an_object(self):
        write(self.settings, "[1, 2]")
        result = self.run_cli("--phone", "off")
        self.assertEqual(result.returncode, 1)
        with open(self.settings) as fh:
            self.assertEqual(fh.read(), "[1, 2]")
        self.assertEqual(self.read(self.config), {"channels": {"command": ["true"]}})

    def test_config_command_reports_phone(self):
        write(self.config, json.dumps({"phone": False, "channels": {"command": ["true"], "ntfy": {"topic": "t"}}}))
        info = json.loads(self.run_cli("--config").stdout)
        self.assertIs(info["config"]["phone"], False)
        self.assertNotIn("ntfy", info["channels_in_use"])


class DeliveryTests(unittest.TestCase):
    """The real sending path, which NOTIFY_DRY_RUN skips. Nothing leaves this machine and no banner
    appears: both desktop channels are off, and the one channel is a command that only writes
    what it was handed to a file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-notify-send-")
        self.log = os.path.join(self.tmp, "notify.log")
        self.config = os.path.join(self.tmp, "notify.json")
        self.got = os.path.join(self.tmp, "got.json")
        self.recorder = os.path.join(self.tmp, "recorder.py")
        write(self.recorder, "import json, sys\nwith open(sys.argv[1], 'w') as fh:\n    json.dump(sys.argv[2:], fh)\n")
        # Stand-ins for the banner tools, first on the PATH. A notify.py that wrongly turned a desktop
        # channel on would run these, not the real ones: no banner, and the test says so.
        self.fakes = os.path.join(self.tmp, "fakes")
        self.banners = os.path.join(self.tmp, "banners.log")
        os.makedirs(self.fakes)
        for tool in ("osascript", "notify-send"):
            write(os.path.join(self.fakes, tool), "#!/bin/sh\necho %s >> '%s'\n" % (tool, self.banners))
            os.chmod(os.path.join(self.fakes, tool), 0o755)
        self.env = dict(
            os.environ,
            PATH=self.fakes + os.pathsep + os.environ.get("PATH", ""),
            CLAUDE_NOTIFY_LOG=self.log,
            CLAUDE_NOTIFY_CONFIG=self.config,
            CLAUDE_NOTIFY_SETTINGS=os.path.join(self.tmp, "settings.json"),
            CLAUDE_PROJECT_DIR=os.path.join(os.path.expanduser("~"), "code", "demo-project"),
        )
        self.env.pop("NOTIFY_DRY_RUN", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def deliver(self, command):
        """Run the hook for a finished turn with `command` as the only channel; returns the log
        entry the sending child writes once it is done."""
        write(self.config, json.dumps({"channels": {"macos": False, "linux": False, "command": command}}))
        subprocess.run([sys.executable, SCRIPT], input=json.dumps({"hook_event_name": "Stop", "cwd": self.tmp}),
                       text=True, env=self.env, timeout=15, check=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with open(self.log) as fh:
                    done = [e for e in map(json.loads, fh) if "sent" in e]
            except (FileNotFoundError, ValueError):
                done = []
            if done:
                self.assertFalse(os.path.exists(self.banners), "a desktop channel that is off was used")
                return done[0]
            time.sleep(0.1)
        self.fail("the sending child never reported")

    def test_the_command_channel_runs_with_title_subtitle_and_message(self):
        entry = self.deliver([sys.executable, self.recorder, self.got])
        self.assertEqual((entry["sent"], entry["failed"]), (["command"], []))
        with open(self.got) as fh:
            self.assertEqual(json.load(fh), ["Claude Code", "~/code/demo-project",
                                             "Completed in ~/code/demo-project"])

    def test_a_channel_that_fails_is_logged_as_failed(self):
        entry = self.deliver([sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(entry["sent"], [])
        self.assertEqual([name for name, _detail in entry["failed"]], ["command"])


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

    # what each channel is handed ---------------------------------------------

    TITLE, WHERE, BODY = "Claude Code", "~/code/app", "Completed in ~/code/app"

    def send_one(self, name, channels, event="Stop"):
        """Run one channel with the process and network calls recorded instead of made."""
        calls = []
        fake = types.SimpleNamespace(DEVNULL=subprocess.DEVNULL,
                                     run=lambda argv, **kw: calls.append((list(argv), kw)))

        def post(url, data, headers=None, timeout=8):
            calls.append((url, data, headers or {}))

        with mock.patch.object(self.notify, "subprocess", fake), mock.patch.object(self.notify, "http_post", post):
            self.notify.send_one(name, channels, self.TITLE, self.WHERE, self.BODY, "Glass", event)
        self.assertEqual(len(calls), 1, calls)
        return calls[0]

    def test_macos_banner_gets_the_four_values_its_script_expects(self):
        argv, options = self.send_one("macos", {})
        self.assertEqual(argv, ["osascript", "-e", self.notify.APPLESCRIPT, self.TITLE, self.WHERE, self.BODY, "Glass"])
        self.assertIn("on run {theTitle, theSubtitle, theMessage, theSound}", self.notify.APPLESCRIPT)
        self.assertTrue(options["check"], "a banner that fails must be reported as failed")

    def test_linux_banner(self):
        argv, _ = self.send_one("linux", {})
        self.assertEqual(argv, ["notify-send", "--app-name=Claude Code", "Claude Code: ~/code/app", self.BODY])

    def test_custom_command_gets_title_subtitle_and_message(self):
        argv, options = self.send_one("command", {"command": ["terminal-notifier", "-message"]})
        self.assertEqual(argv, ["terminal-notifier", "-message", self.TITLE, self.WHERE, self.BODY])
        self.assertTrue(options["check"])

    def test_ntfy_posts_the_message_to_the_topic(self):
        url, data, headers = self.send_one("ntfy", {"ntfy": {
            "server": "https://ntfy.example/", "topic": "my-topic", "token": "tk", "priority": "high"}})
        self.assertEqual((url, data), ("https://ntfy.example/my-topic", self.BODY.encode()))
        self.assertEqual(headers, {"Title": "Claude Code: ~/code/app", "Priority": "high",
                                   "Tags": "white_check_mark", "Authorization": "Bearer tk"})

    def test_ntfy_defaults_and_marks_an_error(self):
        url, _, headers = self.send_one("ntfy", {"ntfy": {"topic": "my-topic"}}, event="StopFailure")
        self.assertEqual(url, "https://ntfy.sh/my-topic")
        self.assertEqual(headers, {"Title": "Claude Code: ~/code/app", "Priority": "default", "Tags": "warning"})

    def test_pushover_form(self):
        url, data, _ = self.send_one("pushover", {"pushover": {"token": "app", "user": "me", "sound": "pianobar"}})
        self.assertEqual(url, "https://api.pushover.net/1/messages.json")
        self.assertEqual(urllib.parse.parse_qs(data.decode()), {
            "token": ["app"], "user": ["me"], "title": ["Claude Code: ~/code/app"],
            "message": [self.BODY], "sound": ["pianobar"]})

    def test_webhook_body_is_json(self):
        url, data, headers = self.send_one("webhook", {"webhook": {"url": "https://example.invalid/hook"}})
        self.assertEqual((url, headers), ("https://example.invalid/hook", {"Content-Type": "application/json"}))
        self.assertEqual(json.loads(data), {
            "title": self.TITLE, "subtitle": self.WHERE, "message": self.BODY, "event": "Stop",
            "text": "Claude Code: ~/code/app: Completed in ~/code/app"})

    def test_http_post_really_posts(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"], seen["title"] = self.path, self.headers.get("Title")
                seen["body"] = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        try:
            with mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
                status = self.notify.http_post("http://127.0.0.1:%d/my-topic" % server.server_port,
                                               b"hello", {"Title": "T"})
        finally:
            thread.join(10)
            server.server_close()
        self.assertEqual((status, seen), (200, {"path": "/my-topic", "title": "T", "body": b"hello"}))

    def last_log_entry(self):
        with open(self.notify.LOG_PATH) as fh:
            return json.loads(fh.read().splitlines()[-1])

    def test_send_reports_each_channel_and_logs_the_failures(self):
        spec = {"subtitle": self.WHERE, "message": self.BODY, "channels": {
            "macos": False, "linux": False, "ntfy": {"topic": "t"}, "webhook": {"url": "https://example.invalid/h"}}}

        def flaky(name, *rest):
            if name == "ntfy":
                raise RuntimeError("boom")

        with mock.patch.object(self.notify, "send_one", flaky):
            results = self.notify.send(spec)
        self.assertEqual(results, [("ntfy", False, "boom"), ("webhook", True, "")])
        entry = self.last_log_entry()
        self.assertEqual((entry["sent"], entry["failed"]), (["webhook"], [["ntfy", "boom"]]))

    def test_the_watcher_waits_out_its_delay_before_it_speaks(self):
        # The hook tests can only see this through timing, which a slow machine blurs. Here the
        # wait itself is recorded instead of slept through.
        naps = []
        spec = {"delay": 900, "stop_time": time.time(), "grace": 20, "transcript": "", "tasks": [],
                "subtitle": self.WHERE, "channels": {"macos": False, "linux": False}}
        with mock.patch.object(self.notify.time, "sleep", naps.append), \
                mock.patch.object(self.notify, "send") as send:
            self.notify.later(spec)
        self.assertEqual(naps, [900.0])
        send.assert_called_once()
        self.assertIn("its background work has finished", send.call_args[0][0]["message"])

    def test_the_hook_exits_zero_even_when_handling_fails(self):
        with mock.patch.object(self.notify, "handle", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "argv", ["notify.py"]), mock.patch.object(sys, "stdin", io.StringIO("{}")):
            self.assertEqual(self.notify.main(), 0)
        self.assertEqual(self.last_log_entry()["hook_error"], "boom")

    def test_log_keeps_only_the_newest_lines(self):
        with mock.patch.object(self.notify, "LOG_KEEP", 5):
            for n in range(9):
                self.notify.log({"n": n})
        with open(self.notify.LOG_PATH) as fh:
            self.assertEqual([json.loads(line)["n"] for line in fh], [4, 5, 6, 7, 8])

    def test_a_long_last_message_is_cut_to_fit_a_banner(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        cfg = dict(self.notify.DEFAULTS, include_last_message=True)
        message, _ = self.notify.describe({"hook_event_name": "Stop", "cwd": "/usr/local/share/thing",
                                           "last_assistant_message": "x" * 200}, cfg)
        self.assertEqual(message, "Completed in /share/thing: " + "x" * 140 + "...")

    def test_an_unknown_error_is_not_spelled_out(self):
        for error in ("unknown", "", None):
            message, _ = self.notify.describe({"hook_event_name": "StopFailure", "error": error}, self.notify.DEFAULTS)
            self.assertEqual(message, "Stopped with an error", error)


if __name__ == "__main__":
    unittest.main()
