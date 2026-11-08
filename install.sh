#!/usr/bin/env bash
# Claude Code Notify installer.
#   ./install.sh                                  wire the hook for Stop + StopFailure
#   ./install.sh --events Stop,StopFailure,Notification
#   ./install.sh --dest ~/.claude/hooks           where notify.py and notify.json go
#   ./install.sh --settings ~/.claude/settings.json
#   ./install.sh --phone off                      keep notifications off your phone (on to undo)
#   ./install.sh --uninstall                      remove the settings entries, keep the files
#   ./install.sh --uninstall --purge              also delete notify.py, notify.json, notify.log
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"
EVENTS="Stop,StopFailure"
UNINSTALL=0
PURGE=0
PHONE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --events) EVENTS="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    --settings) SETTINGS="$2"; shift 2 ;;
    --phone) PHONE="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$PHONE" in
  ""|on|off) ;;
  *) echo "--phone takes on or off, not: $PHONE" >&2; exit 2 ;;
esac

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required (macOS: xcode-select --install)" >&2; exit 1; }

DEST="${DEST/#\~/$HOME}"
SETTINGS="${SETTINGS/#\~/$HOME}"

if [ "$UNINSTALL" = 0 ]; then
  mkdir -p "$DEST"
  cp "$HERE/notify.py" "$DEST/notify.py"
  chmod +x "$DEST/notify.py"
  if [ ! -f "$DEST/notify.json" ]; then
    cp "$HERE/notify.example.json" "$DEST/notify.json"
  fi
fi

# The command stored in settings.json. $HOME keeps it portable between Macs.
if [ "$DEST" = "$HOME/.claude/hooks" ]; then
  CMD='python3 "$HOME/.claude/hooks/notify.py"'
else
  CMD="python3 \"$DEST/notify.py\""
fi

python3 - "$SETTINGS" "$CMD" "$EVENTS" "$UNINSTALL" <<'PY'
import json, os, shutil, sys, time

path, cmd, events, uninstall = sys.argv[1], sys.argv[2], sys.argv[3].split(","), sys.argv[4] == "1"
events = [e.strip() for e in events if e.strip()]

try:
    with open(path) as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
if not isinstance(data, dict):
    sys.exit("%s is not a JSON object; fix it by hand first" % path)
before = json.dumps(data, sort_keys=True)
hooks = data.setdefault("hooks", {})


def ours(hook):
    return isinstance(hook, dict) and "notify.py" in str(hook.get("command", ""))


if uninstall:
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            inner = [h for h in group.get("hooks", []) if not ours(h)]
            if inner or not group.get("hooks"):
                kept.append(dict(group, hooks=inner) if group.get("hooks") else group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
else:
    for event in events:
        groups = hooks.setdefault(event, [])
        found = False
        for group in groups:
            for hook in group.get("hooks", []):
                if ours(hook):
                    found = True
                    hook["type"] = "command"
                    hook["command"] = cmd
                    hook.setdefault("timeout", 10)
        if not found:
            groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})

if json.dumps(data, sort_keys=True) == before:
    print("settings already up to date: " + path)
else:
    if os.path.exists(path):
        backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while os.path.exists(backup):  # two runs in one second must not share a backup
            backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S") + "-%d" % n
            n += 1
        shutil.copy(path, backup)
        print("backed up " + backup)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(("removed from " if uninstall else "wired into ") + path)
PY

if [ "$UNINSTALL" = 1 ]; then
  if [ "$PURGE" = 1 ]; then
    rm -f "$DEST/notify.py" "$DEST/notify.json" "$DEST/notify.log"
    echo "deleted $DEST/notify.py, notify.json, notify.log"
  else
    echo "kept $DEST/notify.py and notify.json (add --purge to delete them)"
  fi
  exit 0
fi

if [ -n "$PHONE" ]; then
  echo
  CLAUDE_NOTIFY_CONFIG="$DEST/notify.json" CLAUDE_NOTIFY_SETTINGS="$SETTINGS" \
    python3 "$DEST/notify.py" --phone "$PHONE"
fi

cat <<EOF

Installed
  hook     $DEST/notify.py
  config   $DEST/notify.json   (add an ntfy topic or Pushover keys to reach your phone)
  events   $EVENTS

Next
  python3 "$DEST/notify.py" --test      send a test notification to every channel
  python3 "$DEST/notify.py" --phone off keep notifications off your phone (on to undo)
  Restart your Claude Code sessions: hooks are read when a session starts.
  macOS files the banner under "Script Editor" in System Settings > Notifications.
EOF
