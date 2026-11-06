# Native harness integration

`install.py` installs only the package, in its own per-user folder. All four adapters (Claude Code, Codex, Copilot, Factory) are wired the same way, with `integrations/setup.py`, and share the same Python core and memory store.

Run `py -3.15 living_memory.py setup` once before installing adapters. Every adapter reads `~/.config/agentic-living-memory/config.json`; no per-harness data directory or environment variable is needed. Installer `--location PATH` supports unattended first-time selection. Subsequent adapters reuse the existing selection. Uninstalling an adapter preserves the shared configuration and memory. Generated adapter commands use the interpreter that runs installation; substitute `-3.15` below to select Python 3.15.

```powershell
py -3.15 integrations/setup.py claude --home C:/path/to/.claude
py -3.15 integrations/setup.py codex --home C:/path/to/.codex
py -3.15 integrations/setup.py copilot --home C:/path/to/.copilot
py -3.15 integrations/setup.py factory --home C:/path/to/.factory --hooks
```

These commands generate plans. Apply with the same harness/home arguments plus `--apply-plan ID --expected-plan-sha256 HASH`. `--uninstall-plan` removes only managed blocks/handlers; modified managed content is preserved as a conflict. Unrelated settings and later unrelated hook additions survive reruns. Factory defaults to instructions; `--hooks` opts into native command hooks.

Run adapter setup from the installed package (`%LOCALAPPDATA%/agentic-living-memory/app/integrations/setup.py` on Windows, `~/.local/lib/agentic-living-memory/app/integrations/setup.py` elsewhere) rather than the development checkout, so live hooks change only through a reviewed install. Setup recognises the handlers of an older copy in `CODEX_HOME/agentic-living-memory` and replaces them (see INSTALLATION.md, Upgrading).

Claude uses exec-form settings.json lifecycle groups (including a SessionEnd handler that runs `overlay-sync-all` for Obsidian vaults) and a managed CLAUDE.md block carrying the gated static protocol; SessionStart adds only per-session data (or the full protocol when the block is missing or stale). Codex gets the same block in `~/.codex/AGENTS.md` and its handlers in `~/.codex/hooks.json` from `setup.py codex`, with status messages and context limits. Factory uses its top-level hooks.json event map (no SubagentStart) and an AGENTS.md block. Copilot uses a dedicated hooks/agentic-living-memory.json file and instruction block, native camelCase names, transformed-prompt output, additionalContext, permission decisions, and agentStop continuation. Codex and Claude translate shared output into hookSpecificOutput and native stop decisions. Claude, Factory and Copilot also receive `allow` for exact, unchained script commands in `core/runtime.py` `ALLOWED_SUBCOMMANDS` (search, read, capture, update, supersede, confirm, review, open, check, stats, timeline, history and other read-only or journaled commands). Claude subagent tool calls (payloads with `agent_id`) never arm the parent session's Stop reminder, and their memory-mutating script commands are denied.

File hints: before a write tool changes a file, PreToolUse may add up to two notes about it as `additionalContext`. These are notes whose `files` property lists the file, or whose body names its path. Each file gets this once per session, and a missing index gives no hint. A hint never denies or delays a tool call. Hints are sent where the host documents PreToolUse `additionalContext`:
- Claude Code: yes. It can arrive together with a permission decision.
- Codex: yes, since openai/codex#20692 (May 2026).
- Copilot CLI and Factory: no, because their PreToolUse output documents only permission decisions and modified arguments. They get no hints.

Claude Code is deployed and verified live on this machine alongside Codex. Factory and Copilot wire formats were checked against current vendor documentation but not exercised in live sessions. Host versions and permissions may affect event support. Moving the package requires regenerating adapters, because commands contain absolute interpreter and script paths.
