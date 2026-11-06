# Compact script reference

Use `python living_memory.py COMMAND --help` for exact arguments. Common options are `--root STORE`, `--domain DOMAIN`, `--cwd PROJECT`, and `--actor main|subagent`. Outputs are compact JSON. Stable references are `domain:id`; a REF may also be `domain:` plus a unique 8+ character ID prefix (the short form injected lines show), a bare ID or prefix in the active domain, or an active note's exact title (case-insensitive, active and parent domains; ambiguous matches fail with `ambiguous_ref`). Outputs and receipts always carry full IDs. Revisions hash the full note file.

| Purpose | Commands |
| --- | --- |
| Setup/health | setup [--location PATH] [--non-interactive] [--source PATH]; bind, doctor, migration-plan, note-type-plan, taxonomy, validate, rebuild |
| Retrieval | search QUERY [--limit 5] --scope active|fallback|all; read REF --max-chars N; query SQL |
| New notes | capture TITLE BODY --subject SUBJECT --concern CONCERN [--supersedes REF] [--review-by DATE]; capture --title TITLE --summary TEXT [--detail TEXT]... [--link TEXT]... [--action TEXT]... [--done TEXT]... [--important TEXT]; capture --payload-file FILE |
| Keep current | update REF [--title] [--body/--body-file] [--summary/--detail/--link/--action/--done/--important] [--tag] [--type] [--context] [--review-by]; supersede REF --by REF; review |
| End-turn receipt | capture-finalize --session SESSION [--turn TURN] [--note DOMAIN:ID] |
| Reviewed mutation | plan update|rename|retag|resolve|reopen|archive|supersede|delete REF [--payload-file FILE] |
| Review/apply | inspect-plan ID; apply ID --expected-plan-sha256 HASH |
| History/recovery | history; diff OPERATION; undo OPERATION; recover OPERATION --direction forward|backward |
| Snapshots | backup [--keep 7]; restore-plan FILE (then inspect-plan and apply) |
| Bulk governance | merge-plan, retag-plan, taxonomy-plan, remap-plan |
| Derived views | sync VAULT; overlay-build VAULT; overlay-teardown-plan VAULT |

Update payload: `{"body":"reviewed replacement","expected_revision":"FULL_FILE_SHA256"}`. Rename: `{"title":"New title"}`. Retag: `{"tags":["subject/project","concern/decision"]}`. Supersede: `{"superseded_by":"SUCCESSOR_ID"}`. Merge: `{"target":"domain:id","sources":["domain:other-id"],"body":"reviewed combined knowledge"}`. Payload-based commands read JSON stdin when `--payload-file` is absent.

Capture types: knowledge, decision, preference, context, status, bug, plan, risk, research, policy. [Note types and use cases](NOTE_TYPES.md) defines each type, examples and the boundaries between related types. STATUS includes progress and milestones; corrections use KNOWLEDGE with `concern/correction`. Type determines the uppercase filename prefix: `decision` produces `DECISION - ...`, `bug` produces `BUG - ...`. Canonical schema-3 frontmatter stores type and omits prefix; read/search responses derive it. `--prefix` is a compatibility assertion that must match the type; omit it for new captures. The default type is knowledge. Bug resolve/reopen changes status and moves between archive/notes, retaining type bug and prefix BUG.

Use `note-type-plan` to preview conversion of existing notes, then `inspect-plan ID` and `apply ID --expected-plan-sha256 HASH` to review/apply it. Legacy BUG/RESOLVED prefixes become type bug; progress becomes status and correction becomes knowledge. Other notes retain their type. IDs, created timestamps, tags, lifecycle statuses and history are preserved. The plan updates local Markdown links for renamed files. Legacy notes remain readable until converted. To change an individual type, use `plan update REF` with payload `{"type":"bug"}`; its approved apply renames the file and updates local links. Resolved bugs must be reopened before changing to a non-bug type.

Title/body duplicate detection and optional idempotency keys prevent accidental repeated capture. A duplicate requested with a different type is rejected; update that note instead. Capture arrays and generic `batch` each accept at most 20 items. Batches serialize individual operations and return individual receipts; they are not an all-or-nothing multi-capture transaction. Generic batch payload: `[{"argv":["search","cache"]},{"argv":["taxonomy"]}]`.

Search defaults to active plus configured global domains, then explicitly configured foreign domains only after zero scoped hits. `--scope all` is explicit. Filters use `--filter tag=concern/decision`, prefix/status/type. Stale unfiltered retrieval falls back to canonical BM25; stale filtered retrieval requires `--rebuild`. `score` ranks results; `relevance` is the share of informative query terms found in the note's title or body. SQL is read-only, one statement, at most 100 rows, five-second execution budget.

Mutation plans snapshot all touched files and require their exact digest. `undo` creates a new plan and rejects later conflicting edits. `inspect-plan` bounds each diff at 16,000 characters and marks truncation. Overlay teardown plans are rooted at the vault: inspect/apply them with `--root VAULT`. Installer plans are rooted at CODEX_HOME.

Error exit codes: invalid=2, conflict=5, stale=6. Native host event wrappers fail with their adapter's bounded output contract.

`scripts/safe_write.py DOMAIN notes/FILENAME --content-file FILE --expected-revision HASH` returns an approved update plan, not a direct write.
