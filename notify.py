#!/usr/bin/env python3
"""
Claude Code Notify: a Stop hook that tells you when Claude is actually done.

Claude Code fires its `Stop` hook every time a turn ends. That includes the
turns that end only because Claude handed a build or a script to the
background and is waiting for it, which it does many times in a long task.
A hook that posts "done" on every Stop therefore cries wolf all afternoon.

This hook posts a notification only when the turn is really over:

  Stop          Posted as "Completed in ~/path" (the path tells several open
                sessions apart). If the turn ended with background work still
                running (a shell, a subagent, a workflow), the hook says
                NOTHING: Claude resumes by itself when that work finishes and
                the banner belongs to the Stop that ends the resumed turn.
                A detached watcher re-checks `wait_minutes` later and, if
                Claude never resumed, posts "Turn ended N min ago, still
                waiting on: ..." so a wedged task never means silence.
  StopFailure   "Stopped with an error: rate limit" and the like. An API
                error ends the turn without a Stop, so this is the only way
                you would hear about it.
  Notification  Whatever Claude Code sent ("Claude needs your permission to
                use Bash"); questions and plan approvals are reworded.
                Only wired if you ask the installer for it: Claude Code has
                its own setting for these.

Delivery channels (all optional, configured in notify.json next to this file):
macOS banner (osascript), Linux desktop (notify-send), ntfy.sh (free, reaches
your phone), Pushover, a generic JSON webhook, and any command of your own.

Phone switch: "phone": false in notify.json skips ntfy and Pushover, and
`--phone off` also turns off Claude Code's own phone pushes
(agentPushNotifEnabled, inputNeededNotifEnabled in ~/.claude/settings.json),
so one command keeps everything off the phone. Desktop banners are unaffected.

Usage (as Claude Code runs it):   python3 notify.py  < payload.json
    python3 notify.py --test      send a test notification to every channel
    python3 notify.py --config    print the effective configuration and paths
    python3 notify.py --phone on|off   phone notifications on or off (no value: show)
    NOTIFY_DRY_RUN=1              log what would be sent instead of sending it
    NOTIFY_WATCH_SECONDS=3        shorten the watcher delay (tests)
    CLAUDE_NOTIFY_CONFIG / CLAUDE_NOTIFY_LOG   override the two file paths
    CLAUDE_NOTIFY_SETTINGS        the Claude Code settings.json --phone edits

Every failure is swallowed and the hook always exits 0: a notifier must never
get in Claude's way. Python 3.8+, standard library only.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(__file__)
CONFIG_PATH = os.environ.get("CLAUDE_NOTIFY_CONFIG") or os.path.join(HERE, "notify.json")
LOG_PATH = os.environ.get("CLAUDE_NOTIFY_LOG") or os.path.join(HERE, "notify.log")
SETTINGS_PATH = (os.environ.get("CLAUDE_NOTIFY_SETTINGS")
                 or os.path.join(os.path.expanduser("~"), ".claude", "settings.json"))
LOG_KEEP = 300

# Channels whose only job is reaching a phone. "phone": false skips them;
# webhook and command stay on, since they can point anywhere.
PHONE_CHANNELS = ("ntfy", "pushover")
# Claude Code's own pushes to the phone (Remote Control, mobile app). In
# /config they are "Push when Claude decides" and "Push when actions required".
CLAUDE_PUSH_SETTINGS = ("agentPushNotifEnabled", "inputNeededNotifEnabled")

DEFAULTS = {
    # Banner title. The subtitle is always the project folder.
    "title": "Claude Code",
    # false keeps notifications off your phone: ntfy and Pushover are skipped.
    # `notify.py --phone off` sets this and Claude Code's own push settings.
    "phone": True,
    # Any name in /System/Library/Sounds, or "" for a silent banner.
    "sound": "Glass",
    # How long a turn may sit "waiting on background work" before the watcher
    # speaks up. Builds and simulator runs commonly take 2 to 10 minutes.
    "wait_minutes": 15,
    # Claude Code appends a stop_hook_summary entry to the transcript about
    # 1.5 s after the Stop hook runs, so a transcript change only means
    # "Claude resumed" when it comes later than this.
    "resume_grace_seconds": 20,
    # Append the start of Claude's last message to the body (handy on a phone).
    "include_last_message": False,
    "channels": {
        "macos": True,
        "linux": True,
        "ntfy": {"server": "https://ntfy.sh", "topic": "", "token": "", "priority": "default"},
        "pushover": {"token": "", "user": "", "sound": ""},
        "webhook": {"url": ""},
        # A command of your own, run as: command... TITLE SUBTITLE MESSAGE
        "command": [],
    },
}

# Questions and plan approvals reach hooks as permission requests for these
# tools ("Claude needs your permission to use AskUserQuestion").
TOOL_MESSAGES = {
    "AskUserQuestion": "Claude has a question for you",
    "ExitPlanMode": "Claude has a plan ready for review",
}

# The text travels as run-handler arguments, never spliced into the script,
# so quotes or backslashes in a message cannot break the AppleScript.
APPLESCRIPT = """
on run {theTitle, theSubtitle, theMessage, theSound}
    if theSound is "" then
        display notification theMessage with title theTitle subtitle theSubtitle
    else
        display notification theMessage with title theTitle subtitle theSubtitle sound name theSound
    end if
end run
"""


# ------------------------------------------------------------------ utilities

def as_text(value):
    return value.strip() if isinstance(value, str) else ""


SWITCH_WORDS = {"on": True, "true": True, "yes": True, "1": True,
                "off": False, "false": False, "no": False, "0": False}


def switch(value, default=True):
    """An on/off value from notify.json or the command line, as a bool.
    Accepts true/false and on/off, yes/no, 1/0; anything else is `default`."""
    if isinstance(value, bool):
        return value
    return SWITCH_WORDS.get(str(value).strip().lower(), default)


def log(entry):
    """Append one JSON line to the log and keep the last LOG_KEEP lines."""
    try:
        entry = dict(entry)
        entry.setdefault("at", datetime.now().isoformat(timespec="seconds"))
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        with open(LOG_PATH) as fh:
            kept = fh.readlines()[-LOG_KEEP:]
        with open(LOG_PATH, "w") as fh:
            fh.writelines(kept)
    except Exception:
        pass


def deep_merge(base, over):
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH) as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            cfg = deep_merge(cfg, user)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log({"config_error": str(exc), "path": CONFIG_PATH})
    return cfg


def watch_seconds(cfg):
    try:
        return float(os.environ.get("NOTIFY_WATCH_SECONDS") or cfg["wait_minutes"] * 60)
    except (TypeError, ValueError):
        return 15 * 60


def dry_run():
    return bool(os.environ.get("NOTIFY_DRY_RUN"))


def project_name(payload):
    """Where the work happened, tilde-style and short enough for a banner.

    The basename alone is ambiguous ("hooks", "src", "app" could be anywhere),
    so keep the last two segments: "~/.claude/hooks", "projects/msu-uci".
    """
    path = as_text(os.environ.get("CLAUDE_PROJECT_DIR")) or as_text(payload.get("cwd")) or os.getcwd()
    path = os.path.realpath(path)
    home = os.path.realpath(os.path.expanduser("~"))
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        rel = path[len(home) + 1:]
        parts = rel.split(os.sep)
        return "~/" + os.sep.join(parts[-2:]) if len(parts) > 2 else "~/" + rel
    parts = [seg for seg in path.split(os.sep) if seg]
    if len(parts) > 2:
        return "/" + os.sep.join(parts[-2:])  # keep the slash: /share/thing, not share/thing
    return path


def read_tail(path, size):
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        fh.seek(max(0, end - size))
        return fh.read().decode("utf-8", "ignore")


# ------------------------------------------------------------------- channels

def channels_in_use(channels, phone=True):
    """Names of the channels that would actually deliver on this machine.
    With phone=False the channels that only reach a phone are left out."""
    used = []
    if channels.get("macos") and shutil.which("osascript"):
        used.append("macos")
    if channels.get("linux") and sys.platform.startswith("linux") and shutil.which("notify-send"):
        used.append("linux")
    ntfy = channels.get("ntfy") or {}
    if as_text(ntfy.get("topic")):
        used.append("ntfy")
    pushover = channels.get("pushover") or {}
    if as_text(pushover.get("token")) and as_text(pushover.get("user")):
        used.append("pushover")
    webhook = channels.get("webhook") or {}
    if as_text(webhook.get("url")):
        used.append("webhook")
    if isinstance(channels.get("command"), list) and channels["command"]:
        used.append("command")
    return [name for name in used if phone or name not in PHONE_CHANNELS]


def http_post(url, data, headers=None, timeout=8):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def send_one(name, channels, title, subtitle, message, sound, event):
    if name == "macos":
        subprocess.run(
            ["osascript", "-e", APPLESCRIPT, title, subtitle, message, sound or ""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=True,
        )
    elif name == "linux":
        subprocess.run(
            ["notify-send", "--app-name=" + title, title + ": " + subtitle, message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=True,
        )
    elif name == "ntfy":
        ntfy = channels["ntfy"]
        server = as_text(ntfy.get("server")) or "https://ntfy.sh"
        headers = {
            "Title": (title + ": " + subtitle).encode("utf-8", "ignore").decode("latin-1", "ignore"),
            "Priority": as_text(ntfy.get("priority")) or "default",
            "Tags": "white_check_mark" if event == "Stop" else "warning",
        }
        if as_text(ntfy.get("token")):
            headers["Authorization"] = "Bearer " + as_text(ntfy.get("token"))
        http_post(server.rstrip("/") + "/" + as_text(ntfy["topic"]), message.encode("utf-8"), headers)
    elif name == "pushover":
        pushover = channels["pushover"]
        fields = {
            "token": as_text(pushover.get("token")),
            "user": as_text(pushover.get("user")),
            "title": title + ": " + subtitle,
            "message": message,
        }
        if as_text(pushover.get("sound")):
            fields["sound"] = as_text(pushover.get("sound"))
        http_post("https://api.pushover.net/1/messages.json", urllib.parse.urlencode(fields).encode())
    elif name == "webhook":
        body = json.dumps({
            "title": title, "subtitle": subtitle, "message": message,
            "event": event, "text": title + ": " + subtitle + ": " + message,
        }).encode("utf-8")
        http_post(as_text(channels["webhook"]["url"]), body, {"Content-Type": "application/json"})
    elif name == "command":
        subprocess.run(
            list(channels["command"]) + [title, subtitle, message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=True,
        )


def send(spec):
    """Deliver one notification to every configured channel.

    Returns [(channel, ok, detail)]. Under NOTIFY_DRY_RUN nothing is sent;
    the would-be delivery is logged instead.
    """
    channels = spec.get("channels") or DEFAULTS["channels"]
    title = spec.get("title") or DEFAULTS["title"]
    subtitle = spec.get("subtitle") or ""
    message = spec.get("message") or ""
    sound = spec.get("sound") or ""
    event = spec.get("event") or "Stop"
    names = channels_in_use(channels, switch(spec.get("phone")))
    if spec.get("dry_run"):
        log({"dry_run": True, "channels": names, "subtitle": subtitle, "message": message, "sound": sound})
        return [(name, True, "dry run") for name in names]
    results = []
    for name in names:
        try:
            send_one(name, channels, title, subtitle, message, sound, event)
            results.append((name, True, ""))
        except Exception as exc:
            results.append((name, False, str(exc)[:200]))
    log({"sent": [r[0] for r in results if r[1]], "failed": [(r[0], r[2]) for r in results if not r[1]],
         "subtitle": subtitle, "message": message})
    return results


def detach(mode, spec):
    """Run `notify.py --<mode> <spec>` as a process of its own.

    Claude Code waits for the hook to exit before the turn is over, so the
    hook itself only decides; posting (which can take a second for a network
    channel) and waiting happen in this child, which outlives the hook.
    """
    subprocess.Popen(
        [sys.executable, SCRIPT, "--" + mode, json.dumps(spec)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


# ------------------------------------------------------------------ decisions

def pending_tasks(payload):
    """Background tasks of this turn that have not finished. Claude resumes on
    its own when they do, whatever their type (shell, subagent, workflow)."""
    tasks = payload.get("background_tasks")
    return [
        task
        for task in (tasks if isinstance(tasks, list) else [])
        if isinstance(task, dict) and task.get("status") in ("running", "pending")
    ]


def tasks_dir(payload):
    """Claude Code keeps each background shell's output beside the scratchpad:
    <session>/tasks/<id>.output, ending in "[exited with code N]" once done."""
    scratch = as_text(payload.get("scratchpad_dir"))
    return os.path.join(os.path.dirname(scratch), "tasks") if scratch else ""


def describe(payload, cfg):
    """(message, sound) for a turn that is really over, or a Notification."""
    event = payload.get("hook_event_name")
    sound = cfg.get("sound") or ""

    if event == "Notification":
        message = as_text(payload.get("message")) or "Claude needs your attention"
        for tool, friendly in TOOL_MESSAGES.items():
            if message.endswith(" " + tool):
                return friendly, sound
        return message, sound

    if event == "StopFailure":
        error = as_text(payload.get("error")).replace("_", " ")
        if error and error != "unknown":
            return "Stopped with an error: " + error, sound
        return "Stopped with an error", sound

    message = "Completed in " + project_name(payload)
    if cfg.get("include_last_message"):
        last = " ".join(as_text(payload.get("last_assistant_message")).split())
        if last:
            message += ": " + (last[:140] + "..." if len(last) > 140 else last)
    return message, sound


def delivery(cfg, payload, message, sound):
    return {
        "title": cfg.get("title") or DEFAULTS["title"],
        "subtitle": project_name(payload),
        "message": message,
        "sound": sound,
        "event": payload.get("hook_event_name") or "Stop",
        "channels": cfg.get("channels") or DEFAULTS["channels"],
        "phone": switch(cfg.get("phone")),
        "dry_run": dry_run(),
    }


def watcher_spec(cfg, payload, pending):
    message, sound = describe(payload, cfg)
    spec = delivery(cfg, payload, message, sound)
    spec.update({
        "stop_time": time.time(),
        "delay": watch_seconds(cfg),
        "grace": float(cfg.get("resume_grace_seconds") or 0),
        "transcript": as_text(payload.get("transcript_path")),
        "tasks_dir": tasks_dir(payload),
        "tasks": [
            {
                "id": as_text(task.get("id")),
                "type": as_text(task.get("type")),
                "description": as_text(task.get("description"))
                or as_text(task.get("command"))[:80]
                or "a background task",
            }
            for task in pending
        ],
    })
    return spec


def last_entry_time(transcript):
    """Unix time of the newest timestamped transcript entry, or None."""
    try:
        tail = read_tail(transcript, 1 << 20)
    except Exception:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue  # a line cut by the tail window
        stamp = entry.get("timestamp") if isinstance(entry, dict) else None
        if isinstance(stamp, str):
            try:
                return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def shell_exited(tasks_dir_path, task):
    """True / False for a background shell, None when it cannot be told."""
    if task.get("type") != "shell" or not tasks_dir_path or not task.get("id"):
        return None
    try:
        tail = read_tail(os.path.join(tasks_dir_path, task["id"] + ".output"), 4096)
    except Exception:
        return None
    return "[exited with code" in tail


def later(spec):
    """The watcher: after the delay, speak only if Claude never resumed."""
    time.sleep(max(0.0, float(spec.get("delay") or 0)))
    stop_time = float(spec.get("stop_time") or 0)
    newest = last_entry_time(spec.get("transcript") or "")
    if newest is not None and newest > stop_time + float(spec.get("grace") or 0):
        log({"watcher": "resumed", "subtitle": spec.get("subtitle")})
        return  # Claude came back; the Stop that ends that turn owns the banner
    tasks = spec.get("tasks") or []
    still = [task for task in tasks if shell_exited(spec.get("tasks_dir") or "", task) is not True]
    minutes = max(1, int(round((time.time() - stop_time) / 60)))
    if still:
        what = still[0].get("description") or "a background task"
        if len(still) > 1:
            what += " (+%d more)" % (len(still) - 1)
        message = "Turn ended %d min ago, still waiting on: %s" % (minutes, what)
    else:
        message = "Turn ended %d min ago; its background work has finished" % minutes
    log({"watcher": "posted", "subtitle": spec.get("subtitle"), "message": message})
    spec = dict(spec, message=message)
    send(spec)


# ----------------------------------------------------------------------- main

def slim(payload):
    """A parseable log entry: the fields that matter, trimmed, never cut mid-JSON."""
    tasks = payload.get("background_tasks")
    return {
        "hook_event_name": payload.get("hook_event_name"),
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "error": payload.get("error"),
        "background_tasks": [
            {
                "id": task.get("id"),
                "type": task.get("type"),
                "status": task.get("status"),
                "description": as_text(task.get("description"))[:100],
            }
            for task in (tasks if isinstance(tasks, list) else [])
            if isinstance(task, dict)
        ],
        "last_assistant_message": as_text(payload.get("last_assistant_message"))[:300],
    }


def handle(payload, cfg):
    """Decide what this hook event means and act. Returns the decision name."""
    event = payload.get("hook_event_name")
    if event not in ("Notification", "StopFailure"):
        pending = pending_tasks(payload)
        if pending:
            # Not done: Claude is waiting on this work and resumes when it
            # finishes. Say nothing now; the watcher covers a wedge.
            log({"deferred": [task.get("description") for task in pending]})
            detach("later", watcher_spec(cfg, payload, pending))
            return "deferred"
    message, sound = describe(payload, cfg)
    detach("send", delivery(cfg, payload, message, sound))
    return "sent"


def run_test(cfg):
    spec = {
        "title": cfg.get("title") or DEFAULTS["title"],
        "subtitle": "Claude Code Notify",
        "message": "Test notification: your channels work",
        "sound": cfg.get("sound") or "",
        "event": "Stop",
        "channels": cfg.get("channels") or DEFAULTS["channels"],
        "phone": switch(cfg.get("phone")),
        "dry_run": dry_run(),
    }
    results = send(spec)
    if not results:
        print("No channel is available: enable macos/linux, or set an ntfy topic, "
              "Pushover keys, a webhook URL or a command in " + CONFIG_PATH)
        return 1
    bad = 0
    for name, ok, detail in results:
        print("%-9s %s %s" % (name, "ok" if ok else "FAILED", detail))
        bad += 0 if ok else 1
    return 1 if bad else 0


# --------------------------------------------------------------- phone switch

def read_json_object(path):
    """The JSON object in `path`, or {} when the file does not exist.
    Raises ValueError for a file that is not a JSON object, so a broken file
    is reported instead of overwritten."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ValueError("%s is not valid JSON (%s); fix it by hand first" % (path, exc))
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object; fix it by hand first" % path)
    return data


def write_json_object(path, data, backup=False):
    """Write `data` to `path` (through a symlink, like the installer does).
    With backup=True, first copy the old file to path.bak-<time>; returns
    the backup's path, or None."""
    saved = None
    if backup and os.path.exists(path):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        saved, n = path + ".bak-" + stamp, 1
        while os.path.exists(saved):  # two runs in one second must not share a backup
            saved, n = path + ".bak-" + stamp + "-%d" % n, n + 1
        shutil.copy2(path, saved)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return saved


def claude_push_state():
    """{setting: value} for Claude Code's push settings; None means unset."""
    try:
        settings = read_json_object(SETTINGS_PATH)
    except ValueError:
        settings = {}
    return {key: settings.get(key) for key in CLAUDE_PUSH_SETTINGS}


def show_phone(cfg):
    on = switch(cfg.get("phone"))
    print("Phone notifications from this hook: %s" % ("on" if on else "off"))
    print("  %s  phone: %s" % (CONFIG_PATH, json.dumps(on)))
    print("Claude Code's own phone pushes:")
    for key, value in claude_push_state().items():
        print("  %s  %s: %s" % (SETTINGS_PATH, key, "unset (Claude Code's default)" if value is None
                                else json.dumps(value)))
    print("Change both with: python3 %s --phone on|off" % SCRIPT)
    return 0


def set_phone(on):
    """`--phone on|off`: one switch for every route this setup has to a phone.

    notify.json gets "phone": on (ntfy and Pushover follow it), and Claude
    Code's settings.json gets both push settings set to `on`. Each file is
    written only if it changes, and settings.json is backed up first."""
    word = "on" if on else "off"
    try:
        cfg = read_json_object(CONFIG_PATH)
        settings = read_json_object(SETTINGS_PATH)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("Phone notifications: " + word)
    if cfg.get("phone") is on:
        print("  %s  already phone: %s" % (CONFIG_PATH, json.dumps(on)))
    else:
        cfg["phone"] = on
        write_json_object(CONFIG_PATH, cfg)
        print("  %s  phone: %s%s" % (CONFIG_PATH, json.dumps(on),
                                     "" if on else " (ntfy and Pushover are skipped)"))

    pairs = ", ".join("%s: %s" % (key, json.dumps(on)) for key in CLAUDE_PUSH_SETTINGS)
    if all(settings.get(key) is on for key in CLAUDE_PUSH_SETTINGS):
        print("  %s  already %s" % (SETTINGS_PATH, pairs))
        return 0
    for key in CLAUDE_PUSH_SETTINGS:
        settings[key] = on
    saved = write_json_object(SETTINGS_PATH, settings, backup=True)
    print("  %s  %s" % (SETTINGS_PATH, pairs))
    if saved:
        print("  backed up the old settings to " + saved)
    print("Restart open Claude Code sessions if they still push to your phone.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] in ("--later", "--send"):
        try:
            spec = json.loads(args[1])
            later(spec) if args[0] == "--later" else send(spec)
        except Exception as exc:
            log({"child_error": args[0], "error": str(exc)[:300]})
        return 0

    cfg = load_config()
    if args[:1] == ["--test"]:
        return run_test(cfg)
    if args[:1] == ["--config"]:
        print(json.dumps({"config_path": CONFIG_PATH, "log_path": LOG_PATH,
                          "settings_path": SETTINGS_PATH, "config": cfg,
                          "channels_in_use": channels_in_use(cfg["channels"], switch(cfg.get("phone"))),
                          "claude_push": claude_push_state()}, indent=2))
        return 0
    if args[:1] == ["--phone"]:
        if len(args) == 1:
            return show_phone(cfg)
        on = switch(args[1], None)
        if on is None:
            print("usage: notify.py --phone on|off   (no value: show the current state)", file=sys.stderr)
            return 2
        return set_phone(on)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        payload = {}  # still notify; the payload only adds detail
    log(slim(payload))
    try:
        handle(payload, cfg)
    except Exception as exc:
        log({"hook_error": str(exc)[:300]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
