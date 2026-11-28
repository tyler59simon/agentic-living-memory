# Google Antigravity CLI adapter

`hooks/antigravity_hooks.py` connects the Antigravity CLI (`agy`) to the shared Living Memory runtime. Event names and output fields were checked against the [Antigravity hooks reference](https://antigravity.google/docs/hooks) and the [rules reference](https://antigravity.google/docs/rules) on 2026-11-24.

Run setup from the installed package. It writes directly, as one journaled write in `~/.gemini/config` (operation ID `s-antigravity-...`; `alm undo s-antigravity-...` restores the previous files):

```powershell
py -3 $env:LOCALAPPDATA/agentic-living-memory/app/integrations/setup.py antigravity
```

On macOS and Linux: `python3 ~/.local/lib/agentic-living-memory/app/integrations/setup.py antigravity`.

Setup owns three things in `~/.gemini/config` (or `$ANTIGRAVITY_HOME`):

- the `agentic-living-memory` hook group in `hooks.json`. Antigravity shares this file across the CLI, App and IDE; other groups in it are kept through reruns, `--adopt` and uninstall.
- the managed Living Memory block in `GEMINI.md`, the same gated protocol the other harnesses get in `CLAUDE.md` or `AGENTS.md`. The CLI and App read this file; the IDE reads only `~/.gemini/GEMINI.md`. Text outside the block is kept.
- `.living-memory-antigravity-ownership.json`, which records the digests of the group and the block so an edit to either stops the next setup run (`--adopt` takes ownership and prints the difference it replaced).

Start a new `agy` conversation in a project bound to Living Memory after setup. Use `/hooks` in `agy` to see the group, and `alm doctor --cwd PATH` to check the handler paths and the instruction block.

## Events

- `SessionStart` gives the briefing. The CLI fires it with the first prompt; a child conversation without a prompt waits for its first invocation.
- `PreInvocation` runs prompt recall once per user turn. It fires before every model call, so the adapter acts only on the first invocation of a turn that follows a `USER_INPUT` step in the trajectory log (`transcript_full.jsonl`; only step types and indices are read). The first PreInvocation of a turn also closes a turn left open without a Stop.
- `PreToolUse` applies the protected-path and subagent guards to the file, search and shell tools in `TOOLS`. It returns `deny` for a guarded write, `auto_approve` for an exact `alm` read or capture command (`ALLOWED_SUBCOMMANDS` in `core/runtime.py`, the same rule Claude, Factory and Copilot use) and the neutral `allow` otherwise, which leaves the decision to Antigravity's policy. `auto_approve` still honors rules the user configured.
- `Stop` asks for at most one capture continuation per turn with `decision: continue`; Antigravity injects the `reason` as a system message.

The adapter passes `permissionMode` to the runtime, so plan mode and a prompt that leaves it are handled as on other harnesses. A native subagent starts a separate conversation with no user input and zero trajectory steps; the adapter treats that start as a subagent and applies the read-only write guard. The payload carries no parent conversation ID.

## Not supported

- File hints: PreToolUse has no context output.
- Compaction: Antigravity has no compaction event, so there is no PreCompact steering and no compaction handoff.
- Transcript recall: the runtime does not read Antigravity tool calls, so an approved plan or a failed-then-passing command does not raise a Stop reminder.
- The App and IDE surfaces, and remote CLI sessions. The adapter acts only on conversations under `antigravity-cli/brain/`.

## Windows and status

Use `agy` 1.5.8 or later on Windows: earlier releases did not pass quoted hook commands to `cmd.exe` verbatim. The wire format was verified live with `agy` 1.5.15 on Windows. `scripts/simulate.py --harness antigravity` drives the adapter through the shared scenarios.

Remove the adapter with `setup.py antigravity --uninstall`. It removes the group, the block (and `GEMINI.md` if nothing else is left in it) and the ownership file, and keeps your other hooks and the memory store. See INTEGRATION.md.
