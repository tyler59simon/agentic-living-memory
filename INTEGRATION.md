# Native harness integration

`install.py` installs only the package, in its own per-user folder. Claude Code, Codex, Copilot, Factory and Antigravity CLI use `integrations/setup.py` and share the same Python core and memory store.

Run `py -3 living_memory.py setup` (`python3` on macOS and Linux) once before installing adapters. Every adapter reads `~/.config/agentic-living-memory/config.json`; no per-harness data directory or environment variable is needed. Installer `--location PATH` supports unattended first-time selection. Subsequent adapters reuse the existing selection. Uninstalling an adapter preserves the shared configuration and memory. Generated adapter commands use the interpreter that runs installation; substitute `-3.15` below to select Python 3.15.

```powershell
py -3 integrations/setup.py claude --home C:/path/to/.claude
py -3 integrations/setup.py codex --home C:/path/to/.codex
py -3 integrations/setup.py copilot --home C:/path/to/.copilot
py -3 integrations/setup.py factory --home C:/path/to/.factory --hooks
py -3 integrations/setup.py antigravity
```

On macOS and Linux, run the same commands with `python3` and POSIX paths. `install.py --wire-all` runs the right one for every harness it finds.

Each command writes directly into the harness home as one journaled write (the journal is in that home, operation IDs start `s-HARNESS-`); `alm undo s-HARNESS-...` restores the previous settings and instruction files byte for byte. `--uninstall` removes only managed blocks/handlers; modified managed content is preserved as a conflict. Setup treats a handler as managed when it runs the adapter script from this copy, the installed copy, the source checkout this copy was installed from or the legacy folder, so wiring a harness from the checkout first does not block later runs. A real edit to a managed handler or to the managed instruction block stops setup, before it writes anything, with an error that names the file and the command to run. To keep going without undoing the edit, add `--adopt` (`setup.py HARNESS --hooks --adopt`, or with `--uninstall`): setup takes ownership directly and prints the unified diff it replaced on stderr (also under `summary.adopt`), and `alm undo s-HARNESS-...` puts your edit back. Unrelated settings and later unrelated hook additions survive reruns. Factory defaults to instructions; `--hooks` opts into native command hooks.

Run adapter setup from the installed package (`%LOCALAPPDATA%/agentic-living-memory/app/integrations/setup.py` on Windows, `~/.local/lib/agentic-living-memory/app/integrations/setup.py` elsewhere) rather than the development checkout, so live hooks change only through an install. After an update, `install.py` and `alm upgrade` list only the harnesses whose handlers or instruction block the new copy would change (`core/wiring.py` `needs_rewire`, which writes nothing). Setup recognises the handlers of an older copy in `CODEX_HOME/agentic-living-memory` and replaces them (see INSTALLATION.md, Upgrading).

Claude uses exec-form settings.json lifecycle groups (including a SessionEnd handler that syncs managed Obsidian vaults when there are any, takes a backup when none is recorded yet or the last recorded one is 24 hours old (not for stores over 1,500 notes) and prunes old journals, and a PreCompact handler whose plain-text stdout steers the compaction summary) and a managed CLAUDE.md block carrying the gated static protocol; SessionStart adds only per-session data (or a shorter fallback protocol when the block is missing or stale). Codex gets the same block in `~/.codex/AGENTS.md` and its handlers in `~/.codex/hooks.json` from `setup.py codex`, with status messages and context limits. Factory uses its top-level hooks.json event map (no SubagentStart) and an AGENTS.md block. Copilot uses a dedicated hooks/agentic-living-memory.json file and instruction block, native camelCase names, transformed-prompt output, additionalContext, permission decisions, and agentStop continuation. Antigravity uses a named group in `~/.gemini/config/hooks.json` and a block in `~/.gemini/config/GEMINI.md` (see Antigravity CLI below). Codex and Claude translate shared output into hookSpecificOutput and native stop decisions. Claude, Factory and Copilot also receive `allow` (Antigravity: `auto_approve`) for exact, unchained script commands in `core/runtime.py` `ALLOWED_SUBCOMMANDS` (search, read, help, capture, update, supersede, confirm, review, open, check, stats, timeline, history and other read-only or journaled commands). Claude subagent tool calls (payloads with `agent_id`) never arm the parent session's Stop reminder, and their memory-mutating script commands are denied.

Codex Windows commands: Codex runs `commandWindows` through a shell that is not pinned down (PowerShell or `cmd.exe`), so setup writes one command line that runs in both: a program path with no spaces or shell characters, then `-X utf8`, the quoted script path and the event. The program is the `py` launcher when it is on PATH (with `-3.15` or whichever version ran setup), else the Python that ran setup, else that Python's 8.3 short path (`GetShortPathNameW`; some volumes have 8.3 names turned off). Only when none of these works does setup fall back to `powershell.exe -EncodedCommand`, which endpoint security tools often flag, and then its summary carries a warning; installing the `py` launcher, or Python in a folder without spaces in its path, avoids it. Setup refuses an adapter script path that holds `"`, `$`, a backtick or `%`, because no quoting is safe in both shells. Handlers an older build wrote in the encoded form are still recognised, so setup and `doctor` replace or check them as usual. SessionStart can start detached background jobs (source sync, re-baselining, scheduled maintenance); set `LIVING_MEMORY_NO_BACKGROUND=1` in the environment that starts Codex to stop that.

File hints: before a write tool changes a file, PreToolUse may add up to two notes about it as `additionalContext`. These are notes whose `files` property lists the file, or whose body names its path. Each file gets this once per session, and a missing index gives no hint. A hint never denies or delays a tool call. Hints are sent where the host documents PreToolUse `additionalContext`:
- Claude Code: yes. It can arrive together with a permission decision.
- Codex: yes, since openai/codex#20692 (May 2026).
- Copilot CLI, Factory and Antigravity CLI: no, because their PreToolUse output documents only permission decisions and modified arguments. They get no hints.

Claude Code is deployed and verified live on this machine alongside Codex and Antigravity CLI. Factory and Copilot wire formats were checked against current vendor documentation but not exercised in live sessions. Host versions and permissions may affect event support. Moving the package requires regenerating adapters, because commands contain absolute interpreter and script paths.

## Antigravity CLI

Run `integrations/setup.py antigravity` from the installed package. Setup manages the `agentic-living-memory` group in `~/.gemini/config/hooks.json` and the Living Memory block in `~/.gemini/config/GEMINI.md`, which the CLI and App read as global rules (the IDE does not). Start a new local `agy` conversation in a project bound to Living Memory. Use `/hooks` inside `agy` to check the group, and run `alm doctor --cwd PATH` to check the handler paths and the block. Run `integrations/setup.py antigravity --uninstall` to remove the group and the block while keeping your other hooks, your other `GEMINI.md` text and memory. [adapters/antigravity/README.md](adapters/antigravity/README.md) has the details.

The adapter uses `SessionStart` for the briefing, `PreInvocation` for recall on a new user turn, `PreToolUse` for supported guards, and `Stop` for at most one capture continuation. Antigravity shares the global hooks file across its surfaces. The adapter uses the CLI-specific conversation path before it reads the store. It applies Living Memory denials to the supported file, search and shell tools, returns `auto_approve` for exact `alm` read and capture commands, and leaves other decisions to Antigravity with the neutral `allow`. Native subagents in the verified CLI build start a separate conversation with zero trajectory steps and no user input. The adapter recognizes that startup sequence and applies Living Memory's subagent write guard. The CLI payload provides no explicit parent ID.

The Windows wire format was verified with `agy` 1.5.15. Earlier Windows releases need at least 1.5.8 to handle quoted hook commands. Prompt deduplication reads step types and indices from the tail of the CLI's trajectory log when available. If that metadata is unavailable, repeating the same prompt may skip recall. Transcript search, compaction handoffs, file hints and remote CLI sessions are outside this adapter's supported scope. See the [Antigravity hooks reference](https://antigravity.google/docs/hooks) and [changelog](https://antigravity.google/docs/changelog).

## Managed Claude Code

Claude Code can be locked down by an administrator, and then it silently skips the hooks in `~/.claude/settings.json`. Three settings do this:

- `allowManagedHooksOnly: true` in managed settings: only hooks that managed settings deploy run.
- `disableAllHooks: true` in managed settings: no hooks run, not even managed ones.
- `disableAllHooks: true` in your own `settings.json`: the same, for you only.

`integrations/setup.py claude` warns about the first when it runs, and `doctor` reports all three under `hooks.wiring.claude.blocked` with a remedy in `warnings`, and then `ok` is false: memory that never loads or captures is as broken as a missing script. The managed sources are `managed-settings.json` and `managed-settings.d/*.json` in the system folder (`C:\Program Files\ClaudeCode` on Windows, `/Library/Application Support/ClaudeCode` on macOS, `/etc/claude-code` on Linux) and, on Windows, the `Settings` value under `HKLM` and `HKCU` `SOFTWARE\Policies\ClaudeCode`. Server-managed settings from the claude.ai admin console have no local file, so neither setup nor `doctor` can see them; check `/status` inside Claude Code for those.

To keep memory working under `allowManagedHooksOnly`, ask whoever manages the computer to ship the hooks as managed settings, or to lift the setting. Print the hooks block this copy would write and hand it over:

```powershell
py -3 -c "import json,sys; sys.path.insert(0,r'C:/path/to/app'); from integrations import setup; print(json.dumps({'hooks': setup.hooks('claude')}, indent=2))"
```

Merge the printed `hooks` object into `managed-settings.json` (or a file in `managed-settings.d/`). The handlers hold absolute interpreter and script paths from the machine that printed them, so run this from the installed copy on the machine that will use it, and again after moving the package or changing Python. The installed copy does not need to change.
