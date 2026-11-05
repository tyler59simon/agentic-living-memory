# Direct installation

After cloning the repository, run `py -3.15 living_memory.py setup` from its directory. Python 3.10+ with SQLite FTS5 is required; substitute your selected Python version on other installations. The wizard displays the current store and suggests `~/LivingMemory` (for example `C:\Users\you\LivingMemory`). Press Enter or supply a custom absolute local folder. Setup initializes an empty store or connects to an existing valid store. It never merges stores implicitly.

The shared pointer is `~/.config/agentic-living-memory/config.json`: `{"version":1,"data_root":"ABSOLUTE_PATH"}`. It is independent of every harness installation. Installers and hooks use the same resolver. `--root` takes precedence over `LIVING_MEMORY_ROOT`, then the legacy `LIVING_MEMORY_HOME` alias, then this configuration. An existing legacy store can be discovered when no shared configuration exists; new harness-specific stores are not created implicitly. Invalid configuration or an unavailable selected directory produces an error rather than a fallback store.

For unattended setup use `py -3.15 living_memory.py setup --location C:/Users/you/LivingMemory --non-interactive`. Harness installers also accept `--location`; without existing shared configuration or an intentional environment override, interactive installation launches setup and unattended installation requires a location. With an existing legacy store and an empty new destination, setup produces a migration preview instead of switching immediately.

Then run `py -3.15 install.py`. It uses the interpreter running the installer for Windows hook commands and writes the package to `CODEX_HOME/agentic-living-memory` (default `~/.codex/agentic-living-memory`) with owned handlers in `CODEX_HOME/hooks.json`. Review native hook trust, restart the harness, bind a project with `py -3.15 living_memory.py bind my-project C:/Projects/MyProject`, and run `py -3.15 living_memory.py doctor --cwd C:/Projects/MyProject`. Updating or uninstalling a harness preserves the shared pointer and data. Other harness adapters can reuse the store without changing its location; see INTEGRATION.md.

## Relocating existing memory

Run `py -3.15 living_memory.py setup --location C:/Users/you/LivingMemory --non-interactive`. To select a source explicitly, add `--source C:/Users/you/.codex/agentic-living-memory-data`. Inspect the returned source, destination, note/file counts and digest. Close other harness sessions before applying the preview:

```powershell
py -3.15 living_memory.py setup --apply-plan PLAN_ID --expected-plan-sha256 DIGEST
```

The approved operation locks both stores, copies and hashes durable files, validates notes, rebuilds catalogs and SQLite indexes, retires the source for updated clients, and atomically publishes the shared pointer. Notes, bindings, taxonomy, plans and operation history are preserved. Hook delivery and turn receipts are transient and start fresh. The source remains a backup; no cleanup is performed. Do not use older clients during or after migration: their code cannot enforce the retirement marker.

Migration plans and phase journals are stored beside the shared configuration in `relocations/`. Rerun the exact approved apply command after interruption. Stale source/configuration and conflicting destination files are preserved and reported for review. A failure after source retirement can temporarily block access until the same command completes; it does not silently reopen the old store. Schema upgrades remain the separate `migration-plan` command.

Use `setup --inspect-plan PLAN_ID` to compare an approved relocation snapshot with the current store without exposing note contents. If the source changed before copying started, rerun setup to produce a fresh preview and review the differences before applying. A destination containing only the migration lock file is still considered empty.

## Package management and verification

`install.py --plan` saves a package-and-hooks plan without applying. `--apply-plan ID --expected-plan-sha256 HASH` applies exactly that plan. `--uninstall-plan` proposes removal of owned files and handlers; it does not delete memory data. Source payload and target revision checks preserve unowned or modified files as conflicts. Package changes and merged hooks share a durable transaction rooted at CODEX_HOME.

Start Codex and review the new or changed commands in `/hooks`, then restart or reload as the host requires. Trust is a native host decision; `doctor` intentionally reports trust as unknown rather than inferring it from a config file. The adapter registers SessionStart, UserPromptSubmit, SubagentStart, PreToolUse, and Stop.

Run the installed `living_memory.py doctor --cwd PROJECT`. It reports interpreter, FTS5, effective root, shared configuration path, root-selection source, conflicting overrides, binding, domain data, index freshness, pending recovery, and last observed hook delivery. Bind additional projects explicitly with `bind DOMAIN PROJECT`. Longest path-prefix wins. Unbound projects receive no scoped memory injection. Storage is shared for one user on one computer; remote access and multi-computer synchronization are outside this setup flow.

For an existing store, generate `migration-plan`, inspect it, then apply its digest. This preserves canonical note bytes and legacy tags. A missing taxonomy receives controlled facets; a legacy flat taxonomy requires a separate approved bulk-retag plan.

No skill, marketplace registration, sandbox weakening, or additional model service is required. This release does not automatically remove unrelated or policy-protected residue from earlier experiments. See RECOVERY.md before manually restoring an older installation.
