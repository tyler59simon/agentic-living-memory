# Codex direct adapter

Run install.py to install the package, then `integrations/setup.py codex --hooks` from the installed copy for the journaled native user hooks and the AGENTS.md block. hooks/codex_hooks.py translates SessionStart, UserPromptSubmit, SubagentStart, PreToolUse, Stop, and PreCompact into the shared runtime. Codex ignores PreCompact stdout, so its summary is not steered; the compaction handoff and recall still work. Review native hook trust in Codex. This adapter has no skill or marketplace dependency. See INSTALLATION.md and PROTOCOL.md.
