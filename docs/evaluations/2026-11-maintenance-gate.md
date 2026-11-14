# Maintenance dry-run gate (MAINT-1F), 2026-11-14

The gate from the handoff (`alm-automatic-note-maintenance-plan.md` sections 4.7 and 5.2): one real reviewer dry run on a labelled fixture store and one on a copy of the real store, before `maintenance enable`. Reviewer: Claude Sonnet through the installed `claude` CLI, default limits (10 notes, 300 s, 1 USD). Script: `scripts/eval_maintenance.py`.

Result: **passed on both stores after one fix.** Six paid runs cost 0.308 USD in all.

## Failure found and fixed

The first fixture run failed with `invalid_report`: the decision for one note carried a revision that was not in the batch. The ref matched a candidate, so the model had miscopied that note's 64-character revision hash, and the validator then rejected the whole report. A miscopied hash showed up in 4 of the 6 runs (about 1 note in 50).

Fix (user decision, 2026-11-14): a decision whose ref is in the batch but whose revision does not match becomes a defer for that note only, with reason code `revision_mismatch` and no seven-day hold. An unknown ref, run id or evidence id still rejects the whole report.

A second defect showed up while fixing it: `maintenance_agent.review()` returned the normalised report and `maintenance.run()` validated it again, so the new `code` field failed the second pass (`no unknown keys`). `review()` now validates but returns the report as the model sent it, and `run()` normalises once.

## Runs

| Run | Store | Result | Cost (USD) |
| --- | --- | --- | --- |
| 20261114T172807-69305ffd | fixture | invalid_report (miscopied revision) | 0.042 |
| 20261114T173114-cced8055 | fixture | invalid_report (miscopied revision, then double validation) | 0.035 |
| 20261114T173145-e17290c4 | fixture | dry_run, every check passed | 0.036 |
| 20261114T173230-fc24d0fb | copy | invalid_report (double validation) | 0.080 |
| 20261114T173430-ef312d77 | copy | dry_run, every check passed (one `revision_mismatch` defer) | 0.081 |
| 20261114T173500-1f300511 | fixture | dry_run, every check passed | 0.034 |

## Fixture decisions (final run)

| Case | Expected | Decision |
| --- | --- | --- |
| Expired status note, sprint ended | archive | archive, expired_status |
| Plan with every item checked | resolve | resolve |
| Timeout claim, source changed from 30 to 60 | update | update: "30 seconds" to "60 seconds", citing the file |
| Old decision that still holds | keep or confirm | keep |
| Fixed bug that keeps a caveat | not resolved | keep |
| Pinned note | not retired | defer (no evidence for the claim) |
| Cited source file deleted | not retired | defer ("missing alone does not show it was removed") |
| Duplicate pair | at most one retired | one kept, the other superseded by it |

Checks: no protected or valuable note retired, every retirement allowed by policy and citing evidence, the three positive cases handled, notes, configuration and source files unchanged, accounting matches the run.

## Copied store (sandbox, final run)

Ten candidates, all stale code and doc digests. Decisions: 8 keep, 1 update, 1 defer (`revision_mismatch`). The update adds `pinned` to the list of updatable fields in the `core/notes.py` digest, which matches `UPDATABLE` in `core/notes.py`. The keeps say the excerpts agree with the notes and decline `confirm` because the excerpts do not cover every fact. No retirements were proposed. The copy's notes and the real store's maintenance files were unchanged.

## Open points

- A model that miscopies hashes this often wastes part of each batch. If `revision_mismatch` defers stay frequent in live runs, a short revision token in the packet would remove most of them.
- The copied-store batch had only keeps and one update, so the first live runs on sandbox are the first real test of archive and supersede on real notes. Check `maintenance history` after each and use `restore REF` for a wrong archive.
