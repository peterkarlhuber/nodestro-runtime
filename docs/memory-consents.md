# Private memory spaces: the owner's consent and private runs (RT-089d)

On a server every persistent memory space belongs to one member (RT-089c: its first writer, or the operator's
assignment of a space without an owner). This page describes the two rules RT-089d adds on top. The desktop and Local
Docker have no owners and are unchanged.

## 1. A workflow uses a private space only with the owner's consent

Workflows are shared: any editor can build or change one that names another person's space. Before RT-089d, when
Alice ran Bob's workflow that names her space, its steps read her memory, and the workflow could hand it to any of
its tools. Now:

- A step opens a person's space only when the owner allowed **exactly this content**: a `MemoryConsent`
  (`memory_spaces.CONSENTS`, keyed per space, subject, workflow and digest) names the space, the owner, the workflow
  (or, for an agent test, nothing but the configuration) and the SHA-256 digest of its content.
- **Digest** (`memory_consents.workflow_digest`): the workflow's id, nodes, edges, limits and notifications as a run
  executes it, with every sub-workflow and team pinned to the version it runs (`RunManager.pin_subworkflows`); time
  stamps and the origin of a copy are left out. Every change, of a prompt, a tool, an edge or the version of a
  sub-workflow, changes the digest, so a consent never applies to changed content. Nothing has to be revoked when a
  workflow changes: the old consent simply does not match. Old consents still match a run that executes exactly the
  old content (a pinned earlier version), which is what the owner allowed.
- **Which runs need it.** A step that opens a space needs a consent for its own run's workflow and for every run
  above it (they get its output). The top-level run then records the space (`Run.memory_spaces`), and every run that
  executes afterwards in the same tree needs a consent for its workflow too: a sub-workflow or team after the step,
  a paused run that continues (so a revoked consent stops it), and a continued failed run (it takes over the earlier
  run's results, stays private to the same owner and inherits `memory_owner` and `memory_spaces`). A run marked for a
  person without recorded spaces (from before RT-089d) fails closed.
- **Agent tests** need a consent for the digest of the tested configuration (`memory_consents.agent_digest`).
- **Denial.** Without a matching consent the step (or the run) fails with `memory.consent_required` (`space`,
  `workflow_id`, `workflow`) before anything is read and before any model call. Only the space's owner, or a person
  who would create the space, gets this code; everyone else still gets `memory.space_not_found`, so nobody learns
  that a space exists. A run that acts for no person still uses no space.
- **Granting.** Only explicit: nothing grants a consent by itself, not even for a workflow the owner wrote. The
  owner, or a member who creates the space with the consent (its first writer), calls the HTTP API
  (`POST /api/memory-consents`, nodestro-api): it covers the workflow and every workflow it runs, recursively, in
  their current versions (at most `MAX_TARGETS`, 50; more is refused with `memory.consent_too_large`). With the
  digest the owner was shown (`GET /api/memory-consents/preview`) a changed workflow is refused
  (`memory.consent_stale`) instead of being allowed unseen. Each grant and revocation is audited.
- **Revoking.** `DELETE /api/memory-consents/{id}`: a step that already reads the space finishes; every later step,
  child run and continuation that needs the consent is refused.

Conversations are not affected: an answer's agent gets run memory instead of a persistent space (W3-S1 review), so a
conversation never opens a private space.

## 2. Private runs tell nobody else about themselves

A run that used a person's space is that person's alone (RT-089c, `Run.memory_owner`). RT-089d applies the same rule
to every channel that reports runs:

- **Local inbox** (`notifications.RunNotifier`): the messages of a private run (its events and `notify_user`) go to
  the memory's owner only, whatever the workflow's rules name; a run private to nobody (it acts for no person)
  notifies nobody (`notify.private` for its `notify_user`). The HTTP API also leaves out every listed message, and
  the unread count, of a run the member may not see, so messages written before the run became private are hidden
  too.
- **Server inbox** (`axentra_notify`, `NODESTRO_AXENTRA_NOTIFY=1`): a private run's failure and budget question go to
  its owner only (when their role may act); an approval of a run below a private run (its top-level run's
  `memory_owner`) too; a run private to nobody, or whose top cannot be read, tells nobody (`NotifyReport.private`).
  There is no e-mail or webhook channel for run events.
- **Event streams**: `memory_private` now reaches the step's run and every run above it, so an open stream of a
  parent or grand-parent run ends for another reader too. The API reads the snapshot first and checks it afterwards
  (its own `memory_owner` and the stored runs up to the top), so a run that became private between the check and the
  snapshot is never sent.
- **Lists and counters** (nodestro-api): run lists, approvals and budget requests were already filtered (RT-089c);
  the workflow list's last run status and the usage report's workflow buckets now leave private runs of other people
  out (their cost counts under `private_runs`, so the totals stay complete).

## Open (docs/TODO.md RT-089d)

- **Name squatting**: a space name belongs to its first writer, so another member can take a name first (a consent
  claims a new space too). Proposed: per-person namespaces in the interface (for example `<user>/notes`), or a
  reservation by the operator; nothing is renamed or reassigned automatically.
- **Spaces of removed members** stay theirs, unused and locked, with their consents. Proposed: an operator command
  that lists them and transfers or deletes a space explicitly and audited (like `memory backfill-owners`); never
  automatic.
- **Interface**: the studio shows the error text; a page to review and grant consents (preview, digest, revoke) is
  not built yet.
