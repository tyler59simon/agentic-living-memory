# Learning Phase 2: real-agent evaluation (2026-11-11)

**Question.** Do learned runbooks help real agents on recurring ALM maintenance tasks, compared with no runbook (baseline) and an equally curated hand-written runbook (manual)? This is the evaluation gate in the learning roadmap.

**Answer for these tasks and low-cost models: no demonstrated benefit, and one consistent harm.**

- **The four trained repairs don't separate the conditions.** Every condition scored at or near ceiling.
- **The lookalike exposed a real gap.** Every learned runbook was followed in the lookalike case and failed there (6/6 across both agents). All eight agent-written candidates left "do not use when" empty, and `learn candidate evaluate` admitted them anyway.
- **Learned runbooks cost more.** Agents used more tokens and time with them than without any runbook.

The gate is **not passed**. See the recommendation at the end.

## Method

- **Harness:** `scripts/eval_learning.py` and `scripts/agents/run_agent.py`, as documented in LEARNING.md § Evaluation. Raw results, one JSON line per run, are in `docs/evaluations/data/claude-haiku.jsonl` and `docs/evaluations/data/codex-luna.jsonl`. The tables below come from `eval_learning.py --summarize … --markdown`.
- **Agents.** Both are low-cost models, chosen by the user.
  - **Claude Code 2.1.322** with Claude Haiku 5.5 (`claude -p --model claude-haiku-5-5`). Allowed tools: Bash, PowerShell, Read, Grep, Glob. Edit and Write were not allowed.
  - **Codex CLI 0.167.0** with `gpt-6-luna` at low reasoning effort.
- **Scenarios:**
  - Four repairs ALM maintainers actually face: stale index, stale plan, interrupted operation, restore from a backup.
  - One lookalike, `missing-note`. It reads like a stale index ("search no longer finds note X; the index may be stale"), but the note was deleted and a backup holds it. It is seeded with the stale-index runbook.
- **Conditions:**
  - **Baseline:** no runbook.
  - **Manual:** a hand-written runbook with the same fields and layout as a learned one, written before seeing any agent output.
  - **Learned:** the runbook the same agent recorded (`learn record`) and proposed (`learn candidate create`) in a separate training run. It was admitted only if the harness verified the training repair and `candidate evaluate` found it eligible.
- **Scale:** 3 repeats × 5 scenarios × 3 conditions = 45 trials per agent, plus 4 training runs. Condition order is counterbalanced across repeats. Training and held-out stores differ in marker terms, titles and distractor notes. The task text is identical across conditions.
- **Isolation:**
  - Each trial is a throwaway store bound to its own folder, which is the agent's working directory, with `LIVING_MEMORY_ROOT` pointing at it.
  - **Claude:** its Living Memory hooks ran as in normal use, but against the trial store. Hook logs in the trial stores confirm this.
  - **Codex:** `codex exec` fired no Living Memory hooks in either the trial or the real store, so Codex only saw notes through its own `search`.
  - **Codex sandbox:** its Windows `workspace-write` sandbox can't start Python, which lives under the user's AppData, so every Codex trial failed before acting. Codex therefore ran with `-s danger-full-access` (user decision).
  - **Real-store guard:** 6 of the 90 trials flagged a real-store change. All six fell inside 14:47–14:57 UTC, when other sessions captured and updated notes in `sandbox` and `_global` (checked in the real store's command log). No trial content reached the real store.

## Results

### Conditions

| agent | mode | trials | checked | false_success | misapplied | collateral | procedure_available | reported_used | recorded_use | errors | median_seconds | median_tokens | total_cost_usd | real_store_changed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku | baseline | 15 | 12 | 0 | 0/0 | 0 | 0 | 0 | 0 | 0 | 25.93 | 245185 | 0.1214 | 1 |
| claude-haiku | learned | 15 | 12 | 0 | 3/3 | 0 | 15 | 15 | 14 | 0 | 31.53 | 270669 | 0.1289 | 1 |
| claude-haiku | manual | 15 | 15 | 0 | 0/3 | 0 | 15 | 15 | 0 | 0 | 22.0 | 138505 | 0.1142 | 1 |
| codex-luna | baseline | 15 | 11 | 0 | 0/0 | 0 | 0 | 0 | 0 | 0 | 40.71 | 168313 |  | 1 |
| codex-luna | learned | 15 | 11 | 0 | 3/3 | 0 | 15 | 10 | 9 | 0 | 48.83 | 243749 |  | 2 |
| codex-luna | manual | 15 | 12 | 0 | 3/3 | 0 | 15 | 12 | 0 | 0 | 33.74 | 155202 |  | 0 |

What the columns mean:
- `misapplied` counts lookalike trials where the agent followed the seeded runbook and the note stayed missing.
- `reported_used` is the agent's own claim. `recorded_use` counts trials with a `learn use report` record; only learned runbooks can have one.
- Tokens include cached input.
- Codex reports no cost.

Totals:
- **Claude:** $0.41 and 12.1M tokens across 49 runs, about 24 minutes of agent time.
- **Codex:** 10.1M tokens, about 37 minutes.

### Checked success by scenario

| agent | scenario | baseline | manual | learned |
|---|---|---|---|---|
| claude-haiku | interrupted-operation | 3/3 | 3/3 | 3/3 |
| claude-haiku | missing-note | 0/3 | 3/3 | 0/3 |
| claude-haiku | restore | 3/3 | 3/3 | 3/3 |
| claude-haiku | stale-index | 3/3 | 3/3 | 3/3 |
| claude-haiku | stale-plan | 3/3 | 3/3 | 3/3 |
| codex-luna | interrupted-operation | 3/3 | 3/3 | 3/3 |
| codex-luna | missing-note | 0/3 | 0/3 | 0/3 |
| codex-luna | restore | 2/3 | 3/3 | 3/3 |
| codex-luna | stale-index | 3/3 | 3/3 | 3/3 |
| codex-luna | stale-plan | 3/3 | 3/3 | 2/3 |

### Training runs

All eight training runs repaired their store. Each proposed exactly one candidate, and each candidate was eligible and admitted. Every one carried the two standard warnings:
- provisional (one supporting task);
- every step cites all the evidence by default.

## Findings

1. **No benefit on the trained repairs.** Both models fixed the four known repairs without a runbook: Claude 12/12, Codex 11/12. Learned runbooks matched that and didn't beat it (12/12 and 11/12). The tasks sit at ceiling for these models, so they can't show a benefit; that needs tasks the model can't solve from general knowledge plus the CLI's own hints.
2. **Learned runbooks are applied outside their scope.** In the lookalike, both agents followed the learned stale-index runbook in every trial and never restored the note (6/6). All eight learned candidates have an empty "do not use when", and none of their fallbacks covers a missing note.
   - The manual runbook names the exclusion ("the canonical note file is missing … restore it instead") and gives a restore fallback. With it, Claude succeeded 3/3.
   - Codex followed the manual runbook too and still failed 0/3. It reported `used` but didn't act on the fallback.
   - Without any runbook, neither agent found the restore (0/6).
3. **Admission doesn't check applicability.** `learn candidate evaluate` checks evidence structure and coverage. It accepted runbooks with no exclusions at all, and the empty field did not even raise a warning.
4. **Learned runbooks cost more.** Learned trials took more time and tokens than baseline:
   - Claude: median 31.5 s vs 25.9 s; 271k vs 245k tokens.
   - Codex: median 48.8 s vs 40.7 s; 244k vs 168k tokens.

   The agent-written runbooks are long and add verification steps. The shorter manual runbooks were the cheapest condition for both agents (Claude 139k, Codex 155k).
5. **Use reporting works when the hint is seen.** Claude recorded `learn use report` in 14 of its 15 learned trials, and Codex in 9 of 15. Codex's self-reported usage (10 used) roughly matched its records.
6. **No safety regressions.** No false success claims (every failure was reported as failed or unknown), no collateral changes to other notes, no notes added, and no harness errors in 90 trials.

## Learned candidates (as admitted)

**Claude Haiku**

- *Repair stale search index after out-of-band note edit.*
  - Use when: doctor reports index dirty_or_missing, or a term present in a canonical note returns no or non-relevant results.
  - Do not use when: (none).
  - Steps: doctor; multi-term search; `rebuild`; doctor; search again; validate.
  - Fallback: stop and report if rebuild reports issues.
- *Check a pending Living Memory update plan before applying it.*
  - Steps: inspect-plan; read the target and compare revision and diff; refuse if stale; apply only if the base matches; validate and doctor.
  - Do not use when: (none).
- *Complete an interrupted operation by forward recovery.*
  - Steps: doctor and history; read the journal (read-only); `recover ID` forward, without `--abandon`; confirm committed; doctor.
  - Fallback: `--abandon` only after review.
  - Do not use when: (none).
- *Restore a deleted note from a backup zip via restore-plan.*
  - Steps: pick the pre-deletion zip; `restore-plan` (expect delete 0); inspect-plan; apply with the digest; verify with list, read, validate and doctor.
  - Fallback: don't apply a plan that deletes.
  - Do not use when: (none).

**Codex gpt-6-luna**

- *Repair a stale search index after an external note edit.*
  - Steps: search a known phrase; domain `rebuild`; search the new term.
  - Fallback: follow doctor and rebuild guidance; never edit files.
  - Do not use when: (none).
- *Refuse stale note update plans.*
  - Steps: read the note's revision; inspect the plan and compare; don't apply if it diverged; confirm the note is unchanged; doctor.
  - Fallback: a fresh plan if the change is still needed.
  - Do not use when: (none).
- *Recover interrupted memory writes.*
  - Steps: doctor and history; inspect the journal; `recover ID --direction forward`; history and doctor.
  - Fallback: don't force a diverged journal.
  - Do not use when: (none).
- *Restore a deleted canonical note from backup.*
  - Steps: confirm the note is absent; restore-plan and inspect; check for no deletions; apply with the digest; verify search, read and list.
  - Fallback: don't apply unexpected plans.
  - Do not use when: (none).

The full candidate records are in the `training` rows of the data files.

## Limitations

- **Synthetic stores, small samples, low-cost models only.** There are 3 repeats per cell and no significance is claimed. Results may not hold for stronger models or real stores.
- **No reviewer.** Admission was decided by the eligibility check, not a person. Review effort was not measured; reading the eight candidates above is the review a user would have done.
- **Codex differed from normal use in two ways:**
  - it ran without its sandbox and without Living Memory hooks;
  - it reports no cost.
- **Self-reported numbers.** Tokens, cost and usage are reported by the agents and not independently verified.
- **Easy trained tasks.** Reference answers exist in the CLI's own hints (doctor, inspect-plan), which favours the baseline.

## Recommendation for the roadmap gate

Do not treat real-agent benefit as established, and don't start the later learning releases on that basis. Two things to decide first:

1. **Make applicability part of admission.** Have `candidate evaluate` warn or error when "do not use when" is empty. Have the training guidance ask for exclusions and a fallback for lookalike causes. Re-run the lookalike to see whether that closes the 6/6 misapplication.
2. **Test where procedure knowledge matters.** Add held-out tasks a model can't solve from general knowledge: environment-specific quirks, multi-step local conventions. Optionally repeat with a stronger model. Only those can show a benefit over baseline.
