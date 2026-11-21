# Notebooks

Notebooks hold pages: free-form Markdown the user writes, mostly in Hivemind's Notes Studio. Pages are a record kind of their own. They are not notes, so no list, review, maintenance, SessionStart, recap or wiki path reads them, and they need no frontmatter or tags. `search` and `ask` list them apart from notes, in a `pages` array. The design is in `docs/notebooks-design.md`.

## Layout

```
domains/D/notebooks/<notebook>/
  notebook.json            title, order, icons, pins, visibility, private, uid
  <section>/<page>.md      pages, nested up to 8 levels; a page's subpages sit in a folder of the same name
  assets/<sha8>-<name>     attachments, added with doc attach
  .proposals/<id>.json     edits agents staged for the user
```

- A notebook id is 2 to 40 lowercase letters, digits or hyphens. Page names end in `.md`, are unique ignoring letter case within a folder, and may not use `/ \ : * ? " < > |`, control characters, Windows device names, or a leading or trailing space or trailing dot. `notebook.json`, `assets` and `.proposals` are reserved at the top of a notebook.
- A page is kept byte for byte: CRLF, a BOM and a missing final newline all survive. Its revision is the SHA-256 of its bytes.
- Limits: a page is at most 4 MiB, an attachment at most 25 MiB; a notebook holds at most 20,000 pages and 2 GiB of attachments (doctor warns at 80%).
- Links and junctions inside a notebook are refused; Living Memory never follows one.

## Commands

Every write is one journaled operation (`doc-...`) and `doc undo OPERATION` (or `undo OPERATION`) reverses it. No write may delete a notebook's `notebook.json` while the notebook keeps other files, so undoing the creation of a notebook that has pages is refused: delete the notebook as a whole.

| Command | What it does |
| --- | --- |
| `doc notebooks [--all-domains] [--include-hidden]` | Notebooks of the domain, with page, attachment and proposal counts |
| `doc notebook create D NB --title T [--visibility hidden\|searchable\|injectable] [--private]` | Create a notebook |
| `doc notebook update D NB [--expected-revision R] [--title] [--visibility] [--private\|--no-private] [--order-file F] [--icons-file F] [--pinned-file F] [--settings-file F]` | Change its settings |
| `doc notebook delete D NB [--with-pages]` | Delete an empty notebook directly; one with pages becomes a plan to approve |
| `doc list D NB [--section PATH]` | Pages and sections with size and revision |
| `doc read D NB PATH [--max-chars N]` | A page's text and revision |
| `doc create D NB PATH [--body-file F]` | Create a page (text from the file or stdin) |
| `doc write D NB PATH --expected-revision R [--body-file F]` | Replace a page; a stale revision exits 6 |
| `doc move D NB FROM TO [--rewrite-links]` | Rename or move a page or section; `--rewrite-links` rewrites `[[links]]` to it in the same notebook (at most 200 pages), and a bare `[[Name]]` only when that name was unique before the move. Pending proposals follow the pages they change. A change of letter case only takes two moves, through another name |
| `doc delete D NB PATH --expected-revision R` | Delete a page |
| `doc attach D NB --from-file F [--name N]` | Copy a file into `assets/`; the same bytes under the same name are reused |
| `doc history D NB PATH [--limit N] [--version OP]` | Operations that changed a page; with `--version`, the page as that operation left it |
| `doc propose D NB PATH [--base-revision R] [--note TEXT] [--body-file F]` | Stage an edit for the user (agents may run it); leave out the base revision for a new page |
| `doc proposals [D [NB]] [--status pending\|accepted\|rejected\|all]`, `doc proposal ID` | Staged edits |
| `doc accept ID [--expected-revision R]`, `doc reject ID [--reason TEXT]` | Settle a staged edit (main actor) |
| `doc search WORDS [--all-domains] [--notebook NB] [--limit N] [--rebuild]` | Search pages; a hidden notebook only when `--notebook` names it |
| `doc tasks [--all-domains] [--notebook NB] [--open]` | Task lines (`- [ ]`, `- [x]`) with page, line, text and due date (`📅 2026-12-01` or `due:: 2026-12-01`) |
| `doc query --payload-file F [--all-domains]` | Pages by frontmatter properties: `{"where": [{"key", "op", "value"}], "sort": [{"key", "order"}], "limit": N, "notebook": NB}`; `op` is `=`, `!=`, `<`, `<=`, `>`, `>=`, `contains`, `exists` or `missing`, numbers compare as numbers, and `title`, `path` and `notebook` work as keys |
| `doc folder set PATH --visibility V [--private\|--no-private]`, `doc folder unset PATH [--forget]` | Register a folder notebook outside the store and how far agents reach into it (Hivemind runs these) |
| `domain create NAME --notebook-only` | A domain that holds notebooks only |

## Who may do what

- Agents read pages (`doc notebooks`, `list`, `read`, `history`, `search`, `tasks`, `query`, `proposals`, `proposal`) and stage edits with `doc propose`. Every other `doc` action, an unknown one included, and `domain create` are the main actor's: the hook denies them to subagents, never auto-allows them, and denies them to agents in a Hivemind terminal (`HIVEMIND_SESSION`). The hook also denies `serve` to agents, since its requests carry arguments the hook never sees.
- Visibility: a `hidden` notebook is listed only with `--include-hidden`, searched by `doc search` only when `--notebook` names it, and never found by `search` or `ask`. A `searchable` one (the default) is found by `search`, `ask` and `doc search`. An `injectable` one is also offered to the prompt hook (see below).
- Private notebooks look missing to every command, writes, `apply`, `undo` and `recover` included, unless it carries `--include-private`, which only Hivemind's own bridge passes. `doctor`, `backup` and relocation previews count entries inside private notebooks (`private_withheld`) without naming them; `doctor --include-private` names them. Inside `batch` the flag is refused, because the hook sees only the outer command. The hook denies `--include-private` to any agent, and denies Read, Glob and Grep on a private notebook's folder (or a search over a folder that holds one). `history` (note history too), `doc history`, `diff` and `inspect-plan` leave out changes inside a notebook that is or ever was private (`notebook-privacy.json` keeps them, and neither undo nor restore removes an entry). Private is not file protection: a shell command can still read the files.

## Search and reach

- Living Memory pages are indexed in `domains/D/docs.sqlite3`, derived and never backed up (`doc search --rebuild` recreates it). Every `doc search`, `doc tasks`, `doc query`, `search` and `ask` first checks the notebooks for changed pages for at most 0.2 seconds, resuming where the last check stopped, then reindexes for at most 0.5 seconds; a reply that left work over says `pages_stale: true`. A `doc` write reindexes its own notebook at once. The prompt hook never reindexes.
- Each page hit has `origin` (`lm` or `folder`), `domain`, `notebook`, `notebook_title`, `title`, `path`, `heading`, `snippet`, `line_start`, `line_end`, `revision`, `score`, `relevance` and `label: notebook page`. `ask` gives each question of a several-question query its own `pages`. Frontmatter is left out of snippets, and credentials are redacted as in sources.
- Private notebooks are searched only with `--include-private` (Hivemind's bridge). Whether a notebook is private or hidden is read from its `notebook.json` when results are served, so a change takes effect at once.
- Folder notebooks are folders outside the store that Hivemind opens as notebooks. `doc folder set` records one in `notebook-folders.json` with its domain, visibility and private flag; its sidecar `.hivemind/notebook.json` (`visibility`, `private`, `title`) is a second opinion, and the stricter of the two wins (private, then hidden, then searchable, then injectable). A sidecar that cannot be parsed counts as hidden; a folder inside a notebook takes the strictest policy above it. `doc folder unset` of a hidden or private folder keeps its entry as a tombstone, so the folder stays out of reach even where the sidecar was never written; `--forget` drops it.
- Folder notebook pages are indexed by the knowledge base (`core/sources.py`): by the source that already walks the folder, else by a source of their own named `nb-` and a hash of the path. Hidden and private notebooks are never walked. Each hit is checked again against the registry and sidecars when it is served, so a notebook made hidden or private disappears before the next sync, and its rows go at that sync. `search --sources`, `ask`'s `sources` and `ingest-scan` leave every notebook file out. A damaged `notebook-folders.json` serves no source excerpt or folder page at all until doctor's warning is acted on.
- Prompts: the prompt hook's source excerpt channel (at most two excerpts, 800 characters, only when notes gave fewer than two answers, never for subagents or machine prompts, each excerpt once a session) also takes excerpts of injectable notebooks: folder notebooks of the session's domain, and Living Memory notebooks of the session's domain and its parent and child domains. Each is labelled `[notebook TITLE]`. Searchable notebooks are never injected, and SessionStart never shows a page.

## Obsidian

In a domain with a managed vault, each searchable or injectable notebook is mirrored byte for byte to `<vault>/Notebooks/<domain>/<notebook>/` at every vault sync, attachments under `assets/`. Hidden and private notebooks are never mirrored; when one turns hidden or private its copies leave the vault at the next sync, and a copy edited in the vault is kept as a proposal first.

- A page edited in the vault comes back after the quiet period as a page write against the revision last mirrored. If the page changed in memory too, the vault text becomes a proposal and the vault shows the memory version again; the Sync Status page says so.
- A new `.md` file in a mirrored notebook becomes a page. A name pages may not use is listed under "Notebook pages" on the Sync Status page.
- A page deleted in the vault becomes a delete plan: tick its "Delete page" line on the Sync Status page and the next sync applies it. Five or more copies missing at once (and at least a fifth of them) count as a vault reset: they are copied back instead.
- Attachments flow from memory to the vault only.
- The vault's `Notebooks` folder is never indexed as a source, and agents may not write it. A notebook-only domain has no vault, so its notebooks are not mirrored.

## Notebook-only domains

`domain create NAME --notebook-only` makes a domain with no folder binding and no notes; run again, it recreates a missing `domains/NAME/notebooks` folder. `capture`, `propose` and `bundle` import refuse it; `unbind` refuses while it holds pages; `domains` reports its `kind` as `notebook`. `bind NAME FOLDER` turns it into an ordinary domain, keeping its notebooks; if that stops half way, run the same `bind` again.

## Journals, backups and relocation

- An attachment is a blob effect: the journal keeps only hashes, and the operation keeps both sides' bytes in `.operations/<op>/blobs/<sha256>` until retention deletes it, so an attachment takes twice its size meanwhile. The asset file is replaced in one step and always holds one side.
- Retention: a finished notebook operation is deleted once it is not among the newest 100 operations of any file it changed, or once it is older than 90 days. It runs with the daily journal pruning.
- Snapshots carry `manifest.json` (version 2) listing the notebooks they cover. A restore over a page that has since become a folder, or the reverse, is refused before anything changes, with the path to move. Attachments are stored uncompressed. Automatic snapshots leave out attachments past 500 MB and list them as `omitted`; `backup` takes everything. A restore never touches a notebook the snapshot does not list, never deletes an omitted attachment, keeps the live binding of a domain that still holds such a notebook, and only adds to `notebook-privacy.json`.
- Relocation copies notebooks and leaves out every derived index (`search`, `sources`, `vectors` and `docs` SQLite files with their `-wal`, `-shm` and `-journal` files, and their `.sources-`, `.vectors-` and `.docs-` temporary files). `notebook-folders.json` is backed up and restored like the privacy registry.
