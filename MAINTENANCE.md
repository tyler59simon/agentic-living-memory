# Maintenance

There are no background engines, and nothing is sent anywhere. The only usage records are a local diary and an operational log (see below). Maintenance is the notes themselves plus a few explicit commands:

- `validate` and `doctor` report notes that fail to parse, duplicate IDs, and pending operations.
- `remap-plan` moves notes between domains with stable IDs as a reviewed, undoable plan.
- `merge-plan`, `retag-plan` and `taxonomy-plan` handle bulk changes through reviewed plans.

**Usage diary.** `.turns/usage-YYYY-MM-DD.jsonl` (one file per UTC day) records which notes memory showed (`inject`: at session start, on a prompt, or as a file hint) and which were then used: `read`, `update`, `check`, `supersede`, `confirm`, or `duplicate` when a capture re-stated a saved note. It also records each Stop reminder's kind (`reminder`) and each note saved on a turn whose prompt carried a durable statement (`signal-capture`). Each line holds only the event, the note ref, a 12-character session hash, a kind and a UTC time. There is no prompt text, title or body. The diary stays on this machine. Days older than 30 are removed with the rest of `.turns/`, and deleting the files by hand loses nothing canonical. `stats [--days 30]` reads it: notes injected 3+ times and never used (candidates for a sharper title or an archive plan), the most-used notes, reminders by kind and signal-turn captures per ISO week. Reads by subagents are logged too; that is a sidecar write, and notes are untouched.

**Operational log.** `logs/log-YYYY-MM-DD.jsonl` (one file per UTC day) has one JSON object per line. Each line has `ts`, `level`, `kind`, `event` and `pid`. `kind` is one of:
- `hook`: a hook run, with `harness`, `domain`, a 12-character `session` hash, `ms`, `outcome` (which of context/deny/allow/continue_reason/warning/instructions it returned) and `context_chars`.
- `command`: a script command, with `actor`, `domain`, `ms`, `exit_code`, and `error` `{code, message}` on failure.
- `internal`: an exception that housekeeping deliberately absorbs (vault sync, daily backup, pruning, provenance, file hints, recall, usage diary, index reconcile). It carries `error` `{type, message, frames}`.

There is no prompt text, injected context, title, body or command argument. Exception messages are kept, capped at 300 characters with credentials redacted, so they can name a path or a note title. Tracebacks are kept only as `file:line function` frames. `LIVING_MEMORY_LOG_LEVEL` sets the lowest level kept: `debug`, `info` (the default), `warning`, `error` or `off`. Read-only app (`--actor viewer`) commands log at `debug`. Days older than 30 are removed once a day by session housekeeping. Backups skip the folder, and deleting it loses nothing canonical. `logs [--level L] [--kind K] [--event E] [--days 1] [--limit 50]` reads it newest first, and `doctor` reports `logs` (counts for the last 7 days and the last error) and warns about recent errors.

**Semantic model.** `.models/` holds the model `semantic install` downloaded. It is not canonical, and backups skip it. `semantic remove`, or deleting the folder followed by `rebuild`, returns search to lexical-only.

Stores created by earlier releases may contain an `improvement/` folder (categorical search/read events and engine state). Nothing reads it any more; it is safe to delete by hand.
