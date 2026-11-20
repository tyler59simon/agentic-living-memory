# Automatic note maintenance

Automatic note maintenance reviews a few of the oldest or most doubtful notes each day and keeps the store tidy without a per-note approval prompt. It is off until the user turns it on. This page is the reference for the feature: `SCRIPT_REFERENCE.md` lists the command flags, `RECOVERY.md` covers interrupted runs, and `VALIDATION.md` has the dry-run gate to pass before enabling it on a real store.

## What it does and does not do

A model reviews a small batch of candidate notes and recommends one action for each: keep, defer, confirm, update, resolve, archive, supersede or, for a queued pair of notes, distinct. The model only recommends. The script re-checks every recommendation against the current note, pin, action items, configuration and source files, and commits each accepted one as its own journaled, undoable change.

Maintenance does not:

- delete any note. Retired notes move to `archive/` (or are resolved or superseded) and can be brought back.
- touch preference or policy notes, or retire a pinned note (a pinned note can still be confirmed or get a factual update). The one exception to reading them is a queued pair (see Possible replacements): the reviewer may read such an older note and record the pair as distinct, but never change or supersede it.
- retire maps, briefs or runbooks. These can be kept, deferred, confirmed or updated.
- merge, retag, remap, change the taxonomy or edit configuration. Merge and retag need an approved plan; remap, taxonomy and configuration changes are direct commands for the main agent, never maintenance's.
- retire a note only because it is noisy (retrieved often but never used). Usage, age and review dates are signals, never proof.
- run when it is disabled, except a manual dry run.

Installing or upgrading never turns it on. `maintenance.json` starts disabled, and a snapshot restore or a store relocation always leaves it disabled.

## Before the model: re-baselining

A note goes stale when any byte of a file it lists changes. Before anything reaches the reviewer, an hourly sweep with no model (`rebaseline`, see SCRIPT_REFERENCE.md) re-baselines the notes whose cited lines, definitions and identifiers the change did not touch, and shifts their line citations. Only the rest are left stale for the reviewer.

## Enable and disable

```
maintenance enable --domain D [--all-domains] [--model NAME] [--max-notes-per-day N] [--timeout-seconds N] [--daily-budget-usd D]
maintenance disable
maintenance status
```

`enable` adds the session's domain (`--domain`, or, with `--all-domains`, every configured non-global domain) to the configured domains and sets `enabled_at`. A global domain is included only when it is named explicitly with `--domain`. The settings flags replace the matching defaults and leave the day's accounting alone. `disable` keeps the domains, settings and accounting, so disabling and re-enabling never creates a new allowance.

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

One packet on standard input to a single `claude -p` call: for each candidate a short key (C1, C2, ...), its title, type, tags, dates, the full note body (up to 24 KB), the review signals, the actions the policy allows, up to four of the files and sources the note cites (a file of 16000 characters or less whole; a larger one as an outline of its definitions or headings plus a 1200 character window), a digest flag for notes written from source files, and any active successor note it might be superseded by. The packet is redacted (credentials are replaced) and is capped at 128 KB. A note over 24 KB is deferred; notes that do not fit in the packet are left for a later run and cost no allowance.

Not sent: file contents beyond those, the repository tree, environment variables, other notes, and session history. The call runs with no tools, no MCP servers, no hooks and no session, in a fresh temporary directory, so the model cannot read files or run commands; it can only return a structured answer. Text inside notes that tries to give the model orders is treated as data. The packet is deleted after the run; run records keep metadata, refs, hashes and short reasons, not note bodies or prompts.

## The policy in plain words

- Pinned notes and protected types are never retired. Preference and policy notes are not reviewed at all. Maps, briefs and runbooks are never retired.
- Status notes: may be archived once their review date has passed and they hold no unique current instruction or unresolved work.
- Plan, bug and risk notes: resolved when every action item is checked and the body keeps nothing that is not covered elsewhere; archived only when current evidence shows the work was cancelled or is obsolete. Inactivity or unchecked items alone are not evidence.
- Knowledge, decision, context and research notes: archived only when current evidence shows the content is obsolete; superseded when an active successor covers all its useful content. An old decision that still explains a current constraint stays.
- Any note: may be confirmed when the evidence covers every fact and every tracked file, or updated with at most five exact-text patches. Action items, the important block, links, the title, type, tags, files and pin are never changed by a patch.
- Every archive, supersede, distinct, confirm and update must cite evidence from the packet.
- The model names each candidate by its key, never by ref and revision, so the two cannot disagree; the script maps the key to the note and the revision it sent. A report that breaks the output contract (unknown key, run id or evidence id, bad patch shape) applies nothing. A caller that names a note by ref and revision (scripts, tests) gets a mismatched revision deferred for that note only (`revision_mismatch`, no hold).
- **Holds.** A note the run keeps, defers or cannot act on is not picked again for 7 days; the next hold of the same note revision lasts 14 days, and every one after that 30. A change to the files the note cites does not lift a hold (a file edited daily would otherwise bring the note back daily); an edit to the note itself does, and starts the count again. An expired hold is remembered for 60 days so the count carries over. A possible-replacement review is held per pair: an edit to either note, or another successor, is reviewed on its own.

## Possible replacements

A newer decision on the same subject does not retire the older one by itself, and the capture-time check cannot tell a replacement from a sibling note (about one match in twenty is a real replacement). So the pairs go to the reviewer.

- **Queue.** When a captured decision has a top related decision that passes the strict bar (rank 1, a strong match, and a title score of 0.5 or a meaning match well above the model's threshold) in the same domain, and the pair is not marked distinct, the pair `{new, old, at}` is appended to `maintenance/related-queue.json`. This happens only where maintenance is enabled for the domain, is deduplicated, never fails the capture, and changes nothing in the capture result. A note that capture already lists as `similar` (title score 0.6 to 0.8) is left out, as the related list leaves it out.
- **Backfill.** The first maintenance run on a store with no backfill mark scans the active decision notes of the enabled domains with the same check and queues their pairs (the later-created note is `new`). It stops after 45 seconds, saves its place in the queue file and finishes on the next run. It runs once; the mark is `backfill.done` in the queue file.
- **Candidate `replaced`.** Queued pairs whose two notes are still active, in one domain, not distinct and neither a policy nor a preference (the model never reads those) become candidates of kind `replaced`: the older note is the candidate and the newer one its successor. They rank after every other kind (level 10), share the 10-a-day allowance, and are left for a later batch when another row already reviews the older note. The packet gives both notes in full with their created and updated dates.
- **Actions.** `supersede` (rule `replaced_by_successor`) retires the older note by the newer one, only where the policy table allows a supersede: never a pinned note. `distinct` writes `distinct_from` on the newer note with the reviewer's reason (8 to 200 characters) and `by: maintenance`; the older note is not touched, and the pair is never queued or listed as a duplicate again. `keep` drops the pair without a record. `defer` keeps it, held as described under Holds. A pair that is decided leaves the queue; one whose notes were retired, edited since the packet or marked distinct by hand is dropped or skipped as for any other candidate.
- **By hand.** `update NEWER --distinct OLDER --why '...'` records a pair as distinct (`by: user`); `update NEWER --not-distinct OLDER` removes the record from whichever note holds it, so the pair can be queued again. Both are journaled and undoable. `recap` lists the pairs marked distinct in its window.

## Unavailable sources

An unreadable or unavailable file or source is not proof that a note is wrong. The note is deferred and held (7, then 14, then 30 days), and looked at again when the hold ends or the note is edited. The exception is a digest whose source file is missing (deleted, while the bound folder or registered source it lived in is still there): the reviewer may archive it as `obsolete_source_claim`, and the executor re-checks that the file is still gone and its folder still present before applying. A file whose folder is gone (moved, unmounted or deregistered) is unavailable, not missing.

## Dry runs and preview

`maintenance preview [--limit N]` lists the next batch (ref, title, type, level, kinds, permitted actions, protection reason, size) and the notes that would be dropped. It makes no model call, reserves nothing and records no usage.

`maintenance run --dry-run` makes the real model call and lists each recommendation but changes no note. It works while maintenance is disabled (using the configured domains, or `--domain`), and it spends the same interval, note and budget allowance as a live run. Run it first on a fixture or a copied store (see the MAINT-1F gate in `VALIDATION.md`).

## History, audit and restore

- `maintenance history [--limit N]` lists runs, newest first. `maintenance history --run ID` shows each candidate's action, result, reason code and operation id.
- A retired note carries `last_retirement`: when, the action, the actor (`maintenance`), the successor if any, and the model's rule, reason, run id, operation id and evidence references. `read REF` shows it, and it stays after a restore until the next retirement replaces it.
- `restore REF` returns an archived note to `notes/`, pinned so maintenance cannot retire it again at once. Resolved and superseded notes use `reopen` and their successor.
- Each change is an ordinary journaled operation, so `log` lists it. `restore REF` brings back an archived note; `undo OPERATION` reverses an update when no later edit conflicts.

## Session start

When a run has finished that the session has not been told about, SessionStart adds one line: `Memory maintenance: checked N, updated U, retired R, deferred D. maintenance history shows reasons; restore REF brings archived notes back.` When maintenance is enabled but blocked (`provider_budget`, `accounting_unavailable`, or a provider that is missing or unsupported), it adds one line naming the reason once per session. Nothing is printed if maintenance was never enabled. SessionStart also starts a detached run when one is due, never from a subagent, a headless or plan-mode session, a compaction, with `LIVING_MEMORY_NO_BACKGROUND=1`, or inside the reviewer's own process, and at most every ten minutes. The worker re-checks every condition itself.

## Permissions

The main agent may run `maintenance run`, `enable` and `disable` without a permission prompt (the user chose this on 2026-11-14); the interval and daily limits still apply to every run. Subagents and viewers may only read: `status`, `preview` and `history`. Archiving is also a direct, undoable command for the main agent (`archive REF`); maintenance only archives within its policy and records why. Merge, delete and bulk retag still need an approved plan, and maintenance never runs them.

## Discovery by other tools

`version` lists `maintenance` under `capabilities` with its version and actions. Hivemind and other clients read that, then use the public commands (`maintenance status`, `preview`, `history`, `restore`); they need no private interface. The `review` rows carry `maintenance: {permitted, protected}`.

## Backup, restore and relocation

Snapshots hold `maintenance.json`, `maintenance/state.json` and the run records, never `worker.lock`. A restore keeps the current configuration but sets it disabled (taking the snapshot's when the store has none), merges the checkpoint by element-wise maximum, and only adds run records, so it cannot lower today's consumed allowance. A relocation is refused while a run is unfinished or a worker holds the lock, and the destination starts disabled. Bundles carry notes (including `pinned` and `last_retirement`) but never maintenance configuration.
