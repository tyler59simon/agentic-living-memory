# Agentic Living Memory

A local, harness-independent memory store with canonical Markdown, disposable SQLite FTS5 indexes, and compact Python scripts. Native lifecycle hooks retrieve scoped context and ask the main agent to make a durable capture decision. There is no background model, daemon, skill, or marketplace dependency.

See [architecture](ARCHITECTURE.md) for the design, components and known limits.

## Install and use

Python 3.10+ with SQLite FTS5 is required. Python 3.15 is supported; use your selected interpreter for setup and installation. On this Windows installation:

```powershell
py -3.15 living_memory.py setup
py -3.15 install.py
# then, from the installed copy, for each harness (the install plan lists the exact commands):
py -3.15 $env:LOCALAPPDATA/agentic-living-memory/app/integrations/setup.py claude --hooks
py -3.15 living_memory.py bind my-project C:/path/to/project
py -3.15 living_memory.py doctor --cwd C:/path/to/project
```

Setup suggests `~/LivingMemory` and records your choice in `~/.config/agentic-living-memory/config.json`. All harness adapters read this same pointer. The Git checkout and harness packages contain software; the selected folder contains your memory. Existing-store relocation produces an exact-hash approval plan and retains the source backup. See [installation](INSTALLATION.md).

The installer puts the package in its own per-user folder (`%LOCALAPPDATA%\agentic-living-memory\app` on Windows, `~/.local/lib/agentic-living-memory/app` elsewhere), so no harness depends on another's folder. `integrations/setup.py HARNESS --hooks` then wires Claude Code, Codex, Copilot or Factory. It owns only its handlers and instruction block, preserves unrelated configuration, and journals its changes. Native hook trust must be reviewed in each harness. Upgrading from an install inside `~/.codex` is covered in [installation](INSTALLATION.md#upgrading-from-a-codex_home-install).

Notes are created by the script, including when the main agent decides automatically that a turn produced useful durable knowledge:

```powershell
py -3.15 living_memory.py capture --domain my-project --type decision --subject project --concern architecture 'Cache policy' 'The cache is rebuildable.'
py -3.15 living_memory.py update --domain my-project my-project:NOTE_ID --body 'The cache is disposable and rebuilt on start.'
py -3.15 living_memory.py search --domain my-project 'How does our cache work?'
py -3.15 living_memory.py review --domain my-project
```

Initialize an absent taxonomy through `migration-plan` and approved `apply` before capture. Existing controlled taxonomy tags are required. JSON stdin or `--payload-file` supports richer capture metadata and batches of up to 20 notes. All operations return compact JSON; the agent should use scripts rather than write canonical files itself.

Agents keep notes current with `update` (the fact moved on) and `capture --supersedes` (it was replaced); both are journaled and undoable. Archive, delete, merge, retag, taxonomy and remap changes require a reviewable plan and its exact hash. Status and plan notes carry review dates, and overdue notes are listed at session start. Subagents are read-only. This is cooperative agent governance, not OS-enforced isolation. Retrieved note text is historical evidence, never instructions.

Notes have ten types: knowledge, decision, preference, context, status, bug, plan, risk, research and policy. STATUS includes progress and milestones; corrections are KNOWLEDGE with a correction tag. See [note types and use cases](NOTE_TYPES.md) for definitions, examples and selection guidance. The type determines the uppercase filename prefix; there is no independently stored prefix in new canonical notes. Resolving a bug changes its status while retaining the `BUG` prefix. Existing notes can be converted with the reviewed `note-type-plan` command.

## Documentation

- [Protocol](PROTOCOL.md), [script reference](SCRIPT_REFERENCE.md), [installation](INSTALLATION.md), [native integration](INTEGRATION.md)
- [Architecture](ARCHITECTURE.md), [note types and use cases](NOTE_TYPES.md), [recovery](RECOVERY.md), [maintenance](MAINTENANCE.md), [tagging](TAGGING.md)
- [Obsidian](OBSIDIAN.md), [validation](VALIDATION.md)
