# Internal notifications (NAL-A3)

People learn about important run events without watching the studio, and an agent can leave a short internal
message. Everything stays inside Nodestro: there is no e-mail, webhook or push channel (that is NAL-A4/A5, and
e-mail is not a default channel, see TODO).

## Rules

`Workflow.notifications` (`NotificationRules`):

- `events`: any of `run_failed`, `approval_pending`, `budget_pending`, `quality_failed`, `run_finished`. Default:
  `run_failed` and `approval_pending`. An empty list notifies nothing.
- `recipients`: `members` (everyone who may read the workspace) or `starter` (the person who started the run;
  everyone when that is not known).

Only top-level runs notify; runs of sub-workflows and imported runs never do. A failed run with both `run_failed`
and `run_finished` gives one message. `quality_failed` means a step's quality check did not pass in the end.

## Inbox

`notifications.LocalInbox` keeps the messages as documents of the workspace's store (collection `notifications`),
so they are separated by tenant like every other record, for 90 days. Each event notifies once: the id is derived
from the key `nodestro:notify:<event>:<record>` and written with `put_raw_if_absent`, so a retry, a resume, a
restart or a second worker never writes it twice. Read state is per person (`read`: user id -> time).

The engine calls `RunNotifier` when a run ends or pauses (a run listener) and for each run it finds interrupted after
a restart. Event messages carry the event, ids, the workflow's name and a problem code; never arguments, outputs,
error texts or secrets. Interfaces write the text in their own language.

With per-call tool approvals (S-2, off by default) a call that waits in its thread does not pause the run and is not
notified; a paused run (an approval step, a budget decision) is.

## notify_user

A built-in tool: `notify_user(title, message, level)`, `write` + `internal`, no network, usable offline. The message
goes to the run's starter (everyone when unknown). At most three per run (a step's call limit and the inbox's count
per run). Title and text are model output: control characters are removed, title <= 120 and text <= 2000 characters,
and interfaces show them as plain text marked as coming from an agent.

## Server

On a server whose Axentra inbox is on (`NODESTRO_AXENTRA_NOTIFY=1`) the local inbox is off and the existing Axentra
sweep (`axentra_notify`) stays the only inbox, unchanged: there the workflow rules and `notify_user` do not apply
(`notify_user` answers `notify.unavailable`).
