# Direct installation

After cloning the repository, run `py -3 living_memory.py setup` from its directory. Python 3.10 to 3.15 with SQLite FTS5 is required. Commands in this guide are written for Windows, where `py -3` picks your newest Python 3 (`py -3.13` picks a version); on macOS and Linux see below. The wizard displays the current store and suggests `~/LivingMemory` (for example `C:\Users\you\LivingMemory`). Press Enter or supply a custom absolute local folder. Setup initializes an empty store or connects to an existing valid store. It never merges stores implicitly.

The shared pointer is `~/.config/agentic-living-memory/config.json`: `{"version":1,"data_root":"ABSOLUTE_PATH"}`. It is independent of every harness installation. Installers and hooks use the same resolver. `--root` takes precedence over `LIVING_MEMORY_ROOT`, then the legacy `LIVING_MEMORY_HOME` alias, then this configuration. An existing legacy store can be discovered when no shared configuration exists; new harness-specific stores are not created implicitly. Invalid configuration or an unavailable selected directory produces an error rather than a fallback store.

For unattended setup use `py -3 living_memory.py setup --location C:/Users/you/LivingMemory --non-interactive`. Harness installers also accept `--location`; without existing shared configuration or an intentional environment override, interactive installation launches setup and unattended installation requires a location. With an existing legacy store and an empty new destination, setup moves the store there (see [Relocating existing memory](#relocating-existing-memory)).

Then run `py -3 install.py`. It installs the package into a per-user folder that belongs to no harness:
- Windows: `%LOCALAPPDATA%\agentic-living-memory\app`
- elsewhere: `~/.local/lib/agentic-living-memory/app`

It installs directly, as one journaled write: its journal and ownership record sit beside `app/` in that folder, operation IDs start `i-`, and `alm undo i-...` puts the previous package files back. With `--wire-all` it then runs those wiring commands itself and lists the wired harnesses under `wired` (a failure becomes a warning). Otherwise the receipt's summary lists `next_steps`: `integrations/setup.py HARNESS --hooks` commands, run from the installed copy. A first install lists one for each harness found on the machine (Claude Code, Codex, Copilot, Factory, Antigravity CLI). An update lists only harnesses wired before whose hook handlers or instruction block the new copy would change, and names the others under `wiring_current`; a harness you never wired is left out. A managed block or handler you edited by hand is listed too, and setup then asks for `--adopt` (see [Upgrading the instruction block](#upgrading-the-instruction-block)). Each command writes the harness files directly (see INTEGRATION.md). The interpreter that runs setup is the one the hooks use. After an install, `install.py` also prints those commands on stderr, ready to paste into PowerShell (a leading `&` and quoted paths; no `&` elsewhere), while stdout stays the same JSON result. Then:
1. Review native hook trust and restart the harness.
2. Bind a project with `alm bind my-project C:/Projects/MyProject` (open a new terminal first; see [The `alm` command](#the-alm-command)).
3. Run `alm doctor --cwd C:/Projects/MyProject`.

Updating or uninstalling a harness preserves the shared pointer and data.

On macOS and Linux, use `python3` (3.10 or later) where this guide says `py -3`, and POSIX paths:
- `python3 living_memory.py setup --location ~/LivingMemory --non-interactive`
- `python3 install.py` (add `--wire-all` to wire every harness found)
- `alm bind my-project ~/Projects/MyProject` and `alm doctor --cwd ~/Projects/MyProject`

On macOS, the Python from Xcode's Command Line Tools may be older than 3.10; install a current one from python.org or Homebrew. The python.org build needs its `Install Certificates.command` run once before `semantic` can download a model. Store paths are compared ignoring letter case and Unicode spelling, as APFS names files.

## Updating

Run `alm upgrade` (or `alm upgrade --rewire`, which also runs the wiring commands from step 4). It works from the installed copy and the checkout it was installed from:
1. It refuses while that checkout has uncommitted changes or is on another branch than the one `origin/HEAD` names (else `main`), so work on a feature branch is never installed.
2. It runs `git pull --ff-only` in the checkout. Nothing pulled and the checkout already at the installed commit means `status: current`, and nothing is installed.
3. It runs the checkout's `install.py` with the Python recorded in the install, which installs directly (one journaled write; the result carries `operation_id` and `undo`).
4. It lists `next_steps`: the `integrations/setup.py HARNESS --hooks` commands for harnesses whose wiring the new copy changes, or says the wiring is current.

Then restart running harness sessions. When you pull or commit on that branch without installing, each session start says so with a `Living Memory install:` line; it reads the `.git` files only, so it never fetches and never runs git. `doctor` reports the same drift as a warning. The manual route still works: `git pull`, then `install.py` from the checkout. A store schema change is still the separate `migrate --schema` step.

## Starter instruction file

[templates/CLAUDE.md](templates/CLAUDE.md), [templates/AGENTS.md](templates/AGENTS.md) and [templates/GEMINI.md](templates/GEMINI.md) are a starting point for the harness's global instruction file. All three have the same layout: an empty `## Agentic Living Memory` section that setup fills, then Behavior (coding guidelines) and Writing style (plain writing for replies, commits, docs and notes, matching [WRITING.md](WRITING.md)). Living Memory notes follow the Writing style rules too.

- With no instruction file yet, copy the template before running `integrations/setup.py HARNESS --hooks`: `templates/CLAUDE.md` to `~/.claude/CLAUDE.md`, `templates/AGENTS.md` to `~/.codex/AGENTS.md` or `~/.factory/AGENTS.md`, and `templates/GEMINI.md` to `~/.gemini/config/GEMINI.md` (Antigravity CLI). Setup replaces the placeholder comment under the template's first heading, `## Agentic Living Memory`, with the Living Memory block. In a file with no such heading, setup adds the block at the end.
- With an existing file, paste the sections you want into it. Keep them outside the `agentic-living-memory:begin` and `:end` markers: setup owns the text between them.

Edit the sections freely; setup changes only its own block. Credits: the Behavior section is from [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) (MIT), and the Writing style section adapts [humanizer](https://github.com/blader/humanizer) v4.0.0 (MIT) and Wikipedia's [Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing).

## The `alm` command

`install.py` also installs a launcher named `alm`, so you can type `alm search 'cache'` instead of the interpreter and script paths. For a new user the whole sequence is: clone the repository, run `py -3 install.py`, open a new terminal, then run `alm doctor`.

- **Where:** `~/.local/bin/alm.exe` on Windows, `~/.local/bin/alm` (an sh script) elsewhere. The install receipt lists it under `launcher` (`create`, `replace`, `keep`, `remove` or `none`, with the reason).
- **Windows build:** `alm.exe` is built at install time from pip's bundled launcher stub (`pip/_vendor/distlib/t64.exe`, or the arm64 or 32-bit variant) plus the interpreter path and a zip holding the entry point, the same layout as `pip.exe`. It needs pip: if there is no stub the receipt says `none` and suggests `py -m ensurepip`. No `.cmd` or `.ps1` is written, because `cmd.exe` would expand `%VAR%` text inside note arguments and `.ps1` runs into execution policy. After writing it the installer runs `alm --help`; if that fails (for example Smart App Control blocks the exe) the launcher is removed and a warning says so.
- **PATH:** when `~/.local/bin` is not on your PATH the install adds it and the receipt names it under `path_change`. On Windows it appends the folder to the user `Path` value under `HKCU\Environment` (keeping its value type) and broadcasts the change. Elsewhere it appends `export PATH="$HOME/.local/bin:$PATH"  # agentic-living-memory` to `~/.zprofile` (zsh; `$ZDOTDIR/.zprofile` when `ZDOTDIR` is set), `~/.bash_profile` if it exists else `~/.profile` (bash), or `~/.profile`. For fish it appends `fish_add_path -g $HOME/.local/bin  # agentic-living-memory` to `~/.config/fish/config.fish` (under `$XDG_CONFIG_HOME` when that is set). The profile is written through a temp file and a rename; a symlinked profile is written at its target. On Linux, a terminal that starts a non-login bash reads `~/.bashrc` instead; the receipt's `notice` says to add the line there if `alm` is still not found. Open a new terminal, and restart running agent sessions, to see it.
- **Ownership:** every launcher carries the marker `# agentic-living-memory launcher`. Install and uninstall replace or remove only a file with that marker. A foreign `alm`, or a symlink, is left alone and reported as `none`.
- **Agents:** while this install's launcher is the first `alm` on PATH, hooks give agents `CMD = alm --json --domain NAME` and auto-allow bare `alm` read/search/capture commands as they do the full form. If another `alm` shadows it, a `.cmd` or `.ps1` sits beside it, or it was built for a different interpreter or script, agents keep the full quoted command and the host's normal permission prompt applies. Install and hooks must use the same Python for this to match.
- **Uninstall:** `install.py --uninstall` removes our launcher but leaves the PATH entry, since other programs may use the folder. `undo` does not remove or restore the launcher or the PATH entry: the launcher runs whatever copy is in `app/`.
- **Limits:** in a terminal without ConPTY (Git Bash under mintty) stdout is a pipe, so a person there gets JSON; Windows Terminal and the PowerShell console get text. A shell alias or function named `alm` is invisible to the hook.

## Upgrading from a CODEX_HOME install

Earlier builds installed the package into `CODEX_HOME/agentic-living-memory` (default `~/.codex/agentic-living-memory`). Claude, Copilot and Factory hooks pointed into that Codex folder, so removing Codex broke them. To move:
1. From the source checkout, run `py -3 install.py`. This installs the new copy; the old one keeps working meanwhile.
2. From the new copy, run `integrations/setup.py HARNESS --hooks` for each harness. Setup recognises handlers that point at the old copy and replaces them without a "Modified managed handlers preserved" error and without duplicates. For Codex this takes over `hooks.json` and the `AGENTS.md` block from the old installer.
3. Restart each harness and check `doctor`.
4. Run `py -3 install.py --retire-legacy`. It removes the old copy directly, as one journaled write in CODEX_HOME. It refuses while any harness configuration still references the old folder. It checks the default harness homes, plus any home you set up with `setup.py --home PATH`, which you pass as `--home HARNESS=PATH` (repeatable); the receipt lists every file it checked under `checked`. It removes only the files the old install owns and that are unchanged; anything else is listed under `not_owned_left_in_place`. It does not touch the legacy data store `CODEX_HOME/agentic-living-memory-data` or old lifecycle receipts.

Hook handlers hold the absolute path of the Python that ran `setup.py`. After upgrading, moving or removing that Python, every hooked event fails inside the harness until the harness is wired again: run `doctor` (its `hooks.wiring` section names the missing interpreter), then `integrations/setup.py HARNESS --hooks` from the installed copy with the Python you use now.

## Upgrading the instruction block

When the managed block in `CLAUDE.md`, `AGENTS.md` or `copilot-instructions.md` changes in a release, SessionStart injects the shorter fallback protocol until the block is rewritten. `alm upgrade` and `install.py` list exactly those harnesses under `next_steps`. Re-run `integrations/setup.py HARNESS --hooks` from the installed package for each harness. If you hand-edited the block, setup changes nothing and names the file. Either delete the block between its `agentic-living-memory:begin` and `agentic-living-memory:end` markers, or run `integrations/setup.py HARNESS --hooks --adopt`: setup takes ownership directly, replaces your edited block (and any edited hook handlers) with the managed text, and prints the unified diff it replaced on stderr (also in `summary.adopt`); `alm undo s-HARNESS-...` puts your edit back. The same applies to edited hook handlers and to `--uninstall`.

## Relocating existing memory

Run `py -3 living_memory.py setup --location C:/Users/you/LivingMemory --non-interactive`. To select a source explicitly, add `--source C:/Users/you/.codex/agentic-living-memory-data`. Close other harness sessions first: setup moves the store directly. The destination must be empty (a destination containing only the store lock file counts as empty); stores are never merged.

The relocation locks both stores, copies and hashes durable files, verifies the copy, validates notes, rebuilds catalogs and SQLite indexes, marks the source as relocated for updated clients, and then switches the shared pointer. The result has `status: committed`, the `notes` and `files` counts and a `next` step. Notes, bindings, taxonomy and operation history are preserved. Hook delivery and turn receipts are transient and start fresh. The source stays as a backup; no cleanup is performed. Do not use older clients during or after the move: their code cannot enforce the relocated marker.

Relocation records and phase journals are stored beside the shared configuration in `relocations/`. Rerun the same `setup --location` command after an interruption: it resumes the recorded move. The pointer changes last, so a stop before that leaves the old store in use. If the source store or the shared configuration changed during the move, setup refuses and publishes nothing (after a source change, delete the partial copy in the destination and run the command again); conflicting destination files are preserved and reported. A failure after the source is marked can temporarily block access until the same command completes; it does not silently reopen the old store. Schema upgrades remain the separate `migrate --schema` command.

## Proxies and offline machines

Living Memory itself reaches the network in two places, both optional: the `semantic install` download and `source-sync` for registered Confluence or Notion sources. Both use Python's standard `urllib`. The optional features that call `claude -p` (note maintenance, the digest refresh and the agent wiki build) go through Claude Code and its own network settings instead.

- An `HTTPS_PROXY` (or `https_proxy`) environment variable works. On Windows, the proxy set in Internet Options (read from the registry) is used only when no proxy variable has a value; a non-empty `NO_PROXY` alone turns that lookup off, so set `HTTPS_PROXY` as well in that case. Certificates are checked against the Windows certificate store, so a proxy that inspects TLS with a company root certificate works when that certificate is installed there.
- PAC files (automatic proxy configuration scripts) are not read, and proxies that need NTLM or Kerberos sign-in are not supported.
- `semantic install` downloads three pinned files from huggingface.co, which redirects each download to a storage or CDN host; an allow list needs huggingface.co and the hosts Hugging Face lists for downloads behind a firewall.

On a machine that cannot reach huggingface.co, download `config.json`, `tokenizer.json` and `model.safetensors` for `minishlab/potion-retrieval-32M` at the revision pinned in `core/semantic.py` on another machine, copy them into one folder, and run `semantic install --from DIR`. The files are checked against the same pinned SHA-256 hashes.

## Package management and verification

`install.py` installs the package as one journaled write. `--uninstall` removes the owned package files the same way. It refuses while any harness hook configuration still runs a script from the installed `app/` folder, and the error names each harness file and the `setup.py HARNESS --uninstall` command that unwires it; run those first. It checks the default harness homes, plus any home you set up with `setup.py --home PATH`, which you pass as `--home HARNESS=PATH` (repeatable). Neither deletes memory data. Source payload and target revision checks preserve unowned or modified files as conflicts. Package operations are journaled in the install folder (the parent of `app/`, operation IDs `i-...`), harness wiring in each harness's home (`s-HARNESS-...`); `undo` finds them there by itself. Older releases kept plan files in a `.plans/` folder in each of these roots; daily housekeeping (once a day, from a session start or end hook) deletes those leftovers, and in a harness home only files shaped like an old plan.

Start Codex and review the new or changed commands in `/hooks`, then restart or reload as the host requires. Trust is a native host decision; `doctor` intentionally reports trust as unknown rather than inferring it from a config file. The adapter registers SessionStart, UserPromptSubmit, SubagentStart, PreToolUse, Stop, and PreCompact.

Run the installed `living_memory.py doctor --cwd PROJECT`. It reports interpreter, FTS5, effective root, shared configuration path, root-selection source, conflicting overrides, binding, domain data, index freshness, pending recovery, and last observed hook delivery. `doctor` and `version` also show `release` (the `CORE_RELEASE` constant) and `install`: the git commit of the source checkout the installed copy was made from (`commit`), whether that checkout had uncommitted changes (`dirty`, null when `git status` failed) and its path (`source`), stored in the ownership record `app/.core-ownership-v1.json`. That record also holds `interpreter`, the Python that ran `install.py`; Hivemind starts Living Memory with it unless `HIVEMIND_PYTHON` or its Python setting names another. Records from releases before the field have no `interpreter`; run `install.py` again to add it. `install` is null for a source checkout or when the source was not a git checkout of its own or git was missing. `doctor` warns when the source checkout's HEAD is newer than the installed commit; run `install.py` from it to update. Bind additional projects explicitly with `bind DOMAIN PROJECT`. Longest path-prefix wins. Unbound projects receive no scoped memory injection. Storage is shared for one user on one computer; remote access and multi-computer synchronization are outside this setup flow.

For an existing store, run `migrate --schema` (one journaled write; `undo OPERATION` reverses it). This preserves canonical note bytes and legacy tags. A missing taxonomy receives controlled facets; a legacy flat taxonomy needs a separate `retag`.

No skill, marketplace registration, sandbox weakening, or additional model service is required. This release does not automatically remove unrelated or policy-protected residue from earlier experiments. See RECOVERY.md before manually restoring an older installation.

**Automatic note maintenance** is installed but off. `maintenance.json` is absent or `enabled: false` until someone runs `maintenance enable`, and neither `install.py`, an upgrade, `restore-backup` nor a relocation turns it on. Enabling it lets a detached worker send redacted notes to the configured model (Sonnet by default, within 10 notes and 1 USD a day); see `NOTE_MAINTENANCE.md`. After an install, `version` lists `maintenance` under `capabilities` and `maintenance status` reports `enabled: false`. Because the generated instruction block changed, run the `integrations/setup.py HARNESS --hooks` steps the install lists for each harness.
