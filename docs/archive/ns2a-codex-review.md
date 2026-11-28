# NS-2A code review (Codex, 2026-11-21)

Codex (`gpt-6.2-sol`, effort high, read-only) reviewed commit `349df94`, notebook pages as their own record kind. It reported 22 findings: 12 High and 10 Medium, 21 marked confirmed and R4 a potential risk. Claude checked each one against the code before fixing it and found all 22 accurate. All are fixed in `9322fbd`.

"Test" names the regression test in `tests/test_docs_review.py`, except R1, whose test in `tests/test_docs.py` was changed to expect the refusal. Every test fails on `349df94`. The privacy tests (G1 to G8) share a `setUp` that passes `include_private` to `create_page`, which `349df94` did not accept, so on the old code they stop in `setUp`; their assertions were checked by reading the old code paths instead.

## Data safety

| ID | Finding | Fix | Test |
| --- | --- | --- | --- |
| D1 | An attachment replacement that stopped after the old bytes were moved aside and before the new ones were placed left the asset path missing, and recovery refused both directions | `repository._move_blob` copies the current bytes into the operation's `blobs/` folder, writes the wanted bytes to a temporary file (`_copy_into`) and replaces the asset path with `os.replace`, so the path always holds one side | `test_d1_a_crash_while_placing_new_bytes_leaves_the_old_ones` |
| D2 | A later effect of the same operation consumed staged bytes that undo needed (restore replacing `a.bin` and creating `b.bin` with the old bytes) | Blobs are never moved out: an operation keeps both sides for as long as its journal exists, and the `keep` flag is gone. Attachment bytes take twice their size until retention prunes the operation | `test_d2_an_operation_keeps_both_sides_of_its_attachments` |
| D3 | A notebook delete plan deleted only the files it listed, so a page saved during review was stranded in a notebook that no longer opened | The plan carries an `inventory` guard (the notebook's file list), rechecked in `governance._check_guards` under the apply lock | `test_d3_a_page_saved_after_the_delete_plan_makes_it_stale` |
| D4 | An undo of a `doc-` operation was a `d-` journal outside notebook retention, and `doc undo` could not classify it once its parent was pruned | An undo that changes only notebook files gets a `doc-` id, the journal's `meta` and a `doc.json` index. `doc undo` judges an operation by the files it changed (`docs.is_notebook_journal`), and a `doc-` undo whose parent was pruned is undone directly | `test_d4_undo_of_a_page_write_is_a_notebook_operation` |

## Path containment

| ID | Finding | Fix | Test |
| --- | --- | --- | --- |
| P1 | A snapshot member named `nb1/./p.md` passed verification, and restore wrote it and then deleted the same file under its live spelling | `backup._allowed` refuses empty, `.` and `..` parts and backslashes, applies the page name rules to notebook members, and `verify` refuses two members that differ only in letter case | `test_p1_backup_members_must_be_canonical_page_names` |
| P2 | Proposal listing and moves read `.proposals` through a junction, and `repository.confined` passed a junction that resolved to another notebook | `docs._proposal_files` reads proposals only through a checked folder; `repository.confined` checks every part of a notebook path with `is_link_or_junction` (symbolic links and mount points by reparse tag, so OneDrive placeholders still count as files) | `test_p2_proposals_and_journal_paths_refuse_junctions` |

## Permissions and privacy

| ID | Finding | Fix | Test |
| --- | --- | --- | --- |
| G1 | `doc --dom demo create ...` read as a `demo` action, so the hook auto-allowed a page write | `runtime._option_width` skips options as argparse reads them (an abbreviation of `--root`, `--domain`, `--cwd` or `--actor` takes a value); `_subcommand` and `_doc_action` use it; every `doc` action outside `DOC_AGENT_ACTIONS`, an unknown one included, counts as a write | `test_g1_abbreviated_options_do_not_hide_a_doc_write` |
| G2 | Main-actor writes opened private notebooks without `--include-private` | Every `doc` write takes `--include-private` and refuses an existing private notebook without it | `test_g2_writes_to_a_private_notebook_need_include_private` |
| G3 | `doc history --version` returned text written while a notebook was private, after the slug was reused or the notebook made public | `docs.history` skips journals in the private scope unless `include_private` | `test_g3_page_history_hides_operations_made_while_private` |
| G4 | `inspect-plan` of a restore showed a private notebook the live store had never seen | `private_scope(root, changes)` also counts the plan's incoming `notebook.json` files and registry | `test_g4_inspect_plan_redacts_a_private_notebook_the_plan_brings` |
| G5 | Undoing a privacy change removed the notebook from `notebook-privacy.json` | Undo merges the registry as a union (`docs.merge_privacy`) and drops its precondition | `test_g5_undo_never_shrinks_the_privacy_registry` |
| G6 | `history --note` listed private page paths of the same operation, and `inspect-plan` showed private plan summaries | Note history rows drop private files and report `redacted`; `docs.redact_summary` strips notebook, path and link fields from a redacted plan's summary | `test_g6_note_history_and_plan_summaries_are_redacted` |
| G7 | Without `notebook-privacy.json`, hookfast allowed a Read of a notebook whose damaged `notebook.json` counts as private | hookfast hands every Read, Glob or Grep inside the store, or a search above it, to the full hook | `test_g7_hookfast_hands_store_reads_over_without_a_registry` |
| G8 | A `serve --stdio` request carried `--include-private` past the hook, which never sees request argv | The hook denies `serve` to agents (`runtime.serve_command`, `SERVE_DENY`) | `test_g8_the_hook_denies_serve_to_agents` |

## Moves

| ID | Finding | Fix | Test |
| --- | --- | --- | --- |
| R1 | On a case-insensitive disk a case-only page rename is one file; the journal folds it into one effect, so undo and recovery lost the spelling | A case-only rename is refused for pages as for sections: two moves through another name | `test_docs.Moves.test_move_section_and_case_rename` |
| R2 | Proposals for new pages inside a moved section kept their old path, and a link rewrite made the proposal on that page stale | Proposals follow by prefix; a proposal whose base page had its links rewritten gets the same rewrite and the new base revision | `test_r2_proposals_follow_a_moved_section_and_a_rewritten_base` |
| R3 | A section move could put pages deeper than eight levels | Every expanded destination goes through `clean_path` | `test_r3_every_moved_path_obeys_the_name_rules` |
| R4 | A bare `[[Old]]` was rewritten even when another section also had an `Old.md` | A bare link is rewritten only when its name was unique before the move | `test_r4_a_bare_link_to_a_shared_name_is_left_alone` |

## Domains, notes and relocation

| ID | Finding | Fix | Test |
| --- | --- | --- | --- |
| B1 | A notebook-only domain whose folder was missing (restored, relocated or half created) could not get a notebook, and `domain create` refused to finish it | `docs._domain` accepts a missing folder for a notebook-only domain; `init_notebook_domain` recreates the folder for one already bound | `test_b1_a_notebook_only_domain_without_its_folder_still_works` |
| B2 | Restoring a snapshot from before a domain was bound to a folder turned it back into notebook-only while it held notes | `backup._keep_live_domains` keeps the live memory binding when the restored entry is notebook-only and the domain holds notes | `test_b2_restore_keeps_a_domain_converted_to_memory_since_the_snapshot` |
| N1 | A capture with a positional or payload domain, or a proposal draft, could land in a notebook-only domain | `notes.capture` refuses a notebook-only domain whatever chose it, and `cli._propose` checks its final target | `test_n1_no_note_lands_in_a_notebook_only_domain` |
| N2 | Relocation dropped a page named `.vectors-draft.md`, because the temporary-file filter matched names anywhere | Derived index and temporary-file filters apply only at `domains/D/`; the relocation temporary filter needs a leading dot | `test_n2_pages_named_like_derived_files_are_copied` |

## Found while fixing

- `doc_write` treated any command with the word `doc` among its arguments as a `doc` command, so a note titled "doc" lost auto-allow. It now checks the subcommand (asserted in the G1 test).
- With D4's `doc-` undo ids, undoing an undo whose parent retention had pruned came back as a plan, because the chain could not find the original kind. It is undone directly (D4 test).

## Round 2: re-review of `9322fbd`

Codex reviewed the fix commit with a brief listing every round 1 fix and asking whether each was complete, whether the fixes added bugs, and whether private text could still leak. It reported 14 findings: 7 High and 7 Medium, all marked confirmed. Claude found all 14 accurate; 9 are gaps in round 1 fixes and 5 are new. All are fixed in c19eb2d, with tests in the `RoundTwo` class of `tests/test_docs_review.py` (`test_2_N_...`). Every one fails on `9322fbd`; 2-10 fails there because the old `undo` takes no `include_private`, so its race was checked by reading the old code.

### Fixes that were incomplete

| ID | Finding | Fix |
| --- | --- | --- |
| 2-1 (D3) | A page that a crashed write left pending was finished by `transaction` after `apply` checked the inventory guard; and undoing a notebook's creation removed `notebook.json` under its pages | `commit` and `apply` run `roll_forward_pending` before checking guards; `repository.transaction` refuses any change that deletes a `notebook.json` while the notebook keeps other files (`docs.check_notebook_metadata`) |
| 2-2 (B2) | A restore plan made while a converted domain held no notes still restored its notebook-only binding after a first note was saved | `restore_plan` adds a `no_notes` guard for every domain it would make notebook-only; `apply` refuses once such a domain holds notes |
| 2-3 (P1) | Snapshot verification accepted `A.md` beside `A.md/p.md`, `.proposals/draft.md`, pages deeper than eight levels, and one folder spelled two ways | Page members go through `docs.clean_path`; proposal and attachment members must match their exact shapes; `verify` refuses a member that is also another member's folder and a folder spelled two ways |
| 2-4 (P2) | Backup walked notebooks through a junctioned domain folder, and `_files` followed a junction at its own root (`assets/`) | `_files` returns nothing for a link or junction root; `backup.notebook_files` checks the domain folder |
| 2-5 (R2, R3) | A move could put a retargeted proposal deeper than eight levels | Every retargeted proposal path goes through `clean_path`; the move is refused with the proposal's id |
| 2-6 (N1) | `remap` could move a note into a notebook-only domain | `docs.refuse_notes_in`, shared by `notes.capture` and `remap` |
| 2-10 (G5) | Undo merged the privacy registry before taking the store lock, so a notebook made private meanwhile could be dropped | The merge runs under the same lock as the rest of undo |
| 2-12 (G4) | Privacy prefixes compared case-sensitively, so a restore spelling `domains/DEMO/...` showed a private page on Windows | `is_private_path` compares ignoring letter case |
| 2-14 (G2) | Top-level `undo` and `recover --abandon` acted on, or listed the paths of, private notebook operations | `governance.undo` takes `include_private` and refuses a private operation without it; `recover` checks before acting; both commands gained `--include-private` |

### New defects

| ID | Finding | Fix |
| --- | --- | --- |
| 2-7 | Notebook deletes and restores applied from a plan ran under plan ids, outside notebook retention | `apply` writes the `doc.json` index for a plan that changed only notebook files; `docs.prune_journals` reads every `doc.json` |
| 2-8 | A write that crashed after staging attachment bytes and before its marker left an operation folder nothing ever removed | Retention removes operation folders with no journal and no marker that hold only `blobs/`; `roll_forward_pending` removes a marker-only folder with its blobs |
| 2-9 | The `serve` deny matched a capture titled "serve" in shell text the hook could not parse | In unparsed text, `serve` counts only in the subcommand place after the `living_memory` or `alm` token |
| 2-11 | `batch --payload-file` could run `doc read --include-private`, which the hook never sees | `batch` refuses `--include-private` in its items |
| 2-13 | The restore response, the registry's own diff and `skipped_unreadable` named private notebooks and paths | `restore_plan` redacts its response summary; `redact_summary` drops any string naming a private notebook or path, nested lists included; `notebook-privacy.json` counts as private while any private notebook exists |

## Round 3: final review of `c19eb2d`

Codex reported 13 findings: 5 High and 8 Medium, all marked confirmed. Claude found all 13 accurate. 3-6 is a regression the round 2 cleanup introduced. All are fixed in `25c4982`, with tests in the `RoundThree` and `RoundThreeRelocation` classes (`test_3_N_...`), each failing on `c19eb2d`. No fourth round follows: the delivery rule stops re-reviewing after about three rounds.

| ID | Finding | Fix |
| --- | --- | --- |
| 3-1 | A write that finished a half-done notebook delete through recovery then wrote into the deleted notebook | `check_notebook_metadata` also refuses a write into a notebook whose `notebook.json` is neither on disk (after recovery) nor written by the same change |
| 3-2 | Undoing a bind restored a notebook-only binding over a domain holding notes; a restore over unreadable live bindings skipped its guard | `undo` adds and checks a `no_notes` guard for every domain the restored bindings mark notebook-only; `restore_plan` guards every notebook-only domain in the restored bindings, whatever the live file says |
| 3-3 | Restore planning crashed reading a live folder where the snapshot had a page | `restore_plan` refuses before reading, naming the folder (or the file standing where the snapshot needs a folder) to move |
| 3-4 | The round 2 folder checks rejected note folders that differ only in case, valid on a case-sensitive disk | Case and tree checks apply to notebook members only |
| 3-5 | Backup walked notebooks through a junctioned `domains` folder | `backup.notebook_files` checks `domains` itself |
| 3-6 | A journal damaged to `{}` or `null` read as missing, so the next write deleted its folder and the only copy of an attachment | Only a marker with no journal file clears the folder; a damaged journal is reported as `corrupt_journal` and kept |
| 3-7 | A plan-applied notebook delete that stopped after committing and before its index was written escaped retention | The index is written before the plan runs; a retry keeps its first time |
| 3-8 | `diff` and `recover` judged a pending restore journal by the live store only, so a private notebook that existed only in its images showed | `docs.journal_scope` and `journal_private` add the `notebook.json` files and registry in the journal's own images; used by `diff`, `recover`, `undo`, `doc undo`, `history` and page history |
| 3-9 | `restore_plan` merged the privacy registry outside the store lock | The whole restore plan is made under the store lock |
| 3-10 | The hook's fallback split a quoted `--root "C:/Living Memory"` and missed `serve` | The fallback tokenizes with `shlex` (quotes kept, shell operators split); with unbalanced quotes any `serve` after the Living Memory token counts |
| 3-11 | `apply` changed private pages without `--include-private`, and `undo` checked privacy before taking the lock | `apply` takes `include_private` and refuses a plan that changes a private notebook without it; `undo` checks under the lock |
| 3-12 | `inspect-plan` returned the raw summary when no effect was redacted | The summary is always redacted for a caller without private access |
| 3-13 | `doctor`, `backup` results and relocation previews named private notebooks and pages | `doctor` names them only with `--include-private`; `backup` and `setup --inspect-plan` leave them out and report `private_withheld` |

## Limits that remain

- Private is not file protection: a shell command can still read a private notebook's files. The hook denies Read, Glob and Grep there and every `--include-private` to agents.
- `batch` cannot carry `--include-private`. Hivemind uses `serve`, which the hook denies to agents.
- The hook classifies shell text it cannot parse exactly with `shlex`. A shell construct that `shlex` splits differently from the shell could still mislead the `serve` check; the command then reaches the CLI, which refuses `--include-private` inside Hivemind terminals.
- Notebook plans applied by `c19eb2d` before this fix carry no index. That commit was never pushed, so no store has one.
- The round 3 fixes were not re-reviewed by Codex.

## Codex's report

### Round 1

#### 1. Summary

I would hold NS-2A. An interrupted attachment replacement can leave the live file missing and recovery blocked. A completed restore can consume bytes its undo needs.  
Restore can also delete a page when a ZIP member aliases its path. Relocation can silently omit a valid page.  
Private page text can surface through page history and restore previews. An abbreviated option can cause the hook to auto-approve a page write.  
The static fallback instruction text is 3,399 characters against its 3,400-character cap.

#### 2. Severity scale

- **Critical** - Widespread or unrecoverable store loss under ordinary use.
- **High** - Canonical data loss, blocked recovery, private content exposure, or a write that bypasses approval.
- **Medium** - A broken workflow, inconsistent store state, or private metadata exposure.
- **Low** - A localized failure with a straightforward workaround.

Effort is relative. S is a focused change. M spans multiple paths or requires failure and compatibility tests.

#### 3. Findings by review area

##### Data safety

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| D1 | High | Confirmed defect | `core/repository.py:638`, `core/repository.py:939` | An attachment replacement starts with bytes A and wants B. If the process stops after `_move_blob` moves A aside and before it installs B, the live path is missing. Recovery accepts only the A or B hash at that path, so both directions reject the missing state even though the operation holds both versions. | A restore can stop with a missing attachment and a journal that ordinary recovery cannot finish or reverse. | Stage both versions before applying and atomically replace the live file from a copy. Add failure checks at each filesystem step. | M |
| D2 | High | Confirmed defect | `core/repository.py:642`, `core/repository.py:959`, `core/governance.py:233` | A restore replaces `a.bin` from A to B and creates `b.bin` with A. `_move_blob` unlinks the old `a.bin` because staged A already exists. The later write consumes that staged A. Undo requires A in the operation's blob directory and refuses, even though `b.bin` holds it. | A completed restore can be impossible to undo. The same staging rules affect rollback of a partially applied operation. | Keep each required before and after blob for the journal's lifetime. Copy bytes into place and release them with the journal. | M |
| D3 | High | Confirmed defect | `core/docs.py:365`, `core/governance.py:27`, `core/governance.py:86` | Plan deletion of a notebook containing `a.md`, then create `b.md` before approval. The plan checks the files it enumerated. Apply deletes `notebook.json` and `a.md`, leaving `b.md` in a notebook that `doc read` can no longer open. | A save or proposal made during review can become inaccessible after an approved delete. | Record the notebook's file inventory as a guard and recheck it under the apply lock. | M |
| D4 | Medium | Confirmed defect | `core/docs.py:210`, `core/governance.py:66`, `core/governance.py:261`, `core/cli.py:669` | Undoing a `doc-` write creates a `d-` journal outside notebook retention. If the original is pruned the next day, `doc undo` cannot follow the chain and refuses to reverse the fresh undo. Repeated undo and redo also accumulate full page versions outside the retention limit. | Recent notebook edits can lose direct redo, and repeated edits through undo can grow the journal without the intended bound. | Record notebook identity and original operation class on each reversal. Apply retention to notebook reversals and make them classifiable after their parent expires. | M |

##### Path containment

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| P1 | High | Confirmed defect | `core/backup.py:28`, `core/backup.py:275`, `core/repository.py:736`, `core/repository.py:958` | A covered ZIP member named `domains/demo/notebooks/nb1/./p.md` passes verification. With live `nb1/p.md` present, restore schedules the ZIP spelling as a write and the live spelling as a deletion. `Path` normalizes both into the same journal path. Recovery writes it and then deletes it. | An approved restore can remove the page it was meant to restore. | Require canonical ZIP member names and reject duplicate normalized destinations, Windows aliases, and file/directory conflicts before creating a plan. | M |
| P2 | High | Confirmed defect | `core/repository.py:158`, `core/docs.py:96`, `core/docs.py:593`, `core/docs.py:739`, `core/backup.py:259` | With `open/.proposals` junctioned to `secret/.proposals`, proposal listing opens `open`, then reads JSON through the junction without checking that path. Moves also read proposals there. For restore, a junction from a covered `nb1` to live `nb2` passes `repository.confined` because it resolves inside the store. | Proposal fields can cross the private notebook boundary. Plan apply or recovery can reach another notebook inside the store. | Use the same per-component reparse-point check for notebook enumeration, proposal access, planning, apply, undo, and recovery. | M |

##### Permissions and privacy

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| G1 | High | Confirmed defect | `core/runtime.py:247`, `core/runtime.py:2226`, `core/runtime.py:2236`, `core/cli.py:679` | For `doc --dom demo create demo open p.md`, argparse accepts `--dom` as `--domain`. `_doc_action` skips the option but treats `demo` as the action. The hook classifies the invocation as a read and grants auto-allow in a bound session. The CLI defaults to actor `main` when the environment supplies no actor. | A page write bypasses the normal approval prompt. The same classifier also misses the subagent denial. | Classify with the CLI's argument grammar, including options at each parser level. Keep ambiguous invocations out of auto-allow and handle recognized unparseable writes in the deny path. | M |
| G2 | High | Confirmed defect | `core/cli.py:649`, `core/docs.py:315`, `core/docs.py:467`, `core/docs.py:766` | Main-actor commands outside a Hivemind terminal hard-code `include_private=True` for existing notebook writes. For example, `doc notebook update demo secret --no-private` can make the notebook public without the private opt-in. The write parser has no way to supply that opt-in. | An ordinary main-actor command can change a private page or remove its privacy setting. | Require explicit private authorization on writes and proposal settlement that target an existing private notebook. Pass it from the trusted bridge. | M |
| G3 | High | Confirmed defect | `core/docs.py:657`, `core/docs.py:676` | A private notebook at `demo/secret` once held `p.md`. After the notebook is deleted and its slug reused for a public notebook, `doc history demo secret p.md --version OLD_OP` checks only the current notebook and matches the old journal by path. | Historical private page text is returned from a public notebook with the same slug. | Check the historical effect against its notebook identity and privacy scope before listing or decoding it. | M |
| G4 | High | Confirmed defect | `core/governance.py:154`, `core/governance.py:163`, `core/backup.py:268` | Restore a backup of a private notebook into a different store that has never seen that notebook. `inspect-plan` derives privacy from the destination's live state. The incoming private metadata and registry are still plan changes, so the page diff is shown before apply. | A normal restore preview can expose the incoming private title and page text. | Derive preview privacy from both live and proposed metadata and registries. Redact the corresponding effects and metadata in the response. | M |
| G5 | High | Confirmed defect | `core/docs.py:350`, `core/governance.py:232`, `core/governance.py:265`, `core/cli.py:1405` | Make a public notebook private, then edit a page. Undo the privacy change. The page edit changed neither `notebook.json` nor the registry, so undo restores public metadata and removes the registry entry. Generic `diff` can then show the edit made while private. | Undo erases the durable record that protects historical private edits. | Make privacy tombstones monotonic across undo, redo, and restore. Keep the user-facing visibility change reversible independently. | M |
| G6 | Medium | Confirmed defect | `core/governance.py:123`, `core/activity.py:232`, `core/governance.py:165` | In a restore that changes a note and a private page, `history --note REF` returns the journal's full file list because it takes the early note-history branch. A private notebook delete plan also retains the notebook slug in the `inspect-plan` summary after its effects are redacted. | Restricted notebook names and page paths can appear in otherwise redacted responses. | Apply privacy filtering to note-specific history and plan summaries as well as effect bodies. | S |
| G7 | Medium | Confirmed defect | `core/hookfast.py:44`, `core/docs.py:811`, `core/runtime.py:1915` | In a store without `notebook-privacy.json`, damage an ordinary notebook's `notebook.json`. The full hook treats it as private. With a valid turn pointer, the fast hook can return `{}` for a `Read` of its page because it delegates only when the registry exists. | Identical structured reads can get different privacy decisions depending on the hook path. | Delegate reads into notebook paths and searches over them regardless of registry existence. | S |
| G8 | High | Confirmed defect | `core/runtime.py:263`, `core/cli.py:476`, `core/cli.py:502`, `core/cli.py:830` | The hook checks shell arguments for the private flag. A `serve --stdio` request can carry a nested `doc read` with that flag in its JSON `argv`. The nested CLI only refuses it when `HIVEMIND_SESSION` is set. | An agent outside a Hivemind terminal can reach a private read through the command server without the hook seeing the request arguments. | Give the server an explicit trusted or agent execution context. Enforce the context on each nested request. | M |

##### Revisions and concurrency

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| R1 | Medium | Confirmed defect | `core/repository.py:671`, `core/repository.py:749`, `core/governance.py:232` | On a case-insensitive filesystem, moving `Page.md` to `page.md` collapses the delete and write into a single effect at `page.md`. The spelling change happens outside that effect. Undo has no old spelling to restore, and recovery after a crash before `os.rename` has no rename to replay. | A reported undo or recovered move can leave the wrong filename casing. | Journal both spellings and replay the rename in either direction. | M |
| R2 | Medium | Confirmed defect | `core/docs.py:562`, `core/docs.py:585`, `core/docs.py:593`, `core/docs.py:777` | Move `A` to `B` while a proposal for the new page `A/Todo.md` is pending. Its path is absent from `moved`, so accepting it recreates `A`. Separately, if `Old.md` contains `[[Old]]`, a rename with link rewriting changes its bytes, but the retargeted proposal keeps the old base hash and fails to accept. | Pending edits can land in the old section or become stale solely because of the move. | Retarget pending proposals by section prefix. For automatic link rewrites, update the base and proposal text only when the original base still matches, or mark an explicit conflict. | M |
| R3 | Medium | Confirmed defect | `core/docs.py:544`, `core/docs.py:562`, `core/docs.py:428` | Create `A/B/p.md`, then move `A` to `q/r/s/t/u/v/w/A`. Both move arguments pass the eight-part limit, but the resulting page has ten parts and `doc read` rejects it. | A successful move can make pages inaccessible through ordinary page commands. | Validate every expanded destination path before constructing the transaction. | S |
| R4 | Medium | Potential risk | `core/docs.py:520`, `core/docs.py:531`, `core/docs.py:573` | If `A/Old.md` and `B/Old.md` both exist, moving only `A/Old.md` to `New.md` rewrites every bare `[[Old]]`. The code checks ambiguity among moved pages and checks the new name, but never checks what the original link resolved to. | A link in `B` may be redirected away from `B/Old.md`. The exact resolution depends on the client. | Rewrite a bare link only when its original target is known. Add coverage for equal basenames in separate sections. | S |

##### Backup and restore

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| B1 | Medium | Confirmed defect | `core/docs.py:902`, `core/docs.py:909`, `core/backup.py:267`, `core/storage.py:310`, `core/validate.py:10` | For a notebook-only domain with no notebooks, a snapshot or relocation copies the binding but carries no file that creates the domain directory. A crash after the create command commits its binding has the same result. `doc notebook create` refuses because the directory is absent, and rerunning domain creation refuses because the binding exists. | A restored, relocated, or interrupted empty domain is listed and passes validation but cannot receive its first notebook. | Make directory materialization idempotent from the registered domain kind. Check the invariant in doctor and validate. | M |
| B2 | High | Confirmed defect | `core/backup.py:215`, `core/backup.py:264`, `core/backup.py:268`, `core/cli.py:349` | Back up `journal` while it is notebook-only, then bind it to a project and capture a note. Restoring the old snapshot skips note coverage for `journal` and installs its old notebook-only binding. The newer notes remain on disk while the project prefix disappears and the domain reports kind `notebook`. | An older restore can disconnect current notes from normal project resolution and leave conflicting domain state. | Detect kind transitions when merging bindings. Preserve a live memory binding while live notes remain, or require an explicit migration in the approved plan. | M |

##### Existing note behavior and integration

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| N1 | Medium | Confirmed defect | `core/cli.py:875`, `core/cli.py:955`, `core/cli.py:1006`, `core/cli.py:543`, `core/notes.py:715` | From an unbound folder, `capture journal TITLE BODY` selects the notebook-only domain after the early guard. A capture or proposal payload with `"domain":"journal"` selects its target after the guard too. The note writer accepts the registered domain and can create `notes/` under it. | A notebook-only domain can accumulate ordinary notes while retaining its notebook-only binding. | Check the final canonical target in capture and proposal staging. Enforce the invariant at the note-writing boundary for other entry points. | M |
| N2 | High | Confirmed defect | `core/storage.py:153`, `core/storage.py:165`, `core/storage.py:316`, `core/storage.py:347` | `.vectors-draft.md` is a valid `doc create` filename. Relocation omits it because the transient-file filter matches the basename anywhere in the store. Verification uses the same filtered manifest and publishes the new location. | A valid notebook page silently disappears from the active store. The retired source still holds its bytes. | Scope derived and temporary-file exclusions to their actual directories and filename patterns. Verify all canonical notebook files at the destination. | S |

#### 4. Prioritized fix roadmap

1. Fix attachment staging and recovery first. Cover a stop between each filesystem step, repeated hashes, and rollback and undo after mixed replacements and creates.
2. Validate canonical notebook destinations before planning and again before writing. Add the notebook deletion inventory guard and fix relocation's basename filter in the same data-safety pass.
3. Close the privacy gaps across command parsing, nested requests, journal history, plan previews, and structured reads. Preserve the registry through undo and restore.
4. Make notebook-only domain creation and restore restartable. Enforce the domain kind where note destinations are finalized.
5. Finish move and retention semantics. Cover case-only undo, pending proposals, expanded move paths, ambiguous links, and a recent reversal whose parent journal expired.

#### 5. Coverage and limits

I reviewed commit `349df94` with `git show`, the `23a812a` design correction, `NOTEBOOKS.md`, the notebook design, and the surrounding transaction, CLI, hook, backup, binding, validation, activity, and relocation code. I read the committed notebook tests and the relevant existing test assertions. The worktree had a local change to `tests/test_docs.py`, so I used the committed version for review.

The review was read-only. I did not run the test suite or exercise crash recovery, live hooks, or NTFS behavior. The confirmed findings follow from the cited code paths. I measured the static fallback length with bytecode generation disabled. I also traced the v1 restore coverage and the note-count predicates through `MEMBER`. The documented shell file access and the deferred NS-2B notebook search feature are outside the findings.

### Round 2

#### 1. Summary

Commit `9322fbd` closes several first-round cases, including the attachment replacement gap and direct notebook undo after its parent journal is pruned. I found 14 remaining defects. The highest-priority issues can expose private page text, drop privacy registry entries, strand pages, or remove a live domain binding after a note is saved.

#### 2. Severity scale used

**Critical** means widespread irreversible loss or exposure. **High** means private content exposure, a bypass of a write restriction, or loss of access to canonical data. **Medium** means a recoverable workflow failure, an invariant violation, or storage growth outside retention. **Low** means a narrower failure with limited impact.

#### 3. Findings

##### Review area 1 - Completeness of the first-round fixes

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| 2-1 | High | Confirmed defect | `core/governance.py:86-93`, `core/repository.py:727-744`, `core/governance.py:249-272` | **D3.** Make a notebook delete plan, then interrupt creation of `later.md` after its journal is written and before the page is written. `apply` checks the old inventory, then `transaction` recovers the pending create before applying the delete. The new path is absent from the approved effects. A second route needs no crash: create a notebook, add a page, then undo the notebook creation. Undo checks only the original effects and removes `notebook.json`. | Both paths strand pages in a notebook that `doc read` can no longer open. | Settle pending operations before checking guards. Require every operation that removes notebook metadata to account for the notebook's remaining files. | Medium |
| 2-2 | High | Confirmed defect | `core/backup.py:234-242`, `core/backup.py:275-285`, `core/governance.py:86-93` | **B2.** Snapshot notebook-only `journal`, bind it to a folder, and generate the restore plan while it still has zero notes. Capture its first note before applying. The plan already contains the old notebook-only binding. Capture leaves `bindings.json` unchanged, and apply has no guard on notes added to that domain. | The note remains on disk, but the folder binding disappears and further captures into `journal` are refused. | At apply time, reject a plan that would restore a notebook-only binding over a domain that now holds notes. Require a fresh plan and digest. | Medium |
| 2-3 | Medium | Confirmed defect | `core/backup.py:75-84`, `core/backup.py:157-162`, `core/repository.py:956-983` | **P1.** A version 2 ZIP can list `A.md` and `A.md/p.md` in the same notebook. Both names pass verification because the verifier checks individual components and duplicate full names. On an empty target, recovery can write `A.md` before it fails to create `A.md` as a directory. Members such as `.proposals/draft.md` and pages deeper than eight levels also pass. | Verification accepts a restore that can leave a partially applied journal or create pages outside the notebook path rules. | Validate page members with `clean_path`. Check the archive path tree for file/directory conflicts and inconsistent casing in shared prefixes. | Medium |
| 2-4 | Medium | Confirmed defect | `core/backup.py:43-58`, `core/backup.py:120-124`, `core/docs.py:240-249`, `core/docs.py:288-292` | **P2.** With `domains/alias` as a junction to another domain, backup checks the `notebooks` directory and notebook folder for junctions, then reads their files through the unchecked domain ancestor. Likewise, `doc notebooks` calls `_files` with `assets/` as the walk root even when `assets/` itself is a junction. `_files` checks only entries below its starting path. | Backup can copy bytes through an alias, and notebook listings can enumerate and count files outside the notebook. | Use the full ancestor check before starting either walk, including on its root. | Small |
| 2-5 | Medium | Confirmed defect | `core/docs.py:564-567`, `core/docs.py:606-621`, `core/docs.py:803-808` | **R2/R3.** With a live page `S/one.md`, stage a new-page proposal for `S/a/b/c/d/e/f/new.md`, then move `S` to `X/S`. The live page passes validation, but the proposal is saved at the nine-part path `X/S/a/b/c/d/e/f/new.md`. Accept later rejects it. | A successful move can leave a pending proposal that the user can no longer accept. | Validate every retargeted proposal path before committing the move. | Small |
| 2-6 | Medium | Confirmed defect | `core/governance.py:301-325`, `core/cli.py:1607-1609` | **N1.** Remap an existing note from a memory domain into registered notebook-only `journal`, keeping its ID. `remap` checks registration and collisions, then writes `domains/journal/notes/...`. It never checks `notebook_only`. | A normal CLI command can put notes in a domain where capture, propose, and bundle import refuse them. | Check the target domain kind under the lock in `remap` and reuse that check at other note destination entry points. | Small |

##### Review area 2 - Recovery, retention, and note regressions

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| 2-7 | Medium | Confirmed defect | `core/governance.py:93`, `core/governance.py:291-297`, `core/docs.py:198-201`, `core/repository.py:813-833` | **D2/D4.** Apply `doc notebook delete --with-pages` to a notebook with an attachment. The delete journals the attachment's bytes under the bare plan ID. Notebook retention scans only `doc-*`, and generic retention handles bookkeeping kinds. A notebook-only restore and its plan-based undo use bare plan IDs too. | Repeated delete and restore cycles can retain attachment blobs and page images indefinitely. | Classify finished notebook-only journals by their effects for retention. Keep plan IDs usable for apply retries and preserve mixed note history. | Medium |
| 2-8 | Medium | Confirmed defect | `core/repository.py:758-766`, `core/repository.py:899-906`, `core/docs.py:198-201` | **D1/D2.** Interrupt `doc attach` after `_stage_blob` publishes the staged bytes and before the pending marker is written. The operation folder has a blob but no marker or journal. Retrying `doc attach` allocates a different operation ID. | Interrupted retries can accumulate orphan blobs outside both recovery and retention. | Reclaim stale staging folders with no journal under the store lock, including the marker-without-journal case. | Medium |
| 2-9 | Medium | Confirmed defect | `core/runtime.py:190-202`, `core/runtime.py:272-278`, `core/runtime.py:1933-1935` | **G8.** `python living_memory.py capture --title serve --summary "How the local server starts"` is a valid note command. `_parsed` declines that interpreter spelling, then `serve_command` sees `living_memory.py` and the title token `serve` and denies it. | An ordinary note capture can be denied because of its title or prose. | Identify `serve` in the subcommand position for supported interpreter and launcher spellings. | Small |

##### Review area 3 - Privacy

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| 2-10 | High | Confirmed defect | `core/governance.py:261-272`, `core/governance.py:59-67`, `core/docs.py:948-963` | **G5.** Make A private, then make B private in operation O. Restore an independent snapshot with private C so the live registry order is `[C,A,B]`. Undo O computes `[A,C,B]` before taking the lock. If another writer adds private D before undo acquires the lock, `commit` snapshots the newer registry hash and writes the older union without D. | Once D is made public or deleted, its old effects can lose historical redaction. | Read and merge the registry under the same lock as pending recovery, validation, and commit. | Medium |
| 2-11 | High | Confirmed defect | `core/runtime.py:272-289`, `core/cli.py:831-839`, `core/cli.py:1591-1603` | **G8.** Outside a Hivemind terminal, the hook sees `batch --payload-file request.json`. The file can contain a `doc read` request carrying `--include-private`. `batch` passes the inner arguments to `main`, while the hook only checks the outer command. The host's normal permission prompt still applies. | If the batch runs, Living Memory returns private page text even though the hook never evaluated the private-read request. | Apply the same agent policy to nested dispatch, or exclude agent batches from commands that can request private access. | Medium |
| 2-12 | High | Confirmed defect | `core/docs.py:895-910`, `core/governance.py:167-177` | **G4.** On case-insensitive Windows storage, preview a public snapshot with `domains/DEMO/notebooks/secret/p.md` against a live private notebook at `domains/demo/notebooks/secret/`. The incoming metadata is public, the stored private prefix uses lowercase `demo`, and the case-sensitive prefix check misses the effect. `inspect-plan` then reads the live file through `DEMO` for its before image. | A restore preview without `--include-private` can print private page text and the private title. | Match privacy scope against filesystem-equivalent paths before reading either side of an effect. | Medium |
| 2-13 | Medium | Confirmed defect | `core/backup.py:308-314`, `core/governance.py:167-182`, `core/docs.py:906-910`, `core/docs.py:932-945`, `core/cli.py:1411-1419` | **G4/G6.** A restore that imports private `demo/secret` returns that slug in the initial plan summary. Its `notebook-privacy.json` effect is diffed as ordinary configuration in `inspect-plan` and, after apply, in `diff` because the restore journal has no notebook UID. If `secret/p.md` is skipped for invalid UTF-8, `skipped_unreadable` also survives summary redaction. | Normal restore and review commands disclose private notebook identifiers and skipped page paths. | Redact registry effects and path-bearing summary fields at every response boundary, including the initial restore response and unchanged plans. | Medium |
| 2-14 | High | Confirmed defect | `core/cli.py:675-681`, `core/cli.py:1396-1405`, `core/governance.py:293-299`, `core/repository.py:930-931` | **G2.** For a committed private page write, `doc undo OP` refuses without `--include-private`, while top-level `undo OP` calls the same mutation without a privacy check. For an unfinished private operation, `recover OP --abandon` returns every effect path. | The generic entry points can change a private notebook or disclose its page paths without the private flag. | Enforce privacy in the shared undo and recovery entry points before mutation or output, with explicit private access for the trusted caller. | Medium |

#### 4. Prioritized fix roadmap

1. Close the private-content and private-write paths first. Cover nested dispatch, Windows path equivalence, shared undo and recovery, and the registry merge under lock. Address 2-10 through 2-14 together.
2. Enforce notebook inventory and domain-kind checks after pending recovery and immediately before apply. Cover both crash-created pages and a first note saved after a restore plan. Address 2-1, 2-2, and 2-6.
3. Validate archive path trees and proposal destinations before journaling. Extend retention to finished notebook-only plan journals and reclaim orphan staging folders. Address 2-3 through 2-5 and 2-7 through 2-8.
4. Tighten `serve` classification and add a normal capture with `serve` in its title to the hook regression tests. Address 2-9.

#### 5. Coverage and limits

I reviewed `git show 9322fbd`, its parent-side diff, the changed functions and their relevant callers, `docs/notebooks-design.md`, `NOTEBOOKS.md`, and `tests/test_docs_review.py`. I also read relevant cases in `tests/test_docs.py`, `tests/test_docs_backup.py`, and `tests/test_docs_runtime.py`.

I did not find a separate defect in the tested abbreviated-option path, the `doc history` journal filter, standard in-store Read/Glob/Grep routing, the case-only move refusal, the ambiguous bare-link rule, missing notebook-only folder recreation, or the relocation cases added in this commit.

This was a static, read-only review. I did not run the test suite, inject crashes, perform restores, or exercise Windows junctions or live hooks. Locations refer to `9322fbd`.

### Round 3

#### Summary

I found 13 confirmed defects in `c19eb2d`. The highest priorities are preventing writes from stranding pages after crash recovery, preserving attachment bytes beside damaged journals, and closing two paths that can expose private page contents. The new backup verification also rejects a previously supported layout for ordinary notes on case-sensitive filesystems. This was a read-only code review.

#### Severity scale

**Critical** is widespread irreversible data loss or unrestricted disclosure. **High** is loss of access to canonical data, destruction of the only recoverable bytes, or exposure of private content. **Medium** is a blocked workflow, a retention failure, or disclosure of private names or paths. **Low** is a localized issue with an easy workaround. Effort is Small, Medium, or Large.

#### Area 1. Correctness and regressions

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| 3-1 | High | Confirmed defect | `core/docs.py:462`, `core/docs.py:671`, `core/repository.py:737`, `core/docs.py:1005` | Apply a whole-notebook delete and interrupt it just before `notebook.json` is removed. The next `doc create` or `doc attach` opens the still-present metadata. Its transaction finishes the pending delete, then writes the new file. The metadata check only examines deletes in the new transaction. | The command reports success, but `doc` cannot open the notebook that holds the new page or attachment. | Finish pending operations under the lock before a writer reads notebook state. Also require surviving metadata for any transaction that writes a notebook member. | Medium |
| 3-2 | Medium | Confirmed defect | `core/backup.py:237`, `core/backup.py:329`, `core/governance.py:304`, `core/init_domain.py:29` | Create a notebook-only domain, bind it as a memory domain, and capture a note. Undoing the bind produces a plan that restores the flag with no `no_notes` guard. Restore has a second gap: if live `bindings.json` is invalid JSON, `_keep_live_domains` returns the snapshot value and guard creation silently skips the check. | Either operation can leave notes in a notebook-only domain and remove their normal folder binding. | Check the proposed binding state against the notes that would remain. Apply the check to undo and restore, including repair of unreadable bindings. | Medium |
| 3-3 | Medium | Confirmed defect | `core/backup.py:166`, `core/backup.py:299`, `core/governance.py:28` | Back up a notebook containing `Box.md`. Delete that page and create `Box.md/child.md`. The ZIP passes the new tree check, but restore planning calls `read_bytes()` on the live `Box.md` directory while capturing its expected revision. | A valid snapshot cannot be restored over a layout produced by ordinary `doc` commands. | Detect file/directory conflicts against the live tree before reading revisions. Report the blocking path and support the change with recoverable ordering if it is meant to work in one restore. | Medium |
| 3-4 | Medium | Confirmed defect | `core/backup.py:128`, `core/backup.py:168`, `core/repository.py:422` | On a case-sensitive filesystem, valid notes at `notes/Team/a.md` and `notes/team/b.md` are both accepted by inventory and were accepted by the previous verifier. The new folder-spelling check rejects the store's own backup before publication. The full member names are distinct even after case folding. | Ordinary note backups and restores can fail on an existing store. | Keep snapshot creation compatible with supported note layouts. Check destination filesystem compatibility at restore time. | Small |
| 3-5 | Medium | Confirmed defect | `core/backup.py:43`, `core/backup.py:51`, `core/docs.py:89` | If `<store>/domains` is a junction to another store's domain tree, `canonical_files` enumerates through it. `notebook_files` checks the child domain and notebook directories, which can all be ordinary directories. By contrast, `doc` checks the `domains` component itself. | Backup can copy page and attachment bytes from a tree that `doc` refuses to open. | Check `root / "domains"` before enumerating it and share the notebook ancestor checks across backup and `doc`. | Small |
| 3-6 | High | Confirmed defect | `core/repository.py:130`, `core/repository.py:680`, `core/repository.py:904` | Interrupt an attachment deletion after recovery stages the old bytes and unlinks the asset. If the pending journal is then damaged to valid JSON `{}` or `null`, `read_json` returns a falsey value. The next write recursively deletes the whole operation directory as though the journal were missing. | The only remaining copy of the attachment can be destroyed. | Distinguish a missing journal from a damaged or invalid journal. Preserve the operation directory and blobs when a journal exists but cannot be validated. | Small |
| 3-7 | Medium | Confirmed defect | `core/governance.py:96`, `core/docs.py:205`, `core/docs.py:1030`, `core/repository.py:991` | Interrupt a plan-applied notebook delete after its journal commits and before `record_index` runs. The operation has a bare plan ID. Recovery never creates `doc.json`, and pruning discovers bare IDs only through that file. Previously applied notebook-only plans without an index have the same gap. A successful retry also stamps the index with the retry time. | Completed page journals and attachment blobs can escape retention, or rank as newer than their actual operation. | Make recovery and pruning able to identify notebook-only journals without the sidecar. Build the index from the journal's original timestamp and effects. | Medium |

#### Area 2. Private content, paths, and titles

| ID | Severity | Status | Location | Evidence | Impact | Fix | Effort |
|---|---|---|---|---|---|---|---|
| 3-8 | High | Confirmed defect | `core/cli.py:1409`, `core/cli.py:1425`, `core/docs.py:873`, `core/repository.py:937` | Apply a restore containing a private notebook to a fresh store and interrupt it after the journal is written but before any effect lands. `diff` and `recover` classify the journal from the live store alone. The private metadata and registry exist only in the journal's after-images. `diff` emits the page text and title, and `recover --abandon` returns the private file paths. | Private content and paths are available without `--include-private` during an ordinary crash window. | Build journal privacy scope from both recorded metadata and registry images as well as live state. Use it for history, diff, undo, and recovery. | Medium |
| 3-9 | High | Confirmed defect | `core/backup.py:294`, `core/backup.py:334`, `core/governance.py:26` | Start a restore plan whose snapshot contributes privacy entries. After `_merge_privacy` reads the live registry, create another private notebook before `plan()` takes the lock. The plan records the newer registry hash as its precondition but stages the older merge. Apply succeeds and drops the concurrent entry. | After the concurrent notebook is deleted or made public, its historical private page text can become visible in `diff`. | Merge the registry and capture its expected revision under the same lock. Reject any planned registry write that would drop a current entry. | Medium |
| 3-10 | High | Confirmed defect | `core/runtime.py:190`, `core/runtime.py:277`, `core/runtime.py:1938`, `core/cli.py:502` | `python living_memory.py --root "C:/Living Memory" serve --stdio` is a valid invocation that takes the hook's fallback path. The fallback splits the quoted root into two tokens, skips one, and mistakes `Memory` for the subcommand. The hook therefore omits its `serve` denial. | Outside a Hivemind terminal, a server invocation that proceeds can receive private-read options the hook never sees. | Preserve quoted arguments and `--option=value` in the fallback. Test valid invocations with spaced paths as well as captures that merely mention `serve`. | Medium |
| 3-11 | Medium | Confirmed defect | `core/cli.py:1358`, `core/governance.py:82`, `core/governance.py:267`, `core/governance.py:273` | A restore plan that changes a private page can be applied with its ID and digest alone. `apply` has no private-operation check. Separately, if a public notebook becomes private between `undo`'s privacy check and lock acquisition, undo can still change the page because its revision is unchanged. | Private pages can be modified or deleted without `--include-private`. | Check authorization under the store lock, against the current and proposed state. Give the bridge an explicit private apply path. | Medium |
| 3-12 | Medium | Confirmed defect | `core/backup.py:325`, `core/backup.py:334`, `core/governance.py:176`, `core/governance.py:187` | Take a snapshot containing `demo/secret` and a public notebook, then edit only a public page. `restore-plan` redacts its response but saves the raw summary with `demo/secret`. `inspect-plan` finds zero private effects and returns that raw summary. The same branch can expose a skipped private member's path. | The standard plan review reveals private notebook names and paths without the flag. | Redact summaries whenever the caller lacks private access, regardless of the number of redacted effects. | Small |
| 3-13 | Medium | Confirmed defect | `core/docs.py:1102`, `core/cli.py:182`, `core/backup.py:144`, `core/storage.py:209` | `doctor` names a private notebook near its page or attachment limit. `backup` returns a private page's member path if it contains invalid UTF-8. If a private page changes after relocation planning, `setup --inspect-plan` lists the path under `changed`. Each command formats its results without a privacy filter. | Routine diagnostics and previews disclose private notebook names and page paths. | Filter private names and paths in command output while retaining counts, repair guidance, and the full internal backup or relocation data. | Medium |

#### Prioritized fix roadmap

1. Fix the crash paths first. Validate writes after recovery, preserve blobs beside invalid journals, and classify pending journal effects before exposing them (3-1, 3-6, 3-8).
2. Close the privacy gaps in restore, apply, undo, and the hook. Include tests for a concurrent registry addition and quoted `serve` paths (3-9 through 3-11).
3. Make restore validate the live destination and final binding state. Preserve ordinary note backup compatibility, reject linked notebook ancestors, and repair retention discovery (3-2 through 3-5, 3-7).
4. Apply privacy filtering to plan summaries and operational diagnostics (3-12, 3-13).

#### Coverage and limits

I read `git show c19eb2d`, both notebook documents, all `RoundTwo` tests, and the relevant backup, notebook, hook, and recovery tests. I traced note capture, remap, undo, recovery, restore, relocation, derived note index reconciliation, and both journal pruning paths. The metadata predicate selects canonical notebook metadata deletes, and notebook retention selects `doc-` directories and directories with `doc.json`. The ordinary-note backup regression comes from the separate ZIP verifier.

The findings are based on static traces at `c19eb2d`. I ran no tests, performed no crash or concurrency simulations, and created or changed no files. Windows junction behavior and live hook delivery were checked by source inspection.
