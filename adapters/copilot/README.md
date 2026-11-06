# GitHub Copilot CLI adapter

The optional adapter uses personal hooks under `~/.copilot/hooks/` (or `$COPILOT_HOME/hooks/`). It installs `sessionStart`, `userPromptTransformed`, `subagentStart`, `preToolUse`, and `agentStop` hooks. Event names and output keys were checked against the [Copilot CLI hooks reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference) on 2026-11-03:

- `modifiedTransformedPrompt` appends scoped retrieval to the model-facing prompt.
- `additionalContext` carries session and subagent context.
- `permissionDecision` is `deny`, `ask`, or `allow`.
- `agentStop` uses `decision: block`.

Status: **experimental**. It has not been exercised in a live Copilot session.

Generate and apply a reviewed setup plan with `py -3.15 $env:LOCALAPPDATA/agentic-living-memory/app/integrations/setup.py copilot`. Running from the installed package keeps live hooks stable. Restart Copilot CLI to load the hook file. Regenerate after moving the package. Project domains still need bindings in the shared memory store. Hook failures exit cleanly with `{}` and write a one-line reason to stderr.
