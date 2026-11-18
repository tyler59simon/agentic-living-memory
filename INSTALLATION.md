# Direct installation

After cloning the repository, run `py -3.15 living_memory.py setup` from its directory. Python 3.10 to 3.15 with SQLite FTS5 is required; substitute your selected Python version on other installations. The wizard displays the current store and suggests `~/LivingMemory` (for example `C:\Users\you\LivingMemory`). Press Enter or supply a custom absolute local folder. Setup initializes an empty store or connects to an existing valid store. It never merges stores implicitly.

The shared pointer is `~/.config/agentic-living-memory/config.json`: `{"version":1,"data_root":"ABSOLUTE_PATH"}`. It is independent of every harness installation. Installers and hooks use the same resolver. `--root` takes precedence over `LIVING_MEMORY_ROOT`, then the legacy `LIVING_MEMORY_HOME` alias, then this configuration. An existing legacy store can be discovered when no shared configuration exists; new harness-specific stores are not created implicitly. Invalid configuration or an unavailable selected directory produces an error rather than a fallback store.

For unattended setup use `py -3.15 living_memory.py setup --location C:/Users/you/LivingMemory --non-interactive`. Harness installers also accept `--location`; without existing shared configuration or an intentional environment override, interactive installation launches setup and unattended installation requires a location. With an existing legacy store and an empty new destination, setup produces a migration preview instead of switching immediately.

Then run `py -3.15 install.py`. It installs the package into a per-user folder that belongs to no harness:
- Windows: `%LOCALAPPDATA%\agentic-living-memory\app`
- elsewhere: `~/.local/lib/agentic-living-memory/app`

Its plans, journals and ownership record sit beside `app/` in that folder. The plan's summary lists `next_steps`: one `integrations/setup.py HARNESS --hooks` command, run from the installed copy, for each harness found on the machine (Claude Code, Codex, Copilot, Factory). Each generates a plan to review and apply (see INTEGRATION.md). The interpreter that runs setup is the one the hooks use. Then:
1. Review native hook trust and restart the harness.
2. Bind a project with `alm bind my-project C:/Projects/MyProject` (open a new terminal first; see [The `alm` command](#the-alm-command)).
3. Run `alm doctor --cwd C:/Projects/MyProject`.

Updating or uninstalling a harness preserves the shared pointer and data.

## The `alm` command

`install.py` also installs a launcher named `alm`, so you can type `alm search 'cache'` instead of the interpreter and script paths. For a new user the whole sequence is: clone the repository, run `py -3.15 install.py --plan`, review the plan, apply it with `--apply-plan ID --expected-plan-sha256 DIGEST`, open a new terminal, then run `alm doctor`.

- **Where:** `~/.local/bin/alm.exe` on Windows, `~/.local/bin/alm` (an sh script) elsewhere. The plan summary lists it under `launcher` (`create`, `replace`, `keep`, `remove` or `none`, with the reason).
- **Windows build:** `alm.exe` is built at install time from pip's bundled launcher stub (`pip/_vendor/distlib/t64.exe`, or the arm64 or 32-bit variant) plus the interpreter path and a zip holding the entry point, the same layout as `pip.exe`. It needs pip: if there is no stub the plan says `none` and suggests `py -m ensurepip`. No `.cmd` or `.ps1` is written, because `cmd.exe` would expand `%VAR%` text inside note arguments and `.ps1` runs into execution policy. After writing it the installer runs `alm --help`; if that fails (for example Smart App Control blocks the exe) the launcher is removed and a warning says so.
- **PATH:** when `~/.local/bin` is not on your PATH the plan includes a `path_change`, which you approve with the plan. On Windows it appends the folder to the user `Path` value under `HKCU\Environment` (keeping its value type) and broadcasts the change. Elsewhere it appends `export PATH="$HOME/.local/bin:$PATH"  # agentic-living-memory` to `~/.zprofile` (zsh), `~/.bash_profile` if it exists else `~/.profile` (bash), or `~/.profile`. Apply refuses as stale if that registry value or file changed after the plan. Open a new terminal, and restart running agent sessions, to see it.
- **Ownership:** every launcher carries the marker `# agentic-living-memory launcher`. Install and uninstall replace or remove only a file with that marker. A foreign `alm`, or a symlink, is left alone and reported as `none`.
- **Agents:** while this install's launcher is the first `alm` on PATH, hooks give agents `CMD = alm --json --domain NAME` and auto-allow bare `alm` read/search/capture commands as they do the full form. If another `alm` shadows it, a `.cmd` or `.ps1` sits beside it, or it was built for a different interpreter or script, agents keep the full quoted command and the host's normal permission prompt applies. Install and hooks must use the same Python for this to match.
- **Uninstall:** `install.py --uninstall-plan` removes our launcher but leaves the PATH entry, since other programs may use the folder. `undo` does not touch the launcher.
- **Limits:** in a terminal without ConPTY (Git Bash under mintty) stdout is a pipe, so a person there gets JSON; Windows Terminal and the PowerShell console get text. A shell alias or function named `alm` is invisible to the hook.

## Upgrading from a CODEX_HOME install

Earlier builds installed the package into `CODEX_HOME/agentic-living-memory` (default `~/.codex/agentic-living-memory`). Claude, Copilot and Factory hooks pointed into that Codex folder, so removing Codex broke them. To move:
1. From the source checkout, run `py -3.15 install.py --plan`, then `--apply-plan ID --expected-plan-sha256 DIGEST`. This installs the new copy; the old one keeps working meanwhile.
2. From the new copy, run `integrations/setup.py HARNESS --hooks` for each harness, and apply each plan. Setup recognises handlers that point at the old copy and replaces them without a "Modified managed handlers preserved" error and without duplicates. For Codex this takes over `hooks.json` and the `AGENTS.md` block from the old installer.
3. Restart each harness and check `doctor`.

Hook handlers hold the absolute path of the Python that ran `setup.py`. After upgrading, moving or removing that Python, every hooked event fails inside the harness until the harness is wired again: run `doctor` (its `hooks.wiring` section names the missing interpreter), then `integrations/setup.py HARNESS --hooks` from the installed copy with the Python you use now, and apply the plan.
4. Run `py -3.15 install.py --retire-legacy-plan`, then apply it with `--apply-plan`. It refuses while any harness configuration still references the old folder. It checks the default harness homes, plus any home you set up with `setup.py --home PATH`, which you pass as `--home HARNESS=PATH` (repeatable); the plan lists every file it checked under `checked`. It removes only the files the old install owns and that are unchanged; anything else is listed under `not_owned_left_in_place`. It does not touch the legacy data store `CODEX_HOME/agentic-living-memory-data` or old lifecycle receipts.

## Upgrading the instruction block

When the managed block in `CLAUDE.md`, `AGENTS.md` or `copilot-instructions.md` changes in a release, SessionStart injects the shorter fallback protocol until the block is rewritten. Re-run `integrations/setup.py HARNESS --hooks` from the installed package for each harness and apply the plan. If you hand-edited the block, setup refuses the whole plan: delete the block between its `agentic-living-memory:begin` and `agentic-living-memory:end` markers, then re-run setup.

## Relocating existing memory

Run `py -3.15 living_memory.py setup --location C:/Users/you/LivingMemory --non-interactive`. To select a source explicitly, add `--source C:/Users/you/.codex/agentic-living-memory-data`. Inspect the returned source, destination, note/file counts and digest. Close other harness sessions before applying the preview:

```powershell
py -3.15 living_memory.py setup --apply-plan PLAN_ID --expected-plan-sha256 DIGEST
```

The approved operation locks both stores, copies and hashes durable files, validates notes, rebuilds catalogs and SQLite indexes, retires the source for updated clients, and atomically publishes the shared pointer. Notes, bindings, taxonomy, plans and operation history are preserved. Hook delivery and turn receipts are transient and start fresh. The source remains a backup; no cleanup is performed. Do not use older clients during or after migration: their code cannot enforce the retirement marker.

Migration plans and phase journals are stored beside the shared configuration in `relocations/`. Rerun the exact approved apply command after interruption. Stale source/configuration and conflicting destination files are preserved and reported for review. A failure after source retirement can temporarily block access until the same command completes; it does not silently reopen the old store. Schema upgrades remain the separate `migration-plan` command.

Use `setup --inspect-plan PLAN_ID` to compare an approved relocation snapshot with the current store without exposing note contents. If the source changed before copying started, rerun setup to produce a fresh preview and review the differences before applying. A destination containing only the migration lock file is still considered empty.

## Package management and verification

`install.py --plan` saves a package plan without applying it. `--apply-plan ID --expected-plan-sha256 HASH` applies exactly that plan. `--uninstall-plan` proposes removal of the owned package files; unwire each harness first with `setup.py HARNESS --uninstall-plan`. Neither deletes memory data. Source payload and target revision checks preserve unowned or modified files as conflicts. Package transactions are rooted at the install folder (the parent of `app/`). Harness wiring transactions are rooted at each harness's home.

Start Codex and review the new or changed commands in `/hooks`, then restart or reload as the host requires. Trust is a native host decision; `doctor` intentionally reports trust as unknown rather than inferring it from a config file. The adapter registers SessionStart, UserPromptSubmit, SubagentStart, PreToolUse, Stop, and PreCompact.

Run the installed `living_memory.py doctor --cwd PROJECT`. It reports interpreter, FTS5, effective root, shared configuration path, root-selection source, conflicting overrides, binding, domain data, index freshness, pending recovery, and last observed hook delivery. `doctor` and `version` also show `release` (the `CORE_RELEASE` constant) and `install`: the git commit of the source checkout the installed copy was made from (`commit`), whether that checkout had uncommitted changes (`dirty`) and its path (`source`), stored in the ownership record `app/.core-ownership-v1.json`. `install` is null for a source checkout or when the source was not a git checkout of its own or git was missing. `doctor` warns when the source checkout's HEAD is newer than the installed commit; run `install.py` from it to update. Bind additional projects explicitly with `bind DOMAIN PROJECT`. Longest path-prefix wins. Unbound projects receive no scoped memory injection. Storage is shared for one user on one computer; remote access and multi-computer synchronization are outside this setup flow.

For an existing store, generate `migration-plan`, inspect it, then apply its digest. This preserves canonical note bytes and legacy tags. A missing taxonomy receives controlled facets; a legacy flat taxonomy requires a separate approved bulk-retag plan.

No skill, marketplace registration, sandbox weakening, or additional model service is required. This release does not automatically remove unrelated or policy-protected residue from earlier experiments. See RECOVERY.md before manually restoring an older installation.

**Automatic note maintenance** is installed but off. `maintenance.json` is absent or `enabled: false` until someone runs `maintenance enable`, and neither `install.py`, an upgrade, `restore-plan` nor a relocation turns it on. Enabling it lets a detached worker send redacted notes to the configured model (Sonnet by default, within 10 notes and 1 USD a day); see `NOTE_MAINTENANCE.md`. After an install, `version` lists `maintenance` under `capabilities` and `maintenance status` reports `enabled: false`. Because the generated instruction block changed, run the `integrations/setup.py HARNESS --hooks` steps the plan lists for each harness.
