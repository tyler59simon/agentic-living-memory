# Native harness integration

Codex is configured by the direct installer. The other three adapters are optional and share the same Python core and memory store.

Run `py -3.15 living_memory.py setup` once before installing adapters. Every adapter reads `~/.config/agentic-living-memory/config.json`; no per-harness data directory or environment variable is needed. Installer `--location PATH` supports unattended first-time selection. Subsequent adapters reuse the existing selection. Uninstalling an adapter preserves the shared configuration and memory. Generated adapter commands use the interpreter that runs installation; substitute `-3.15` below to select Python 3.15.

```powershell
py -3.15 integrations/setup.py claude --home C:/path/to/.claude
py -3.15 integrations/setup.py copilot --home C:/path/to/.copilot
py -3.15 integrations/setup.py factory --home C:/path/to/.factory --hooks
```

These commands generate plans. Apply with the same harness/home arguments plus `--apply-plan ID --expected-plan-sha256 HASH`. `--uninstall-plan` removes only managed blocks/handlers; modified managed content is preserved as a conflict. Unrelated settings and later unrelated hook additions survive reruns. Factory defaults to instructions; `--hooks` opts into native command hooks.

Run adapter setup from the installed package (`~/.codex/agentic-living-memory/integrations/setup.py`) rather than the development checkout, so live hooks change only through a reviewed install.

Claude uses exec-form settings.json lifecycle groups (including a SessionEnd handler that runs `overlay-sync-all` for Obsidian vaults) and a managed CLAUDE.md block carrying the gated static protocol; SessionStart adds only per-session data (or the full protocol when the block is missing or stale). Codex gets the same block in `~/.codex/AGENTS.md` from `install.py`. Factory uses its top-level hooks.json event map (no SubagentStart) and an AGENTS.md block. Copilot uses a dedicated hooks/agentic-living-memory.json file and instruction block, native camelCase names, transformed-prompt output, additionalContext, permission decisions, and agentStop continuation. Codex and Claude translate shared output into hookSpecificOutput and native stop decisions. Claude, Factory and Copilot also receive `allow` for exact read/search/capture/update/supersede/review script commands. Claude subagent tool calls (payloads with `agent_id`) never arm the parent session's Stop reminder, and their memory-mutating script commands are denied.

Claude Code is deployed and verified live on this machine alongside Codex. Factory and Copilot wire formats were checked against current vendor documentation but not exercised in live sessions. Host versions and permissions may affect event support. Moving the package requires regenerating adapters because commands contain absolute interpreter and script paths.
