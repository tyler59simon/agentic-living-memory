# Maintenance

There are no background engines, and nothing is sent anywhere. The one usage record is a local diary (see below). Maintenance is the notes themselves plus a few explicit commands:

- `validate` and `doctor` report notes that fail to parse, duplicate IDs, and pending operations.
- `remap-plan` moves notes between domains with stable IDs as a reviewed, undoable plan.
- `merge-plan`, `retag-plan` and `taxonomy-plan` handle bulk changes through reviewed plans.

**Usage diary.** `.turns/usage-YYYY-MM-DD.jsonl` (one file per UTC day) records which notes memory showed (`inject`: at session start, on a prompt, or as a file hint) and which were then used: `read`, `update`, `check`, `supersede`, `confirm`, or `duplicate` when a capture re-stated a saved note. It also records each Stop reminder's kind (`reminder`) and each note saved on a turn whose prompt carried a durable statement (`signal-capture`). Each line holds only the event, the note ref, a 12-character session hash, a kind and a UTC time. There is no prompt text, title or body. The diary stays on this machine. Days older than 30 are removed with the rest of `.turns/`, and deleting the files by hand loses nothing canonical. `stats [--days 30]` reads it: notes injected 3+ times and never used (candidates for a sharper title or an archive plan), the most-used notes, reminders by kind and signal-turn captures per ISO week. Reads by subagents are logged too; that is a sidecar write, and notes are untouched.

**Semantic model.** `.models/` holds the model `semantic install` downloaded. It is not canonical, and backups skip it. `semantic remove`, or deleting the folder followed by `rebuild`, returns search to lexical-only.

Stores created by earlier releases may contain an `improvement/` folder (categorical search/read events and engine state). Nothing reads it any more; it is safe to delete by hand.
