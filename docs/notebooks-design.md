# Notebooks: design for Notes Studio (NS-2A, NS-2B)

Hivemind's Notes Studio (roadmap NOTES-1) lets the user keep notebooks either as folders on disk or inside Living Memory. This document is the Living Memory side: a new record kind for notebook pages, notebook-only domains, an attachment store, and agent reach for both notebook kinds. The Hivemind side, and every decision behind this design, is in `agentic-hivemind-app/docs/notes-studio/README.md`. Decisions made by the user on 2026-11-20 are not reopened here.

Two phases: **NS-2A** (records, commands, journal, backup, permissions) and **NS-2B** (search, agent reach, injection, Obsidian sync). Each ships straight to main after the full offline suite, `scripts/simulate.py` and a Codex review; installing still needs the user's approval of the `install.py --plan` digest.

## 1. Pages are not notes

A notebook page is free-form Markdown the user writes. Notes are structured memory with a required summary, a reformatted body, review dates and staleness, maintenance, SessionStart and wiki presence. None of that fits a page, and custom note types cannot opt out of it (every exclusion is a hard-coded tuple of built-in type names; see `core/maintenance.py` `EXCLUDED`, `core/session_start.py` `PRIORITY`, `core/search.py` `RESOLVED_TYPES`). So pages are a separate record kind:

- stored byte for byte (no `format_body`, no summary rule, no frontmatter requirement, no style warnings, no similar-title refusal, no secret refusal beyond a warning);
- never listed by `list`, `review`, `open`, worklists, maintenance candidates, auto-resolve, SessionStart, `recap` or the wiki;
- reached by agents only through `doc` commands and, as each notebook's setting allows, through `search`, `ask` and prompt injection.

## 2. Layout

```text
domains/D/notebooks/<nb>/
  notebook.json            title, order, icons, pinned, visibility, private, created, updated (canonical, journaled)
  <page>.md                a page; any UTF-8 text, kept byte for byte
  <section>/<page>.md      sections are folders
  <page>/<subpage>.md      a folder named like a page holds its subpages
  assets/<sha8>-<name>     attachments, content-addressed prefix
  .proposals/<id>.json     staged agent edits (doc propose)
domains/D/docs.sqlite3     derived page index (NS-2B), not canonical, not backed up
```

- `<nb>` is a slug (`[a-z0-9][a-z0-9-]{1,39}`), unique per domain. `notebook.json` holds `{ "version": 1, "title", "order", "icons", "pinned", "visibility": "hidden" | "searchable" | "injectable", "private": false, "created", "updated" }`; unknown keys are kept.
- Names: each path part 1 to 120 characters; no `/ \ : * ? " < > |`, no control characters, no Windows device names, no leading or trailing space, no trailing dot; unique ignoring case within a folder; depth at most 8; `notebook.json`, `assets` and `.proposals` are reserved at the notebook root. Pages end in `.md`.
- A page's **revision** is the sha256 of its bytes (the same rule as a note's file revision).
- Confinement: `core/docs.py` validates the domain (`canonical_domain`) and the notebook slug before it builds any path, and resolves every page, folder and asset path inside `domains/D/notebooks/<nb>/`; any part that is a symbolic link or junction is refused, so an alias cannot reach another notebook or anything else in the store.
- Limits: page 4 MiB (`max_page_bytes`); attachment 25 MiB (`max_asset_bytes`); 20,000 pages and 2 GiB of attachments per notebook (doctor warns at 80%).

## 3. Commands (NS-2A)

All output JSON with `--json`. Who may run what is in section 6. Stdin payloads are capped at 1 MiB by `cli._payload` and a `serve` frame at 2 MiB, so Hivemind sends any body over 512 KiB as `--body-file` (a staging file in its app data) and never builds a frame over the cap.

| Command | What it does |
| --- | --- |
| `doc notebooks [--domain D \| --all-domains]` | Notebooks with title, domain, visibility, private, page and asset counts, proposals pending |
| `doc notebook create D NB --title T [--visibility V] [--private]` | Creates `notebook.json`; refuses an existing slug |
| `doc notebook update D NB [--title] [--visibility] [--private\|--no-private] [--order-file F] [--icons-file F] [--pinned-file F] [--settings-file F]` | Changes settings, with `--expected-revision` on `notebook.json`; `--settings-file` merges Hivemind's notebook settings (daily folder and format, templates, inbox) into a `settings` object |
| `doc notebook delete D NB` | Refused while pages exist unless `--with-pages`, which goes through a plan (`plan` and `apply`), like note deletion |
| `doc list D NB [--section S]` | Pages and folders with size, revision, updated |
| `doc read D NB PATH [--max-chars N]` | `{ path, text, revision, size, updated }`; `truncated` past the cap |
| `doc create D NB PATH [--body-file F]` | New page; refuses an existing path |
| `doc write D NB PATH --expected-revision R [--body-file F]` | Replaces the page; `stale` (exit 6) on a mismatch |
| `doc move D NB FROM TO [--rewrite-links]` | Renames or moves a page or folder; with `--rewrite-links`, `[[...]]` links to it in the same notebook are rewritten in the same transaction (at most 200 pages) |
| `doc delete D NB PATH --expected-revision R` | Deletes a page (journaled, undoable) |
| `doc attach D NB --from-file PATH [--name N]` | Copies a file into `assets/` (Living Memory reads the file itself, so the bridge never carries the bytes); returns the relative link |
| `doc history D NB PATH [--limit N]` | Journal entries touching the page, newest first, with operation id, kind, time, before and after revisions; `--version OP` returns that version's text |
| `doc undo OPERATION` | Through the existing `undo` (the `doc_` prefix joins `DIRECT_UNDO_PREFIXES` in `core/governance.py`) |
| `doc propose D NB PATH --base-revision R [--body-file F] [--note TEXT]` | Stages an edit (agents may run it) |
| `doc proposals [D [NB]]`, `doc accept ID [--expected-revision R]`, `doc reject ID [--reason TEXT]` | Lists, applies or drops staged edits; accept and reject are main-actor only |
| `doc search QUERY [--domain D \| --all-domains] [--notebook NB] [--include-private] [--rebuild]` | NS-2B; hidden notebooks are left out unless `--notebook` names one and `--include-private`, which also admits private ones, is given |
| `doc tasks [--domain D \| --all-domains] [--notebook NB] [--open]` | NS-2B; task lines (`- [ ]`, `- [x]`) with page, line, text and due date, from the index |
| `doc query --payload-file F` | NS-2B; pages filtered and sorted by frontmatter properties (`{ "where": [...], "sort": [...], "limit": N }`), from the index |
| `doc folder set PATH --visibility V [--private\|--no-private] [--domain D]`, `doc folder unset PATH` | NS-2B; the folder notebook policy registry (section 8.2) |

Every write is one `repository.transaction` with a kind starting `doc_` (`doc_create`, `doc_write`, `doc_move`, `doc_delete`, `doc_attach`, `doc_notebook`, `doc_propose`, `doc_accept`, `doc_reject`, `doc_folder`). Each `doc_` journal records the domain and notebook id, so later redaction (section 6) still knows which notebook an operation touched after the notebook is renamed or deleted.

## 4. Journal and storage changes

- `core/repository.py` `_write` decodes every payload as UTF-8, which breaks attachments, and recovery rebuilds every effect from the journal's before and after images. Attachments therefore get a binary effect of their own, versioned in the journal (`effect: "blob"`): the bytes are first copied to `.operations/<op>/blobs/<sha256>` and fsynced, then the intent is journaled with the blob's hash and the asset path, then the blob is renamed into `assets/`. Recovery finishes or rolls back from the blob (a missing or mismatched blob means the intent never became recoverable and is dropped). Undo of `doc_attach` moves the asset back into the operation's `blobs/` folder, so redo works. Assets are immutable and content-addressed (`<sha8>-<name>`); a second attach of the same bytes reuses the asset and records a reference, and deleting a page never deletes an asset another page links. `governance.py` reads `blob` effects without decoding them.
- Journals keep base64 before and after images and `prune_journals` never prunes edit kinds. Pages would grow `.operations/` without bound, so `doc_` journals get their own retention: per page, the newest 100 operations or 90 days, whichever keeps more; pruning a `doc_attach` journal deletes its `blobs/` folder only when the asset is no longer in the notebook. Pruning runs where `prune_journals` runs today.
- Undo of a page write restores the before bytes with the after revision as precondition, as for notes.

## 5. Notebook-only domains

- `core/init_domain.py` gains `init_notebook_domain(name)`: writes `bindings.json` domain entry `{ "prefixes": [], "notebook_only": true }` and creates only `domains/NAME/notebooks/`. Command: `domain create NAME --notebook-only` (main actor).
- Verified interactions: `resolve_domain` never resolves a domain without prefixes; every `--all-domains` path that filters on `domains/D/notes` being a folder skips it (the `cli.py` listing and review paths, `maintenance enable --all-domains`, `semantic.rebuild_all`).
- Needed changes: `capture`, `propose` and `update --domain` refuse with "D holds notebooks only"; `unbind` refuses while pages exist; `bind NAME FOLDER` on a notebook-only domain clears `notebook_only` and creates `notes/` and `archive/` inside one journaled transaction (today `init_domain` creates the folders after its transaction; the conversion must be restartable, so `doctor` finishes a half-converted domain); `domains` reports `kind: "notebook"` (and `kind: "memory"` for others) with page counts.
- Validation and relocation are kind-aware: `validate.py` and the relocation check (`storage.py`) accept a domain with `notebook_only` and no `notes/`, and reject one without the flag as today.
- Obsidian mirroring (section 8.4) of a notebook-only domain starts the same way as for any domain, with `sync DOMAIN VAULT`, which records the vault in its `sync-state.json`; Hivemind offers the domain's vault choice in notebook settings (NS-2C).

## 6. Permissions

Trust model: `--actor` is self-declared (it defaults from `LIVING_MEMORY_ACTOR`), so it is not a security boundary. Living Memory tells agents apart in two ways only: its PreToolUse hook, which classifies every command an agent runs, and `HIVEMIND_SESSION`, which agents inside Hivemind terminals inherit and Hivemind's own bridge does not. An agent can still read files it can reach with its own tools; Private stops Living Memory from offering the content, it does not protect files.

- **Reads** (`notebooks`, `list`, `read`, `history`, `proposals`, `search`, `tasks`, `query`) are allowed for every actor, and `doc search --rebuild` too (it writes only the derived page index, as `search --rebuild` does for notes).
- **Private notebooks** are left out of every read, search, task, query and proposal listing unless the command carries `--include-private`. Hivemind's bridge passes it. The hook denies any agent command that carries it, and `cli.py` refuses it when `HIVEMIND_SESSION` is set. Generic `history`, `diff`, `inspect-plan` and `proposals` redact the paths and content of `doc_` operations on private notebooks (identified by the notebook id each journal records) unless `--include-private` is given.
- **Hidden notebooks** are readable by an explicit `doc read` or `doc list` naming them, never returned by `search`, `ask`, `tasks`, `query` or injection.
- **Writes:** `propose` is allowed for main and agents (not viewers). Every other `doc` write, `doc folder set` and `unset`, and `domain create` are main-actor writes. A detector `doc_write(text, grammar)` modelled on `views_write` returns true for them; it feeds the mutating check (so they are never auto-allowed) and the Hivemind terminal rule. `HIVEMIND_GOVERNED` matches whole verbs, so the check is `subcommand == 'doc' and doc_write(...)` plus `domain create`, which keeps `doc propose` allowed inside Hivemind terminals.
- **Paths:** `protected_rule` already denies agent edits under `<store>/domains`. NS-2B adds the vault's `Notebooks` folder to the protected vault folders, and the hook runs the notebook path check before its early return for sessions with no bound domain (today `runtime.py` returns before the protected-path guard for unbound sessions). The hook also denies an agent's file-reading tools (Read, Grep, Glob) on `domains/*/notebooks/<nb>/` of a private notebook; a shell command can still read it, as stated in the trust model.
- `core/instructions.py`: the protocol gains two sentences: read pages with `doc read`, change them only with `doc propose`, which the user reviews.

## 7. Backup, bundles, relocation, doctor

- `core/backup.py`: the snapshot manifest goes to version 2 and lists what it covers (`notes`, `notebooks: [D/NB, ...]`) and what it left out (`omitted: [{ path, reason }]`, for attachments over the automatic-backup budget). `MEMBER` gains `domains/D/notebooks/<nb>/(notebook.json|**/*.md|assets/**|.proposals/*.json)`; `canonical_files` includes them; attachments are stored with `ZIP_STORED` and read back as bytes (restore no longer decodes every member as UTF-8). Restore never deletes a file the snapshot does not cover: a version 1 snapshot, or a notebook absent from a version 2 snapshot, leaves the live notebook untouched, and omitted attachments are kept. Restore plans, `inspect-plan` and `diff` handle binary members as hashes and sizes, never decoded text. Every note-count predicate (`_note_count`, `SHRINK_MIN` and the shrink checks) keeps counting notes only. Automatic backups skip attachments over an asset budget (default 500 MB per snapshot) and list them in `omitted`; manual `backup` includes everything.
- `core/bundle.py`: a separate `MAX_ASSET` (25 MiB) and a bundle total cap; import accepts notebook-only domains.
- Relocation (`core/storage.py`): copies notebooks and assets; leaves out `docs.sqlite3` and its `-wal` and `-shm` files like the other derived indexes, and the destination rebuilds them; validates notebook-only domains (section 5).
- `doctor`: unreadable `notebook.json`, pages over the limit, orphaned assets (no page links them; listed, never deleted automatically), notebooks near their caps, proposals older than 30 days, a half-converted notebook-only domain, folder policy entries whose folder is gone.

## 8. Search and agent reach (NS-2B)

### 8.1 One result shape for both notebook kinds

- `search` and `ask` gain a `pages` array after `results` and `notes`: `{ origin: "lm" | "folder", domain, notebook, title, path, heading, snippet, line_start, line_end, revision, score, label: "notebook page" }`. For several questions, each entry in `questions` gains its own `pages`. Folder notebook chunks appear only in `pages`, never in `sources`, and `search --sources` keeps returning non-notebook sources only. Existing fields keep their meaning, so current parsers are unaffected.
- Filtering, for every actor: private notebooks only with `--include-private`; hidden notebooks never; searchable and injectable ones always.
- Living Memory pages: `core/docs_index.py` keeps a derived `domains/D/docs.sqlite3` with `pages(path, notebook, revision, title, updated, size, mtime)`, an FTS5 table over chunks (title, heading path, text; `porter unicode61`), `tasks(page, line, done, text, due)`, `props(page, key, value)`, and vectors per chunk when a semantic model is installed. Chunking reuses the heading-aware chunker of `core/sources.py`. Freshness does not depend on any write path: every `doc search`, `doc tasks`, `doc query`, `search` and `ask` first runs a stat walk of the domain's notebooks (as `sources.py` does for sources) and reindexes changed pages within 0.5 s, marking the rest pending for the next call, so undo, restore and recovery need no hook of their own. `doc search --rebuild` rebuilds the file.
- Pages never enter `search.sqlite3`'s notes table, so `search_domains`, `relevance_gate`, vocabularies, SessionStart and maintenance are untouched.

### 8.2 Folder notebooks: the policy registry

Folder notebooks live outside the store; Living Memory reaches them through sources (`core/sources.py`). Their reach is decided by a policy registry, with the sidecar as a second opinion:

- `<store>/notebook-folders.json`: `{ "version": 1, "folders": [{ "path": "<canonical plain path>", "domain": "D", "visibility": "hidden" | "searchable" | "injectable", "private": bool, "updated": "<iso>" }] }`. Identity is the canonical path compared in lowercase. `doc folder set` adds or updates an entry (idempotent: an unchanged entry writes nothing) and `doc folder unset` removes it; both are journaled (`doc_folder`) and undoable. Hivemind calls `set` when a notebook is registered and whenever its reach changes, and `unset` when it is removed.
- The effective policy of a folder is the stricter of its registry entry and its sidecar `.hivemind/notebook.json` (private beats everything; hidden beats searchable beats injectable). A sidecar that exists but cannot be parsed counts as hidden. A notebook folder found by its sidecar but absent from the registry uses the sidecar alone; one in neither is an ordinary folder, as today.
- A registered folder outside every bound repository is indexed as a source of its domain (no `source add` needed); one inside a bound repository is indexed by the implicit repo source, with the policy applied to its subtree.
- Indexing: `walk` skips hidden and private notebook subtrees entirely; chunks gain `notebook` (the canonical folder path) and `inject` columns; `VERSION` goes up, so the next sync rebuilds.
- Retrieval: policy is checked again when results are served, on every path (lexical and semantic `search`, `ask`, injection, digests and ingest), against the current registry and sidecar (cached by the files' mtimes), so a notebook made hidden or private disappears at once even before a sync. A policy change also purges that notebook's rows and vectors at the next sync, tracked by a policy revision kept apart from content hashes.

### 8.3 Injection

- UserPromptSubmit adds up to two source excerpts (800 characters) when notes gave fewer than two answers. Notebook excerpts use that channel and inherit all of its rules: no injection for subagents or machine prompts, each excerpt at most once a session, and only when the block fits the 4,000-character prompt budget of `core/injection.py`.
- Only chunks of injectable, non-private notebooks are candidates; they are labelled `[notebook TITLE]`. Living Memory pages of injectable notebooks in the session's scope domains join the same channel, after notes. Searchable notebooks are never injected.
- SessionStart is unchanged, and a test asserts that no page or notebook excerpt ever appears in it.

### 8.4 Obsidian sync

`core/sync.py` mirrors each searchable or injectable, non-private notebook byte for byte to `<vault>/Notebooks/<domain>/<nb>/` with its assets (pages are never rewritten by `build_overlay`). Hidden and private notebooks are never mirrored, and a notebook that becomes hidden or private has its mirror removed at the next sync. Edits in the vault come back after the existing 30-second quiet period as `doc write --expected-revision <last synced revision>`; a conflict becomes a `doc propose` with the vault text instead of an overwrite. New `.md` files under a vault notebook become pages; deleted files become a pending deletion on the Sync Status page, like notes. Vault edits are trusted as the user's edits, the same rule as notes today; the vault's `Notebooks` folder joins the folders agents may not write. Every source walk excludes `<vault>/Notebooks` of each managed vault by canonical identity, including sources rooted inside the vault, so a mirrored page is never indexed twice or outside its policy.

## 9. Version capability

`version` capabilities gain `docs: { "version": 1, "max_page_bytes": 4194304, "max_asset_bytes": 26214400, "notebook_only_domains": true }` in NS-2A, and `"search": true, "folder_policy": true` in NS-2B. `tests/test_restore.py` asserts the whole capabilities dictionary and must be updated; SCRIPT_REFERENCE.md documents the object.

## 10. Tests

- NS-2A: `tests/test_docs.py` (byte-exact round trip incl. CRLF, BOM and no trailing newline; names, limits and links refused inside a notebook; stale writes; `--body-file` over 1 MiB; move with link rewrite; undo of every kind; attach from file, crash recovery of an attach at each step, undo and redo of an attach, a shared asset kept when one page is deleted; propose, accept with a stale base, reject), `tests/test_docs_backup.py` (backup, verify, restore of pages and assets; restoring a version 1 or a partial snapshot leaves live notebooks and omitted assets alone; binary members in plans and diffs; asset budget; bundle caps; relocation leaves out `docs.sqlite3`), `tests/test_docs_runtime.py` (the permission matrix per actor and inside a Hivemind terminal; `--include-private` refused under `HIVEMIND_SESSION` and denied by the hook; private notebooks redacted from `history`, `diff`, `inspect-plan` and `proposals`; hidden reads; the notebook path check for an unbound session), `tests/test_notebook_domains.py` (create, refusals, a crash during bind conversion finished by `doctor`, validation and relocation of a notebook-only domain, `domains` kind, every `--all-domains` path skips them), and pages never appearing in `list`, `review`, `open`, worklists, maintenance candidates, SessionStart, `recap` or `wiki`.
- NS-2B: `tests/test_docs_search.py` (visibility per actor, `pages` array in `search`, `ask` and per-question results for both origins, freshness after undo and restore without a sync, `doc tasks` and `doc query`), `tests/test_docs_inject.py` (searchable never injected, injectable labelled, private never; no injection for subagents and machine prompts), `tests/test_sources_notebooks.py` (registry and sidecar policy inside a bound repo and in a registered folder; the stricter one wins; an unparseable sidecar is hidden; a notebook made private disappears from lexical and semantic results before any sync and its rows go at the next; `inject` column; version bump rebuild), `tests/test_docs_sync.py` (vault mirror of searchable notebooks only, mirror removed when a notebook turns private, the vault `Notebooks` folder never indexed as a source, edit back, conflict to proposal). `scripts/eval_retrieval.py` results on notes are unchanged.
- Docs: a new `NOTEBOOKS.md` (user-facing), SCRIPT_REFERENCE.md rows for `doc` and `domain create`, ARCHITECTURE.md (the record kind and its index), README.md (one paragraph), OBSIDIAN.md (the `Notebooks` folder), PROTOCOL.md and the instruction block (`doc read`, `doc propose`).

## 11. Exit

- NS-2A: pages are byte-exact and invisible to every note path; agents can read and propose but not write; Hivemind terminals likewise; undo and backup restore work for pages and attachments; notebook-only domains behave as listed.
- NS-2B: a searchable notebook of either kind is found by agent `search` and `ask` and never injected; an injectable one is injected with a label; hidden and private ones are never found, including folder notebooks inside a bound repository and copies in the vault; a reach change takes effect at once; an Obsidian edit comes back as a page write or a proposal.

## 12. Shipped

(Filled in when each phase lands.)
