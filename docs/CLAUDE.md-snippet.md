# CLAUDE.md snippet: notify me when you finish, not when you pause

Paste this into `~/.claude/CLAUDE.md` (global) or a project's `CLAUDE.md`. It is the
behavioural half of Claude Code Notify: the hook posts the "done" banner by itself,
this teaches Claude when to use its own `PushNotification` tool, which also reaches
your phone when Claude Code's Remote Control or mobile app is connected.
To keep those pushes off your phone, run `python3 ~/.claude/hooks/notify.py --phone off`
(and `--phone on` to undo it).

---

## Notifications: ping me when you finish, not just when you need me

I already get a notification when you need my input (permission prompts,
questions, plan approvals). Never send me a notification for those: it would
arrive twice. Notify me only when you **finish**.

**At the end of any turn where I may have walked away, call the
`PushNotification` tool before your final message.** "May have walked away"
covers: a build, test run, install, deploy or long script; anything that took
more than a minute or two; background agents finishing; or any point where I
would plausibly have switched windows.

Say that it completed and **where**, not a summary of what you did. The
location tells me which of my open sessions just finished. Use a tilde path,
the last two segments are plenty.

- good: `Completed in ~/code/my-app`
- good: `Completed in ~/.claude/hooks`
- bad: `build green, 0 failures, ready to commit` (that is you recapping yourself)
- bad: `task complete` (no location)

If the turn ended badly, keep the location and lead with the problem:
`Build failed in ~/code/my-app`.

Skip it when I am obviously sitting there watching: a quick question I just
asked and you answered in seconds, a one-line edit, a file read. The tool also
declines on its own when I am active at the terminal; a "not sent" result is
fine and expected. Do not retry it, do not announce it, and never fabricate a
question just to trigger a notification.

**Only when you are 100% done.** Ending a turn to wait on a build or a script
you started in the background is fine; you resume by yourself when it
finishes. But never call `PushNotification` before such a wait, and never
describe work as finished before it is. The `Stop` hook
(`~/.claude/hooks/notify.py`) is the backstop: it posts "Completed" when a turn
ends with nothing left running, stays silent while you are waiting on
background work, and only speaks up if nothing has resumed after 15 minutes.
Treat it as insurance, not as a reason to skip the call.
