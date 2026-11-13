# Claude native adapter

Run setup from the **installed package**, not the development checkout, so live hooks change only through a reviewed install:

```powershell
py -3.15 $env:LOCALAPPDATA/agentic-living-memory/app/integrations/setup.py claude --home C:/Users/you/.claude
py -3.15 $env:LOCALAPPDATA/agentic-living-memory/app/integrations/setup.py claude --home C:/Users/you/.claude --apply-plan ID --expected-plan-sha256 DIGEST
```

Inspect `~/.claude/.plans/ID.json` before applying. Setup merges seven hook groups into `settings.json` (SessionStart, UserPromptSubmit, SubagentStart, PreToolUse, Stop, PreCompact, SessionEnd) and keeps the file pretty-printed with unrelated keys preserved. It adds the static protocol block (the gate sentence, the protocol and the command shapes, with no paths, domains or sessions) to `CLAUDE.md` and records ownership in `.living-memory-ownership.json`. Claude Code's file watcher picks up the hooks immediately.

- Hooks use exec form (`command` = interpreter, `args` = [script, event]), so they run without depending on Git Bash or PowerShell.
- PreToolUse is limited to `Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Read|Glob|Grep|Agent|Task`. Read, Glob, Grep and Agent come for codebase maps: a repo's first reads get its map hint or count toward a map reminder (a spawn gets no hint; SubagentStart gives the subagent the map), and later reads are answered by `core/hookfast.py` (standard library only, no logging). Other read-only tools don't start Python.
- SessionStart injects, in bound projects only, the session line, the exact `CMD` and the tag list while `CLAUDE.md` carries the current static block; when the block is missing or out of date it injects the full protocol instead, so there is never no copy or two differing copies. An unbound project gets the block from `CLAUDE.md` but no `Living Memory session=` line, and the block says to ignore Living Memory without one (SessionStart only offers a bind hint there). Bound sessions are told not to duplicate facts into Claude's built-in auto-memory.
- PreToolUse returns `allow` for exact, unchained `living_memory.py` read/search/capture commands (no permission prompt) and `deny` for Write/Edit/NotebookEdit calls that target store files. Shell commands are not path-checked; they only feed `--files auto`. Plan-application commands follow your normal Claude Code permissions.
- If the store is unavailable, only SessionStart and UserPromptSubmit show a banner. Other events stay silent.
- PreCompact writes plain text, not JSON: Claude Code passes a PreCompact hook's stdout to the summarizer as instructions. On an error it prints nothing. SessionStart with `source: "compact"` carries the handoff. PostCompact is not registered, because it cannot add context and its order relative to SessionStart is not documented. See PROTOCOL.md, Compaction.
- The end-of-turn reminder is returned as Stop `hookSpecificOutput.additionalContext`. Claude continues the turn as with `decision: block` (same loop protections), but the transcript shows it as Stop hook feedback rather than a hook error. Factory shares this adapter and keeps `decision: block`.

Notes:
- The hooks point at the installed package (`%LOCALAPPDATA%/agentic-living-memory/app`), not at any harness folder. Regenerate this adapter after moving the package; setup replaces handlers of an older copy in `~/.codex/agentic-living-memory`.
- Plans and journals in `~/.claude/.plans` and `.operations` keep copies of `settings.json` for rollback. Don't keep secrets in `settings.json` `env`, or rotate them after installation.
- Uninstall with `--uninstall-plan`, then apply its digest. The memory store is untouched. See INTEGRATION.md.
