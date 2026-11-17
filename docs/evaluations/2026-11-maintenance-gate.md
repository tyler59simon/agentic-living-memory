# Maintenance dry-run gate (MAINT-1F), 2026-11-14

The gate from the handoff (`alm-automatic-note-maintenance-plan.md` sections 4.7 and 5.2): one real reviewer dry run on a labelled fixture store and one on a copy of the real store, before `maintenance enable`. Reviewer: Claude Sonnet through the installed `claude` CLI, default limits (10 notes, 300 s, 1 USD). Script: `scripts/eval_maintenance.py`.

Result: **passed on both stores.** Eleven paid gate and diagnostic runs cost 0.594 USD in all, plus 0.077 USD for the first live run.

## Failure found and fixed

The first fixture run failed with `invalid_report`: the decision for one note carried a revision that was not in the batch. The ref matched a candidate, so the model had miscopied that note's 64-character revision hash, and the validator then rejected the whole report. A miscopied hash showed up in 4 of the 6 runs (about 1 note in 50).

Fix (user decision, 2026-11-14): a decision whose ref is in the batch but whose revision does not match becomes a defer for that note only, with reason code `revision_mismatch` and no seven-day hold. An unknown ref, run id or evidence id still rejects the whole report.

A second defect showed up while fixing it: the report is validated three times (`maintenance_agent.review()`, `maintenance.run()` and `maintenance.apply()`), so the new `code` field failed the second pass (`no unknown keys`). The validator is now idempotent: it accepts `code` only as `revision_mismatch` on a defer and carries it through, and it runs every structural check (evidence ids, patches, rule) before turning a mismatch into a defer, so a miscopied revision never hides another contract break. The Codex review of the first fix found the `apply()` pass and the skipped checks; a live (non-dry) run test now covers both.

The same review tightened the gate script: the `changed_source` case counts only when its patches apply and turn "30 seconds" into "60 seconds", and `copy` mode sets the copy's configured domains to the requested one.

## Follow-up: one key per candidate (2026-11-14)

The first live run (20261114T182521-bb89b590: 10 reviewed, 7 kept, 3 deferred, nothing retired, 0.077 USD) had two `revision_mismatch` defers, and one note (`sandbox:1ea15070`) mismatched in every run on the real store. A diagnostic dry run on a copy (0.078 USD) kept the raw report: Sonnet's decision for that note was correct, but it wrote the revision of the note before it in the packet. The miscopies are slips between neighbouring candidates, not only typos, so a shorter revision would not stop them.

Fix (user decision, 2026-11-14): each packet block carries one key (`C1`, `C2`, ...) in place of its ref and revision, and the report names candidates by that key. The validator maps a key to the ref and the batch revision, so a ref and a revision can no longer disagree. The ref-and-revision form stays accepted for internal callers and for normalising twice.

With keys, both gates pass again (runs below); `sandbox:1ea15070` got a real decision (keep) for the first time.

A restore check on a copy of the real store passed: maintenance (with a scripted reviewer) archived a real note with a full retirement record, `restore` brought it back active and pinned, a second restore was refused, and the pinned note was no longer offered for archiving. The live store has no archived notes yet, and none of the archivable candidates had inbound links, so link rewriting on real notes is still covered only by the unit tests.

## Runs

| Run | Store | Result | Cost (USD) |
| --- | --- | --- | --- |
| 20261114T172807-69305ffd | fixture | invalid_report (miscopied revision) | 0.042 |
| 20261114T173114-cced8055 | fixture | invalid_report (miscopied revision, then double validation) | 0.035 |
| 20261114T173145-e17290c4 | fixture | dry_run, every check passed | 0.036 |
| 20261114T173230-fc24d0fb | copy | invalid_report (double validation) | 0.080 |
| 20261114T173430-ef312d77 | copy | dry_run, every check passed (one `revision_mismatch` defer) | 0.081 |
| 20261114T173500-1f300511 | fixture | dry_run, every check passed | 0.034 |
| 20261114T175729-27edd149 | fixture | dry_run, every check passed (after the Codex fixes) | 0.032 |
| 20261114T175752-953d18cb | copy | dry_run, every check passed (after the Codex fixes): 9 defer, 1 update | 0.078 |
| (diagnostic) | copy | dry_run with the raw report kept: the mismatch was the previous candidate's revision | 0.078 |
| 20261114T191608-5e28561d | fixture | dry_run with candidate keys, every check passed | 0.033 |
| 20261114T191620-c191a23c | copy | dry_run with candidate keys, every check passed: 9 keep, 1 update, no mismatch defers | 0.066 |

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
- The copied store's first ten candidates are all stale code and doc digests. With excerpts of at most 1200 characters the reviewer cannot confirm a whole digest, so it keeps or defers them (two runs: 8 keep, then 9 defer), and each defer holds that note for seven days. The first live days on sandbox will therefore make few changes until those digests are worked through.
- The copied-store batch had only keeps, defers and one update, so the first live runs on sandbox are the first real test of archive and supersede on real notes. Check `maintenance history` after each and use `restore REF` for a wrong archive.

## Follow-up: whole files and deleted digest sources (MAINT-DIGEST, 2026-11-17)

The open point above came true. In the first three live runs on sandbox (2026-11-14 to 11-16), 22 of 30 decisions were defer, 8 keep, and nothing was retired or updated. Every candidate was a digest of one whole file, the reviewer deferred because the 1200-character excerpt showed only the file header, and the digests of `core/learning.py` and `LEARNING.md` (deleted when learn was retired) were deferred because a missing source was not proof.

Change: a cited file of at most 16000 characters (`FILE_EXCERPT_CHARS`) is sent whole (`whole: true`); a larger one is sent as an outline of its definitions and headings plus the 1200-character window. Each candidate carries `digest: true` when it has `sources`. The prompt now lets the reviewer archive a digest whose source is `missing` (the file is gone where its repo or folder is known) with `obsolete_source_claim`; `unavailable` and unreadable sources still defer, and the executor still re-checks that the file is gone before applying.

Gate runs on 2026-11-17 with the change:

| Run | Result | Cost |
| --- | --- | --- |
| Fixture | all 12 checks pass; 1 archive, 1 resolve, 1 update, 2 supersede, 1 distinct, 3 keep, 1 defer | 0.037 USD |
| Copied store (sandbox) | 1 archive, 5 confirm, 1 update, 3 defer; packet 84 KB of 128 KB, 0 dropped | 0.167 USD |

On the copied store the archive is the `LEARNING.md` digest (source deleted). The five confirms cite whole files (OBSIDIAN.md, RECOVERY.md, `core/changes.py`, `core/logs.py`, `core/style.py`), and the update corrects the `core/validate.py` digest's claim about reconcile. Two of the three defers are the files over the cap (`core/digests.py` at 18747 and `core/session_start.py` at 23301 characters), which still get only an outline and a window; the third is the `core/commands.py` digest, which mentions learned procedures that are no longer in the file. A run now costs about 0.17 USD instead of 0.07 USD, within the 1 USD daily budget.
