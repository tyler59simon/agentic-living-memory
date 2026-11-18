# PERF-2: memory per entry point at 1000 notes (2026-11-18)

Measurement only. No production code was changed. The user will decide what to cut.

## Setup

- Store: 1000 synthetic notes from `scripts/benchmark.py` `synthetic_store` (the builder behind `--scale 1000`), one domain `scale`, indexed once (first index build 17.9 s). The `potion-retrieval-32M` model (126 MB) was copied in read only from the installed store's `.models`, so semantic search and native sqlite-vec were on (2000 units, 15 MB vector file, 8.8 MB search index). `benchmark.py --scale` deletes its store when it finishes, so the script builds the same store once and keeps it.
- Every run is its own process on a fresh copy of that store (`.models` shared by a junction). Hooks and CLI use the same command lines the harness uses (`hooks/claude_hooks.py user-prompt|session-start`, `living_memory.py --json search ...`).
- Peaks come from `GetProcessMemoryInfo` on the child's process handle after it exits: peak working set (RAM resident, includes touched pages of the memory-mapped model) and peak commit (`PeakPagefileUsage`, the private bytes the process asked for). tracemalloc peaks come from separate runs of the same command under a wrapper (Python allocations only; sqlite, mmap and interpreter internals are not seen).
- Top allocation sites come from the highest tracemalloc snapshot taken near the peak (a watcher thread snapshots whenever traced memory grows 10 percent), so they describe memory near the peak, not at exit.
- 3 runs per entry and per mode; cells are median (min-max). Python 3.15.2, Windows 11.
- Timing caveat: other test suites were running on the machine (CPU load about 70 percent when checked), so wall times are inflated and noisy. Per-process peak memory is not affected much (first and later runs on one copy gave the same peaks to 0.2 MB). The first run after a store copy also pays for a cold file cache (3 to 4 s against under 1.2 s for the second run), so both are shown.
- Repeat with: `py -3.15 scripts/measure_memory.py build --out DIR --model-from STORE`, then `run` and `diary` (see the script docstring).

## Results per entry point

| entry | peak working set MB | peak commit MB | tracemalloc peak MB | wall s, first run | wall s, second run |
|---|---|---|---|---|---|
| bare interpreter (`-c pass`) | 11.0 (11.0-11.1) | 5.5 (5.5-5.6) | 0.3 | 0.03 | n/a |
| baseline: `import core.runtime` | 20.8 (20.7-20.8) | 11.6 (11.6-11.6) | 2.4 | 0.12 | n/a |
| baseline: `import core.cli` | 19.0 (18.8-19.0) | 11.1 (10.9-11.1) | 1.6 | 0.11 | n/a |
| hook session-start (Claude) | 41.3 (41.2-41.3) | 30.6 (30.5-30.6) | 15.6 (15.6-15.6) | 3.42 (3.33-4.07) | 1.15 (1.14-1.18) |
| hook user-prompt (Claude, a prompt that injects 3 notes) | 50.7 (50.7-51.0) | 40.5 (40.4-40.7) | 21.0 (21.0-21.0) | 3.03 (2.96-3.06) | 0.73 (0.72-0.74) |
| CLI `search` | 52.7 (52.4-52.8) | 42.8 (42.2-42.8) | 21.3 (21.3-21.3) | 3.07 (2.96-3.19) | 0.85 (0.85-0.89) |
| `semantic rebuild` | 77.2 (77.0-77.4) | 61.0 (60.9-61.2) | 40.3 (40.3-40.3) | 11.12 (11.10-11.16) | 9.31 (9.22-9.36) |
| `serve --stdio`, 8 typical requests | 55.7 (55.6-55.9) | 46.5 (46.4-46.5) | 24.8 (24.8-25.3) | 4.16 (4.14-5.46) | n/a |
| `serve --stdio`, soak of 301 requests | 63.4 (62.6-63.7) | 53.9 (53.7-54.1) | not run | 118 (118-120) | n/a |

Reading the table:

- The import floor is 11.6 MB commit. The session-start hook adds about 19 MB commit on top of it, the prompt hook about 29 MB. The extra 10 MB in the prompt hook and search is the semantic path (model open, vocabulary, vector rows).
- `semantic rebuild` is the largest short-lived entry at 61 MB commit, 77 MB working set. The 20 MB difference to the prompt hook is mostly the unpacked vocabulary row cache (16.5 MB, see below) plus the existing-vector reuse map (2.2 MB).
- The hook prompt used for these runs matches one note by title, so it injects three notes and writes 6 diary rows (3 inject, 3 surface). A prompt with no lexical match returns early and writes nothing.

### serve --stdio

Requests sent: `version`, `domains`, `list --domain scale --actor viewer`, four `search`, one `read`. Peak working set after each step (median of 3 runs, MB):

| after request | peak working set MB | request ms |
|---|---|---|
| `version` (includes interpreter start) | 23.8 | 150 |
| `domains` | 24.9 | 21 |
| `list` (1000 notes) | 33.9 | 2400 to 3600 |
| first `search` | 53.7 | 430 |
| later `search` (x3) | 54.3, 55.5, 55.7 | 350 to 400 |
| `read` | 55.5 | 77 |

The first `search` loads the model and vector index and costs about 20 MB; the process then stays flat. In the soak run (one `list`, then 300 requests: searches on random three-word queries and a `read` every 10th) the peak working set rose from 54 MB after the first search to about 62 MB at request 50, and then to 62.6, 63.7, 63.4 MB (three runs) at request 300. Growth after request 50 was under 2 MB. Hypothesis: the rise to request 50 is the row cache and parsed-note cache filling, and it is bounded. Two other things were not checked: a run of hours, and a store that changes while serve runs.

## Top allocation sites

Top 10 by size near the tracemalloc peak, median run. `<frozen importlib._bootstrap_external>:500` is module code loaded from `.pyc` files (the import floor).

### `semantic rebuild` (peak 40.3 MB traced, snapshot 37.6 MB)

| site | KB | blocks |
|---|---|---|
| `core/semantic.py:141` (row cache: `struct.unpack_from` result per vocabulary row) | 16516 | 526340 |
| `core/semantic.py:51` (`marshal.loads` of the tokenizer vocabulary) | 6788 | 124964 |
| importlib `.pyc` code objects | 3734 | 23375 |
| `core/search.py:210` (existing note-vector blobs, reuse map) | 2246 | 3003 |
| `core/note_format.py:159` (note bodies after the title strip) | 1397 | 1000 |
| `json/decoder.py:374` (frontmatter values) | 727 | 12009 |
| `core/repository.py:221` (frontmatter parse) | 558 | 12000 |
| `core/repository.py:434` (parsed-note cache) | 524 | 4001 |
| `core/vectors.py:187` | 471 | 347 |
| `importlib._bootstrap:491` | 406 | 3892 |

The row cache is at its cap here: 1024 rows x 512 floats is 16.5 MB, which matches the 17 MB the SEM-1 bound was meant to give. The unbounded version held 84 MB for 392 notes.

### `serve --stdio` (8 requests; peak 24.8 MB traced, snapshot 24.0 MB)

| site | KB | blocks |
|---|---|---|
| `core/semantic.py:51` (tokenizer vocabulary) | 6788 | 124962 |
| importlib `.pyc` code objects | 3596 | 22439 |
| `core/note_format.py:159` (note bodies) | 1397 | 1000 |
| `json/__init__.py:228` | 1083 | 19000 |
| `json/__init__.py:301` | 1015 | 18087 |
| `json/__init__.py:267` | 744 | 3011 |
| `core/repository.py:413` (`_PARSED` entries) | 740 | 15957 |
| `json/decoder.py:374` | 728 | 12032 |
| `argparse.py:1672` | 700 | 4059 |
| `core/repository.py:221` | 558 | 12000 |

The row cache does not appear: search queries embed a few words, so few rows are unpacked. In `serve`, about 3 MB (note bodies, `_PARSED`, frontmatter) belongs to the parsed-note cache that the process keeps by design.

### CLI `search` (peak 21.3 MB traced) and prompt hook (peak 21.0 MB traced)

The two are nearly identical, so one table (CLI `search`):

| site | KB | blocks |
|---|---|---|
| `core/semantic.py:51` (tokenizer vocabulary) | 6788 | 124966 |
| importlib `.pyc` code objects | 3520 | 21742 |
| `core/note_format.py:159` (note bodies) | 1397 | 1000 |
| `core/vectors.py:267` (unit blobs read from the vector index) | 1149 | 5352 |
| `json/decoder.py:374` | 728 | 12027 |
| `core/repository.py:221` | 558 | 12000 |
| `core/repository.py:434` | 523 | 4000 |
| `core/vectors.py:270` | 517 | 6657 |
| `importlib._bootstrap:491` | 399 | 3823 |
| `core/repository.py:223` | 391 | 1000 |

For the session-start hook (no model loaded) the list is led by importlib code (4.4 MB), note bodies (1.4 MB), `json/__init__.py` (0.9 and 0.8 MB), and `repository.py` (0.75 and 0.56 MB).

## PERF-1b: usage-diary reads in `core/usage.py`

### How often the hooks read the diary

Counted by wrapping `usage.events`, `usage.strength_map`, `usage.used_refs`, `usage.noisy` and `usage.capture_coverage` in a process that runs `runtime.process` (empty diary):

- SessionStart: `usage.events()` is called three times, each parsing every diary file of the last 30 days again: `usage.used_refs` (from `worklist._due`), `usage.events` (from `worklist._noisy`), and `usage.strength_map` (from `session_start.build_context`, `core/session_start.py:210`). `capture_coverage` reads the activity ledger, not the diary. `strength_map` also reads the inventory of every domain it ranks.
- UserPromptSubmit: one `events()` call, inside `strength_map` from `search_domains` (`core/search.py:602`), and only when the search has hits. A prompt with no hits makes no diary read. `strength_map` runs over all active notes of the hit domains, not only the hits.
- CLI `search` and each `serve` search request: the same single read when there are hits.
- Nothing caches `events()` between calls, in a process or across processes.
- Writes: `usage.record` appends one line per ref (6 lines for the prompt hook above). The diary is one file per UTC day under `.turns/`, and the 30-day prune removes old days, so `events(days=30)` reads everything that is kept.

### Size of the diary at scale

No real diary was read. These sizes are synthetic, 30 days of rows on real note refs, 80 percent `inject`, about 156 bytes per row on disk:

| rows | files | on disk MB |
|---|---|---|
| 5000 | 31 | 0.74 |
| 50000 | 31 | 7.43 |
| 500000 | 31 | 74.3 |

The real size is unknown. The prompt hook writes 6 rows for a prompt that injects 3 notes, so 100 such prompts a day for 30 days is about 18000 rows. To check a real store, the byte size of `.turns/usage-*.jsonl` divided by 156 gives the row count (read-only check; not done here).

### Time and memory of one read

In-process, one call in a fresh process (median of runs 2 and 3; the first run per size is a cold file cache). tracemalloc numbers are from separate runs, since tracemalloc slows the call 4 to 5 times.

| rows | `events()` ms | `events()` traced peak MB | `strength_map` (1000 notes) ms | `strength_map` traced peak MB | `strength_map` peak working set MB |
|---|---|---|---|---|---|
| 0 | 1.5 | 0.2 | 250 | 8.6 | 31.4 |
| 5000 | 25 | 4.0 | 278 | 9.9 | 33.1 |
| 50000 | 146 | 32.0 | 457 (422-492) | 35.5 | 62.2 |
| 500000 | 1441 (1441-1687) | 312.4 | 2092 (2076-2109) | 344.4 | 396.0 |

Memory per row held in Python is about 640 bytes (32 MB for 50000 rows, 312 MB for 500000), against 156 bytes on disk. The 250 ms at zero rows is the inventory read of 1000 notes in a cold process; the diary adds about 25 ms at 5000 rows, about 200 ms at 50000 and about 1.8 s at 500000.

### Effect on the whole hook process

Same hooks as in the main table, with the diary seeded (peak working set / peak commit / tracemalloc peak, MB; median of 3):

| rows | prompt hook | session-start hook |
|---|---|---|
| 0 | 50.7 / 40.5 / 21.0 | 41.3 / 30.6 / 15.6 |
| 5000 | 50.8 / 40.6 / 22.3 | 42.9 / 32.9 / 16.8 |
| 50000 | 80.1 / 70.3 / 51.4 | 73.3 / 64.1 / 45.8 |
| 500000 | 416.6 / 406.8 / 360.0 | 409.6 / 399.7 / 354.7 |

The session-start peak is close to the prompt hook's because the three `events()` calls run one after another and each list is freed before the next, so the three reads cost time (three parses) but not three times the memory. Wall time (cold copy, loaded machine): the 500000-row session start took 10.2 s against 5.0 s at 5000 rows.

Conclusion for PERF-1b: below about 5000 rows the diary is not a memory issue (under 2 MB). At 50000 rows each hook gains about 30 MB, which is more than the semantic path costs. Beyond that it grows linearly with no bound except the 30-day prune.

## Candidate cuts, ranked by expected saving

Savings are at 1000 notes unless stated. "Hypothesis" marks anything not measured as a before and after.

1. Stop building the whole diary in memory (`core/usage.py` `events` and `strength_map`). Evidence: 640 bytes per row in Python against 156 on disk; +30 MB per hook at 50000 rows, +340 MB at 500000 (tables above). `strength_map` only needs per-ref (event, time) pairs of six event types, and `noisy`/`used_refs` only need counts and a set, so a streaming pass that keeps counters per ref would hold memory in proportion to the notes, not the rows. Reading the diary once per SessionStart instead of three times saves about two thirds of that hook's diary time (hypothesis: about 290 ms at 50000 rows, 2.9 s at 500000). Expected saving: about 1 to 2 MB at 5000 rows, about 30 MB at 50000, about 340 MB at 500000; the actual diary size decides.
2. Lower `ROW_CACHE` in `core/semantic.py` from 1024. Evidence: the cache is 16.5 MB at its cap in `semantic rebuild` (`semantic.py:141`). In an ad hoc test (not in the committed script) the tracemalloc peak of a rebuild was 36.3 MB at 1024 rows, 24.2 MB at 256 and 21.2 MB at 64, identical in two runs, with 215089 row lookups and 1053 to 1054 misses at every size and no change in time (44.8 to 51.6 s under tracemalloc). Expected saving: about 12 MB at 256 for the rebuild only; hooks and `serve` never fill the cache. Caveat: the synthetic corpus uses about 45 distinct words, so about 1050 distinct vocabulary rows; real notes have a wider vocabulary and a lower hit rate, so this needs a re-run on a realistic corpus (hypothesis for real data).
3. Do not hold the tokenizer vocabulary as a Python dict (`core/semantic.py:51`, the `tokenizer.marshal` load). Evidence: 6.8 MB and 125000 blocks in every process that uses the model: about a third of the traced peak of the prompt hook and CLI `search` (21 MB), and of `serve` (25 MB). Options are a lookup in a memory-mapped sorted table or a small sqlite file, which would trade some lookup time for memory. Expected saving: up to about 6 MB per semantic process (hypothesis; not prototyped, and the lookup cost is not measured).
4. Parsed-note cache and bodies (`core/note_format.py:159`, `core/repository.py:221,223,413,434`). Evidence: about 3 to 3.5 MB at 1000 notes (about 3 KB per note) in every process, and held for hours in `serve`. Dropping the body from the cached copy, or keeping only the fields a search needs, would help at larger stores. Expected saving at 1000 notes: up to about 3 MB; scales linearly (hypothesis: about 30 MB at 10000 notes).
5. Import floor (importlib `.pyc` code objects, 3.5 to 4.4 MB traced, in every entry). Evidence: `import core.runtime` is 11.6 MB commit against 5.5 MB for the bare interpreter. Lazy imports of modules a given hook never reaches might save 1 to 3 MB (hypothesis; no import audit was done).

None of these affects correctness. At 1000 notes the entry points peak at 41 to 77 MB of working set, so the only entry whose memory depends on something other than the store size is the diary reader (item 1).

## Limits

- Synthetic notes use a small vocabulary (about 45 words) and uniform text, so row-cache hit rates and vocabulary-dependent costs are likely better than on real notes.
- The model came from the installed store by a read-only copy; the store under test, its diary and its session state were temporary copies. The installed store was not written.
- tracemalloc sees Python allocations only. The gap between traced peak and commit (about 20 MB in the prompt hook) is the interpreter, `.pyc` data, sqlite and the native vector extension.
- Diary sizes are synthetic. Wall times include other load on the machine.
