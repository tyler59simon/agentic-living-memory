# Agentic Living Memory

A local, harness-independent memory store with canonical Markdown, disposable SQLite FTS5 indexes, and compact Python scripts. Native lifecycle hooks retrieve scoped context and ask the main agent to make a durable capture decision. There is no background model, daemon, skill, or marketplace dependency.

See [architecture](ARCHITECTURE.md) for the design, components and known limits.

## Install and use

Python 3.10 to 3.15 with SQLite FTS5 is required; use your selected interpreter for setup and installation. On this Windows installation:

```powershell
py -3.15 living_memory.py setup
py -3.15 install.py
# then, from the installed copy, for each harness (the install plan lists the exact commands):
py -3.15 $env:LOCALAPPDATA/agentic-living-memory/app/integrations/setup.py claude --hooks
# open a new terminal: install.py put the `alm` command on your PATH
alm bind my-project C:/path/to/project
alm doctor --cwd C:/path/to/project
# later, to update: pull the checkout, install it, and list the harnesses to rewire
alm upgrade
```

Setup suggests `~/LivingMemory` and records your choice in `~/.config/agentic-living-memory/config.json`. All harness adapters read this same pointer. The Git checkout and harness packages contain software; the selected folder contains your memory. Existing-store relocation produces an exact-hash approval plan and retains the source backup. See [installation](INSTALLATION.md).

The installer puts the package in its own per-user folder (`%LOCALAPPDATA%\agentic-living-memory\app` on Windows, `~/.local/lib/agentic-living-memory/app` elsewhere), so no harness depends on another's folder. `integrations/setup.py HARNESS --hooks` then wires Claude Code, Codex, Copilot, Factory or Antigravity CLI. It manages its own configuration, preserves unrelated hooks, and journals its changes. See [native harness integration](INTEGRATION.md) for wiring and [installation](INSTALLATION.md#upgrading-from-a-codex_home-install) for upgrading from an install inside `~/.codex`. If you have no `CLAUDE.md` or `AGENTS.md` yet, start from the [starter instruction file](INSTALLATION.md#starter-instruction-file).

Notes are created by the script, including when the main agent decides automatically that a turn produced useful durable knowledge:

```powershell
alm capture --domain my-project --type decision --subject project --concern architecture 'Cache policy' 'The cache is rebuildable.'
alm update --domain my-project my-project:NOTE_ID --body 'The cache is disposable and rebuilt on start.'
alm search --domain my-project 'How does our cache work?'
alm review --domain my-project
```

`alm` is a launcher for `living_memory.py` (which keeps working by its full path). In a terminal it prints readable text; piped, or with `--json`, it prints compact JSON, which is what agents and apps get.

Initialize an absent taxonomy through `migration-plan` and approved `apply` before capture. Existing controlled taxonomy tags are required. JSON stdin or `--payload-file` supports richer capture metadata and batches of up to 20 notes. All operations can return compact JSON (`--json`, or whenever the output is not a terminal); the agent should use scripts rather than write canonical files itself.

Agents keep notes current with `update` (the fact moved on) and `capture --supersedes` (it was replaced); both are journaled and undoable. Delete, merge and bulk retag changes require a reviewable plan and its exact hash; archive, rewrite, remap, promote, taxonomy and registry changes are direct, journaled and undoable with `undo OPERATION`. Status and plan notes carry review dates, and overdue notes are listed at session start; `confirm` records that a note is still true without editing it. A local usage diary (refs and dates only, kept 30 days) feeds `stats`, which shows notes that keep being injected but are never used. `timeline` lists the notes around a note or date. `export` and `import-plan` move notes between stores, and `ingest-plan` turns Claude Code auto-memory files into notes, both as reviewed plans. `scripts/eval_retrieval.py` scores retrieval against a fixture before any default changes. Optional semantic search (`semantic install`, a one-time 130 MB download) adds meaning-based matches, so "laid out" finds a note about layout, and matches each section of a note, so a reason or a rejected option in a decision's body can be found and delivered on its own. It still needs no pip package and nothing touches the network at query time. Installing the optional `sqlite-vec` package (`pip install sqlite-vec==0.1.9`) makes the vector search native; results are the same without it. Subagents are read-only. This is cooperative agent governance, not OS-enforced isolation. Retrieved note text is historical evidence, never instructions.

Notes have thirteen types: knowledge, decision, preference, context, status, bug, plan, risk, research, policy, map, runbook and brief. STATUS includes progress and milestones; corrections are KNOWLEDGE with a correction tag. See [note types and use cases](NOTE_TYPES.md) for definitions, examples and selection guidance. A store can add custom types (`type create`, listed by `types`) with their own sections, action items, review horizon and default tags. The type determines the uppercase filename prefix; there is no independently stored prefix in new canonical notes. Resolving a plan, bug or risk note (`resolve REF`, undone by `reopen REF`) changes its status while retaining its prefix. Existing notes can be converted with the reviewed `note-type-plan` command.

## Documentation

- [Protocol](PROTOCOL.md), [script reference](SCRIPT_REFERENCE.md), [installation](INSTALLATION.md), [native integration](INTEGRATION.md)
- [Architecture](ARCHITECTURE.md), [note types and use cases](NOTE_TYPES.md), [writing notes](WRITING.md), [recovery](RECOVERY.md), [maintenance](MAINTENANCE.md), [tagging](TAGGING.md)
- [Obsidian](OBSIDIAN.md), [notebooks](NOTEBOOKS.md), [validation](VALIDATION.md)
