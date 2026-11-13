# Section-level units, optional sqlite-vec and the injection controller: evaluation

Measured 2026-11-12 on branch `feat/sqlite-vec-memory-retrieval`, built from `origin/main` at `c01b0827f9231f35cf412412d999fc18135956f5` (PR #40). Windows 11 Home 10.0.26100, Intel i7-9750H, 16 GB, Python 3.15.2, SQLite 3.53.4, sqlite-vec 0.1.9, model potion-retrieval-32M (512 dimensions, read from the file). All fixtures are synthetic; no private note text is in this folder.

## What changed

- `core/units.py`: notes are cut into units (overview, caveat, one per section) with roles and file line spans.
- `core/vectors.py`: the derived per-domain unit index `vectors.sqlite3`, its incremental update, and the optional sqlite-vec backend with Python fallback.
- `core/search.py`: the semantic channel reads the unit index (best unit per note, independent discovery), falls back to note-level vectors, and reports `backend` and `backendReason`.
- `core/injection.py` and `core/runtime.py`: pointer, evidence or expanded delivery per note, acknowledgments, follow-ups, per-section dedupe and one 4,000-character budget.
- `core/sources.py`: explicit questions also get source chunks found by meaning alone.
- CLI: `semantic native on|off`, `semantic rebuild`, `search --explain`, `read REF --section HEADING`; `semantic status` and `doctor` report the backend.

## Variants

| Config | Semantic channel | Delivery |
|---|---|---|
| A | none (FTS5 and gates) | one line per note |
| B | note-level vectors in `search.sqlite3`, Python (before this change) | one line per note |
| C | the same vectors through sqlite-vec | one line per note |
| P | every section embedded, Python | one line per note |
| D | every section embedded, sqlite-vec | one line per note |
| E | D | injection controller |

## Results, `tests/fixtures/eval_units.json` (measured)

27 positive and 5 negative single prompts over 12 notes plus 60 seeded distractors; 4 sessions of 13 prompts.

| Config | recall@1 | injected hit | false injection | answer in hook text | answered with one read | polarity errors | est. tokens per answered | prompt p50 / p95 ms | session steps correct |
|---|---|---|---|---|---|---|---|---|---|
| A | 1.0 | 0.704 | 0.0 | 0.370 | 0.704 | 0 | 182 | 16.8 / 19.0 | 0.615 |
| B | 1.0 | 0.778 | 0.0 | 0.444 | 0.778 | 0 | 165 | 16.7 / 20.1 | 0.615 |
| C | 1.0 | 0.778 | 0.0 | 0.444 | 0.778 | 0 | 165 | 20.6 / 23.8 | 0.615 |
| P | 1.0 | 0.852 | 0.2 | 0.481 | 0.852 | 0 | 157 | 21.8 / 24.7 | 0.615 |
| D | 1.0 | 0.852 | 0.2 | 0.481 | 0.852 | 0 | 157 | 19.8 / 22.9 | 0.615 |
| E | 1.0 | 0.852 | 0.2 | 0.778 | 0.852 | 0 | 110 | 19.9 / 30.2 | 1.000 |

Reading it:

- B to C and P to D change nothing but speed: native and Python rank the same embeddings identically.
- Section units (B to D) found 2 of 8 buried paraphrases that note-level vectors missed, and let one negative through: a short distractor section full of the word "autumn" scored 0.56 against "write a haiku about autumn leaves". Short sections give peakier similarities; this is a real cost of units, not hidden by a tuned threshold.
- The controller (D to E) is where most of the measured gain is: the answer is in the injected text for 0.778 of prompts instead of 0.481, and the estimated cost per answered prompt, counting the follow-up read, falls from 157 to 110 tokens. Sessions: "why?", "what were the alternatives?", a revised note and new questions on the same topic are answered; acknowledgments and repeats inject nothing.
- Rank metrics saturate (1.0 everywhere): the distractors share no vocabulary with the targets, so this fixture cannot separate the variants by rank. A harder fixture with near-miss notes is future work.
- Misses common to every variant: "are Japanese depot clock readings adjusted before comparing" (section similarity 0.37, under the 0.45 gate), and three buried paraphrases with too few shared words and similarity under 0.45.
- Tokens are characters divided by 4, an estimate. Answerability is a phrase check, not an agent's task outcome.

## Results, `tests/fixtures/eval_synthetic.json` (measured, regression check)

Every variant: retrieval hit@3 1.0, false injection 0.0, 265 mean injected characters; long prompts recall 1.0 and false injection 0.0; quizzes recall 1.0; sources ask hit@3 1.0, prompt recall 1.0, false injection 0.0; session start 2,860 characters. No regression.

## Backend parity and speed

- Same embeddings, both backends, on a copy of an 87-note store (490 units, 3 domains): 24 of 24 queries gave identical top-20 note lists, largest score difference 5.8e-7 (measured).
- SYNTHETIC random 512-dimension vectors (`scripts/benchmark.py --vectors N`):

| Vectors | Python p50 | sqlite-vec p50 | Python build | vec0 build | File, blobs only | File with vec0 | Identical top-20 |
|---|---|---|---|---|---|---|---|
| 1,000 | 33 ms | 2.8 ms | 0.24 s | 0.05 s | 4.1 MB | 6.3 MB | 20/20 |
| 10,000 | 334 ms | 25 ms | 2.5 s | 0.51 s | 41 MB | 62 MB | 20/20 |
| 100,000 | 3,406 ms | 226 ms | 25 s | 5.1 s | 411 MB | 619 MB | 5/5 |

- Extension load: about 0.5 ms per connection (0.9 to 1.7 ms the first time).
- Real prompt hook as a fresh process, store copy above, prompt of 65 characters, 10 runs (`--hook-store`): p50 399 ms native, 417 ms Python; the code before this change measured 389 to 401 ms on a fresh copy of the same store. Process start and imports dominate; the vector backend is a small share at this size. Context for that prompt: 3,417 characters now, 3,145 before (section evidence lines are longer than pointers).

## Tests

- Baseline before any change: 1,085 tests, OK (1 skipped), 468 s (`baseline/unittest.txt`).
- After, including the review fixes: 1,139 tests OK (1 skipped) with sqlite-vec loaded, and 1,139 OK (6 skipped: the 5 native tests and the existing skip) with `LIVING_MEMORY_NATIVE=0` (`results/unittest-final.txt`, `results/unittest-final-native-off.txt`); about 555 s each, the two run side by side. One more test, added after those runs for the read --section finding, passes on its own.
- Codex review (gpt-6.1-sol, extra-high reasoning, `codex review --base origin/main`): 2 P1 and 6 P2 findings, all confirmed and fixed; each has a regression test in `tests/test_injection.py` (class `ReviewFindings`) or `tests/test_vectors.py` (`test_ties_at_the_knn_cutoff_resolve_like_python`) that fails on commit 499371c and passes after. New: `tests/test_units.py`, `tests/test_vectors.py` (its `Native` class needs sqlite-vec and is skipped without it), `tests/test_injection.py`, one source test. Adapted: `tests/test_index_v3.py` (note-level embed counts exclude the unit index), `tests/scenarios/retrieval-gate.json` (an explicit "remind me" now delivers again, per the task; a repeat without it is still suppressed).
- Hook smoke test (Claude and Codex adapters, native on, off, and a store with no model): valid JSON, 2,495 to 2,877 characters.
- Not run: macOS and Linux, a Python built without extension loading (covered by a mocked `load_unsupported` path only), an endpoint policy that blocks DLL loading (covered by a mocked `load_blocked` path).

## Files

`results/units-*.json`, `results/synthetic-*.json`, `results/vectors-*.json`, `results/hook-store-copy.json`, `baseline/` (the code before the change). Reproduce with the commands in VALIDATION.md.
