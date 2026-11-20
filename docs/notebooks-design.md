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
- Limits: page 4 MiB (`max_page_bytes`); attachment 25 MiB (`max_asset_bytes`); 20,000 pages and 2 GiB of attachments per notebook (doctor warns at 80%).

## 3. Commands (NS-2A)

All output JSON with `--json`. Reads are allowed for every actor; writes are main-actor only (section 6). Stdin payloads are capped at 1 MiB by `cli._payload`, so page bodies above that use `--body-file`.

| Command | What it does |
| --- | --- |
| `doc notebooks [--domain D \| --all-domains]` | Notebooks with title, domain, visibility, private, page and asset counts, proposals pending |
| `doc notebook create D NB --title T [--visibility V] [--private]` | Creates `notebook.json`; refuses an existing slug |
| `doc notebook update D NB [--title] [--visibility] [--private\|--no-private] [--order-file F] [--icons-file F] [--pinned-file F]` | Changes settings, with `--expected-revision` on `notebook.json` |
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
| `doc search QUERY [--domain D \| --all-domains] [--notebook NB]` | NS-2B; viewer results ignore visibility, agent results honour it |
| `doc folder link PATH --domain D`, `doc folder unlink PATH` | NS-2B; registers a folder notebook outside bound repositories as a source |

Every write is one `repository.transaction` with a kind starting `doc_` (`doc_create`, `doc_write`, `doc_move`, `doc_delete`, `doc_attach`, `doc_notebook`, `doc_propose`, `doc_accept`, `doc_reject`).

## 4. Journal and storage changes

- `core/repository.py` `_write` decodes every payload as UTF-8, which breaks attachments. Add a bytes path: content passed as `bytes` is written with an fsync-then-replace bytes writer; `str` keeps today's path.
- Journals keep base64 before and after images and `prune_journals` never prunes edit kinds. Pages and attachments would grow `.operations/` without bound, so `doc_` journals get their own retention: per page, the newest 100 operations or 90 days, whichever keeps more; `doc_attach` journals keep only the after hash plus the asset path (the asset file itself is the content; undo deletes it), so a 25 MB attachment does not become a 33 MB journal. Pruning runs where `prune_journals` runs today.
- Undo of a page write restores the before bytes with the after revision as precondition, as for notes.

## 5. Notebook-only domains

- `core/init_domain.py` gains `init_notebook_domain(name)`: writes `bindings.json` domain entry `{ "prefixes": [], "notebook_only": true }` and creates only `domains/NAME/notebooks/`. Command: `domain create NAME --notebook-only` (main actor).
- Verified interactions: `resolve_domain` never resolves a domain without prefixes; every `--all-domains` path that filters on `domains/D/notes` being a folder skips it (the `cli.py` listing and review paths, `maintenance enable --all-domains`, `semantic.rebuild_all`).
- Needed changes: `capture`, `propose` and `update --domain` refuse with "D holds notebooks only"; `unbind` refuses while pages exist; `bind NAME FOLDER` on a notebook-only domain clears `notebook_only` and creates `notes/` and `archive/`; `domains` reports `kind: "notebook"` (and `kind: "memory"` for others) with page counts.

## 6. Permissions

- Actors as today: main, viewer, subagent. `doc` read actions (`notebooks`, `list`, `read`, `history`, `proposals`, `search`) are allowed for all; `propose` for main and agents (not viewers); every other write for main only.
- `core/runtime.py`: a detector `doc_write(text, grammar)` modelled on `views_write` returns true for every `doc` action except the reads and `propose`. It feeds the mutating check (so writes are never auto-allowed) and the Hivemind terminal rule: `HIVEMIND_GOVERNED` matches whole verbs, so the check is `subcommand == 'doc' and doc_write(...)`, which keeps `doc propose` allowed inside Hivemind terminals.
- `protected_rule` already denies agent edits under `<store>/domains`; NS-2B adds the vault's `Notebooks` folder to the protected vault folders.
- Private notebooks: agent-actor `doc read`, `doc list` and `doc search` refuse them with "private notebook"; viewer and main see them (Hivemind). Hidden notebooks are readable by explicit `doc read` (an agent that knows the path) but never returned by `search`, `ask` or injection.
- `core/instructions.py`: the protocol gains two sentences: read pages with `doc read`, change them only with `doc propose`, which the user reviews.

## 7. Backup, bundles, relocation, doctor

- `core/backup.py`: `MEMBER` gains `domains/D/notebooks/<nb>/(notebook.json|**/*.md|assets/**|.proposals/*.json)`; `canonical_files` includes them; `verify` checks pages decode as UTF-8 and `notebook.json` parses; attachments are stored with `ZIP_STORED`; `_note_count` and `SHRINK_MIN` keep counting notes only. Automatic backups skip attachments over an asset budget (default 500 MB per snapshot) and say so in `doctor`; manual `backup` includes everything.
- `core/bundle.py`: a separate `MAX_ASSET` (25 MiB) and a bundle total cap; import accepts notebook-only domains.
- Relocation (`core/storage.py`) already copies every file; tests only.
- `doctor`: unreadable `notebook.json`, pages over the limit, orphaned assets (no page links them; listed, never deleted automatically), notebooks near their caps, proposals older than 30 days.

## 8. Search and agent reach (NS-2B)

### 8.1 Living Memory pages

- `core/docs_index.py`: a derived `domains/D/docs.sqlite3` with `pages(path, notebook, revision, title, updated)`, an FTS5 table over chunks (title, heading path, text; `porter unicode61`), and vectors per chunk when a semantic model is installed. Chunking reuses the heading-aware chunker of `core/sources.py`. Updated inside each `doc_` write (incrementally, after the transaction commits) and rebuilt by `doc search --rebuild`; never rebuilt wholesale inside `reconcile`, because the Hivemind bridge is one serialised request with no timeout.
- Pages never enter `search.sqlite3`'s notes table, so `search_domains`, `relevance_gate`, vocabularies, injection and SessionStart are untouched.
- `search` and `ask` gain a `pages` array (`{ domain, notebook, path, title, heading, snippet, revision, score, label: "notebook page" }`) after `results` and `notes`; existing fields are unchanged. For agent actors, only `searchable` and `injectable`, non-private notebooks are included; for viewers, every notebook.

### 8.2 Folder notebooks

Folder notebooks live outside the store; Living Memory reaches them through sources (`core/sources.py`), reading visibility from the notebook's own sidecar `.hivemind/notebook.json` at each sync (the sidecar is the single source of truth; Living Memory never writes it).

- `walk` treats any folder containing `.hivemind/notebook.json` as a notebook: `hidden` or `private` means skip the whole folder (this also fixes today's behaviour, where implicit repo sources index such folders); `searchable` means index with `inject = 0`; `injectable` means index with `inject = 1`. Chunks gain an `inject` column and a `notebook` label; `VERSION` goes up, so the next sync rebuilds.
- `doc folder link PATH --domain D` registers a notebook outside every bound repository as a source (`kind` `folder`, `notebook: true`, through `source_write`, journaled and undoable); `unlink` removes it. Inside a bound repository no registration is needed.
- `source add` and the implicit repo sources keep their existing rules for everything that is not a notebook.

### 8.3 Injection

- UserPromptSubmit already adds up to two source excerpts (800 characters) when notes gave fewer than two answers. Excerpts from chunks with `inject = 0` are skipped; excerpts from notebooks are labelled `[notebook TITLE]`.
- Living Memory pages from `injectable`, non-private notebooks in the session's scope domains join the same channel: after notes, labelled `[notebook TITLE]`, within the existing 4,000-character prompt cap (`core/injection.py`). `searchable` pages are never injected.
- SessionStart is unchanged and a test asserts that no page or notebook excerpt ever appears in it.

### 8.4 Obsidian sync

`core/sync.py` mirrors each notebook byte for byte to `<vault>/Notebooks/<domain>/<nb>/` with its assets (unlike notes, pages are never rewritten by `build_overlay`). Edits in the vault come back after the existing 30-second quiet period as `doc write --expected-revision <last synced revision>`; a conflict becomes a `doc propose` with the vault text instead of an overwrite. New `.md` files under a vault notebook become pages; deleted files become a pending deletion on the Sync Status page, like notes. The vault's `Notebooks` folder joins the folders agents may not write.

## 9. Version capability

`version` capabilities gain `docs: { "version": 1, "max_page_bytes": 4194304, "max_asset_bytes": 26214400, "notebook_only_domains": true, "folder_reach": true }` (the last only from NS-2B). `tests/test_restore.py` asserts the whole capabilities dictionary and must be updated; SCRIPT_REFERENCE.md documents the object.

## 10. Tests

- NS-2A: `tests/test_docs.py` (byte-exact round trip incl. CRLF, BOM and no trailing newline; names and limits; stale writes; move with link rewrite; undo of every kind; attach from file; propose, accept with a stale base, reject), `tests/test_docs_backup.py` (backup, verify, restore of pages and assets; asset budget; bundle caps), `tests/test_docs_runtime.py` (the permission matrix per actor and inside a Hivemind terminal; private and hidden reads), `tests/test_notebook_domains.py` (create, refusals, bind conversion, `domains` kind, every `--all-domains` path skips them), and pages never appearing in `list`, `review`, `open`, worklists, maintenance candidates, SessionStart, `recap` or `wiki`.
- NS-2B: `tests/test_docs_search.py` (visibility per actor, `pages` array, incremental index), `tests/test_docs_inject.py` (searchable never injected, injectable labelled, private never), `tests/test_sources_notebooks.py` (sidecar visibility inside a bound repo and a linked folder, `inject` column, version bump rebuild), `tests/test_docs_sync.py` (vault mirror, edit back, conflict to proposal). `scripts/eval_retrieval.py` results on notes are unchanged.
- Docs: a new `NOTEBOOKS.md` (user-facing), SCRIPT_REFERENCE.md rows for `doc` and `domain create`, ARCHITECTURE.md (the record kind and its index), README.md (one paragraph), OBSIDIAN.md (the `Notebooks` folder), PROTOCOL.md and the instruction block (`doc read`, `doc propose`).

## 11. Exit

- NS-2A: pages are byte-exact and invisible to every note path; agents can read and propose but not write; Hivemind terminals likewise; undo and backup restore work for pages and attachments; notebook-only domains behave as listed.
- NS-2B: a searchable notebook is found by agent search and never injected; an injectable one is injected with a label; hidden and private ones are never found, including folder notebooks inside a bound repository; an Obsidian edit comes back as a page write or a proposal.

## 12. Shipped

(Filled in when each phase lands.)
