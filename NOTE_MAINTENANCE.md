# Automatic note maintenance

Automatic note maintenance reviews a few of the oldest or most doubtful notes each day and keeps the store tidy without a per-note approval prompt. It is off until the user turns it on. This page is the reference for the feature: `SCRIPT_REFERENCE.md` lists the command flags, `RECOVERY.md` covers interrupted runs, and `VALIDATION.md` has the dry-run gate to pass before enabling it on a real store.

## What it does and does not do

A model reviews a small batch of candidate notes and recommends one action for each: keep, defer, confirm, update, resolve, archive or supersede. The model only recommends. The script re-checks every recommendation against the current note, pin, action items, configuration and source files, and commits each accepted one as its own journaled, undoable change.

Maintenance does not:

- delete any note. Retired notes move to `archive/` (or are resolved or superseded) and can be brought back.
- touch preference or policy notes, learned procedures, or pinned notes.
- retire maps, briefs or runbooks. These can be kept, deferred, confirmed or updated.
- merge, retag, remap, change the taxonomy or edit configuration. Those still need an approved plan.
- retire a note only because it is noisy (retrieved often but never used). Usage, age and review dates are signals, never proof.
- run when it is disabled, except a manual dry run.

Installing or upgrading never turns it on. `maintenance.json` starts disabled, and a snapshot restore or a store relocation always leaves it disabled.

## Enable and disable

```
maintenance enable --domain D [--all-domains] [--model NAME] [--max-notes-per-day N] [--timeout-seconds N] [--daily-budget-usd D]
maintenance disable
maintenance status
```

`enable` adds the pinned session domain (or, with `--all-domains`, every configured non-global domain) to the configured domains and sets `enabled_at`. A global domain is included only when it is named explicitly with `--domain`. The settings flags replace the matching defaults and leave the day's accounting alone. `disable` keeps the domains, settings and accounting, so disabling and re-enabling never creates a new allowance.

## Limits and defaults

| Limit | Default |
| --- | --- |
| Model | Sonnet (`sonnet`) |
| Notes reviewed a day | 10 (kept and deferred notes count too) |
| Time for one run | 300 seconds; the process tree is killed after that |
| Spend a day | 1 USD (`"1.00"`) |
| Runs | one per rolling 24 hours (86400 seconds), dry runs included |
| Candidates a run | at most 10 |

A run reserves its notes and money before the model starts, and an unknown cost is charged at the full reservation. Deleting `.cache` or the checkpoint cannot reset the allowance: it is rebuilt from the run records. When the records cannot be read, no automatic run starts and `status` reports `accounting_unavailable`. When the model reports spend above the reserved budget, maintenance blocks itself (`provider_budget`) until the configuration changes.

## What is sent to the model

One packet on standard input to a single `claude -p` call: for each candidate its ref, revision, title, type, tags, dates, the full note body (up to 24 KB), the review signals, the actions the policy allows, a few short excerpts (up to four, 1200 characters each) from the files and sources the note cites, and any active successor note it might be superseded by. The packet is redacted (credentials are replaced) and is capped at 128 KB; a note too large for it is deferred.

Not sent: file contents beyond those excerpts, the repository tree, environment variables, other notes, and session history. The call runs with no tools, no MCP servers, no hooks and no session, in a fresh temporary directory, so the model cannot read files or run commands; it can only return a structured answer. Text inside notes that tries to give the model orders is treated as data. The packet is deleted after the run; run records keep metadata, refs, hashes and short reasons, not note bodies or prompts.

## The policy in plain words

- Pinned notes and protected types are never retired. Preference and policy notes and learned procedures are not reviewed at all. Maps, briefs and runbooks are never retired.
- Status notes: may be archived once their review date has passed and they hold no unique current instruction or unresolved work.
- Plan, bug and risk notes: resolved when every action item is checked and the body keeps nothing that is not covered elsewhere; archived only when current evidence shows the work was cancelled or is obsolete. Inactivity or unchecked items alone are not evidence.
- Knowledge, decision, context and research notes: archived only when current evidence shows the content is obsolete; superseded when an active successor covers all its useful content. An old decision that still explains a current constraint stays.
- Any note: may be confirmed when the evidence covers every fact and every tracked file, or updated with at most five exact-text patches. Action items, the important block, links, the title, type, tags, files and pin are never changed by a patch.
- Every archive, supersede, confirm and update must cite evidence from the packet.

## Unavailable sources

A missing, unreadable or unavailable file or source is not proof that a note is wrong. The note is deferred, held for seven days, and looked at again when its revision or sources change.

## Dry runs and preview

`maintenance preview [--limit N]` lists the next batch (ref, title, type, level, kinds, permitted actions, protection reason, size) and the notes that would be dropped. It makes no model call, reserves nothing and records no usage.

`maintenance run --dry-run` makes the real model call and lists each recommendation but changes no note. It works while maintenance is disabled (using the configured domains, or `--domain`), and it spends the same interval, note and budget allowance as a live run. Run it first on a fixture or a copied store (see the MAINT-1F gate in `VALIDATION.md`).

## History, audit and restore

- `maintenance history [--limit N]` lists runs, newest first. `maintenance history --run ID` shows each candidate's action, result, reason code and operation id.
- A retired note carries `last_retirement`: when, the action, the actor (`maintenance`), the successor if any, and the model's rule, reason, run id, operation id and evidence references. `read REF` shows it, and it stays after a restore until the next retirement replaces it.
- `restore REF` returns an archived note to `notes/`, pinned so maintenance cannot retire it again at once. Resolved and superseded notes use `reopen` and their successor.
- Each retirement is an ordinary journaled operation, so `log` shows it with actor `maintenance` and `undo OPERATION` reverses it.

## Session start

When a run has finished that the session has not been told about, SessionStart adds one line: `Memory maintenance: checked N, updated U, retired R, deferred D. maintenance history shows reasons; restore REF brings archived notes back.` When maintenance is enabled but blocked (`provider_budget`, `accounting_unavailable`, or a provider that is missing or unsupported), it adds one line naming the reason once per session. Nothing is printed if maintenance was never enabled. SessionStart also starts a detached run when one is due, never from a subagent, a headless or plan-mode session, a compaction, with `LIVING_MEMORY_NO_BACKGROUND=1`, or inside the reviewer's own process, and at most every ten minutes. The worker re-checks every condition itself.

## Permissions

The main agent may run `maintenance run`, `enable` and `disable` without a prompt; the user has authorized those by deciding to use the feature, and `enable` is the user's explicit act. Subagents and viewers may only read: `status`, `preview` and `history`. This is the one exception to the rule that archiving needs an approved plan; everything else (merge, delete, retag, taxonomy, remap) is unchanged.

## Discovery by other tools

`version` lists `maintenance` under `capabilities` with its version and actions. Hivemind and other clients read that, then use the public commands (`maintenance status`, `preview`, `history`, `restore`); they need no private interface. The `review` rows carry `maintenance: {permitted, protected}`.

## Backup, restore and relocation

Snapshots hold `maintenance.json`, `maintenance/state.json` and the run records, never `worker.lock`. A restore keeps the current configuration but sets it disabled (taking the snapshot's when the store has none), merges the checkpoint by element-wise maximum, and only adds run records, so it cannot lower today's consumed allowance. A relocation is refused while a run is unfinished or a worker holds the lock, and the destination starts disabled. Bundles carry notes (including `pinned` and `last_retirement`) but never maintenance configuration.
