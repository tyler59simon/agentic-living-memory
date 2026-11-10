# Reviewed procedural learning (pilot)

Learning is available in every domain by default. It records only explicit agent submissions, not transcripts or every tool call. There is no model service, automatic promotion, or outcome-based ranking change. Explicit preference capture keeps its existing workflow.

Use `version` to check `capabilities.learning == 1`. All commands below accept the usual `--root`, `--domain`, `--cwd` and actor options. Structured writes take JSON on stdin or `--payload-file`. Reads (`list`, `read`, `evaluate`, `status`) support viewer/subagent actors; writes do not. Each reply is JSON. Hivemind uses the same commands through `serve --stdio`.

## One complete loop

1. `learn task create`: submit `{objective, family, acceptance_criteria: [string], project?: slug, idempotency_key?: string}`. Keep objectives bounded. A family describes comparable work, not all engineering work.
2. `learn attempt start`: submit `{task_id, retry_of?: attempt_id, source_origin_id?: string, environment?: object}`. Preserve the source ID when importing a copy; retries retain one attempt-family identity.
3. `learn evidence add`: submit `{attempt_id, criterion, result, method, observer, excerpt, limitations, source?}`. Criterion must exactly match a task acceptance criterion. Result is `pass`, `fail`, or `unknown`. Method is `tool_check`, `user_acceptance`, `external_check`, or `agent_assertion`. State what a check did not cover.
4. `learn attempt close ID`: submit `{expected_revision, outcome, reason?}`. Outcomes are `succeeded`, `partial`, `failed`, `abandoned`, `unknown`. A closed success is not automatically verified.
5. `learn candidate create`: submit the candidate below. Capture failed/contradicting evidence too. Do not count copies or retries as independent experience.
6. `learn candidate evaluate ID`: inspect eligibility, errors, provisional warnings and title conflicts. This checks evidence structure/coverage; it cannot prove a procedure caused success.
7. Review the candidate in **Hivemind → Review → Learning**, or run `learn candidate promote-plan ID` with `{expected_revision}`. Inspect the resulting plan and evidence before explicitly applying its exact digest with the existing `apply` command. Agents must not auto-apply it.
8. On a subsequent task, record `learn use report` with `{attempt_id, procedure_id, version, usage, deviations?}`. Usage is `selected`, `used`, `partially_used`, `ignored`, or `inapplicable`. Reading a note is not use.
9. After closing that attempt, `learn feedback add` with `{use_id, contribution?, evidence_ids?, reason?, corrects?}`. Contribution defaults to `unknown`; a non-unknown claim requires evidence from that attempt and a reason. Task outcome and contribution remain separate.

Create operations accept `idempotency_key`; replaying the same payload returns the original record. An expected revision is the record's returned byte hash, not a timestamp. Re-read after mutations. Record references are full returned IDs within the explicit domain.

```json
{
  "content": {
    "title": "Repair a stale disposable index",
    "body": "Rebuild the index when canonical notes are valid but search is stale.",
    "use_when": ["Canonical notes are readable and the index is stale"],
    "do_not_use_when": ["Canonical notes themselves are corrupt"],
    "steps": ["Rebuild the domain index, then check the expected search result"],
    "done_when": ["The expected note is returned and the index is fresh"],
    "fallback": "Inspect canonical notes and doctor output; do not delete source notes"
  },
  "project": null,
  "evidence_ids": ["RETURNED_EVIDENCE_ID"],
  "step_evidence": [["RETURNED_EVIDENCE_ID"]],
  "contradictions": []
}
```

Each step must cite supporting evidence. At least one successful attempt must cover every acceptance criterion with non-assertion evidence. The reviewer still judges whether the evidence supports the actual steps and applicability. One supporting task produces a **provisional** procedure, not an established fact or causal confidence score.

## Inspection and control

- `learn ENTITY list [--all-domains]` and `learn ENTITY read ID`: inspect tasks, attempts, evidence, candidates, procedures, versions, uses or feedback. ENTITY names are singular. Version IDs are `PROCEDURE_ID.NUMBER`.
- `learn candidate update ID`: submit the complete candidate fields plus `expected_revision`. `reject` and `defer` need only the expected revision. Admitted/rejected candidates are finalized; create a new candidate for a procedure revision.
- `learn procedure suspend ID` / `resume ID`: expected revision required. Resume rechecks evidence. Suspended/disabled procedures are excluded from normal search and automatic context; history remains inspectable.
- `learn procedure rollback-plan ID`: `{expected_revision, version}`. Rechecks the old evidence and republishes as a new version after review; never rewrites historical versions.
- `learn evidence correct ID`: full replacement evidence plus `expected_revision`; suspends dependent current procedures. `delete-plan` creates an approved deletion plan and suspends affected procedures. Historical unavailable evidence references remain visible. Journals/backups may retain old data under their existing retention policies; deletion is not secure erasure.
- `learn config status`: effective/global/domain settings. `learn config set`: `{enabled: false}` disables the current domain; `{enabled: false, global: true}` disables learning globally. Re-enable with true. Controls are also in Hivemind.

Changing evidence/configuration/candidate/target versions between review and apply invalidates approval. Inspect the fresh proposal again; do not automatically refresh and apply. Approval authorizes this publication only, not future external actions in a runbook.

## Editing and storage contract

Canonical records live in `domains/DOMAIN/learning/{tasks,attempts,evidence,candidates,procedures,versions,uses,feedback}/ID.json`. Global/domain controls live in `learning.json`. These are durable, journaled records, not disposable indexes or 30-day receipts. Only the CLI writes them. Backups and store relocation include them.

Approved procedure versions are immutable. Their Markdown runbook is a readable projection. Ordinary title/body edits create candidates and preserve the approved version. Obsidian sync stages edits the same way. If a canonical file was edited externally, reads/search serve the approved snapshot; `doctor` reports the discrepancy. Use `learn procedure capture-edit ID` with `{expected_revision}` to stage that edit and restore the projection. A malformed external file requires repair from backup/approved history.

Evidence excerpts are limited to 8 KiB each and 64 KiB per attempt. Oversized submissions are rejected, not silently truncated. Credential-looking content is rejected before writes; remove/redact sensitive material before submission. Heuristic credential checks do not guarantee all confidential data is detected. Unreferenced records are not automatically pruned in this pilot.

Default `export` includes readable notes without learning authority metadata or evidence. Explicit `export --include-learning` includes procedure history and evidence dependencies. `import-plan` retains those dependencies as inert provenance in a new candidate; imported evidence does not confer local verification, and new local evidence/review is required before admission. Keep client material within its authorized domain; no automatic global promotion exists.

## Evaluation and subsequent releases

Run `scripts/eval_learning.py --self-test` for isolated maintenance scenarios and verifier checks. To evaluate a real agent, provide its argv as `--command-json '[...]'`; each trial receives JSON on stdin with its task, isolated store, allowed repair interface and reporting contract. It must return JSON with outcome, reported usage and optional token counts. The harness compares baseline, manual-runbook and learned-runbook conditions on separate stores and reports checked success, time and reported token costs. It never launches a model by default. The scripted self-test validates fixtures, not learning efficacy.

Before enabling broader automation, compare repeatable held-out tasks against equally curated manual runbooks and inspect misapplication/review effort. Later releases cover purpose-scoped style profiles, opt-in edit-derived preference proposals, consolidation/source-refresh review and passive harness adapters. None of those is silently enabled by this pilot.
