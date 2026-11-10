# Validation

Run `py -3.15 -m unittest discover -s tests -v` from the project. Tests use temporary stores and installation targets and do not read your configured store; they do not fabricate production memories. The suite covers capture/deduplication, canonical staging isolation, retrieval ranking, strict SQL, corrupt config preservation, revision checks, taxonomy/secret rejection, governed undo, recovery after an actual process exit, simultaneous script writers, installer rollback/restart, install/uninstall preservation, native payload translation, capture completion, reference remap/undo, staged Obsidian promotion, generated canvas refresh, merge/lifecycle transitions, non-destructive migration, BOM/CRLF/YAML parsing, interrupted-transaction roll-forward and abandon, direct update/supersede and review dates, vault conflicts/renames/resets/properties, Stop-hook change detection, prompt relevance and dedupe, shell-safe command detection, conversational durable-statement reminders and their Claude delivery, the budgeted session-start index, short refs and exact-title lookup, vendor credential screening, and snapshot backup, retention, staleness and restore-plan round trips including rejected archive members.

`py -3.15 scripts/benchmark.py` constructs 100 synthetic notes and records 40 in-process search timings plus four conversational retrieval checks in .cache/benchmark.json. This small benchmark does not certify real-world recall. The <15 ms target is not a guarantee; measured full canonical freshness checks on this machine exceed it. Interpreter startup and native host overhead are additional.

`py -3.15 scripts/benchmark.py --scale 1000 [--semantic-from STORE]` builds a synthetic store of that many notes and times a prompt hook, a capture and a session start as a new process runs them (median of 7; `prompt_warm_ms` is what a long-lived `serve` process sees). `--semantic-from` copies an installed model from a real store so writes pay for embeddings as they do in use. It exits non-zero when the prompt hook exceeds 400 ms or the capture 1,500 ms. Measured on this machine at 1,000 notes with potion-retrieval-32M: prompt 276 ms, capture 464 ms, session start 334 ms (before the per-process scan memo and vector reuse: 760 ms, 3,795 ms and 3,545 ms).

`py -3.15 scripts/eval_retrieval.py --fixture FILE [--root STORE] [--json]` scores the real prompt-injection, session-start and durable-signal code against a fixture of prompts with the notes each should (`expect`) and should not (`forbid`) surface, and prompts labelled with the signal kind they carry. It reports hit@3, false-injection rate, forbidden injections, injected characters, session-start size and index coverage, and per-kind signal precision, recall and false-arm rate, listing every miss. A fixture with a `notes` list is synthetic and built into a temporary store (`tests/fixtures/eval_synthetic.json`, run by `tests/test_eval.py`). Otherwise it runs against your store without changing notes. Keep personal fixtures outside the repo, e.g. `~/LivingMemory/.eval/`. A ref that no longer resolves fails the run, so stale fixtures are visible. Change retrieval defaults only when this report improves. `--semantic on|off` sets whether the installed semantic model is used. It rebuilds the store's indexes to match, then rebuilds them again for the hooks; it is refused for synthetic fixtures, whose temporary store has no model. The report includes `mean_ms`, the in-process prompt retrieval time.

**Functionality simulation.** `py -3.15 scripts/simulate.py [--scenario NAME|all] [--harness claude|codex|copilot|factory|all] [--subprocess] [--package DIR] [--content] [--keep] [--out FILE] [--json]` runs scripted sessions through the real hook adapters and CLI. Scenarios live in `tests/scenarios/*.json`.

Each scenario runs once per harness it lists (default: all four) against a fresh sandbox store: a temporary folder marked with `sandbox.json`, selected through `LIVING_MEMORY_ROOT`, with every harness home pointed at an empty folder. Your store and configuration are never written.

Steps are sent in each harness's own wire format:
- Copilot gets camelCase names and JSON-string tool arguments; only Codex gets a `turn_id`.
- Claude and Factory send only `Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Read|Glob|Grep|Agent|Task` tool calls to the hook. Repeat Read, Glob and Grep calls are answered by `core/hookfast.py` without loading the runtime.
- Events a harness does not wire are skipped, not failed.

**Expectations** are checked against the adapter's parsed reply and the debug log's decision detail (see MAINTENANCE.md). Hook steps can expect `inject`, `inject_includes`, `path`, `gates`, `context_contains`, `context_excludes`, `deny` (a rule such as `protected:domains`, or `subagent`), `allow`, `reminder`, `stop_reason`, `mutation`, `hints`, `recall`, `private_notice`, `instructions`, `system_message`, `internal`, `level` and a `detail` subset. CLI steps can expect `exit`, `status`, `reason` and a `result` subset. `save`/`as` keep values and new notes for later steps.

**Invariants** checked after every step:
- one log line for the step that agrees with the reply;
- injected refs that appear in the delivered text and match the usage diary;
- no scenario `secrets` in the log, the diary or session state;
- no error line without an injected `fault`.

Each run ends with `validate` and `doctor`.

**Modes:**
- In-process (the default) is what `tests/test_simulation.py` runs. It also checks that a deliberately broken relevance gate is reported.
- `--subprocess` runs real processes.
- `--package` runs an installed copy, for example `--package %LOCALAPPDATA%/agentic-living-memory/app` after a deploy. Fault scenarios (`in_process_only`) are skipped there.
- `--content` also logs text (allowed only because the store is a sandbox) and checks that the logged context equals the delivered one.

The report goes to `.cache/simulation/<UTC time>.json`, and `--keep` keeps each store for inspection.

The simulation exercises Living Memory's side of each harness contract as written in the adapters. It does not prove what a live harness sends.

Native adapter fixtures establish wire-format behavior, not live support in every harness version. Claude Code and Codex are installed and used live on this machine; Factory and Copilot are checked against fixtures and vendor documentation only. Hook config alone does not prove trust or delivery: verify native `/hooks`, then check the last observed invocation in `doctor`. There is no fresh-machine or non-Windows installation test in this environment.
