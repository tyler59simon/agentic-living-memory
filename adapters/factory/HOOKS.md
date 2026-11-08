# Factory Droid adapter

Generate a native setup plan with `py -3.15 $env:LOCALAPPDATA/agentic-living-memory/app/integrations/setup.py factory --home PATH`, then apply the exact reviewed plan digest.

The default is instructions only: a full protocol block in `~/.factory/AGENTS.md`. That mode has no session or turn identity, so it requests no per-turn receipts.

Add `--hooks` to also write `~/.factory/hooks.json`. Per the [Factory hooks docs](https://docs.factory.com/harness/hooks), that file is a top-level event map, uses shell-form `command` strings, and takes timeouts in seconds. It registers SessionStart, UserPromptSubmit, PreToolUse (matcher limited to write-capable tools) and Stop. Factory has no SubagentStart event. PreCompact is not registered: per the [hooks reference](https://docs.factory.com/reference/hooks-reference), its stdout only appears in transcript views and exit code 2 does not block. The compaction handoff comes from SessionStart with `source: "compact"`. It has no recall line, because Factory's transcript format is undocumented.

`hooks/factory_hooks.py` reuses the Claude-compatible translator, including `allow`/`deny` decisions. Status: **experimental**. It has not been exercised in a live Droid session. See INTEGRATION.md.
