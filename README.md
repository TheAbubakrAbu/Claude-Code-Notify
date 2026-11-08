# Claude Code Notify

**A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) hook that tells you when Claude is *actually* done.** A banner on your Mac, and optionally a push on your phone, the moment the work is finished, without crying wolf every time Claude hands a build to the background and waits for it.

[![tests](https://github.com/TheAbubakrAbu/Claude-Code-Notify/actions/workflows/tests.yml/badge.svg)](https://github.com/TheAbubakrAbu/Claude-Code-Notify/actions/workflows/tests.yml)

Created by **Abubakr Elmallah** on **October 22, 2026**.

## At a glance

| | |
|---|---|
| **1** file | `notify.py`, Python 3.8+, standard library only |
| **0** dependencies | nothing to `pip install` |
| **6** delivery channels | macOS, Linux, ntfy, Pushover, a webhook, your own command |
| **1** switch | phone notifications on or off, for every route at once |
| **3** events | `Stop`, `StopFailure`, and optionally `Notification` |
| **28** tests | run on macOS and Ubuntu on every push |

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [What you'll see](#what-youll-see)
- [Getting Started](#getting-started)
- [Commands](#commands)
- [Settings](#settings)
  - [General settings](#general-settings)
  - [Channels](#channels)
  - [Environment variables](#environment-variables)
- [Phone Notifications](#phone-notifications)
  - [Turning the phone on or off](#turning-the-phone-on-or-off)
- [How it decides](#how-it-decides)
- [Teach Claude the other half](#teach-claude-the-other-half)
- [Known Limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Privacy & Data](#privacy--data)
- [Testing](#testing)
- [Uninstall](#uninstall)
- [License](#license)
- [Feedback & Contact](#feedback--contact)

---

## Why this exists

**Claude Code fires its `Stop` hook every time a turn ends, not every time the work ends.**

During a long task Claude often starts a build or a script in the background, ends its turn to wait for it, and resumes by itself when it finishes. Each of those hand-offs is a `Stop`. A hook that posts "Completed" on every `Stop` therefore pings you a dozen times while Claude is still busy.

Measured in one real session: 27 `Stop` events, of which **17** fired while a background shell was still running, each with a last message like "Waiting on the build". Every one of them rang "Completed".

This hook reads the `background_tasks` list in the `Stop` payload and stays silent when the turn ended only to wait. The banner is posted by the `Stop` that ends the resumed turn, when nothing is left running. A detached watcher covers the one way that could fail: if Claude never resumes (a wedged task, a closed session), it posts "Turn ended 15 min ago, still waiting on: …" so you are never left in silence. **The failure direction is always toward notifying you.**

## Features

- **Knows when the work is done:** silent while Claude waits on a background shell, subagent, or workflow; one banner when the turn that finishes the job ends.
- **Never silent on a wedge:** if Claude never comes back, a watcher posts "still waiting on: Build the app" after `wait_minutes`.
- **Hears about errors:** an API error (rate limit, server error) ends the turn without a `Stop`, so the hook also listens to `StopFailure` and tells you what went wrong.
- **Tells sessions apart:** every banner names the project as a short tilde path (`~/code/my-app`), so several open sessions never blur together.
- **Desktop and phone:** macOS banner with a sound, Linux desktop notification, and phone delivery through [ntfy](https://ntfy.sh) (free, no account) or [Pushover](https://pushover.net).
- **One switch for the phone:** `notify.py --phone off` keeps everything off your phone, including Claude Code's own pushes, and `--phone on` brings it back.
- **Never in Claude's way:** the hook decides and returns at once; sending and waiting happen in a detached process. Every error is swallowed and the hook always exits 0.
- **Everything logged:** one JSON line per event in `notify.log`, so you can always see what it decided and why.

## What you'll see

| Event | Banner | When |
| --- | --- | --- |
| `Stop`, nothing pending | **Completed in ~/code/my-app** | the turn is really over |
| `Stop`, background work pending | *(silent)* | Claude will resume on its own |
| … and nothing resumed after `wait_minutes` | **Turn ended 15 min ago, still waiting on: Build the app** | a wedged task or a closed session |
| … the work finished but Claude never resumed | **Turn ended 15 min ago; its background work has finished** | a session closed mid-task |
| `StopFailure` | **Stopped with an error: rate limit** | an API error ended the turn without a `Stop` |
| `Notification` (optional) | **Claude has a question for you**, **Claude has a plan ready for review**, **Claude needs your permission to use Bash** | only if you install with `--events …,Notification` |

The title is `title` (default "Claude Code"), the subtitle is always the project folder shortened to its last two segments, and the body is the message above.

## Getting Started

1. **Clone the project:**

   ```bash
   git clone https://github.com/TheAbubakrAbu/Claude-Code-Notify.git
   cd Claude-Code-Notify
   ```

2. **Run the installer:**

   ```bash
   ./install.sh                 # or: ./install.sh --phone off
   ```

   It copies `notify.py` to `~/.claude/hooks/`, creates `~/.claude/hooks/notify.json` from [notify.example.json](notify.example.json) if there is none, and wires the hook into `~/.claude/settings.json` for `Stop` and `StopFailure`. A backup of `settings.json` is written first, and running it again changes nothing.

3. **Send a test notification:**

   ```bash
   python3 ~/.claude/hooks/notify.py --test
   ```

   It prints each channel with `ok` or `FAILED`.

4. **Allow the banner on macOS.** The banner is posted through `osascript`, so macOS files it under **Script Editor** in System Settings > Notifications. Allow it there, and while you are in that pane, turn off **Show in Notification Center** (see [Known Limitations](#known-limitations) for why).

5. **Restart your Claude Code sessions.** Hooks are read when a session starts. Run `/hooks` inside Claude Code to see it listed.

6. **Optional: reach your phone.** See [Phone Notifications](#phone-notifications).

## Commands

### Installer

| Command | Description |
| --- | --- |
| `./install.sh` | Install the hook and wire it for `Stop` and `StopFailure` |
| `./install.sh --events Stop,StopFailure,Notification` | Also notify on permission prompts, questions, and plan approvals (Claude Code has its own setting for these, so this is off by default) |
| `./install.sh --phone off` | Install and keep notifications off your phone (`--phone on` to undo) |
| `./install.sh --dest DIR` | Put `notify.py` and `notify.json` in `DIR` instead of `~/.claude/hooks` |
| `./install.sh --settings FILE` | Wire the hook into another `settings.json`, for example a project's `.claude/settings.json` |
| `./install.sh --uninstall` | Remove the hook from `settings.json`, keep the files |
| `./install.sh --uninstall --purge` | Also delete `notify.py`, `notify.json`, and `notify.log` |

### The hook

| Command | Description |
| --- | --- |
| `python3 ~/.claude/hooks/notify.py --test` | Send a test notification to every channel and print which ones worked |
| `python3 ~/.claude/hooks/notify.py --config` | Print the effective settings, the file paths, the channels that would deliver on this machine, and Claude Code's push settings |
| `python3 ~/.claude/hooks/notify.py --phone off` | Turn every route to your phone off |
| `python3 ~/.claude/hooks/notify.py --phone on` | Turn them back on |
| `python3 ~/.claude/hooks/notify.py --phone` | Show whether the phone is on, without changing anything |

## Settings

All settings live in **`~/.claude/hooks/notify.json`**. Every key is optional: anything you leave out uses its default, so the smallest useful file is just the keys you change. Keys starting with `_` are comments and are ignored. Changes take effect on the next notification; there is nothing to restart.

A file that only reaches your phone through ntfy and stays silent on the Mac:

```json
{
  "sound": "",
  "include_last_message": true,
  "channels": {
    "ntfy": { "topic": "claude-k3x9v2m8" }
  }
}
```

The full set of defaults is in [notify.example.json](notify.example.json).

### General settings

| Key | Default | What it does |
| --- | --- | --- |
| `title` | `"Claude Code"` | The banner's title. The subtitle is always the project folder. |
| `phone` | `true` | `false` skips the channels that only reach a phone (ntfy and Pushover). `notify.py --phone off` sets this and also turns off Claude Code's own pushes. See [Phone Notifications](#phone-notifications). |
| `sound` | `"Glass"` | The macOS banner sound: Basso, Blow, Bottle, Frog, Funk, Glass, Hero, Morse, Ping, Pop, Purr, Sosumi, Submarine, or Tink (anything in `/System/Library/Sounds`). `""` for a silent banner. |
| `include_last_message` | `false` | `true` appends the start of Claude's last message (up to 140 characters) to the body: **Completed in ~/code/my-app: Build green. All tests pass.** Handy on a phone, where you cannot see the terminal. |
| `wait_minutes` | `15` | How long a turn may wait on background work before the watcher posts "still waiting on …". Builds and simulator runs commonly take 2 to 10 minutes; raise it if your jobs run longer. |
| `resume_grace_seconds` | `20` | How much later than the `Stop` a transcript entry must be to count as "Claude resumed". Claude Code writes its own bookkeeping entry about 1.5 s after every `Stop`, and the grace keeps that from counting. You should not need to change it. |

### Channels

Channels live under `channels`. A channel sends only when it is switched on and has what it needs (a topic, a key, a URL), so the defaults send to the Mac or Linux desktop and nowhere else. `notify.py --config` lists the channels that would deliver on this machine.

#### macOS banner: `channels.macos`

| Key | Default | What it does |
| --- | --- | --- |
| `macos` | `true` | Posts a banner through `osascript` with `title`, the project as subtitle, and `sound`. Only used when `osascript` exists, so it is harmless on Linux. `false` turns it off. |

#### Linux desktop: `channels.linux`

| Key | Default | What it does |
| --- | --- | --- |
| `linux` | `true` | Posts through `notify-send` (from `libnotify`). Only used on Linux when `notify-send` is installed. No sound. |

#### ntfy (phone): `channels.ntfy`

[ntfy](https://ntfy.sh) is free and needs no account: install the app (iOS or Android), subscribe to a topic, and put the same topic here.

| Key | Default | What it does |
| --- | --- | --- |
| `topic` | `""` | The topic to publish to. Empty means ntfy is off. On the public server anyone who knows the topic can read it, so pick one nobody would guess (`claude-k3x9v2m8`). |
| `server` | `"https://ntfy.sh"` | Your own ntfy server, if you run one. |
| `token` | `""` | An access token, for a protected topic or a self-hosted server with auth. |
| `priority` | `"default"` | `min`, `low`, `default`, `high`, or `urgent`: how insistently the ntfy app alerts you. |

The push is titled "Claude Code: ~/code/my-app" and tagged ✅ for "Completed" and ⚠️ for everything else. Skipped when `phone` is `false`.

#### Pushover (phone): `channels.pushover`

| Key | Default | What it does |
| --- | --- | --- |
| `token` | `""` | Your Pushover application's API token. |
| `user` | `""` | Your Pushover user key. Both `token` and `user` are needed; with either one empty, Pushover is off. |
| `sound` | `""` | A Pushover sound name (`pushover`, `cosmic`, `none`, …). Empty uses your Pushover default. |

Skipped when `phone` is `false`.

#### Webhook: `channels.webhook`

| Key | Default | What it does |
| --- | --- | --- |
| `url` | `""` | A URL that receives a `POST` with a JSON body. Empty means off. |

The body:

```json
{
  "title": "Claude Code",
  "subtitle": "~/code/my-app",
  "message": "Completed in ~/code/my-app",
  "event": "Stop",
  "text": "Claude Code: ~/code/my-app: Completed in ~/code/my-app"
}
```

A Slack incoming webhook accepts it as is (it reads `text`). The webhook still runs when `phone` is `false`, since it can point anywhere.

#### Your own command: `channels.command`

| Key | Default | What it does |
| --- | --- | --- |
| `command` | `[]` | A program to run, as a list: `["/path/to/program", "--flag"]`. It is run with three more arguments, the title, the subtitle, and the message, and is given 8 seconds. Empty means off. |

`["say"]` reads every notification aloud. For anything fancier, point it at a script of your own and read `$1`, `$2`, and `$3`. The command still runs when `phone` is `false`.

### Environment variables

For testing and unusual setups. None of them are needed day to day.

| Variable | What it does |
| --- | --- |
| `NOTIFY_DRY_RUN=1` | Log what would be sent instead of sending it |
| `NOTIFY_WATCH_SECONDS=3` | Shorten the watcher's wait (normally `wait_minutes`) |
| `CLAUDE_NOTIFY_CONFIG` | Use another `notify.json` |
| `CLAUDE_NOTIFY_LOG` | Write the log somewhere else |
| `CLAUDE_NOTIFY_SETTINGS` | The Claude Code `settings.json` that `--phone` edits (default `~/.claude/settings.json`) |

## Phone Notifications

There are three ways to reach your phone. Use any of them, or several.

**1. Claude Code's own push.** Claude Code's mobile app and Remote Control can push to your phone by themselves. Two Claude Code settings control it, both in `~/.claude/settings.json` and both in `/config`:

| Setting | In `/config` | What it pushes |
| --- | --- | --- |
| `agentPushNotifEnabled` | Push when Claude decides | Claude's `PushNotification` tool, which the [CLAUDE.md snippet](docs/CLAUDE.md-snippet.md) teaches to send "Completed in ~/code/my-app" once the work is done |
| `inputNeededNotifEnabled` | Push when actions required | Permission prompts and questions, when Claude is waiting on you |

Claude Code skips these while you are focused on the connected terminal.

**2. ntfy (free).** Set `channels.ntfy.topic`. See [ntfy](#ntfy-phone-channelsntfy) above.

**3. Pushover.** Set `channels.pushover.token` and `user`. See [Pushover](#pushover-phone-channelspushover) above.

### Turning the phone on or off

One command covers all three routes:

```bash
python3 ~/.claude/hooks/notify.py --phone off   # nothing reaches the phone
python3 ~/.claude/hooks/notify.py --phone on    # back on
python3 ~/.claude/hooks/notify.py --phone       # show the current state
```

`--phone off` does two things:

1. **Sets `"phone": false` in `notify.json`**, so the hook skips ntfy and Pushover. Desktop banners, the webhook, and your own command keep running.
2. **Sets `agentPushNotifEnabled` and `inputNeededNotifEnabled` to `false`** in `~/.claude/settings.json`, which turns off route 1. The old file is backed up to `settings.json.bak-<time>` first.

Running it again with the same value changes nothing. Restart open Claude Code sessions if they still push to your phone. For finer control, for example questions on the phone but not "done", leave `phone` alone and flip the two `/config` toggles yourself.

## How it decides

The `Stop` payload Claude Code sends on stdin includes, among other fields:

- **`background_tasks`:** a list of `{id, type, status, description, command}` for this turn's background work, with `type` one of `shell`, `subagent`, `workflow` and `status` such as `running`, `pending`, `completed`;
- **`transcript_path`:** the session's JSONL transcript;
- **`scratchpad_dir`:** the session's scratch folder, next to which Claude Code keeps each background shell's output as `tasks/<id>.output`, ending in `[exited with code N]` once the shell has finished;
- **`cwd`, `last_assistant_message`, `session_id`**, and for `StopFailure` an `error`.

The hook itself only decides; posting and waiting happen in a detached child process, so the hook returns right away and never delays the turn, even with a slow network channel.

When a `Stop` arrives with any task still `running` or `pending`, the child sleeps `wait_minutes` and then checks two things:

1. **Did Claude resume?** If the transcript's newest timestamped entry is later than the `Stop` plus `resume_grace_seconds`, Claude came back, and the watcher exits quietly. The `Stop` that ends the resumed turn owns the banner.
2. **What is still running?** Otherwise it reads each shell's output file and posts "still waiting on: …" naming the tasks that have not exited, or "its background work has finished" if they all did and nothing resumed anyway.

## Teach Claude the other half

The hook posts the "done" banner by itself. If you also want Claude to push to your phone through Claude Code's own `PushNotification` tool, or to say *where* it finished, paste [docs/CLAUDE.md-snippet.md](docs/CLAUDE.md-snippet.md) into your `~/.claude/CLAUDE.md`. It tells Claude to notify only in the message where the work is complete, with the project path in the text, and never before handing something to the background.

## Known Limitations

- **The macOS banner belongs to Script Editor.** `osascript` posts on Script Editor's behalf, so the banner shows Script Editor's icon, is listed under Script Editor in System Settings, and opens Script Editor if you click its body.
- **Old banners pile up, and clearing a pile can freeze Notification Center (macOS 27).** `osascript` cannot remove or replace a banner it posted, so every "Completed" stays in Notification Center and they stack up under Script Editor all day. On macOS 27, clearing a stack of several notifications (any app's, not just this one's) sometimes leaves Notification Center spinning at 100% CPU in its own layout code until you run `killall NotificationCenter`. That is Apple's bug, but you can stop this hook from feeding it: System Settings > Notifications > Script Editor, turn off **Show in Notification Center**. The banner still pops up with its sound; it just is not kept afterwards, so there is never a stack to clear.
- **Only background shells can be checked for "finished".** The watcher reads a shell's output file to see whether it exited. Subagents and workflows leave no such file, so if Claude never resumes, they are always named as "still waiting on".
- **Hooks are read when a session starts.** After installing or uninstalling, restart open sessions.
- **Uninstalling does not undo `--phone off`.** It leaves Claude Code's push settings as they are; run `notify.py --phone on` first if you want them back.
- **Linux banners have no sound.** The Linux channel ignores `sound`.

## Troubleshooting

- **Nothing fires.** Run `/hooks` in a fresh Claude Code session and check the `Stop` entry is there. Look at `~/.claude/hooks/notify.log`: a line per event means the hook ran. `python3` must be on the PATH of whatever launched Claude Code; on macOS the Command Line Tools provide `/usr/bin/python3`.
- **No banner on the Mac.** Allow **Script Editor** in System Settings > Notifications, and check that a Focus mode is not hiding it. `notify.py --test` prints which channels worked.
- **Two banners at the end of a turn.** Claude's own `PushNotification` call and this hook both fired. That is by design: the hook is the backstop for the turns where Claude forgets or the turn dies early.
- **A "still waiting on" banner during a long but healthy job.** Raise `wait_minutes`.
- **My phone still buzzes after `--phone off`.** Restart open Claude Code sessions, then check `notify.py --phone`. If another app also posts Claude's notifications (the Claude desktop app, or a browser tab with claude.ai open), turn notifications off there too.
- **Notification Center froze after I cleared a stack.** Run `killall NotificationCenter`, then see [Known Limitations](#known-limitations).
- **I want the old behaviour, a banner on every `Stop`.** This hook is not for you; a one-line `osascript` hook does that.

## Privacy & Data

**Claude Code Notify** keeps everything on your machine unless you tell it otherwise:
- **Nothing leaves your computer by default:** only the desktop channels are on. ntfy, Pushover, and the webhook send only once you give them a topic, a key, or a URL.
- **What is sent:** the title, the project folder, and the message. With `include_last_message` on, also the first 140 characters of Claude's last message.
- **What is logged:** `~/.claude/hooks/notify.log` keeps the last 300 events: the event name, session id, working folder, background task descriptions, the first 300 characters of Claude's last message, and what was sent where. It never leaves your machine.
- **Your keys stay out of git:** `.gitignore` excludes `notify.json` and `notify.log`, which hold your ntfy topic, Pushover keys, and history.

## Testing

```bash
python3 -m unittest discover -s tests -v          # 28 tests, nothing is posted
NOTIFY_DRY_RUN=1 python3 notify.py --test         # what a test notification would do
NOTIFY_DRY_RUN=1 NOTIFY_WATCH_SECONDS=3 python3 notify.py < payload.json   # feed a payload by hand
```

The tests run under `NOTIFY_DRY_RUN` against a throwaway `notify.json`, log, and `settings.json`, so they never post anything or touch your real settings. The [GitHub Actions workflow](.github/workflows/tests.yml) runs the suite on macOS and Ubuntu and exercises the installer, including `--phone off`, against a throwaway `settings.json`.

## Uninstall

```bash
./install.sh --uninstall          # removes the settings entries, keeps the files
./install.sh --uninstall --purge  # also deletes notify.py, notify.json, notify.log
```

Uninstalling leaves Claude Code's push settings as they are; run `notify.py --phone on` first if you turned them off.

## License

This project is licensed under the [MIT License](LICENSE). Feel free to use, modify, and distribute the code, but please provide attribution.

## Feedback & Contact

I'd love to hear your thoughts! Open an issue, or reach out with feedback, feature requests, or questions:
- **Email:** ammelmallah@icloud.com
- **Website:** [abubakrelmallah.com](https://abubakrelmallah.com/)
- **LinkedIn:** [Abubakr Elmallah](https://www.linkedin.com/in/abubakr-elmallah-416a0b273/)
