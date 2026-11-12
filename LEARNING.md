# Reviewed procedural learning

Agents record verified work, propose procedures from it, and the user approves what becomes a published runbook. Learning is on in every domain by default. It records only what agents explicitly submit: no transcripts, no tool-call capture, no model service, no automatic admission and no outcome-based ranking. Explicit preference capture keeps its existing workflow.

`version` advertises `capabilities.learning == 1`. Every `learn` command accepts the usual `--root`, `--domain`, `--cwd` and `--actor` options and replies with JSON. `CMD learn` with no arguments prints a short guide. Hivemind uses the same commands through `serve --stdio`.

## When agents use it

After a checked fix or workflow that is likely to recur in this environment (an install quirk, a repair, a release step), record it and propose a procedure. One-off tasks and generic knowledge a model already has are not worth a procedure.

```text
CMD learn record --objective 'Repair a stale index' --family 'alm.index' --criterion 'expected note returned' \
    --result pass --method tool_check --observer 'search' --excerpt 'search returned the note' \
    --limitations 'one store checked' --outcome succeeded
CMD learn candidate create --title 'Repair a stale disposable index' --summary 'Rebuild the index when notes are valid but search is stale.' \
    --use-when 'Canonical notes are readable and search is stale' \
    --do-not-use-when 'The canonical note file is missing or unreadable: restore or repair it instead' \
    --step 'Rebuild the domain index' --step 'Search for the expected note' \
    --done-when 'The expected note is returned' --fallback 'If the note file is missing, restore it from a backup; never delete canonical notes' --evidence EVIDENCE_ID
CMD learn candidate evaluate CANDIDATE_ID
```

"Do not use when" names the cases where the steps would be wrong: a lookalike symptom with a different cause, or a state where a step does harm. Make the fallback cover them. A candidate without one is not eligible; the create reply says so, and `learn candidate update ID --expected-revision REV --do-not-use-when '...'` adds it.

`learn record` creates the task, a closed attempt and its evidence in one transaction. With flags it records one check per `--criterion`; give several checks with a JSON payload (`evidence: [...]`). Its reply names the evidence IDs and the next command. The user then approves in **Hivemind → Review → Learning**, or applies `learn candidate promote-plan ID --expected-revision REV` with the existing `apply`. Agents never apply admission plans.

When search, read or prompt context returns a learned procedure, the hit names it and the command to report use: `CMD learn use report --procedure ID --usage used|partially_used|ignored|inapplicable`, then `CMD learn feedback add --use ID --outcome succeeded|partial|failed`. Reading a note is not use.

In Claude Code, exact `learn` invocations are auto-allowed for the main agent except `learn config set`, which stays the user's call. Subagents may only read (`list`, `read`, `candidate evaluate`, `config status`, the guide); their learning writes are denied.

## Records and the full loop

The flag forms map onto JSON payloads; `--payload-file FILE`, or stdin when no flag is given, takes the full JSON. Flags override payload keys. Record references accept a full ID or a unique 8+ character prefix; version IDs are `PROCEDURE_ID.NUMBER`.

1. `learn task create`: `{objective, family, acceptance_criteria: [string], project?: slug, idempotency_key?}`. Keep objectives bounded; a family groups comparable work.
2. `learn attempt start`: `{task_id, retry_of?, source_origin_id?, environment?: object}`. Retries keep one attempt family; preserve the source ID when importing a copy.
3. `learn evidence add`: `{attempt_id, criterion, result, method, observer, excerpt, limitations, source?}`. Criterion matches a task criterion exactly. Result is `pass`, `fail` or `unknown`; method is `tool_check`, `user_acceptance`, `external_check` or `agent_assertion`. State what the check did not cover.
4. `learn attempt close ID`: `{expected_revision, outcome, reason?}`. Outcomes are `succeeded`, `partial`, `failed`, `abandoned`, `unknown`. A closed success is not automatically verified.
5. `learn candidate create`: `{content: {title, body, use_when, do_not_use_when, steps, done_when, fallback}, evidence_ids, step_evidence?, project?, tags?, contradictions?}`. `step_evidence` holds one evidence list per step; omitted, every step cites all the evidence and evaluation warns the reviewer to confirm it. `tags` adds registered tags (platform, technology, system) to the runbook. Record failed or contradicting evidence too; copies and retries are not independent experience.
6. `learn candidate evaluate ID`: eligibility, errors, warnings and title conflicts. It checks evidence structure and coverage; it cannot prove a procedure caused success.
7. Review and approval as above. `learn candidate update ID` takes `expected_revision` plus the fields to change (content fields not given keep their values); `reject` and `defer` take `expected_revision` and an optional `reason`.
8. `learn use report`: `{procedure_id, version?, usage, deviations?, attempt_id?, session?}`. Version defaults to the current one. Without an attempt, feedback states the outcome itself.
9. `learn feedback add`: `{use_id, outcome?, contribution?, evidence_ids?, reason?, corrects?}`. Contribution defaults to `unknown`; any other value needs evidence and a reason. Task outcome and contribution stay separate.

Create operations accept `idempotency_key`; replaying the same payload returns the original record, a different payload is a conflict. An expected revision is the record's returned byte hash. Re-read after mutations.

**Eligibility.** Every step cites supporting evidence, at least one successful attempt covers every acceptance criterion with non-assertion evidence, no evidence is failed, corrected or deleted, scope matches, learning is enabled for the domain, no other note has the title, and "do not use when" names at least one exclusion. Versions approved before exclusions were required still resume and roll back without one. One supporting task gives a **provisional** procedure; two or more distinct tasks give **corroborated**. Neither is a causal confidence score, and the reviewer still judges whether the evidence supports the actual steps.

## Inspection and control

- `learn ENTITY list [--all-domains]` and `learn ENTITY read ID` for task, attempt, evidence, candidate, procedure, version, use and feedback. A malformed record is listed under `problems` and does not hide the others.
- `learn procedure suspend ID` / `resume ID` (`expected_revision`, optional `reason`): suspended procedures leave search and context; history stays readable. Resume rechecks the evidence.
- `learn procedure retire-plan ID` (`expected_revision`, `reason?`): a reviewed plan that archives the runbook and retires the procedure. Retired procedures cannot be revised; propose a new one. Ordinary `archive`, `supersede` and `resolve` refuse learned runbooks and point here.
- `learn procedure rollback-plan ID` (`expected_revision`, `version`): rechecks that version's evidence and republishes it as a new version once approved. Nothing is written until the plan is applied; history is never rewritten.
- `learn procedure promote-plan ID` (`expected_revision`, `target?` default `_global`): copies the current version's evidence chain into the target domain with a draft candidate there, for review in that domain. The original stays published; retire it once the copy is approved. Top-level `promote-plan` refuses learned runbooks and points here.
- `learn evidence correct ID`: full replacement evidence plus `expected_revision`. Active procedures that depend on it are suspended; resume fails until a new candidate cites the replacement.
- `learn evidence delete-plan ID`: a reviewed plan that replaces the evidence with a tombstone (attempt link and deletion time only) and suspends dependent procedures. References stay explainable and doctor stays clean. Journals and backups keep old data until their own retention ends; deletion is not secure erasure.
- `learn task prune-plan [--days 90]`: a reviewed plan that removes tasks older than the cutoff, with their attempts and evidence, when no candidate, version, use or feedback depends on them. A plan that would remove something referenced since it was made fails as stale.
- `learn config status`; `learn config set --enable|--disable [--global]`. Disabling stops new tasks, attempts, evidence, candidates, admissions and runbook edits in that domain (or everywhere). Approved procedures stay published, and use and feedback still record; suspend a procedure to unpublish it. A pending admission plan fails while learning is disabled. Controls are also in Hivemind.

Changing evidence, configuration, the candidate or the target version between review and apply invalidates approval. Inspect the fresh proposal again; do not refresh and apply automatically. Approval authorizes this publication only, not future external actions in a runbook.

## Editing and storage contract

Canonical records live in `domains/DOMAIN/learning/{tasks,attempts,evidence,candidates,procedures,versions,uses,feedback}/ID.json`; global and domain switches in `learning.json`. These are durable, journaled records, not disposable indexes or 30-day receipts. Only the CLI writes them. Backups and store relocation include them.

Approved procedure versions are immutable, and the Markdown runbook is a readable projection: its Key Details list each field one item per line. An ordinary `update` of a learned runbook's title or body becomes a candidate and the approved version stays published (`plan REF update` previews the proposal without saving it). Obsidian edits are staged the same way, and the vault shows the approved version again with a note naming the proposal. If the canonical file was edited externally, reads and search serve the approved snapshot and `doctor` reports the difference; `learn procedure capture-edit ID` (`expected_revision`) stages that edit and restores the projection. A malformed external file needs repair from a backup or approved history.

Learned runbooks are served from their manifests. If `learning.json` is unreadable, learned runbooks are still served and only learning writes fail; if a procedure manifest is unreadable, that runbook is withheld until `doctor` or a restore repairs it. Manually written runbooks are never affected.

Evidence excerpts are limited to 8 KiB each and 64 KiB per attempt; oversized submissions are rejected, not truncated. Credential-looking content is rejected before writes; redact sensitive material before submitting. Heuristic credential checks do not catch every kind of confidential data.

Default `export` includes readable notes, suspended learned runbooks among them, without learning metadata or evidence; importing such a bundle adds the approved text as an ordinary runbook. `export --include-learning` includes procedure history and evidence. Its `import-plan` turns each procedure into a draft candidate carrying only that procedure's own evidence chain as inert provenance: imported evidence confers no local verification, and new local evidence and review are required before admission. Keep client material within its authorized domain; nothing is promoted to the global domain without review.

## Evaluation

`scripts/eval_learning.py --self-test` runs isolated maintenance scenarios (stale index, stale plan, interrupted operation, restore, and a missing-note lookalike) through baseline, manual-runbook and learned-runbook conditions, with the scripted reference repairs. It validates fixtures and verifiers, not learning efficacy.

- **Conditions.** Baseline has no runbook. Manual gets a hand-written runbook with the same fields and layout as a learned one. Learned gets the procedure the agent itself recorded and proposed in a separate training run (`--learned-from scripted` builds it from the reference repair instead). The harness admits a training candidate only when it verified the training repair and `learn candidate evaluate` finds it eligible; that stands in for reviewer approval, so read the admitted candidates in the results. No admissible candidate leaves the learned trial without a runbook, and the result says so.
- **Held-out stores.** Training and test stores use different marker terms, titles and distractor notes. The agent's task text never names the condition.
- **Lookalike.** `missing-note` looks like a stale index but the note was deleted; it is seeded with the stale-index runbook. A misapplication is reporting or recording use of that runbook while the note stays unrestored; following a runbook whose fallback leads to the restore is correct use.
- **Isolation.** Each trial store is bound to its own folder, which is the agent's working directory, and the agent runs with `LIVING_MEMORY_ROOT` set to that store. The agent's own Living Memory hooks therefore run as in real use, but only against the throwaway store. Claude Code session variables are removed so the agent is not a child of the calling session. A fingerprint of the configured real store is compared around each run (`real_store_changed`); your own sessions can change it meanwhile, so it is a warning, not proof.
- **Measures.** Checked success (verified from the store), false success claims, misapplications, collateral changes to other notes, notes the agent added, recorded `learn use report` calls against the agent's self-reported usage, time, and the tokens, cost and turns the agent reports.

To evaluate a real agent, pass its argv as `--command-json '[...]'`, with `--label NAME`, `--repeats N`, `--timeout SECONDS` (default 600), `--scenarios` / `--modes` (comma lists) and `--out FILE` (one JSON line per run, written as each finishes). `scripts/agents/run_agent.py -- AGENT ARGS [{prompt}]` adapts command-line agents to the trial contract: it builds one prompt for every condition (training runs also ask for `learn record` and `learn candidate create`), sends it on stdin when the argv has no `{prompt}` (which survives Windows `.cmd` shims), and parses `claude -p --output-format json` and `codex exec --json` output. `--summarize FILE... [--markdown]` tabulates result files per agent and condition and runs nothing. `--keep` keeps trial folders for inspection.

The harness never launches a model by default. Agent-reported usage and cost are not independently verified, and the stores are synthetic. Compare repeatable held-out tasks against equally curated manual runbooks, and inspect misapplications and review effort, before relying on learned procedures more broadly.

Later releases cover purpose-scoped writing preferences, opt-in edit-derived preference proposals, consolidation and source-refresh review, and passive harness adapters. Records already carry a proposal `kind` (currently `procedure`) so those releases extend the same review lifecycle. None of them is enabled silently.
