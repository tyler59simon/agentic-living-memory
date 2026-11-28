# Claude native adapter

Run setup from the **installed package**, not the development checkout, so live hooks change only through an install:

```powershell
py -3 $env:LOCALAPPDATA/agentic-living-memory/app/integrations/setup.py claude --home C:/Users/you/.claude
```

On macOS and Linux: `python3 ~/.local/lib/agentic-living-memory/app/integrations/setup.py claude --home ~/.claude`.

Setup writes directly, as one journaled write in the Claude home (operation ID `s-claude-...`; `alm undo s-claude-...` restores the previous `settings.json` and `CLAUDE.md` byte for byte). It merges seven hook groups into `settings.json` (SessionStart, UserPromptSubmit, SubagentStart, PreToolUse, Stop, PreCompact, SessionEnd) and keeps the file pretty-printed with unrelated keys preserved. It adds the static protocol block (the gate sentence, the protocol and the command shapes, with no paths, domains or sessions) to `CLAUDE.md` and records ownership in `.living-memory-ownership.json`. Claude Code's file watcher picks up the hooks immediately.

- Hooks use exec form (`command` = interpreter, `args` = [script, event]), so they run without depending on Git Bash or PowerShell.
- PreToolUse is limited to `Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Read|Glob|Grep|Agent|Task`. Read, Glob, Grep and Agent come for codebase maps: a repo's first reads get its map hint or count toward a map reminder (a spawn gets no hint; SubagentStart gives the subagent the map), and later reads are answered by `core/hookfast.py` (standard library only, no logging). Other read-only tools don't start Python.
- SessionStart injects, in bound projects only, the session line, the exact `CMD` and the tag list while `CLAUDE.md` carries the current static block; when the block is missing or out of date it injects the full protocol instead, so there is never no copy or two differing copies. An unbound project gets the block from `CLAUDE.md` but no `Living Memory session=` line, and the block says to ignore Living Memory without one (SessionStart only offers a bind hint there). Bound sessions are told not to duplicate facts into Claude's built-in auto-memory.
- PreToolUse returns `allow` for exact, unchained `living_memory.py` read/search/capture commands (also as bare `alm`, only while this install's launcher is the first `alm` on PATH) (no permission prompt) and `deny` for Write/Edit/NotebookEdit calls that target store files. Shell commands are not path-checked; they only feed `--files auto`. `recover` and the bulk writes (`delete`, `merge`, `retag`, `migrate`, `restore-backup`, `import`, `ingest`, `overlay-teardown`) follow your normal Claude Code permissions, so Claude Code asks you before each.
- If the store is unavailable, only SessionStart and UserPromptSubmit show a banner. Other events stay silent.
- When local managed settings set `allowManagedHooksOnly` to `true`, Claude Code runs only hooks that managed settings deploy, so it skips the hooks this setup adds to `settings.json`. Setup still writes the hooks, adds a `warning` to its summary and prints it to stderr. It reads `managed-settings.json` and `managed-settings.d/*.json` in the system folder (`C:\Program Files\ClaudeCode` on Windows) and, on Windows, the `Settings` value under `HKLM` and `HKCU` `SOFTWARE\Policies\ClaudeCode`. It cannot see server-managed settings from the claude.ai console; check `/status` for those. `doctor` reports the same blocks, and also `disableAllHooks` in managed settings or in your own `settings.json`, with the remedy and `ok: false`; INTEGRATION.md, Managed Claude Code, says how to deploy the hooks through managed settings.
- PreCompact writes plain text, not JSON: Claude Code passes a PreCompact hook's stdout to the summarizer as instructions. On an error it prints nothing. SessionStart with `source: "compact"` carries the handoff. PostCompact is not registered, because it cannot add context and its order relative to SessionStart is not documented. See PROTOCOL.md, Compaction.
- The end-of-turn reminder is returned as Stop `hookSpecificOutput.additionalContext`. Claude continues the turn as with `decision: block` (same loop protections), but the transcript shows it as Stop hook feedback rather than a hook error. Factory shares this adapter and keeps `decision: block`.

Notes:
- The hooks point at the installed package (`%LOCALAPPDATA%/agentic-living-memory/app`), not at any harness folder. Regenerate this adapter after moving the package; setup replaces handlers of an older copy in `~/.codex/agentic-living-memory`.
- Journals in `~/.claude/.operations` keep copies of `settings.json` for undo. Don't keep secrets in `settings.json` `env`, or rotate them after installation.
- Uninstall with `--uninstall`. The memory store is untouched. See INTEGRATION.md.
