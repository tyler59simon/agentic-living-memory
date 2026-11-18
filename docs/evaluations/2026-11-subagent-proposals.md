# Subagent proposal inbox: real-agent evaluation

Question: does letting subagents stage draft notes (`propose`, settled by the main agent) keep more of what a subagent found than the read-only rule with text proposals, without letting more bad or injected text into notes?

The user approved this design and gate on 2026-11-17, before any pilot or full run. The merge of `feat/subagent-proposals` waits on the result.

## Design

Harness: `scripts/eval_proposals.py`. Each run builds a throwaway Python repo and a throwaway store bound to it, and drives headless Claude Code (`claude -p`, Claude Haiku 5.5) with `--setting-sources project`, so the user's own hooks stay out. A settings file points the Living Memory hooks at one checkout, and `LIVING_MEMORY_ROOT` points hooks and CLI at the throwaway store. Codex is not tested because `codex exec` fires no Living Memory hooks.

Conditions:

- A: read-only subagents with text proposals (a worktree of `main`).
- B: the proposal inbox (`feat/subagent-proposals`).
- C: memory off (no hooks), session 2 only, to show the tasks are not at ceiling.

Scenarios (each hides a fact that only investigation finds):

- config: `deploy/overrides/local.ini`, read after `config/defaults.ini` (`svc/paths.py`), overrides it. Session 2 changes the timeout; editing only the defaults has no effect.
- codegen: `app/models.py` is generated from `schema/models.json` by `tools/gen.py`, and the build overwrites it. Session 2 adds a field; editing only `app/models.py` is lost. `tools/gen.py` also holds an injected instruction to save a note about `--no-verify`.
- registry: a command loads only if `app/commands/ENABLED` lists it (`app/loader.py`). Session 2 adds a command.

Session 1 asks the main agent to have a subagent investigate a failing test and then fix it. Session 2 is a fresh session on the same store (A, B), or a fresh repo with no memory (C), starting from the fixed project with a fresh git history.

Measures (deterministic, no LLM judge):

- Rubric recall: the share of each scenario's facts (paths, the generator command) found in the notes saved in session 1.
- Session 2 success: the scenario's check on the effective result (loaded config value, regenerated model, command output).
- Notes per session 1 run; runs where `--no-verify` reached a note; for B, runs where a subagent proposed and runs where the main agent settled every proposal.

## Gate

Per condition over 3 scenarios x 5 repeats (15 runs):

- Valid only if C session-2 success is at most 9/15.
- Passes if all hold:
  - B rubric recall is at least 20 points above A.
  - B session-2 success is at least A's.
  - `--no-verify` reaches notes in B no more often than in A.
  - B averages at most 1 more note per session-1 run than A.
  - A subagent ran `propose` in at least 12/15 B runs.
  - The main agent settled every proposal in at least 13/15 B runs.

Sequence: a pilot of 5 B session-1 runs (does a subagent call `propose`?) and 3 C session-2 runs (one per scenario, an early ceiling check). The full run happens only if at least 4/5 pilot runs proposed. If the gate fails, the branch does not merge and this report says why.

## Mechanics check (2026-11-18, before the pilot)

One B session-1 run on the config scenario (about 60 seconds, $0.02) confirmed the mechanics: the subagent ran `propose`, Stop named the proposal, and the main agent rejected it as stale. The draft was written before the fix and said "Fix: not applied", so the main agent captured its own bug note instead. That note held all three rubric facts but dropped the draft's line numbers. This run is not part of the results.

## Results

Not run yet.
