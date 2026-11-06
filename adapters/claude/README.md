# Claude native adapter

Run setup from the **installed package**, not the development checkout, so live hooks change only through a reviewed install:

```powershell
py -3.15 ~/.codex/agentic-living-memory/integrations/setup.py claude --home C:/Users/you/.claude
py -3.15 ~/.codex/agentic-living-memory/integrations/setup.py claude --home C:/Users/you/.claude --apply-plan ID --expected-plan-sha256 DIGEST
```

Inspect `~/.claude/.plans/ID.json` before applying. Setup merges five hook groups into `settings.json` and keeps the file pretty-printed with unrelated keys preserved. It adds a two-line pointer block to `CLAUDE.md` and records ownership in `.living-memory-ownership.json`. Claude Code's file watcher picks up the hooks immediately.

- Hooks use exec form (`command` = interpreter, `args` = [script, event]), so they run without depending on Git Bash or PowerShell.
- PreToolUse is limited to `Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell`. Read-only tools don't start Python.
- The full protocol is injected once at SessionStart, only in bound projects. Unbound projects see only the pointer block and ignore Living Memory. Bound sessions are told not to duplicate facts into Claude's built-in auto-memory.
- PreToolUse returns `allow` for exact, unchained `living_memory.py` read/search/capture commands (no permission prompt) and `deny` for Write/Edit/NotebookEdit calls that target store files. Shell commands are not path-checked; they only feed the end-of-turn reminder. Plan-application commands follow your normal Claude Code permissions.
- If the store is unavailable, only SessionStart and UserPromptSubmit show a banner. Other events stay silent.
- The end-of-turn reminder is returned as Stop `hookSpecificOutput.additionalContext`. Claude continues the turn as with `decision: block` (same loop protections), but the transcript shows it as Stop hook feedback rather than a hook error. Factory shares this adapter and keeps `decision: block`.

Notes:
- Because the hooks point at `~/.codex/agentic-living-memory`, uninstalling or relocating the Codex package breaks them. Regenerate this adapter after moving the package.
- Plans and journals in `~/.claude/.plans` and `.operations` keep copies of `settings.json` for rollback. Don't keep secrets in `settings.json` `env`, or rotate them after installation.
- Uninstall with `--uninstall-plan`, then apply its digest. The memory store is untouched. See INTEGRATION.md.
