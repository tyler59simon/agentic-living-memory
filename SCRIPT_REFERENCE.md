# Compact script reference

Use `alm COMMAND --help` (or `python living_memory.py COMMAND --help`) for exact arguments. `alm` prints text in a terminal and JSON when piped or with `--json`. Common options are `--root STORE`, `--domain DOMAIN`, `--cwd PROJECT`, and `--actor main|subagent|viewer`.

`--root` must name an existing store (a folder with `bindings.json`), so a mistyped path fails with `no_store` instead of creating one; `bind` may start a store in an existing empty folder, and `restore-backup`, `version` and the journal commands (`history`, `diff`, `recover`, `undo`, which also run on the install folder, harness homes and a vault; see RECOVERY.md) need only an existing folder.

Outputs are compact JSON unless `alm` writes to a terminal. Stable references are `domain:id`; a REF may also be `domain:` plus a unique 8+ character ID prefix (the short form injected lines show), a bare ID or prefix in the active domain, or an active note's exact title (case-insensitive, active and parent domains; ambiguous matches fail with `ambiguous_ref`).

IDs are unique across the store, so a `domain:id` whose note has since moved (promoted to the global domain, or remapped) still finds it in its new domain. Outputs and receipts always carry full IDs. Revisions hash the full note file.

## Commands

| Purpose | Commands |
| --- | --- |
| Setup/health | setup [--location PATH] [--non-interactive] [--source PATH]; upgrade; bind, unbind, config get\|set, doctor, migrate --schema\|--note-types\|--baseline, taxonomy, validate, rebuild |
| Semantic search | semantic status; semantic install [--model NAME] [--from DIR]; semantic remove; semantic rebuild; semantic native on\|off |
| Retrieval | search QUERY [--limit 5] --scope active|fallback|all [--file PATH] [--explain]; search --file PATH; search QUERY --sources; search QUERY --pages [--all-domains] [--limit 10] (pages of folder notebooks; at most 50); search --links [--all-domains] [--status active\|any]; search [--type T]... [--tag T]... [--status S] [--all-domains] [--with-summary] [--offset N] [--limit 200] (no query: a list of notes; at most 1000); read REF --max-chars N [--section HEADING] (the result carries `stale` when a digest's source or a hashed file changed or is missing, and `currency` for a note that tracks files or a repo: `{state, verified_at, files:[{path, state}]}`, or `{state, verified_at, changed}` for a map or brief, from the currency cache with no git; `state` is `current`, `changed`, `missing`, `unavailable` or `unchecked`); query SQL |
| New notes | capture TITLE BODY [--global] [--subject SUBJECT] [--concern CONCERN] [--supersedes REF@REVISION [--drop-links]] [--review-by DATE] [--pin] [--rule] [--redact] [--file PATH]... [--files auto]; capture --title TITLE --summary TEXT [--section HEADING TEXT]... [--detail TEXT]... [--link TEXT]... [--action TEXT]... [--done TEXT]... [--important TEXT] (each value needs its own flag: with --title a positional value is refused, since read as TITLE BODY it would replace the title or body); capture --payload-file FILE |
| Keep current | update REF --expected-revision R [--title] [--body/--body-file] [--summary/--detail/--link/--action/--done/--important] [--section HEADING TEXT]... [--drop-section HEADING]... [--add-detail TEXT]... [--set-detail '**Label:** TEXT']... [--add-link TEXT]... [--add-action TEXT]... [--replace-actions] [--item TEXT | --item-index N] [--due DATE|none] [--owner NAME|none] [--priority P|none] [--tag] [--type] [--context] [--review-by] [--pin|--unpin] [--rule|--no-rule] [--distinct REF --why TEXT | --not-distinct REF] [--redact] [--file PATH]... [--files auto|none]; supersede REF --expected-revision R --by REF@REVISION [--drop-links] (the successor takes over the replaced note's links and files: `carried_links`, `carried_files`); confirm REF --expected-revision R [--session S]; review [--all-domains] [--tag TAG]... ; open [--all-domains] [--tag TAG]... [--due-by DATE] [--owner NAME] [--sort updated|due] |
| Subagent proposals | propose (the capture flags plus `--session S`; subagents may run it): checks the type, title, content and tags and refuses credentials (or redacts them with `--redact`) and text the user marked `<private>`, then stages the draft as `.turns/proposal-ID.json` and returns `{ok, id, status: proposed, domain, title, next, quality_warnings?}`; one command, `proposals`, covers staged notes (8 hex digit IDs) and page edits (12 hex digit IDs, in folder notebooks); proposals [list] [--kind note\|page\|all] [--status pending\|accepted\|rejected\|all] [--session S] [--domain D] [--notebook NB] (read-only) returns `{ok, kind, status, proposals:[{kind: note\|page, id, status, created, ...}], next}` (note rows: domain, type, title, note_ref?, reason?; page rows: domain, notebook, path, base_revision, size, edited?, and for a folder notebook `folder`, `file`, `orphaned` and `applied`); proposals show ID (read-only) returns `{ok, kind, proposal}`; proposals accept ID saves a staged note as staged (`--rule`, `--pin`, `--session S`, `--turn T`) or writes a page edit (`--expected-revision R`; `--edited` with `--body-file` or stdin writes the user's merge instead); proposals reject ID [--reason R] (a note needs the reason; main agent only, and a page edit only outside a Hivemind terminal's agent); capture --from-proposal ID with a full note (title and content) saves that note instead of the draft; alone it fails and names proposals accept; an edited note keeps the draft's domain unless `--domain` or `--global` is given; settling rechecks the proposal under the store lock; either way the proposal is marked accepted with `note_ref` and the result carries `proposal: {id, status}` |
| Open work | open (each item carries its note's `revision`); check REF 'ITEM TEXT' \| --index N --expected-revision R [--undo] [--session S]; resolve REF --expected-revision R [--session S]; reopen REF --expected-revision R [--session S]; restore REF --expected-revision R [--session S] [--turn T] |
| Usefulness | stats [--days 30] [--limit 10] |
| Protocol detail | help [TOPIC]: the protocol lines the shorter SessionStart fallback leaves out (the managed instruction block carries them all). Topics: capture, update, review, writes, briefs, ask, history, pages, writing. With a topic it returns `{ok, topic, markdown}`; with none, `{ok, topic: null, topics: {name: summary}, markdown}`. An unknown topic exits 2 with a `next` hint. It reads no store, so it works with no store, while memory is off and for subagents, and it is auto-allowed |
| Session switch | off [--session S]; on [--session S]; status [--session S] (see Session off switch below) |
| Review | review [--kind stale\|due\|interim\|duplicate\|warned\|noisy]... [--limit 20] [--all-domains] [--tag TAG]... |
| Operational log | logs [--level debug\|info\|warning\|error] [--kind hook\|command\|internal] [--event NAME] [--days 1] [--limit 50] |
| Recap | recap [--days 7] [--domain D \| --all-domains] [--project SLUG] |
| Activity | log [--domain D] [--project SLUG] [--since DATE] [--until DATE] [--note REF] [--type TYPE] [--event EVENT] [--limit 50] |
| Around a note | timeline [REF \| --date YYYY-MM-DD] [--before 5] [--after 5] [--type TYPE]... [--by updated\|created] [--all] |
| Before compaction | search QUERY --transcript --session SESSION [--limit 5] [--all]: excerpts from this session's own transcript, before the latest compaction (`--all`: the whole transcript) |
| End-turn receipt | capture-finalize --session SESSION [--turn TURN] [--note DOMAIN:ID] |
| Direct note retirement | archive REF --expected-revision R [--session S] [--turn T]: retires a note without a replacement (journaled, undoable; the result carries `operation_id` and `undo`); delete REF --expected-revision R removes a note the same way (the journal keeps its bytes, so undo brings it back) |
| History/recovery | history [--note REF] [--limit 20] [--markdown] (a bare list of `{operation_id, kind, state, created, files, summary, actor}`, where `summary` and `actor` are those of the operation's first note or change row (a count prefixes the summary when it changed several; an operation with no row, such as one still unfinished, shows its kind and the actor its ID and kind imply); with `--note` only the operations that changed that note itself, newest first, each also with `event`, `title`, `summary` and `actor`; `--markdown` returns `{ok, rows, markdown}` instead of the bare list; an operation on folder notebook bookkeeping (the registry, staged page edits) shows no summary and only its other paths, with `redacted` counting the rest, unless `--include-private`); diff OPERATION [--include-private] (attachments as hashes, private notebook changes as `{redacted: true}`); undo OPERATION; recover OPERATION --direction forward|backward (backward only for unfinished operations; committed ones use undo) |
| Maintenance | rebaseline [--dry-run]; maintenance status; maintenance preview [--limit N]; maintenance run [--dry-run]; maintenance enable [--all-domains] [--model NAME] [--max-notes-per-day N] [--timeout-seconds N] [--daily-budget-usd D]; maintenance disable; maintenance history [--limit N] [--run ID] |
| Snapshots | backup [--keep 7] (result may carry `unreadable`, `retained` and `warning`; the snapshot's `manifest.json` is version 3; a snapshot from before 2026-11-24 that holds `notebooks/` folders or `notebook-privacy.json` still verifies and restores, and a restore drops those parts); restore-backup FILE (one journaled write; undo OPERATION reverses it) |
| Housekeeping | housekeep: the daily backup plus pruning of old journals and logs (SessionStart starts it in the background) |
| Folder notebook pages | doc read --file PATH [--max-chars N] [--include-private], doc propose --file PATH [--base-revision R] [--note T] [--body-file F] [--include-private], doc folder set\|unset, doc undo (staged page edits are settled with `proposals`). Pages are the Markdown files of registered folder notebooks, not notes; agents read them, search them with `search QUERY --pages` and stage edits with `doc propose --file`. A plain `search` and `ask` add a `pages` array. Hosted notebooks, which kept pages inside the store, were retired on 2026-11-24: their `doc` commands and `domain create --notebook-only` are gone, and `--include-private` is gone from `search`, `ask` and `doctor`. See [NOTEBOOKS.md](NOTEBOOKS.md) |
| Bulk writes | merge [--payload-file FILE] (`{"target": "DOMAIN:ID@REV", "sources": ["DOMAIN:ID@REV", ...], "body": "..."}`: the target takes the body, each source is archived as superseded by it); retag [--payload-file FILE] (`{"OLD": "NEW"}` across every note and the taxonomy); remap [--payload-file FILE] (direct: moves notes between domains, journaled kind `reference_remap`; each mapping carries the `revision` its caller read); taxonomy add TAG... (direct: registers tags, kind `taxonomy_expansion`; plain `taxonomy` lists them) |
| Prose rewrite | rewrite [--payload-file FILE]: one direct, journaled write that replaces the bodies of up to 50 notes, from a JSON list of `{"ref", "revision", "body"}`. A stale revision, a title, or a map or brief is refused; `updated` and `review_by` are kept, and `sources` and `file_hashes` are not re-hashed (nor by `check`). Result: `operation_id`, `undo`, `summary`, `notes` (each with any `quality_warnings`) and `unchanged`. See [WRITING.md](WRITING.md#rewriting-a-note). |
| Batch | batch [--payload-file FILE]: up to 20 commands, `[{"argv":["search","cache"]},{"argv":["taxonomy"]}]`, run one at a time with one receipt each (not all-or-nothing) |
| Global domain | promote --suggest [--only DOMAIN] (writes nothing); promote REF@REVISION... (direct and journaled, so `undo OPERATION` reverses it; drops project tags and files, refuses digests, maps, briefs and notes naming a project path, like the automatic move); see [PROTOCOL.md](PROTOCOL.md) for `capture --global` and duplicates across domains |
| Projects | projects [--status active\|paused\|archived\|any] (read-only): `{ok, projects:[{slug,name,status,repos,aliases,description,charter,created,updated,tag,tag_registered}], warnings}`; project create\|update\|archive [--payload-file FILE] (payload on stdin otherwise; direct, journaled, undoable) |
| Systems | systems [--status active\|deprecated\|retired\|any] (read-only): `{ok, systems:[{slug,name,status,description,owners,capabilities,repos,depends_on,project,architecture,passport,created,updated,tag,tag_registered,dependents}], warnings}`; system create\|update\|retire [--payload-file FILE] (payload on stdin otherwise; direct, journaled, undoable) |
| Note types | types [--all] (read-only): `{ok, types:[{name,builtin,status,description,summary,sections,actions,review_days,concern,subject,created?,updated?}], warnings}`, built-in types first; `--all` adds retired custom types; warnings name note types in use that are not registered. type create\|update\|retire [--payload-file FILE] (payload on stdin otherwise; direct, journaled, undoable): create takes `name`, `description` and optionally `summary`, `sections`, `actions`, `review_days`, `concern`, `subject`; update takes `name` and any of those plus `status` (active\|retired); retire takes `name`. See [NOTE_TYPES.md](NOTE_TYPES.md#custom-types). |
| Knowledge base | ask QUESTION [--limit 5] (read-only): `{ok, query, domain, notes:[...label "note"], questions?:[{question, notes, sources, relevant}] (only when the query holds several questions: three or more list items, or at least two `?` sentences; up to ten, each with its own notes and excerpts, at most three of each), sources:[{label "source excerpt", source, path, file, kind, symbol, line_start, line_end, citation "path:12-30", snippet, score, relevance}], knowledge_base:{files, chunks, sources, synced, syncing}, hint?}`, notes first; search QUERY --sources (sources only; with several questions it adds `questions:[{question, results}]` too, as plain `search` adds `questions:[{question, results, relevant}]`); sources (read-only): `{ok, domain, implicit_disabled, sources:[{name,domain,kind,path,include,exclude,implicit,files,chunks,synced,state; remote kinds add base_url, token_env, remote:{synced,pages,skipped,truncated,error:{at,message}}}], index, warnings (a failed remote sync is a warning)}`; hits of a remote source add `title` and `url`, and `citation` is `Title <url>`; source-sync [NAME] [--rebuild] [--all-remote] (writes only the derived index; one at a time per domain; NAME of a remote source or --all-remote also pulls from the network, see Live wiki connectors); source add\|remove\|implicit [--payload-file FILE] (payload on stdin otherwise; direct, journaled, undoable) |
| Digest notes | ingest-scan [--source NAME] [--limit 50] [--changed] (`--changed`: neither current nor skipped files; read-only, subagents may run it; needs a synced index): `{ok, domain, total, counts:{new,stale,current,skipped}, shown, items:[{path, source, kind, size, status, abs, digest?, file?}]}`, ranked; capture\|update `--source PATH` (repeatable; stores `sources: [{path, sha256}]`); `scripts/ingest_run.py` (below); ingest-skip [--payload-file FILE] (`{"skipped": [{"path", "reason"}, ...], "clear": ["path", ...]}`) |
| Views | views [--all-domains] (read-only): `{ok, views:[...]}`, one row per view with a `kind` of `note` (`{kind,note,domain,title,note_title,path,fresh,artifact_url,artifact_current,exists}`) or `query` (`{kind,id,title,spec,added}`); views add REF [--title SHORT]; views add --spec-file FILE --title T (a query view); views update ID [--spec-file FILE] [--title T]; views remove REF\|ID; views render REF\|--all [--target local\|artifact] [--format html\|json] [--out FILE]; views published REF --url URL (see Views below) |
| Wiki | wiki [--archived] [--format html\|json] [--out FILE] [--theme purple\|emerald\|emerald-light]: html writes the page (default `<store>/.views/<domain>/wiki.html`) and returns `{ok, domain, path, pages, format}`; `--format json` with no `--out` returns `{ok, model}` (read-only: subagents and viewers may run only this form). `wiki --agent [--project SLUG] [--format html\|json] [--out FILE] [--theme T]` reads the saved agent-written edition the same way (default `<store>/.views/<domain>/agent-wiki.html`, or `agent-wiki-<slug>.html`) and makes no model call; `wiki --agent --build [--project SLUG] [--restructure] [--model ALIAS] [--budget USD] [--dry-run] [--progress]` calls the model to write it (main actor only, costs money, never auto-allowed by the hook) (see Wiki below) |
| Import/export | ingest [--source DIR] [--redact] [--allow-similar]; export --out FILE [--type T]... [--tag TAG]... [--all]; import FILE [--domain D] |
| Derived views | sync VAULT; overlay-build VAULT; overlay-teardown VAULT; overlay-sync-all [--primary DOMAIN] (every domain with a managed vault; the Claude Code SessionEnd hook runs it) |
| App server | serve --stdio: JSON-lines requests on stdin (`{"id", "argv", "stdin"?}`), one reply line each on stdout |

## Revisions

Revisions (since 2026-11-26): every command that writes an existing note needs the revision the caller read, the sha256 of the note file that `read REF` prints (the full value or its first 12 or more hex characters).

Pass `--expected-revision R` for the note a command names and `REF@REVISION` for the other notes it writes (`--supersedes`, `supersede --by`, `update --not-distinct`, `promote`); a JSON payload carries `expected_revision`, a `remap` mapping and a `rewrite` item carry `revision`.

A missing revision fails with code `revision_required` (exit 2) and an old one with `stale` (exit 6, message `stale_revision: ...`); either way nothing is written. Every write receipt returns `revisions: {ref: sha256}` for the notes it wrote, so the next write can pass them without reading again.

`open`, `timeline`, `review`, `search` and `search --file` rows carry each note's `revision`; index-backed rows can lag a direct file edit, so `read` is authoritative. Link rewrites in other notes, bulk retag, migrations, import, ingest, maintenance and Obsidian sync read the current file under the store lock instead, because the caller never read those notes.

## Payloads

Update payload (`update REF`): `{"body":"reviewed replacement","expected_revision":"REVISION"}`; with `"title":"New title"` it renames the note, and with `"tags":["subject/project","concern/decision"]` it replaces the note's tags, each with `expected_revision`. Supersede takes flags, not a payload: `supersede REF --expected-revision REVISION --by SUCCESSOR_REF@REVISION`.

Retag renames tags across notes: `{"OLD_TAG":"NEW_TAG"}`. Merge: `{"target":"domain:id@REVISION","sources":["domain:other-id@REVISION"],"body":"reviewed combined knowledge"}`. Payload-based commands read JSON stdin when `--payload-file` is absent.

## Projects

A project is a `project/<slug>` tag plus an entry in `<store>/projects.json` (slug, name, status active|paused|archived, absolute `repos`, `aliases`, `description`, `charter` note ref). Projects exist only through `project`, never `taxonomy add`.

`project create` payload: `{"slug":"hivemind","name":"Hivemind","repos":["C:/path/repo"],"aliases":["HM"],"description":"...","charter":{"domain":"optional","title":"optional","body":"The summary ..."}}` makes one journaled write (kind `project_create`) that adds the tag, the registry entry and the charter note (a `context` note tagged `project/<slug>`; domain: `charter.domain`, else the domain bound to `repos[0]`, else global).

`update` takes `{slug, name?, repos?, aliases?, description?, status?, charter?}` (kind `project_update`) and `archive` takes `{slug}` (kind `project_archive`); both change only `projects.json`. `capture --project SLUG` adds `project/SLUG` to a new note; `update` has no `--project` because facet flags there replace the whole tag set (use `--tag`).

`projects` and `doctor` warn about a missing repo folder, an unregistered project tag or a charter ref that no longer resolves. `projects.json` is protected config and part of `backup`.

### Project auto-attribution

On capture only (never `update`, `supersede` or `retag`; batch and payload captures included), a note with no `project/*` tag gets one from the first level that matches an active project whose tag is in the taxonomy: (1) the note's own `files`, relative paths resolved against every prefix of the target domain; (2) with `--session`, the files this turn touched (the hooks' session state, stored the same way); (3) with `--session`, the cwd.

Without `--session` the cwd is ignored, because `serve --stdio` passes the app's own cwd to every request. Per file the longest repo prefix wins, paths compare case-insensitively on Windows, and the union over the level's files is used: one or two projects are tagged and reported in `inferred_tags`; more than two add no tag and the result carries `project_candidates: [slugs]`.

An explicit `--project` or `project/*` tag disables inference. A broken `projects.json` skips inference silently.

SessionStart adds `Project: NAME (project/SLUG; capture with --project SLUG) — charter summary · charter REF` when the cwd lies in a project's repo (that project's open items sort first; the summary is the first thing dropped under budget pressure), or `Projects here: SLUG, SLUG — tag captures with --project SLUG` (160 characters at most) when the cwd lies above project repos.

## Digest notes

Any note may carry `sources: [{path, sha256}]` (at most 20; path text of at most 300 characters; sha256 64 hex digits), set by `capture --source PATH` and `update --source PATH` (repeatable; the file's current hash is computed; a path already listed takes the new hash).

PATH is absolute, or relative to a folder bound to the domain (or to `--cwd`); the stored path is relative to the bound folder (`billing/README.md`, like `files`), or `name:rel/path` for a file of a registered source outside the bound folders; a file outside both is refused.

With one `--source` and no `--idempotency-key`, capture uses the key `digest:<path>:<sha256>`: the same file content is digested once (a rerun answers `existing`, whatever the wording), and a digest of other content for exactly the same paths replaces the old one (`superseded`). Digests are exempt from the similar-title guard against each other, since each is about its own file.

A note is a stale digest when a listed file's sha256 differs or the file is gone (a note in a domain with no bound folder is never called stale): prompt hits, file hints, SessionStart lines and `ask`/`search` results say `STALE: <path> changed since this digest` (`ask` also sets `stale` on the hit and prefixes its snippet), `review` lists stale digests (`stale_digests` count, `stale` on each row), and a `sources` path counts like a `files` entry for file hints and Stop follow-through when its file is edited (the Stop reminder names the notes about all files edited in the turn in one sentence, at most five, with the `confirm` and `update` commands once; files a shell command or git changed are looked up too, except after a pull of more than 3 commits or 30 files).

`update` with a body change and `confirm` record the files' current hashes (the author checked them); other updates do not. `sources` is system-managed: the Obsidian vault copy keeps it but cannot edit it, and the search index stores it in a `sources` column.

`file_hashes: [{path, sha256}]` has the same shape (at most 30 entries on disk, at most 5 written) and is system-managed too: a knowledge, runbook, policy, bug, risk or research note gets the hashes of the first five regular files in its `files` (not folders, globs, files in `sources`, or missing files) at capture and at an update of its body, title or type; an update that only changes `files` or `sources` hashes the new entries and keeps the rest; `confirm` re-hashes them and writes `checked` (an ISO timestamp); `check` and `rewrite` leave them alone.

A changed or missing file reads `STALE: <path> changed since this note`. The index (version 4) stores both in `file_hashes` and `checked` columns.

`ingest-scan` ranks the knowledge-base index's files: decision records (`adr`; an ADR folder's README is a plain README), then READMEs and top-level docs, then wiki pages and other docs, then entry points and build files (`entry`, `build`), then modules ordered by how many other files' chunks mention their name and by size, then the rest (tiny modules `other`; tests `test`, which is anything under a `tests`, `spec`, `fixtures` or `testdata` folder, Markdown included, and which a search also ranks below code unless the query is about tests); `status` is `skipped` while the file's hash matches its skip marker (and no digest lists it), `current` when an active note's `sources` lists the path with the file's present hash, `stale` when it lists it with another one, else `new` (`digest` is that note's ref, `file` the path for `--file` when it lies in a bound folder, `abs` where to read it).

### Ingest run script

`scripts/ingest_run.py [--root R] [--domain D] [--cwd DIR] [--source NAME] [--batch 8] [--max-batches N] [--budget-usd X] [--parallel 1] [--all] [--kinds KIND,...|all] [--model sonnet] [--command-json '[...]'] [--allowed-tools ...] [--timeout 1800] [--log FILE] [--dry-run]` drives headless agents over the `ingest-scan` list, one batch of files per agent run (the agent writes its digests through this CLI; LM itself calls no model).

The agent argv template takes `{prompt}` and `{model}` (default `claude -p {prompt} --model MODEL --output-format json --permission-mode acceptEdits --allowedTools Read Glob Grep` plus a rule for this CLI as the prompt tells the agent to type it with the Bash tool (`Bash("<python>" "<CLI>" *)`; Claude Code PowerShell rules did not match a quoted path, so a PowerShell call relies on the Living Memory hook's auto-allow); without `{prompt}`, or for a Windows `.cmd` shim, the prompt goes to stdin; no shell).

`--dry-run` prints the batches and prompts and runs nothing; by default only `new` and `stale` files are sent (`--all` adds `current` and `skipped` ones; `--kinds KIND,...` limits the kinds sent to those of `ingest-scan` (`adr`, `readme`, `doc`, `wiki`, `entry`, `build`, `module`, `other`, `test`; `all` for every kind), and the default is every kind except `test`; `--changed-only` is an accepted no-op alias); `--budget-usd` stops starting batches once the agents' reported costs (`total_cost_usd` or `cost_usd`, their own claim) add up to it; `--parallel N` runs N batches at a time.

Each agent ends with `{"digested": [paths], "confirmed": [paths], "skipped": [{"path", "reason"}], "failed": [{"path", "reason"}]}`.

For a `stale` file the prompt names the old digest (`old digest: REF`) and the agent reads it first, then makes the smallest change that makes it true again: `confirm` (nothing it says changed; listed under `confirmed`), `update --set-detail` of the wrong bullets only, so the others (hand-added ones included) stay, or a new capture that replaces it and carries over the facts the file does not contradict (both listed under `digested`).

`failed` is a file whose capture was refused or errored, and it is never recorded as a skip.

When Claude's JSON reports `permission_denials`, none of that batch's skips is recorded either (they are logged as failed, since a blocked capture may have been reported as a skip); a log row has `failed`, `permission_denials` (a count) and `denied` (`[{tool, input}]`, `input` being the first 200 characters of the call's command, or of its JSON input).

The run log (JSON lines, one per batch plus a summary) goes to `<store>/logs/ingest-run-*.jsonl` or `--log`; stdout is the summary `{ok, batches_run, batches_planned, files_digested, files_confirmed, files_skipped, files_failed, cost_usd, verified:{current, skipped, still_open}, batches_failed, log}`, where `verified` comes from a fresh `ingest-scan`, not the agents' word.

A file an agent skips as trivial is recorded through `ingest-skip` (a main-agent write taking `{"skipped": [{"path", "reason"}], "clear": [path, ...]}`, either key or both, and answering `{ok, domain, recorded, cleared}`; `clear` removes the markers of those paths, so they read `new` again, and an unknown path counts 0) in the non-canonical sidecar `domains/D/.ingest-skips.json` (`{path: {sha256, reason, at}}`, newest 5,000; not validated or backed up) and shows as `skipped` in `ingest-scan` until its hash changes, when it is `new` again.

Interactively, an agent works the same list a few files per turn.

## Knowledge base

`ask` answers a question about the project (a chunk of a test file ranks a little below the code it exercises, unless the question mentions test, tests, testing or spec) from notes first, then from a deterministic library of source excerpts: the docs, wiki exports and code of the domain's sources, indexed in `domains/DOMAIN/sources.sqlite3` (FTS5, porter stems, camelCase words split; optional vectors when a semantic model is installed, embedding only changed chunks within a time cap).

The index is derived: not canonical, not backed up, not in a relocation, rebuilt by `source-sync --rebuild`.

A source is a folder (`kind` folder: docs and code), an HTML wiki export (`html-export`: headings kept, scripts, styles, nav, header, footer and breadcrumb or sidebar blocks dropped, table rows kept as lines) or a clone of a git wiki (`git-wiki`: Markdown, reStructuredText, AsciiDoc and HTML pages; a page without a heading is named by its file, `Retry-Policy.md` is "Retry Policy").

Every git repo under a bound folder (or the bound folder itself) is an implicit `folder` source named by its folder, with no setup (a linked git worktree of a repo already found is not a second source; the main checkout is kept).

`source add` registers another one in `<store>/sources.json` (`name` slug of 2 to 40 characters, since `w:Page.md` reads as a drive, `domain`, `kind`, absolute `path`, `include` and `exclude` globs, at most 20 each; an include glob indexes any matching text file) and `source remove` drops it; `source implicit` with `{"domain":"D","enabled":false}` turns the implicit repo sources off for a domain (`true` on again).

These are main-agent writes, like `project`; `ask`, `sources`, `search --sources`, `ingest-scan` and `source-sync` are auto-allowed and also allowed for subagents and viewers (`source-sync` writes only the derived index, as `search --rebuild` does for notes), except a sync that reaches the network (see Live wiki connectors below).

The memory store itself is never indexed (`source add` refuses a folder that is or lies inside it; one that merely holds it skips it), and a source holds at most 100,000 files (`truncated` in the sync result says so).

Sync skips binaries (a NUL byte in the first 8 KB), files over 1 MB, symlinks, hidden folders (except `.github`), `.git`, `node_modules`, `target`, `build`, `dist`, `.venv`, `venv`, `__pycache__`, `.idea`, `.gradle`, `vendor`, `bin`, `obj` and the like, and whatever the top-level `.gitignore` names (the common patterns: `*`, `?`, `[..]`, `**`, `/anchored`, `dir/`, `!negation`).

Credentials are replaced with `<redacted type='KIND'/>` (the detector `capture` uses) before anything is indexed, keeping line numbers.

Files are cut into chunks of about 1,200 characters with a small overlap, at headings (Markdown, reStructuredText, AsciiDoc, HTML) or top-level definitions (Python, Java, Kotlin, C#, TypeScript and JavaScript, Go, Rust, COBOL divisions and paragraphs, SQL statements); each chunk keeps its path, its symbol or heading path and its line range, which is the citation.

Only files whose (mtime, size) changed are read, and only those whose sha256 changed are re-chunked; deleted files leave the index.

Hooks never write the index themselves: SessionStart adds one line (`Knowledge base: N files indexed (synced DATE); ...`) and, when a source changed since the last sync (a stat walk of at most 0.1 s that stops at the first newer file or folder, plus `.git/HEAD`, `.git/index` and `.gitignore`, each against its own source's last sync; a tree too big for that walk is re-synced when its index is a day old), starts a detached background `source-sync` (a lock keeps two apart, and a new one starts at most every five minutes, never while one runs; `LIVING_MEMORY_NO_BACKGROUND=1` turns it off).

UserPromptSubmit adds up to two source excerpts (800 characters in all, usage kind `source`, each excerpt at most once a session) when notes gave fewer than two answers and the prompt's most informative words, chosen with the index's own vocabulary as for long prompts, are carried by a chunk (two words and two thirds of their weight; three words for a long prompt).

With a semantic model installed the chunk's stored vector must also be at least the model's similarity threshold (0.45) from the prompt, so a chunk that only shares its words (typically a test file) is left out; a chunk without a vector, or no model, keeps the word test alone. The debug detail lists up to six chunks dropped this way as `sources.off_meaning` with their similarity.

## Live wiki connectors

Live wiki connectors (`core/connectors.py`; the only network use besides `semantic install`; opt-in, explicit, credential by environment variable): `source add` with `kind` `confluence` or `notion` registers a remote source with no folder.

Payload: `name`, `domain`, `kind`, `token_env` (the NAME of an environment variable holding the credential, capital letters, digits and underscores; the token itself is never stored, and a payload that looks like a credential is refused), `base_url` (https; http only for 127.0.0.1 or localhost; no user, password or query; required for Confluence, default `https://api.notion.com` for Notion).

Confluence: `space` (key), `root_page` (id: the page and its descendants) and/or `page_ids`; `api_path` defaults to `/wiki/rest/api` (Cloud), use `/rest/api` for Server or Data Center; `token_env` holds a bearer token (a Data Center personal access token) sent as `Authorization: Bearer`, or `email:api_token` (Atlassian Cloud), sent as HTTP Basic.

Notion: `database_ids` (every page of the database) and/or `page_ids`; the variable holds the integration secret and the integration must be shared with those pages; sub-pages are not followed, list them or put them in a database.

`source-sync NAME` (or `source-sync --all-remote` for every remote source of the domain) pulls the pages into the derived cache `<store>/sources-cache/NAME/` (Confluence `body.export_view` as `.html`; Notion blocks converted to Markdown: paragraphs, headings, lists, to-dos, code, quotes, callouts, toggles, tables, six levels of nesting) with a `manifest.json` {page_id: title, version or last_edited_time, url, path}, then indexes it like an HTML export or Markdown folder; hits cite the page title and URL (`citation` is `Title <url>`, `file` the URL).

It is incremental (a page with an unchanged version is not rewritten, a Notion page with an unchanged `last_edited_time` is not fetched), follows removals (pages gone from a completed listing leave the cache and the index), and `source-sync NAME --rebuild` fetches every page again.

Limits: 20 s per request, at most three retries on 429 and 5xx (Retry-After honoured, up to 30 s), 5,000 pages, 2 MB per page (larger ones are skipped and listed in the manifest), 5,000 blocks per Notion page, 600 s per sync; redirects to another host are refused; credentials found in a page are redacted before it is written to the cache.

A failure (a wrong token, a timeout) exits 2 with a message that never carries the token, keeps the cache as it was, and is recorded in the manifest: `sources` shows each remote source's last sync, page count and last error, and `doctor` warns.

Only the explicit command reaches the network: plain `source-sync` (the SessionStart background sync, `--rebuild` without a name) only re-indexes the cache it has, and the permission layer never auto-allows `source-sync NAME` for a remote source or `--all-remote` (the host prompts; subagents and viewers are refused).

The cache is derived: not canonical, not backed up, not relocated; a removed source's cache goes at the next full `source-sync`. Deferred: SharePoint and Google Docs connectors.

## Systems

A system is a `system/<slug>` tag plus an entry in `<store>/systems.json`: slug, name, status active|deprecated|retired, `description` (its purpose, 500 characters at most), `owners` (10 at most), `capabilities` (20 at most), absolute `repos` (10 at most; may be empty for a system you depend on but do not own), `depends_on` (slugs of other registered systems; `systems` derives `dependents` from it), `project` (the slug of a registered project, in any status, or null; `update` with null clears it), `architecture` (the absolute folder holding its Architecture Suite output, or null) and a `passport` note ref.

Systems exist only through `system`, never `taxonomy add`.

`system create` payload: `{"slug":"billing","name":"Billing","description":"...","owners":["Payments team"],"capabilities":["Take payments"],"repos":["C:/path/repo"],"depends_on":["ledger"],"project":"billing-revamp","architecture":"C:/path/architecture/billing","passport":{"domain":"optional","title":"optional","body":"The summary ..."}}` makes one direct, journaled write (kind `system_create`) that adds the tag, the registry entry and the passport note (a `context` note tagged `system/<slug>`; domain: `passport.domain`, else the domain bound to `repos[0]`, else global).

`update` takes the slug and any of those fields plus `status` (kind `system_update`; `passport` is then a note ref) and `retire` takes `{slug}` (kind `system_retire`); both change only `systems.json`. `capture --system SLUG` adds `system/SLUG` to a new note.

A system's passport is assembled by readers from the registry entry and its tagged notes: `runbook` notes are its runbooks, `decision` notes its architectural decisions, `risk` and `bug` notes its weaknesses and debt.

Auto-attribution works as for projects (above), by the system's `repos`, for active systems only, and reports `system_candidates`; a note in a repo that belongs to both a project and a system gets both tags.

`systems` and `doctor` warn about a missing repo or architecture folder, an unregistered system tag, an unknown `depends_on` slug, a `project` that is not in the project registry or a passport ref that no longer resolves. `systems.json` is protected config and part of `backup`.

## Views

There are two kinds. A note view shows one note as a self-contained HTML dashboard; the rest of this section describes it. A query view is a saved page query (a table or a board over notebook pages) that Hivemind runs over its own index: ALM stores and checks the definition and never runs it.

The registry is `<store>/views.json`: `{"version":2,"views":[{"note":"<full id>","domain":"...","title":"<short title>","added":"<iso>","artifact_url":null,"published_revision":null},{"kind":"query","id":"q-1a2b3c4d","title":"Active projects","spec":{...},"added":"<iso>"}]}`. A row without `kind` is a note view, and a version 1 file (note views only) still loads.

It is protected config and part of `backup`, and its writes are journaled (kinds `views_add`, `views_update`, `views_remove`, `views_published`), so `undo` reverts them. The commands are direct: they change only derived output, never a note, so there is no plan or digest step.

`views` lists the registry with `kind` and, for a note view, `note`, `domain`, `title` (the short title), `note_title`, `path` (the absolute path of the local page), `fresh` (the page was rendered from the note's current revision), `artifact_url`, `artifact_current` (the published revision equals the note's current one) and `exists`; it is read-only and allowed for subagents and viewers, `--all-domains` lists every domain and without it the bound domain is listed (a query view has no domain and is always listed).

`views add REF` registers the note (`--title` is the short title, 120 characters at most, default the note's title), renders the page at once and returns its `path` and the `operation_id`; adding a note that has a view re-renders it. `views remove REF` drops the entry and deletes its pages; `views remove ID` drops a query view.

`views render REF|--all` writes the pages again: with no `--out` and `--target local` (default) it rewrites the standard path, with `--target artifact` it writes `<name>.artifact.html` beside it, and `--out FILE` writes one page there (a note without a view needs `--out` or `--format json`).

`--format json` prints the view model as `{ok, model}` (`--all`: `{ok, count, models}`) instead of a page, writes nothing without `--out`, and is allowed for subagents and viewers like `views` itself; the other subcommands need `--actor main`.

`views published REF --url URL` records `artifact_url`, sets `published_revision` to the note's current revision, and records `project`: the origin identity of the git repo around `--cwd` (else that repo's folder), or drops it when `--cwd` is outside a repo.

### Query views

`views add --spec-file FILE --title T` (no REF) registers one and returns its `id` (`q-` and 8 hex digits; the title is one line of 120 characters at most); `views update ID [--spec-file FILE] [--title T]` replaces the query and/or the title (`status` is `unchanged` when nothing differs).

`views render` refuses a query ID with `query_views_run_in_hivemind`, and `render --all`, `refresh`, `doctor` and the artifact hint skip query views.

The query is JSON: `version` 1; `from` `{notebooks: [id], folder, tag}` (folder and tag may be null; a tag loses a leading `#` and is lower-cased); `where`, at most 10 `{prop, op, value}` with an `op` of `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `contains` (these need a `value`, text or a number), `exists` or `missing`; `sort`, at most 5 `{prop, dir}` with `dir` `asc` or `desc`; `view` `table` or `board` (a board needs `groupBy`); `columns`, a list of property names; `limit` from 1 to 1000 (default 500).

Unknown fields are dropped and the file is at most 64 KB. A query that breaks a rule is refused with `invalid_view_spec: REASON`. The checks mirror `parseSpec` in Hivemind's `src/features/notes/queries.ts`. `views add --spec-file` and `views update` need `--actor main`. `version` reports `capabilities.views` as `{version: 2, kinds: ['note', 'query']}`.

### Note view paths

The standard path is `<store>/.views/<domain>/<slug>-<id8>.html`: the slug of the view's short title (the note filename rule) and the first eight characters of the note ID. `.views/` and `.cache/views.json` (the revision each page was rendered from) are derived: they are left out of snapshots and relocation, PreToolUse denies agent writes to `.views/`, and `views render --all` rebuilds them.

`reconcile` re-renders the local page of every view in its domain whose note revision differs from the cached one, so each write that changes a note keeps its pages current; a failure there is logged and never fails the write.

A view of a resolved, superseded or archived note still renders, with a status line (and the replacing note's title); a view of a note that no longer exists renders a "note not found" page, and `doctor` warns about it.

### Note view model

The model (`views render --format json`): `id`, `domain`, `title`, `type`, `status`, `updated`, `revision`, `summary` (plain text), `important`, `items` (`index` is the 1-based number `check --index` uses, `done`, `text`, `tags`, `id`, `detail`), `sections` (`heading`, `text`: Details bullets that no item took, Links and the named sections), `counts` (`open`, `done`, `total`), `superseded_by` (the replacing note's title, else its short ID) and `view_title` (the short title).

Action items are read as described in [WRITING.md](WRITING.md). The page has no network access: styles, script, the Plus Jakarta Sans font (SIL OFL, `assets/OFL-plus-jakarta-sans.txt`) and the logo are inline, and note text only reaches the page as JSON that the script turns into text nodes.

The page lists the items under Open, Done and All tabs, with a search box, a sort (written order, priority, ID) and one chip per tag. Chips combine by kind: tags of one kind (priority `P1`-`P3`, status words such as `approved` or `failed`, and any other tag) match with OR, and different kinds match with AND.

The local page is dark, reloads itself every 15 seconds while visible, and keeps its filters in `location.hash`; the artifact page follows the viewer's light or dark theme and never reloads. Both use the colours of Hivemind's Emerald Enterprise Dark theme; the artifact page uses Emerald Enterprise Light for light-mode viewers.

### Published note views

A page published as a Claude artifact is a copy: nothing local can push to claude.ai.

Claude's session start and prompt context therefore carry one line per view whose `artifact_url` is set and whose `published_revision` differs from the note's current revision (read from `.cache/views.json`) and whose `project` is unset or is the session's repo: run `views render REF --target artifact`, publish the printed file to the URL with the Artifact tool, then run `views published REF --url URL`.

Other harnesses never see the line.

## Wiki

`wiki` shows one domain as a structured wiki, on demand only (nothing re-renders it on write). `core/wiki.py` builds the model with no model calls.

Projects come from `projects.json`: a note belongs to the project whose `project/<slug>` tag it carries; with several such tags the repo of its first file breaks the tie; with none, the project whose repo holds its first file; anything left belongs to the pseudo-project General (slug `general`, or `general-2` and so on when a registered project already uses it).

Projects are ordered by page count, General last. Sections by type: Overview (brief, context), Architecture (map, and knowledge tagged `concern/architecture` with no files), Decisions (decision, policy, preference), Guides (runbook), Known issues (bug, risk), Plans and status (plan, status), Research, Reference (other knowledge with no files) and Code reference (knowledge with files).

The code tree: each note sits in the folder of its first file, as `<repo folder>/<path>`: a relative path is resolved against the domain's bound folders (as capture stored it), the owner is the registered repo whose full path holds it (a path outside every repo goes under `other files`); a path that exists on disk says whether it is a folder, otherwise a last segment with no dot is taken as one; a note whose file is a folder is that folder's "About this folder" page; chains of folders holding one subfolder and no notes collapse into one entry (`src-tauri/src`).

Notes are articles: `lead` is the summary as plain text and `body` the body without it; `out` and `backlinks` come from the `links` graph (same resolution rules); links that resolve into another domain are listed in `external` by casefolded title.

The model (`--format json`): `domain`, `generated`, `renderer`, `archived`, `notes` (`id`, `ref` (8 characters), `title`, `type`, `status`, `retired`, `tags`, `files`, `updated`, `created`, `lead`, `body`, `out`, `backlinks`, `project`, `section`, `folder`, `filename`), `projects` (`slug`, `name`, `description`, `aliases`, `repos` (folder names), `status`, `charter` (a note id in this domain, else null), `sections` (`{key: {notes, count}}`, Code reference `{folders, count}`), `count`, `updated`, `briefs`, `maps`), `folders` (`id` `c-` plus ten hex digits of a hash of project and path, so it survives rebuilds, `project`, `name`, `path`, `parent`, `folders`, `notes`, `count`), `sections` (`key`, `label`, `blurb`) and `external`.

The page is one self-contained file in Hivemind's shell (page tree, breadcrumbs, search, and a picker for Hivemind's three themes: Purple, Emerald Dark and Emerald Light) and needs no network.

With no saved choice it follows the system setting (dark is Emerald Dark, light is Emerald Light), or the host's `data-theme` when the page is a fragment; the choice is kept in `localStorage`, and `--theme purple|emerald|emerald-light` sets the theme the file starts in, which wins over a saved choice.

In Emerald Light the page tree stays dark, as Hivemind's sidebar does; its routes are plain hash names (`#p-<slug>`, `#s-<slug>-<section>`, `#c-<id>`, `#n-<ref>`, `#az`, `#recent`), so the same page also works as a claude.ai artifact. `--archived` adds resolved, archived and superseded notes, marked retired. `--out` follows the views rule: outside the store, or under its `.views` folder.

### Agent-written wiki

`wiki --agent` is a second edition in which a model, not the fixed rules above, chooses the structure.

`wiki --agent --build` runs tool-free `claude -p` calls through `core/maintenance_agent.py` (the maintenance provider, with its own policy and schema per call; `LIVING_MEMORY_MAINTENANCE_CHILD=1` keeps hooks and memory off inside them) and saves the result as `<store>/.views/<domain>/agent-wiki/domain.json`, or `project-<slug>.json` for `--project SLUG` (the prefix keeps a slug such as `con` from being a Windows reserved file name).

`claude` is found on PATH unless `LIVING_MEMORY_CLAUDE` names the native executable (an absolute path; a `.cmd` or `.bat` shim is refused, and the `--help` flag check still applies); maintenance ignores that variable. That file is derived output: it is not a note, is not journaled, and is rewritten atomically after every finished call, so an interrupted build loses at most one call.

The build refuses `--actor subagent|viewer`, `HIVEMIND_SESSION` and `LIVING_MEMORY_MAINTENANCE_CHILD`; `runtime.wiki_write` counts any `--build` (also an abbreviation) as a write and `runtime.auto_allowed` never allows it, so the harness's own permission prompt decides. A per-scope file lock refuses a second build of the same scope.

The model sees short keys (`N1` notes, `P1` pages, `S1` sections), never a ref, path or revision; validation refuses an unknown or repeated key, a repeated page title and any text over its limit, and a failing call changes nothing (an outline or place failure stops the build with `stopped: "error"`, a page failure keeps the page's previous version and is listed in `failed`).

Calls: the outline call (the first build, or `--restructure`) gets one entry per active note (key, title, type, tags, project, last updated date, first files, lead, headings; no bodies; a note that another note says describes a removed or replaced feature is to be omitted, or kept only as history) and returns at most 12 sections and 60 pages in all, each page with a title, a purpose and 1 to 25 source notes, plus an `omit` list with reasons; a packet over 400000 characters is refused with a hint to build one project at a time.

Later builds skip the outline, so they never take a note off a page that already uses it (after a feature is removed, `--restructure` plans again): the place call puts each new active note on a page (the packet gives each page's remaining capacity of the 25 sources; an answer that overfills a page, leaves out a new note or reuses a saved page title fails the call), a new page or the omit list, and one page call writes each page that is unwritten, gained a note, cites a note whose revision changed (the same revision maintenance uses) or cites a note that is archived, deleted or out of scope.

An untouched current page is never called again; `--restructure` plans again but keeps the id and text of a page whose title and planned notes did not change.

A page call gets the page's title, purpose and section, the titles and purposes of the other pages (so the text can link to them), and its source notes in full, with the longest bodies cut first so that all together stay under 150000 characters.

It returns `lead` (at most 600 characters), `body` (Markdown, at most 20000) and `cited`; `[N3]` markers become `[^k]` footnotes numbered in order of first appearance (the lead first) and `[[P4]]` becomes `[[Page title]]`, except inside what the page draws as code: fenced blocks (three or more backticks at any quote depth, closed by a line of only a run at least as long) and inline code spans, found the way the page's script reads the text (per table cell, with links, page links and citations taken whole); the page then cites only those notes.

`core/style.py` warnings run on the text, are stored with the page and never fail it, as does a warning for a page title or `##` heading in Title Case (every policy asks for sentence case; nothing is rewritten); a section title in Title Case is warned about on the section's first page, worked out when the wiki is read.

The writing rules are one constant (`WRITING_RULES`) in every policy, and each policy states that the packet is untrusted data. Page calls run four at a time under one budget counted in microdollars (as in maintenance): each call reserves at most $2 (outline), $1 (place) or $1 (page) of what is left, `--max-budget-usd` enforces it, and no call starts with less than $0.05 left.

`--budget USD` defaults to 5.00 and `--model` to `sonnet`. When the budget is used the build stops with `stopped: "budget"`, keeps the finished pages and lists the rest in `pending`; the next build continues.

A call that the provider charges above what it was granted also ends the build with `stopped: "budget"` and adds `overrun_usd` to the result (maintenance would block itself with `provider_budget`). SIGTERM or SIGINT during a build kills every running call, keeps the edition as saved and ends it with `ok: false`, `stopped: "error"` and `error: "cancelled"`.

`--dry-run` prints the calls it would make, the packet sizes and the budget, and calls nothing (for a first build `calls.pages` is null, because the outline decides it); `--progress` writes one JSON line per event (`start`, `call`, `end`) to stderr.

The build result: `{ok, domain, scope, dry_run, model, calls: {outline, place, pages}, written, failed: [{page, reason}], pending, stopped, cost_usd, budget_usd, path}` (`ok` false and exit code 1 when `stopped` is `error`, with an `error` text). Page ids are `pg` and six hex digits, kept across builds.

Without `--build`, `wiki --agent` reads the edition and fails with `no agent wiki for <scope> yet; run wiki --agent --build` when there is none.

Its model (`--format json`) has the structured model's shape plus `edition: "agent"`, `scope`, and `build` (`built`, `model`, `cost_usd` over the whole edition, `pages`, `written`, `stale`, `pending`, `complete` (false while a new note still waits for a place call), `omitted: [{id, title, reason}]`).

`notes` holds one `article` note per page with a `page` object (`purpose`, `written`, `stale`, `built`, `model`, `cost_usd`, `warnings`, `sources: [{id, ref, title, type, current, missing}]`; `[^k]` cites `sources[k-1]`; an unwritten page has an empty `body` and its purpose as `lead`), followed by the cited or planned source notes (section `sources`, shown at `#n-<ref>` and left out of the tree).

There is one pseudo-project `wiki`, and the agent's sections are keyed `a1`, `a2` and so on. A page is `stale` when a cited note's revision moved or the note is gone. `version` reports `capabilities.wiki_agent = {version: 1}`. The page adds citation links, a Sources list and an out-of-date flag, and skips the chronological grouping, code tree and Start here that the structured edition has.

## Binding

`bind NAME PREFIX...` registers folders for a domain. `unbind NAME` removes the domain's binding, as a journaled change to `bindings.json` (to reverse it, run `bind` again). It is refused for a global domain, an unknown name, and while the domain's `notes` or `archive` folder holds any note: the error names `remap`, which moves the notes to another domain first. The domain's folders stay on disk.

`unbind NAME --prefix PATH` removes only that folder and keeps the domain and its notes; it is refused for a folder the domain is not bound to and for its last folder. When a bound folder is renamed, `doctor` warns about the missing folder: run `bind NAME NEW_PATH`, then `unbind NAME --prefix OLD_PATH`. Both forms need `--actor main` and are not auto-allowed.

## Cross-domain reads

`open` and `review` take `--all-domains` (every domain folder; the result's `domain` is null) and `--tag` (repeatable, all must match). `review` rows also carry `revision` and `domain`, so a client can snooze with `update --expected-revision` without another read.

## Review worklist

`review` lists the notes that need a decision, each once, with `kinds`, a `reason` and an `action`.

Kinds, in this order: `stale` (a digest source or a hashed file changed or is missing, worded `STALE: <path> is missing` for a missing one; a map or brief whose cited files changed, read from the currency cache `.cache/currency.json` while the note's fingerprint (verification baseline, body, `files`, repo; not tags) and the git head still match, and, on `review` only, tracked uncommitted edits to cited files found with one bounded `git diff HEAD`), `due` (review date passed or old), `interim` (an UNCONFIRMED note unchanged and unconfirmed for 14 days), `duplicate` (a same-type note with a similar title, a title score of 0.6 or more with numbered status titles counted as similar, or a nearly identical body; the row carries `pair`, the older note, and a digest, map or brief is never paired), `warned` (the content checks of `quality_warnings`, not the style checks) and `noisy` (retrieved 3+ times in 30 days and never used).

Actions: `confirm`, `update`, `supersede`, `resolve`, `merge` and `archive` (the command `archive REF`); a noisy note gets a sharper title or summary, or a confirm, never an archive. A `confirm` on or after a note's last update dismisses its `warned` and `duplicate` rows (a pair: the later confirm on or after the later update).

The result keeps `due` (now the number of rows in all kinds), `stale_digests` (all stale rows) and `notes` (the rows, at most `--limit`, up to 100), and adds `kinds` (the kind order), `counts` per kind and a `hint`; each row of a note that tracks files or a repo also has `currency` (as on `read`; a map whose cache entry is missing or obsolete is `unchecked`, never `current`); `--kind` (repeatable) computes only those kinds.

The worklist only proposes: archive REF and merge are direct and undoable; the harness asks the user before a merge. It writes no note and no usage rows, so a `--actor viewer` run leaves the diary alone; its only write is the derived quality cache (see MAINTENANCE.md).

SessionStart shows one line from it (counts by kind and two notes, without the duplicate scan) and the Obsidian `Stale Notes` page lists the same rows.

## Capture

### Related notes

`capture` also returns `related` when active notes of the same type group may be replaced by the new one: up to 3 `{ref, title, type}` from one search over the new title and summary through the prompt hook's relevance gate (decision, policy and preference count as one group; the new note, notes already listed in `similar`, digests, maps and briefs are left out) and a `related_hint`: if the new note replaces one of them, `supersede REF --by NEW`; a different subject needs nothing.

It is advisory like `similar`, never blocks, and `update` does not return it. Status notes whose titles differ only by an identifier ("Sprint 3 status" and "Sprint 4 status") land in `similar` and are never refused as duplicates; plan titles keep the digit rule.

### User brief

`capture --global --type brief` with no `--file` captures the user brief (see PROTOCOL.md): a `brief` with no `files` in a global domain, one per store. A second active one is refused with `brief_exists conflict` (also when `update` retypes a note into one), and a brief with no `files` outside a global domain still needs `--file DIR`. A new user brief takes `subject/user` when the taxonomy has it.

`capture` and `update` return `user_brief` (text) for a preference, a note in a global domain or a note tagged `subject/user`, except the user brief itself: `user brief REF: update it if this changes how you work with the user (update REF --set-detail '**Label:** ...')` when one exists, else, with at least two active preferences in the global domains, `no user brief yet (N global preferences): capture --global --type brief --title 'User brief' --summary '...' --detail '**Working style:** ...' ...`.

It is advisory and never blocks.

### Metadata-only updates

Metadata-only updates keep `updated`: an `update` or `retag` that changes only `tags`, `review_by` and/or `files` leaves `updated` alone. `updated` means the content changed and drives recency and review age, so triage, retagging and snoozing must not reorder recency lists or reset the review clock. Changing the title, body, type or context still bumps it.

### Capture types

Capture types: knowledge, decision, preference, context, status, bug, plan, risk, research, policy, map, runbook, brief, plus the store's active custom types (`types` lists them). [Note types and use cases](NOTE_TYPES.md) defines each type, examples and the boundaries between related types, and how custom types work.

STATUS includes progress and milestones; corrections use KNOWLEDGE with `concern/correction`. Type determines the uppercase filename prefix: `decision` produces `DECISION - ...`, `bug` produces `BUG - ...`. Canonical schema-3 frontmatter stores type and omits prefix; read/search responses derive it. `--prefix` is a compatibility assertion that must match the type; omit it for new captures.

The default type is knowledge. Bug resolve/reopen changes status and moves between archive/notes, retaining type bug and prefix BUG.

### Baseline migration

`migrate --baseline` is one journaled write for stores written before `checked` became the verification baseline: for each map, brief or note with `sources` or `file_hashes` whose `checked` is older than its `updated`, it sets `checked` to `updated`, the baseline the old rule `max(updated, checked)` gave. Only `checked` changes. Run it once after upgrading; a second run changes no notes.

`migrate --note-types` converts existing notes in one journaled write (undo OPERATION reverses it). Legacy BUG/RESOLVED prefixes become type bug; progress becomes status and correction becomes knowledge. Other notes retain their type. IDs, created timestamps, tags, lifecycle statuses and history are preserved. It updates local Markdown links for renamed files.

Legacy notes remain readable until converted. To change an individual type, use `update REF --type bug`; it renames the file and updates local links. Resolved bugs must be reopened before changing to a non-bug type.

### Facet flags

Facet flags accept `--subject x` or `--subject subject/x`. When a capture names no `subject/` tag it gets `subject/user` for preferences and `subject/project` otherwise. When it names no `concern/` tag, decision, preference, bug, knowledge and policy notes get `concern/decision`, `concern/preference`, `concern/bug`, `concern/knowledge` and `concern/workflow`.

A default is used only if the tag is in the taxonomy, and the result reports it as `inferred_tags`. With `--session` and no `--domain`, capture, update and capture-finalize use the domain the hooks pinned for that session. Credentials are rejected by default.

`--redact` on capture or update replaces each one with `<redacted type='KIND'/>` (an assignment keeps its key name) and reports `redacted: [{kind, field}]`.

## Re-baselining

Re-baselining (`core/rebaseline.py`): a digest or hashed note whose file changed is re-baselined with no model when the change cannot touch what the note says.

Whenever a hash is recorded (capture, `--source`, `update --body`, `confirm`), the file's bytes are kept in the derived cache `.cache/baselines/` (text files up to 1 MiB with no credential in them; safe to delete); for an older hash the version is looked up in the last 50 commits of the file's local git history (never fetched from a remote; a dry run keeps nothing it finds).

A change is irrelevant only when the note cites the file (a `name.ext:N` or `:A-B` line citation, `line N`, or a symbol the file defines), no changed line is within 2 lines of a cited line, no cited definition (or member of a cited class) changed, no changed line names an identifier the note uses (a name alone in backticks, such as `` `db` ``, snake_case, CamelCase, UPPER_CASE or a defined symbol; the plain words of a backticked command or call, such as `git` in `` `git add -N` ``, do not count), and under 30% of the file changed.

Then the recorded hash becomes the current one and the note's line citations of that file are shifted to where those lines are now, as one journaled `rebaseline` operation per note (`undo OPERATION` reverses it); `updated`, `checked`, `review_by` and `confirmed` stay. A note with no baseline, a missing file, or any doubt, stays STALE.

`rebaseline [--dry-run] [--domain D]` runs it by hand and lists each stale note's files with `relevant` and a `reason`; subagents and viewers may run `--dry-run` only.

SessionStart starts a detached `rebaseline --scheduled` at most once an hour (marker `hooks/last-rebaseline.json`; not from a subagent, a headless or plan-mode run, a compaction or with `LIVING_MEMORY_NO_BACKGROUND=1`), limited to 20 seconds and one at a time.

A maintenance run re-baselines the notes it picked before it reserves anything (at most 20 seconds or a quarter of its timeout) and then picks again; `config set auto_rebaseline false` turns both off. A file with a credential or a line over 2000 characters is not cached, and history is not read from a partial clone. Maps and briefs keep their commit-level check.

## Automatic maintenance

Automatic maintenance (`core/maintenance.py`): off until `maintenance enable` (it never turns on at install).

`enable` adds `--domain D` (or every configured non-global domain with `--all-domains`; a global domain only when named with `--domain`) to the configured domains and sets `enabled_at`; the settings flags replace the matching defaults (model `sonnet`, 10 notes a day, 300 seconds, a budget of "1.00" dollars a day, one run per 86400 seconds) and leave the day's accounting alone.

`disable` keeps the domains, settings and accounting. `status` reads the configuration, eligibility, accounting (money as decimal strings), whether a worker is running, `blocked` and the latest run's counts; `errors` lists a malformed `maintenance.json` or unreadable run records.

`preview` lists the next batch (`ref`, `title`, `type`, `level`, `kinds`, `permitted` actions, `protected` reason or null, `bytes`), the notes too large for a packet and the estimated packet size, with no model call, reservation or usage event; it uses `--domain` when none are configured.

`run` is one synchronous review that changes notes; it needs maintenance enabled and returns `skip_reason` `disabled` otherwise. `run --dry-run` also works while disabled (it uses the configured domains, or `--domain`), calls the model, changes no note, and spends the same interval, note and budget allowance; its `decisions` list every recommendation.

`history` lists run summaries newest first (`--limit`, default 10); `--run ID` adds each candidate's action, result, `reason_code` and `operation_id`, and for a retired note its `rule` and `reason`. Subagents and viewers may run `status`, `preview` and `history`; `run`, `enable` and `disable` are main-agent commands, which the hook auto-allows for the main agent and denies to subagents.

SessionStart starts a detached `maintenance run --scheduled` when a run is due (never from a subagent, a headless or plan-mode run, a compaction, `LIVING_MEMORY_NO_BACKGROUND=1` or the reviewer's own child process; at most every ten minutes) and adds one line per finished run and session, `Memory maintenance: checked N, updated U, retired R, deferred D. ...`, or one line when maintenance is blocked.

`version` lists `maintenance` under `capabilities` with its actions. `review` rows carry `maintenance: {permitted, protected}`. `NOTE_MAINTENANCE.md` is the feature reference (limits, what the model sees, policy, restore).

`backup` includes `maintenance.json`, `maintenance/state.json` and the run records (never `worker.lock`), and `restore-backup` merges them so that a restore never lowers today's consumed allowance and always leaves the configuration disabled. A relocation is refused while a run is unfinished or a worker holds the lock, and the destination starts disabled.

### Distinct pairs

`distinct_from` is a list of at most 20 `{ref (domain:full-id), why (one line, 8 to 200 characters), by (maintenance or user), at}` on the newer note of a pair of same-domain notes judged to be different subjects, so neither replaces the other.

`update REF --distinct OTHER --why 'TEXT'` adds the pair (`by: user`); `update REF --not-distinct OTHER` removes it from whichever of the two notes holds it and is refused when the pair is not marked. Both notes must be in one domain. They are ordinary journaled, undoable updates, never move `updated`, and need no stdin.

The worklist's duplicate scan skips distinct pairs, the capture-time queue never queues one, and maintenance writes the record with `by: maintenance` (see `NOTE_MAINTENANCE.md`, Possible replacements).

Capture, the first maintenance run (a one-time backfill with the same check over the existing decision, policy and preference notes) and `maintenance run` keep `maintenance/related-queue.json`; it is advice, a damaged file reads as empty, and it is not part of a backup. `maintenance history --run ID` shows a `distinct` action and its count.

## Settings

`config get [KEY]` (read-only; subagents and viewers may run it) returns `{ok, settings:{KEY:{value, default}}}` for the store-wide settings at the top level of `bindings.json`, where `value` is what applies now and `default` is true when nothing is stored: `search_scope` (`{"global_domains": [...], "foreign_domains": [...]}`, default `["_global"]` and `[]`), `repo_aliases` (`{"REPO_FOLDER_NAME": ["alias", ...]}`, other names a prompt may use for a repo), `suggest_bind`, `auto_resolve`, `auto_rebaseline` and `cross_project` (each `true` by default; `false` turns off the unbound-repo bind hint, SessionStart auto-resolve, re-baselining and the sibling-domain lookup), and `digest_refresh` (off by default; `{"daily_budget_usd": "1.00", "model": "haiku"}` turns on the daily digest refresh: SessionStart starts `scripts/ingest_run.py --every-domain --stale-only` detached at most once a day, marker `hooks/last-digest-refresh.json`, not from a subagent, a headless or plan-mode run, a compaction or with `LIVING_MEMORY_NO_BACKGROUND=1`; it refreshes only digests whose files changed (confirming, updating or replacing each, as above), never digests files that have none, and stops starting batches once the agents' reported cost reaches the budget, so a day can go over by one batch; the budget is above 0 and at most 50).

`config set KEY VALUE` takes VALUE as JSON (`false`, `["_global"]`, `{"agentic-living-memory": ["LM"]}`; `null` removes the key so the default applies), validates it (`invalid_value`: booleans must be JSON booleans, domain names must be valid, `global_domains` cannot be empty, `search_scope` takes no other keys; `unknown_setting` for any other KEY), and writes `bindings.json` in one journaled change (kind `config_set`), so `undo OPERATION_ID` reverts it.

It returns `{ok, setting, value, default, previous, operation_id}`; an unchanged value writes nothing and has no `operation_id`. Only `--actor main` may set, and `config` is not auto-allowed by the hook. Domain bindings and aliases are not settings: `bind`, `unbind` and `remap` own them.

## Pin, retirement record and restore

`capture --pin` and `update REF --pin` or `--unpin` (mutually exclusive) set `pinned`, a true or false property (absent means false; a string such as "true" is refused).

A pin blocks automatic retirement only (archive, resolve or supersede by note maintenance, when it exists); manual `resolve`, `supersede` and `archive` still work on a pinned note, and factual updates and review reminders are unaffected. A pin change does not move `updated` and never counts as checking the note.

Every lifecycle change that retires a note (`resolve`, `supersede`, `capture --supersedes`, `archive`, a merge, auto-resolve, a vault lifecycle request) writes a system-managed `last_retirement` object: `version` 1, `at`, `action` (archive, supersede or resolve), `actor` (agent for a direct command, vault for a change made in Obsidian, auto_resolve, maintenance; plan in notes retired by a plan before 2026-11-26), and `successor` (`domain:id`) for a supersession; `reason`, `rule`, `run_id`, `operation_id` and up to 8 `evidence` entries are reserved for note maintenance.

Capture and update cannot set it. It describes the most recent retirement even after a restore, and the next retirement replaces it; notes retired before it existed have none.

`restore REF` (not to be confused with `restore-backup FILE`, which restores a snapshot) returns an archived note to `notes/` as active and pinned, keeping its id, body, created date and verification baseline, and fills `last_retirement.restored_at` and `restored_by`.

A resolved note is refused (use `reopen REF`), a superseded note is refused (its successor is named), and an active note is refused (`Only archived notes can be restored`), so a second restore changes nothing. A filename clash, or a second map or brief for the same folder, is refused before anything is written.

Links in other notes that point at the note are rewritten against their current text, in every domain, when it moves between `notes/` and `archive/` (resolve, reopen, archive and restore alike); a move that would rewrite more than 100 notes fails with `too_many_link_updates`. `restore` is journaled and undoable.

`list` rows carry `last_retirement` (`action`, `at`, `actor`, `reason`) when set, and `list --with-summary` and `review` rows carry `pinned`. `version` lists `restore`, `pin` and `direct_writes` under `capabilities`.

## Rules

`capture --rule` and `update REF --rule` or `--no-rule` (mutually exclusive) set `rule`, a true or false property (absent means false; a string such as "true" is refused) on a policy or preference note.

`capture` and `update` refuse the flag on any other type with `rule_type: only policy and preference notes can be rules, and this note is TYPE` (error code `invalid`, with a `next` hint), and refuse retyping a rule to another type; `capture` refuses `--rule` beside a JSON payload as it does `--pin` (a payload carries `"rule": true` itself).

Every SessionStart shows the rules of the active, parent, child and global domains in a Rules block after the last-session line and before the guidance: the global domains' rules first, then the other domains in scope order, each domain's by title. The header is `Rules (follow them in every turn unless the user's current instructions or the source say otherwise):`.

Each line is the short ref, the title and the whole summary as written (code spans and markup kept exact) while the block stays within 1,200 characters; from the first rule that does not fit, the rest show by title with `(title only; read it)`, and none is left out.

The block takes none of the three guidance slots and no index line, the degrade loop never cuts it and the SessionStart budget does not count it, so it survives a small budget and a compaction (SessionStart re-fires with source=compact).

For Codex, SessionStart sizes the block to what the protocol and the lines before it leave within `runtime.SESSION_LIMITS` (9,000 characters, the `additionalContextLimit` that `setup.py codex` writes), and the block always ends inside that limit; the titles that do not fit become one `- +N rules did not fit the context limit` line.

A rule counts as shown for prompt injection, like guidance with its snippet, only when its whole summary is in the delivered context. On Claude, PreCompact asks the summary to keep the rules verbatim and names them by title (`- Keep these rules verbatim: A; B`, at most 600 characters, then `(+N more)`).

`capture` and `update` of a rule return `rule_warning` (text) when the domain and the global domains hold more than 15 rules; it is advisory and never blocks, and a terminal shows it as a `rule_warning:` line.

`capture --rule` that matches an existing note (a duplicate or an idempotent retry) does not change it: when that note is not a rule, the result says so in `rule_warning` with the command, `update REF --rule`; when it is archived, the command is `restore REF` (then `update REF --rule` if it is not a rule yet), and when it is superseded, `rule_warning` follows the chain to the note that replaced it last and names that note: `update SUCCESSOR --rule`, `restore SUCCESSOR` when it is archived, or that its type cannot be a rule, since SessionStart shows neither.

Likewise `capture --pin` (or a pinned proposal) that matches an active note that is not pinned leaves it unpinned and returns `pin_warning` with `update REF --pin`. A capture payload's `rule` must be true or false (`0`, `null` and `""` are refused too).

`propose --rule` and `propose --pin` keep the flag in the draft (a draft's `pinned` must be true or false too), and `proposals accept ID --rule` or `--pin` marks the saved note. A rule change does not move `updated` and never counts as checking the note.

`list --with-summary` rows carry `rule`, the activity log records `ruled` and `unruled`, an Obsidian vault edit may set it like `pinned`, and `version` lists `rule: 1` under `capabilities`. Note maintenance never reviews policy or preference notes, so a rule needs no pin to stay.

## Open work

`open` lists the unchecked action items of active plan, bug and risk notes in the active, parent and child domains (and the global domains), newest note first.

Each item has `ref`, `short_ref`, `title`, `index` (1-based among the note's items) and `item` (the full text), plus `task` (the text without its tail), `due`, `owner`, `priority` (null when absent) and `overdue` (true when the due date is before today, false when it is not, null without a due date); see WRITING.md for the `(due: YYYY-MM-DD, owner: NAME, priority: high)` tail.

Each row has a `kind`: `note` for the rows above, `page` for an unchecked task of a notebook page (`domain`, `notebook`, `path`, `title`, `line`, `item`, `task`, the same `due`, `owner`, `priority` and `overdue`, and no `ref` or `index`: a page task is ticked by editing its page).

`--pages injectable|searchable|none` picks which notebooks add page tasks (default injectable, hosted or folder; `none`: notes only), and `--tag` lists no page tasks.

`--due-by DATE` keeps dated items due on or before DATE, `--owner NAME` keeps items with that owner (case-insensitive), `--sort due` lists the earliest due date first with undated items last (default `--sort updated`: newest note first), and `--limit` applies after the filters.

`update REF --item TEXT` (or `--item-index N`) with `--due DATE`, `--owner NAME` or `--priority P` sets those fields on one item, and `none` clears one; it is refused with `--action`, `--done` or `--replace-actions`. `check REF ITEM` ticks one item, matched by exact text (case-insensitive), else by a unique part of it; `--index N` picks one by position, and `--undo` unticks.

It changes only that checkbox, as a journaled update: `undo` reverts it, and with `--session` it records the turn's receipt. Ambiguous text fails with `ambiguous_item` and lists the candidates. Both commands are auto-allowed. `open` is read-only, so subagents may run it. When `check` ticks the last open item it returns a `hint` naming `resolve REF`.

`resolve REF` marks a plan, bug or risk note resolved and moves it to the archive, so finished work leaves session context and search; it is a journaled, undoable change that needs no plan, and `reopen REF` reverses it. SessionStart counts overdue items in its Open work header and names the three oldest on an `Overdue:` line.

It also names active notes whose action items are all done, and resolves those that were last updated more than 3 days ago and are not pinned: once per 24 hours per domain, for the active domain only, unless `"auto_resolve": false` is set at the top level of `bindings.json`; it reports them in one `Auto-resolved N finished note(s): TITLE (reopen REF), ...` line (see [MAINTENANCE.md](MAINTENANCE.md)).

## Provenance

The PreToolUse hook records the paths, never the content, of files the main agent changes in a turn. Paths are relative to the bound project folder, or absolute outside it; scratchpad and memory-store paths are skipped, and shell commands count only for files inside the project. Captures and updates with `--session` list the turn's paths not yet on the note as `suggested_files`.

`--files auto` stores them in the note's `files` property, `--file PATH` adds one, and on update `--files none` clears the list (update otherwise adds to it). Captures with `--session` also store `origin: "HARNESS:SESSIONHASH"`, a 12-character hash rather than the session ID. `files` is editable in Obsidian; `origin` is not.

`search --file PATH` with no query lists notes whose `files` include the path, or anything under it if it is a folder, followed by notes that name the path in their body. It honors `--scope`, including the foreign fallback; `--filter` needs a query. With a query, `--file` acts as a filter.

## Usefulness

`stats` summarises the local usage diary (see [MAINTENANCE.md](MAINTENANCE.md)) for the active, parent and global domains.

It returns `noisy` (retrieved for a prompt or a file 3+ times and never read, updated, checked, superseded, confirmed, resolved or re-captured; session-start guidance and codebase maps are standing context and are counted apart as `standing`), `used` (most used first), `reminders` by kind (`nudge-preference`, `nudge-correction`, `nudge-decision` and `nudge-remember` for the line a prompt adds to its own context; `remember`, `decision`, `directive`, `fix`, `doc`, `follow`, `stale`, `unbriefed`, `brief`, `explored`, `grown` and `proposals` for a Stop continuation; the prompt nudges are `nudge-remember`, `nudge-correction`, `nudge-preference`, `nudge-decision` and `nudge-directive`) `proposals` (`{proposed, accepted, accepted_edited, rejected}` from the `propose`, `proposal-accept` and `proposal-reject` diary events), `signal_captures` per ISO week, and `capture_coverage` (`{days, mutating, covered, ratio}`: of the sessions in the window whose last ledger row changed files, how many saved a note or left a receipt, from `ledger/sessions-YYYY.jsonl`; `ratio` is null with no such session; a read-only PowerShell command counted as an edit inflates `mutating`).

Each note entry has `ref`, `short_ref`, `title`, `status` (`missing` once a note is gone) and its non-zero `counts`. It is read-only and auto-allowed, and subagents may run it. SessionStart adds `Capture coverage: N of M sessions that changed files in the last 14 days left a note or receipt.` when M is at least 5 and N/M is under 0.30, at most once a day (a date in `.cache/coverage-told`).

## Session off switch

`off` turns Living Memory off for one session, `on` turns it back on and `status` reports it, each as `{ok, session (12-character hash), memory on|off, reason switch|env|null}` (`status` adds `known`: whether a hook has seen the session).

The session is `--session`, else `CLAUDE_CODE_SESSION_ID`, which Claude Code sets for every shell command, so `! alm off` inside a Claude Code session needs no flag; Codex and other harnesses pass `--session`. Without either the command fails with `no_session`. The flag lives in the session's hook state (`.turns/<sha256>.current.json`).

`LIVING_MEMORY_OFF=1` in the environment that starts the harness turns the whole session off from its first hook, and `on` then fails with `env_off`.

While off, hooks inject nothing (one notice on the first prompt after `off`, and after a compaction, tells the agent memory is off), write no session state, usage diary rows or ledger entries, and raise no Stop reminders; session-end housekeeping (vault sync, daily backup, journal prune) still runs and direct edits of store files are still denied.

Every other command run with that session (by `--session` or the environment) fails with `memory_off`, reads included; `version` and `doctor` still run.

The first prompt after `on` repeats the session-start protocol, led by `Living Memory is back on for this session.` `status` is read-only and auto-allowed; `off` and `on` are not auto-allowed (an agent running them gets the normal permission prompt) and subagents and viewers may not run them. Hook log lines for an off session keep metadata only, even for a sandbox store.

## Test clock

`LIVING_MEMORY_CLOCK_OFFSET_SECONDS` (a number of seconds, negative allowed) shifts the clock that stamps and ages records, but only in a headless run (`LIVING_MEMORY_HEADLESS=1`, `CLAUDE_CODE_SESSION_ATTENDED=0` or `CLAUDE_CODE_ENTRYPOINT` starting with `sdk`); in any other run it is ignored.

It moves note, journal, ledger and usage timestamps, review dates, the daily maintenance allowance day, recap windows and the once-a-day markers, and a headless run with an offset also auto-resolves finished work (`core/clock.py`). A value that is not a finite number is ignored and logged once.

Real time still governs hook timing, timeouts, lock ages, cooldown markers, log files and backups, and scheduled maintenance stays off in a headless run. The codebase map "commits since" check (`core/maps.py`) compares note dates with git commit dates, so a simulation that sets the offset also sets `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE` to the simulated time.

## Operational log

`logs` returns `level_setting`, `matched` and `entries`, newest first, from the local operational log (see [MAINTENANCE.md](MAINTENANCE.md)): hook runs, command runs and absorbed exceptions, with no prompt text, context or bodies (error messages can name a note title or path; a pre-tool-use run that returned nothing is logged at debug only). `--level` is the lowest level shown (default info).

`--days N` is the last N x 24 hours (default 1), at most 30, and `--limit` is at most 1000. The command does not log itself. `LIVING_MEMORY_LOG_LEVEL` (`debug|info|warning|error|off`, default info) sets what is written. `debug` adds each hook's decision detail and each command's result status. `LIVING_MEMORY_LOG_CONTENT=1` adds text, but only for a sandbox store (see MAINTENANCE.md).

It is read-only and auto-allowed, and subagents may run it.

## Activity log

`log` answers "what changed, and when?" across every domain (`--domain` narrows it).

It returns `count` and `rows`, newest first, each `{time, event, ref, title, type, domain, projects, op, summary, actor, user}`, where `user` is the operating system login name of the process that wrote the journal (left out of rows from older journals and when the name could not be read; never an email or git identity).

`summary` is one line of at most 200 characters (`Created as bug: first sentence`, `title "A" -> "B"; tags +x -y; ticked "item"; Why +3/-1 lines`, `by maintenance, rule: reason`, `superseded by ID8`, `promoted to DOMAIN`, `undid OP8`); `actor` is `maintenance`, `auto_resolve`, `obsidian`, `undo`, `plan`, `agent:HARNESS` (a capture that recorded its origin) or `command`. Session rows have neither.

Note rows come from the operation journals: `created`, `updated`, `confirmed` (a confirm that changed nothing else), `superseded`, `resolved`, `reopened`, `archived`, `restored`, `pinned`, `unpinned`, `ruled`, `unruled`, `deleted`, `promoted`, `moved` (to another domain) and `undone`; a note whose links were only rewritten because another note moved has no row.

Session rows (`event` `session`, `type` `session`) come from the session ledger: `ref` is the session hash, with `harness`, `notes` (refs saved), `files` (files changed) and `user` (the OS user name from the ledger row's `u`, when it has one); only the last row of a session is shown.

`--since` and `--until` take an ISO date or time (`--until` includes the whole day), `--note` one note (a REF or ID, deleted notes too, which also lists the sessions that saved it), `--type` a note type or `session`, `--project` a project slug, and `--event` one of the events above. The default `--limit` is 50. The command only reads, so subagents, viewers and the hook auto-allow it.

Summaries quote titles, tags, the first sentence of a new note, checklist items and a retirement reason, never the rest of the body. Rows of the note journals are cached in `.cache/activity.json` (see [ARCHITECTURE.md](ARCHITECTURE.md)); `timeline` is unchanged.

`history` without `--note` lists every operation, now with `summary` and `actor`; `history --note` reads the same cache (a row for the note in a final operation), so it no longer opens every journal and no longer lists operations that only mention the note (derived-state markers, link rewrites in other notes, registry edits).

`--markdown` renders one line per row, such as `- 2026-11-14 18:25 archived (maintenance, tyler): by maintenance, obsolete_content: reason` (the user name follows the actor when the row has one). `history` rows carry the same `user`, after `actor`.

`version.capabilities.history` is 2 when `history --note` and `--markdown` work this way, and 3 when bare `history` rows also carry `summary` and `actor`.

## Recap

`recap` summarizes the last `--days` days (1 to 90, default 7) of one domain (the bound domain, or `--all-domains`), optionally for one `--project`.

It returns `since`, `until`, `days`, `domains`, `counts` (rows per event), `changes` (per event created, updated, resolved, superseded, archived, restored and deleted: one entry per note with the latest `summary`, its `actor`, `time` and how many rows it had in `count`), `maintenance` (`runs`, `reviewed`, `kept`, `deferred`, `updated`, `confirmed`, `retired`, `charged_usd` and `retired_notes` with `title`, `action`, `rule` and `reason`; only runs that reviewed a note in scope count, and only those notes, but `charged_usd` is each such run's whole charge, which cannot be split by domain), `distinct` (pairs marked distinct in the window, newest first: `new`, `new_title`, `old`, `old_title`, `why`, `by`, `at`, and a Marked distinct section in `markdown` when there are any), `sessions` (`count`, `by_harness`, `notes_saved`), `open_work` (`notes`, `items`, the `top` five), `review` (`due` and `by_kind`, the cheap worklist without the duplicate scan) and `markdown`, a readable page that lists at most 15 notes per group.

It reads the activity log, the maintenance run records, `open` and `review`, writes nothing, and is open to subagents and viewers.

## Timeline

`timeline` lists the active domain's notes in date order around an anchor, answering "what else was decided around then?". The anchor is a REF, else the first note on or after `--date`, else the newest note. It shows `--before`/`--after` notes either side (default 5, at most 50) by `--by updated` (the default) or `created`. `--type` (repeatable) narrows the list but always keeps the anchor.

`--all` includes superseded, archived and resolved notes; a retired anchor is always shown. Each row has `ref`, `short_ref`, `date`, `type`, `title` and `status`, and the anchor row has `anchor: true`. It is read-only and auto-allowed, and subagents may run it.

## Auto-memory ingest

`ingest` turns Claude Code auto-memory topic files into notes in the active domain, in one journaled write (undo OPERATION reverses it). The default source is Claude's folder for `--cwd`: `CLAUDE/projects/<cwd with every non-alphanumeric character as ->/memory`, where `CLAUDE` is `CLAUDE_HOME` when set (a test override), else `CLAUDE_CONFIG_DIR`, else `~/.claude`.

`MEMORY.md`, symbolic links, files over 64 KB and non-UTF-8 files are skipped. The frontmatter `metadata.type` (or `type`) maps user and feedback to preference, project to context, and reference to knowledge. `name` becomes the title, `description` the summary, and the body's lines become Details bullets (headings become bold labels).

Tags default from the type; context notes get `concern/knowledge`. Each note's capture key is `auto-memory:` plus the file's SHA-256, and its ID derives from that key. A re-run therefore adds nothing: unchanged files are listed under `unchanged`, and a note with the same ID added meanwhile makes the write fail as stale.

Files with credentials are skipped unless `--redact` is given; near-duplicate titles are skipped unless `--allow-similar` is given. The result lists `created`, `unchanged` and `skipped` with reasons. It is not auto-allowed, and subagents may not run it.

## Bundles

`export --out FILE` writes a zip of the active domain's notes: active notes only, or every note with `--all`. `--type` (repeatable) keeps any of the types, and `--tag` (repeatable) keeps notes carrying all the given tags.

The zip holds the canonical files as they are, under `notes/` and `archive/`, plus `manifest.json` (`schema`, `domain`, `exported`, each note's `id`, `path` and `sha256`, and the tags used). It refuses an existing file or a path inside the store's domains. `import FILE` checks that every member is listed in the manifest with a matching hash and lies inside the bundle layout.

It then adds the bundle's notes in one journaled write, keeping their IDs, to the bundle's domain, or to the domain `--domain` names, which must be bound. A note already present unchanged is listed as `unchanged`. One present with other content is a `conflict` and is not written, so update it instead. Retired notes go to `archive/`.

Tags outside this store's taxonomy reject the import, and the message names the `taxonomy add` command to run first. Re-importing a bundle writes nothing. Neither command is auto-allowed, and subagents may run neither.

## Duplicates and batches

Title/body duplicate detection and optional idempotency keys prevent accidental repeated capture. A duplicate requested with a different type is rejected; update that note instead. Capture arrays and generic `batch` each accept at most 20 items. Batches serialize individual operations and return individual receipts; they are not an all-or-nothing multi-capture transaction.

Generic batch payload: `[{"argv":["search","cache"]},{"argv":["taxonomy"]}]`.

## Apps

These commands serve a desktop app or other human-facing tool.

### App server

- `serve --stdio` keeps one process open and reads one JSON request per line on stdin: `{"id":ANY,"argv":["COMMAND",...],"stdin":"optional text"}`. The command comes first and its options follow it. `--root` and `--cwd` default to the server's. `stdin` stands in for standard input, for commands that read a payload from it.
- Each request gets one reply line: `{"id","exit_code","result","error"}`. `result` is the command's usual JSON. `error` is `{code,message}` (plus `next` when there is a next step), or null when `exit_code` is 0. Exit codes match the CLI. A malformed request gets `exit_code` 2 and does not end the server; end of input does.
- Requests run one at a time, in-process, as `batch` runs them, so there is no Python start-up per call. `serve` and `setup` are not accepted as requests. Only the main actor may start the server. Each request may pass `--actor viewer` or `--actor main`.
- A loaded semantic model stays mapped for the server's lifetime. On Windows, stop the server before another process runs `semantic remove`.

### Note lists and search forms

- `search` with no query and no mode flag lists one page of notes (`mode: "list"`) for `--domain`, or for every domain folder with `--all-domains`, newest update first. Options: `--status active|resolved|archived|superseded|any` (default active), `--type` (repeatable), `--tag` (repeatable, all must match), `--with-summary`, `--offset`, and `--limit` (default 200, at most 1000).
- Each list row has `ref`, `id`, `domain`, `title`, `type`, `status`, `tags`, `created`, `updated`, `revision` (pass it to `update --expected-revision`) and `path`, relative to the store.

  With `--with-summary` each row also has `summary` (the summary as plain text, at most 300 characters), `files` (the note's files list, or `[]`), `pinned` and `rule` (true or false) and, for plan, bug and risk notes only, `actions: {open, done}` (checkbox counts in Action items).

- `search` chooses its form from its flags. A query (or `--file`) searches notes; `--sources`, `--pages`, `--transcript` and `--links` each choose another search and exclude one another; no query and no mode flag lists notes. A flag that belongs to another form is refused (exit 2) with the right form named.

  `--pages` returns `{ok, mode: "pages", query, domains, pages}` for the primary domains, or every domain with `--all-domains`. `--transcript` needs a query and `--session`, and returns what `recall` did plus `mode: "transcript"`. `--limit` defaults to 5 (notes, sources, transcript), 10 (pages) or 200 (a list) and is capped at 100 (a file list), 20 (sources, transcript), 50 (pages) or 1000 (a list).

### Domains, upgrade and version

- `domains` lists bound domains and any domain folder on disk. Each has `bound`, `prefixes`, `active` and `archived` note counts. The result also includes `aliases`, `global_domains` and `foreign_domains`.
- `upgrade` updates the installed copy from the git checkout it was installed from (`install.source` in `version`). It refuses unless that checkout has no uncommitted changes and is on the branch `origin/HEAD` names (else `main`), runs `git pull --ff-only` there, then runs the checkout's `install.py` with the Python recorded in the install (one journaled write in the install folder).

  It returns `{ok, status, source, branch, from, to, commits, files, next_steps, wiring_current, operation_id, undo, warnings?, notice?, restart}`: `status` is `upgraded` or `current` (the checkout is at the installed commit, so nothing is installed). `undo i-...` puts the previous package files back.

  `next_steps` are the `setup.py HARNESS --hooks` commands for harnesses whose wiring the new copy changes. It needs no store, is not auto-allowed for agents (it reaches the network), and subagents, viewers and Hivemind terminals are refused.

- `version` returns `api` (bumped when the app surface changes incompatibly; 2 adds `open`/`review` `--all-domains` and `--tag`, `list --with-summary`, `review` rows with `revision` and `domain`, and project auto-attribution; 3 folds `list`, `links`, `recall` and `doc search` into `search` and renames `plan delete` to `delete-plan` (since removed: `delete`); 4 removes hosted notebooks: the `doc` commands other than `read --file`, `propose --file`, `undo` and `folder`, `domain`, and `--include-private` on `search`, `ask` and `doctor`), `release` (the `CORE_RELEASE` constant), `install` (`release`, `commit`, `dirty` and `source` from the installed copy's ownership record, or null outside an installed copy), `python`, `commands` and `capabilities` (`items.fields` is 1 when action items carry due, owner and priority fields; `systems.project` is 1 when a system takes a project; `docs` is `{version, max_page_bytes, folder_policy, accept_edited, folder_proposals}`, where `folder_proposals` is 1 when `proposals` covers folder notebook pages; `views` is `{version: 2, kinds: ['note', 'query']}`).

  `doctor` returns the same `release` and `install`, and warns when the source checkout's HEAD is newer than the installed commit.

### Link graph

- `search --links` returns the link graph (`mode: "links"`) of `--domain`, or of every domain folder with `--all-domains`. By default it covers active notes only; `--status any` adds archived, resolved and superseded notes.
  - A `[[Title]]` link resolves the way a title REF does: case-insensitive, first in the linking note's domain, then its parent domains, then the global domains. Text after `|` or `#` is ignored. An active note wins a title over a retired one.
  - The result has `nodes` (`ref`, `id`, `domain`, `title`, `type`, `status`, `tags`; it includes linked notes from parent or global domains) and `edges` (`from`, `to`, `kind` `link` or `supersedes`, newer note to the note it replaced). It also has `unresolved` (`from`, `target`) for links that match no note.
  - A note has at most one edge to another note, and a link wins over supersedes. Self-links are skipped.

### Actors and read-only commands

- `--actor viewer` is read-only. It allows every subagent command plus `taxonomy` (listing only; `taxonomy add` is refused), `review` and `diff`. Its `read` writes no usage-diary event, so browsing does not change `stats` or note strength.
- `search` in every form, `domains` and `version` are read-only, and subagents may run them.
- `propose` is the one command that writes for a subagent: a draft in `.turns/`, never a note. `proposals list` and `proposals show` are read-only for every actor; `proposals accept`, `proposals reject` and `capture --from-proposal` are main-agent only, and settling a page edit (12 hex digit ID) is refused in a Hivemind agent terminal (`HIVEMIND_SESSION`) and keeps the harness's own prompt.

## Search

Search defaults to active, parent, child and configured global domains (a hit's `sourceScope` is `active`, `parent`, `child`, `global` or `foreign`), then explicitly configured foreign domains only after zero scoped hits. `--scope all` is explicit and includes the global domains, also with `--file`. With `--file`, a child domain is asked for the path relative to its own folder.

As for prompts, relevance is judged within each domain before the merge, and global notes rank like the active domain's; `relevant` is false when nothing passed that check, and the closest matches are returned with a `hint`. Filters use `--filter tag=concern/decision`, prefix/status/type. Stale unfiltered retrieval falls back to canonical BM25; stale filtered retrieval requires `--rebuild`.

`score` ranks results; `relevance` is the share of informative query terms found in the note's title or body.

With a semantic model installed, `engine` is `fts5+semantic` and each hit has `similarity`: the cosine between the query and the note's best unit (its title plus summary, its caveat, or one section; see the unit index below), or between the query and its title plus summary when the unit index is missing or stale.

Every hit has `backend` (`native`, `python`, `notes` or `lexical`) and, when the first choice was not available, `backendReason`; `evidence` lists up to two matched sections as `{heading, role, lines, similarity}` (lines are file lines; `read REF --section HEADING` returns that section's text, as `{section:{heading, role, lines}, body}`).

`--explain` adds `explain:{model, native, native_enabled, semantic:{domain: {backend, reason, vectors, semantic_candidates, lexical_candidates, ms}}, candidates:[{ref, rank, engine, engine_score, relevance, similarity, gate}], prompt_hook:{ref: {level, section}}}`: why each note was found, the gate's verdict, and what the prompt hook would deliver for this text (no note text).

The prompt hook additionally offers resolved bug and risk notes (`[resolved] `) and, when this project answers weakly, one strong portable note (knowledge, bug or runbook with no `project/` tag) from another bound project (`[from DOMAIN] `, at most `SIBLING_DOMAINS` = 4 siblings, read from their indexes only); `"cross_project": false` at the top level of `bindings.json` turns the sibling lookup off (like `"auto_resolve": false` for auto-resolve).

The `search` command is unchanged.

## Semantic search

`semantic install` downloads the pinned model (potion-retrieval-32M, about 130 MB) into `<store>/.models/` from Hugging Face. The first load of a model writes `tokenizer.marshal` beside tokenizer.json (a parsed-tokenizer cache, derived, safe to delete). `--from DIR` copies the files from a folder instead, for an offline install.

Every file's SHA-256 must match the pinned value, and nothing replaces the old model until all of them do. It then rebuilds every domain's index with a vector per note. Search fuses the lexical and semantic rankings with reciprocal rank fusion (k = 60).

A prompt injects a note found by meaning alone only when its similarity reaches the model's `min_similarity` (0.45), and one-word prompts still need a title match. `semantic remove` deletes the model and rebuilds the indexes lexical-only.

`semantic status` reports `enabled`, `model`, `dim` (read from the model file), `size_bytes`, each index as `vectors` or `lexical`, `backend` (what a prompt would use now: `native`, `python` or `lexical`), `native:{installed, loadable, version or reason, enabled}`, `units:{domain: {units, notes, vectors, generation, mode, native_table fresh|stale|absent, size_bytes, updated}}`, and `error` when the model cannot be read; search then stays lexical, and `doctor` warns.

The model is read with the standard library: no pip package, and no network after install. Section-level units (`domains/D/vectors.sqlite3`) are kept by every index rebuild; `semantic rebuild` discards and rebuilds them for every domain.

Native vector search is optional: `pip install sqlite-vec==0.1.9` (any 0.1.x from 0.1.6) into the Python the hooks run, then `semantic rebuild`; nothing is downloaded by Living Memory itself, and without the package everything works with the same results in Python.

`semantic native off` (stored in `<store>/.models/native.json`; `LIVING_MEMORY_NATIVE=0` for one process) keeps the extension unloaded; `semantic native on` allows it again; `semantic native` alone reports. None of these is auto-allowed. Neither install nor remove is auto-allowed, and subagents may run none of the three.

SQL is read-only, one statement, at most 100 rows, five-second execution budget.

## Journaled changes and undo

There are no plans (the user removed them on 2026-11-26). Every change is one direct, journaled step that records each file's before-bytes, with the same revision and guard checks.

The bulk writes (delete, merge, retag, migrate, restore-backup, import, ingest, overlay-teardown) are left out of the harness auto-allow list, so the harness asks the user before each; subagents, viewers and Hivemind terminals are refused. `undo OPERATION` reverses any operation and rejects later conflicting edits (`Undo conflicts with later edits`).

Overlay teardown journals in the vault: undo it with `--root VAULT`. The package install journals in the install folder (the parent of `app/`, operation IDs `i-...`), adapter setup in each harness home (`s-HARNESS-...`) and the legacy-copy retirement in CODEX_HOME; `undo` finds `i-` and `s-` operations there by itself.

## Private spans

With `--session`, capture and update refuse text that repeats a `<private>` span from the session's prompts, with a `private_content` conflict (see PROTOCOL.md).

## Errors

Errors print `{"ok":false,"error":{"code","message","next"}}` on stderr (at a terminal without `--json`: one `alm: error (CODE): MESSAGE` line and, when there is one, a `next: ...` line).

`next` is a plain next step and appears only where there is one: binding or `--domain` for `unbound_project`, `COMMAND --help` for `bad_arguments` (an unknown flag or value, also reported as JSON), `history` for `not_found: no operation`, `search` for an unknown note, and a retry for `busy`. Error exit codes: invalid=2, busy=2 (the store lock timed out; retry), conflict=5, stale=6.

A process that has `HIVEMIND_SESSION` set (every program Hivemind starts in an agent terminal) is refused `undo`, `recover`, `upgrade`, `setup`, the direct writes and the bulk writes (`delete`, `merge`, `retag`, `migrate`, `restore-backup`, `import`, `ingest`, `overlay-teardown`), also through `batch` and `serve`, with code `hivemind` and exit 2; the pre-tool-use hook refuses the same commands earlier.

The code comes from the message's own wording, the text before any quoted value or `: detail`. A note title, tag or path quoted in a message never changes it. Native host event wrappers fail with their adapter's bounded output contract.

## Evaluation scripts

`scripts/eval_related.py --root STORE [--copy [--keep]] [--pair NEW OLD]... [--labels FILE] [--sample 20] [--days 30] [--json]` measures how often the capture-time related check would fire (it changes nothing in `capture`).

It runs `notes._related_hits` for every active decision, policy and preference note as if it were captured now, widened to its parent, child and global domains (`notes.related_domains`), skipping the note and its `similar` titles.

Each hit carries `rank`, `gate`, `coverage`, `similarity`, `pair_score`, `strong` and `owed_bar` (rank 1, `search.strong`, and a second signal: `pair_score` >= 0.5 or similarity >= threshold + `STRONG_MARGIN`; body coverage alone never counts).

The report gives, for the `listing` bar (top 3 hits) and the `owed` bar: `notes_scanned`, `notes_firing`, `fire_rate`, `unique_pairs`, `same_domain_pairs`, `cross_domain_pairs`, the same counts for notes created in the last `--days` (`recent`), and a `sample` of owed pairs then listing-only pairs, each with a `key` "NEWREF|OLDREF" to label in a `--labels` JSON file ({key: true|false}), which adds `labelled`, `unlabelled` and `precision` per bar.

`--pair NEW OLD` (refs, ID prefixes or exact titles; needs `--copy`) reports `found_listing`, `found_owed` and the signals; a retired OLD is made active again in the copy only. `--copy` runs in a temporary copy of the store (like `eval_retrieval.py --copy`), so the store is never written.

## Flags by command

Generated from the argparse parser in `core/cli.py` by `python scripts/gen_script_reference.py`; `tests/test_script_reference.py` fails when this table is out of date. The options every command accepts (`--root`, `--domain`, `--cwd`, `--json`, `--actor`) are left out of the rows. A command whose first argument is an action shows the actions after its name; its flags apply to the actions that use them. Flags hidden from `--help` (the legacy `--tags` and `--prefix` on `capture` and `propose`, and `--scheduled`) are not listed.

<!-- flags:begin -->
| Command | Flags of its own |
| --- | --- |
| `archive` | `--expected-revision`, `--payload-file`, `--session`, `--turn` |
| `ask` | `--limit` |
| `backup` | `--keep` |
| `batch` | `--payload-file` |
| `bind` | none |
| `capture` | `--action`, `--allow-similar`, `--body-file`, `--command`, `--concern`, `--content-file`, `--context`, `--detail`, `--done`, `--drop-links`, `--file`, `--files`, `--from-proposal`, `--global`, `--idempotency-key`, `--important`, `--link`, `--payload-file`, `--pin`, `--platform`, `--project`, `--redact`, `--review-by`, `--rule`, `--section`, `--session`, `--source`, `--subject`, `--summary`, `--supersedes`, `--system`, `--tag`, `--technology`, `--title`, `--turn`, `--type` |
| `capture-finalize` | `--note`, `--session`, `--turn` |
| `check` | `--expected-revision`, `--index`, `--session`, `--turn`, `--undo` |
| `config` | none |
| `config get` | none |
| `config set` | none |
| `confirm` | `--expected-revision`, `--session`, `--turn` |
| `delete` | `--expected-revision` |
| `diff` | `--include-private` |
| `doc` | none |
| `doc folder` | none |
| `doc folder set` | `--no-private`, `--private`, `--visibility` |
| `doc folder unset` | `--forget` |
| `doc propose` | `--base-revision`, `--body-file`, `--file`, `--include-private`, `--note`, `--session` |
| `doc read` | `--file`, `--include-private`, `--max-chars` |
| `doc undo` | `--include-private` |
| `doctor` | none |
| `domains` | none |
| `export` | `--all`, `--out`, `--tag`, `--type` |
| `help` | none |
| `history` | `--include-private`, `--limit`, `--markdown`, `--note` |
| `housekeep` | none |
| `import` | none |
| `ingest` | `--allow-similar`, `--redact`, `--source` |
| `ingest-scan` | `--changed`, `--limit`, `--source` |
| `ingest-skip` | `--payload-file` |
| `log` | `--event`, `--limit`, `--note`, `--project`, `--since`, `--type`, `--until` |
| `logs` | `--days`, `--event`, `--kind`, `--level`, `--limit` |
| `maintenance` | none |
| `maintenance disable` | none |
| `maintenance enable` | `--all-domains`, `--daily-budget-usd`, `--max-notes-per-day`, `--model`, `--timeout-seconds` |
| `maintenance history` | `--limit`, `--run` |
| `maintenance preview` | `--limit` |
| `maintenance run` | `--dry-run` |
| `maintenance status` | none |
| `merge` | `--payload-file` |
| `migrate` | `--baseline`, `--note-types`, `--schema` |
| `off` | `--session` |
| `on` | `--session` |
| `open` | `--all-domains`, `--due-by`, `--limit`, `--owner`, `--pages`, `--sort`, `--tag` |
| `overlay-build` | none |
| `overlay-sync-all` | `--primary` |
| `overlay-teardown` | none |
| `project create/update/archive` | `--payload-file` |
| `projects` | `--status` |
| `promote` | `--only`, `--suggest` |
| `proposals` | none |
| `proposals accept` | `--body-file`, `--edited`, `--expected-revision`, `--include-private`, `--pin`, `--rule`, `--session`, `--turn` |
| `proposals list` | `--include-private`, `--kind`, `--notebook`, `--session`, `--status` |
| `proposals reject` | `--include-private`, `--reason`, `--session` |
| `proposals show` | `--include-private` |
| `propose` | `--action`, `--allow-similar`, `--body-file`, `--command`, `--concern`, `--content-file`, `--context`, `--detail`, `--done`, `--drop-links`, `--file`, `--files`, `--global`, `--idempotency-key`, `--important`, `--link`, `--payload-file`, `--pin`, `--platform`, `--project`, `--redact`, `--review-by`, `--rule`, `--section`, `--session`, `--source`, `--subject`, `--summary`, `--supersedes`, `--system`, `--tag`, `--technology`, `--title`, `--turn`, `--type` |
| `query` | none |
| `read` | `--max-chars`, `--metadata-only`, `--section` |
| `rebaseline` | `--dry-run` |
| `rebuild` | none |
| `recap` | `--all-domains`, `--days`, `--project` |
| `recover` | `--abandon`, `--direction`, `--include-private` |
| `remap` | `--payload-file` |
| `reopen` | `--expected-revision`, `--session`, `--turn` |
| `resolve` | `--expected-revision`, `--session`, `--turn` |
| `restore` | `--expected-revision`, `--session`, `--turn` |
| `restore-backup` | none |
| `retag` | `--payload-file` |
| `review` | `--all-domains`, `--kind`, `--limit`, `--tag` |
| `rewrite` | `--payload-file` |
| `search` | `--all`, `--all-domains`, `--explain`, `--file`, `--filter`, `--limit`, `--links`, `--offset`, `--pages`, `--project`, `--rebuild`, `--scope`, `--session`, `--sources`, `--status`, `--tag`, `--transcript`, `--type`, `--with-summary` |
| `semantic` | none |
| `semantic install` | `--from`, `--model` |
| `semantic native` | none |
| `semantic rebuild` | none |
| `semantic remove` | none |
| `semantic status` | none |
| `serve` | `--stdio` |
| `setup` | `--location`, `--non-interactive`, `--source` |
| `source add/remove/implicit` | `--payload-file` |
| `source-sync` | `--all-remote`, `--rebuild` |
| `sources` | none |
| `stats` | `--days`, `--limit` |
| `status` | `--session` |
| `supersede` | `--by`, `--drop-links`, `--expected-revision`, `--session`, `--turn` |
| `sync` | none |
| `system create/update/retire` | `--payload-file` |
| `systems` | `--status` |
| `taxonomy` | none |
| `taxonomy add` | none |
| `taxonomy show` | none |
| `timeline` | `--after`, `--all`, `--before`, `--by`, `--date`, `--type` |
| `type create/update/retire` | `--payload-file` |
| `types` | `--all` |
| `unbind` | `--prefix` |
| `undo` | `--include-private` |
| `update` | `--action`, `--add-action`, `--add-detail`, `--add-link`, `--add-tag`, `--body`, `--body-file`, `--command`, `--concern`, `--content-file`, `--context`, `--detail`, `--distinct`, `--done`, `--drop-section`, `--due`, `--expected-revision`, `--file`, `--files`, `--important`, `--item`, `--item-index`, `--link`, `--no-rule`, `--not-distinct`, `--owner`, `--payload-file`, `--pin`, `--platform`, `--priority`, `--project`, `--redact`, `--remove-tag`, `--replace-actions`, `--review-by`, `--rule`, `--section`, `--session`, `--set-detail`, `--source`, `--subject`, `--summary`, `--system`, `--tag`, `--technology`, `--title`, `--turn`, `--type`, `--unpin`, `--why` |
| `upgrade` | `--rewire` |
| `validate` | none |
| `version` | none |
| `views` | none |
| `views add` | `--spec-file`, `--title` |
| `views list` | `--all-domains` |
| `views published` | `--url` |
| `views remove` | none |
| `views render` | `--all`, `--format`, `--out`, `--target` |
| `views update` | `--spec-file`, `--title` |
| `wiki` | `--agent`, `--archived`, `--budget`, `--build`, `--dry-run`, `--format`, `--model`, `--out`, `--progress`, `--project`, `--restructure`, `--theme` |
<!-- flags:end -->
