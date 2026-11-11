# Note types and use cases

Living Memory has thirteen note types. Each note has one primary type, selected by what the note is meant to preserve. The lowercase `type` value determines the uppercase filename prefix: `type: "decision"` produces `DECISION - ...`. New canonical notes do not store an independent prefix field.

`STATUS` includes progress, completed milestones and meaningful current-state snapshots. `PROGRESS` is no longer a separate type. Corrections belong in `KNOWLEDGE`, with the existing `concern/correction` tag.

## The thirteen types

| Type / prefix | What it records | Use case and example |
| --- | --- | --- |
| **KNOWLEDGE** | Established facts, explanations, reusable findings and corrected understandings. | Preserve something future work can rely on: “The search index is disposable and can be rebuilt from Markdown.” For a correction, record the accurate fact and what it replaces. |
| **DECISION** | A choice that has been made, its rationale and relevant tradeoffs. | Preserve why an approach was selected: “Use one shared local store so every harness reads the same notes.” |
| **PREFERENCE** | A person's desired approach or default, allowing exceptions when appropriate. | Avoid repeatedly asking how someone likes work done: “Prefer concise explanations and Python 3.15 examples.” Record whose preference it is and its scope. |
| **CONTEXT** | Background and circumstances needed to interpret a particular project or situation. | Establish the setting: “This project is a local Windows implementation used by one person across several harnesses.” |
| **STATUS** | Meaningful current state, progress, completed milestones, blockers and outcomes, as of a stated time. | Preserve a useful checkpoint: “As of November 3, migration is complete, retrieval works, and the original folder remains a backup.” Keep durable milestones; skip routine narration of work in flight. |
| **BUG** | An observed defect: actual behavior that differs from expected behavior. | Support diagnosis and verification: “The migration rejects a destination containing only its own lock file.” Include reproduction conditions, expected/actual behavior and evidence where available. |
| **PLAN** | Intended future steps toward a goal, including dependencies and how success will be checked. | Make agreed or proposed work resumable: “Copy and verify the store, rebuild indexes, then switch the shared pointer.” State whether the plan is proposed or approved; the type alone does not grant approval. |
| **RISK** | A possible future problem, its consequences and potential mitigation. | Preserve an uncertainty before it becomes an incident: “An older harness could keep writing to the retired store; upgrade clients before migration.” Describe the trigger, impact and mitigation, and the owner if known. |
| **RESEARCH** | Investigations, sources, experiments, competing explanations and open questions. | Preserve evidence and uncertainty: “Compare shared configuration files and environment variables for root discovery.” Cite the sources or experiments, separate observations from inference, and state unresolved questions. |
| **MAP** | One repo's codebase orientation: purpose, layout, entry points, key modules, data flow, build/test/run, conventions. | Spare the next session a re-exploration: “runtime/ holds the CLI; skills/ is generated from it by build.py.” One per repo, keyed by `--file REPO`; Key Details bullets labelled **Purpose**, **Layout**, **Entry points**, **Key modules**, **Data flow**, **Build/test/run** and **Conventions**, plus links to the repo's gotcha and decision notes; at most about 3,000 characters. Synthesize, don't copy code. Reviewed every 90 days. |
| **BRIEF** | The living project brief: what the project is, who owns what, where it stands and what was decided. | Give a new session or a new owner the project in one read: “Phase 2 of 3, blocked on the CFO sign-off; Priya owns billing, Marcus took over ledger on Nov 3; money is BigDecimal (docs/adr/ADR-004).” One per project folder, keyed by `--file DIR` (the folder relative to the bound folder, `.` for the bound folder itself); Key Details bullets labelled **Goals and scope**, **Stakeholders and decision-makers**, **Team and ownership**, **Current phase, status and blockers**, **Key decisions** (one line each, pointing to the ADR, ticket, doc path or note that records it), **Active risks** and **Glossary**; about 2,500 characters. Update it in place; don't append history. Reviewed every 30 days. |
| **RUNBOOK** | An operational procedure for a system: when to use it, preconditions, steps, verification and rollback. | Let the next person (or agent) carry out a known operation safely: “Restart the billing worker: drain the queue, restart, confirm the lag metric recovers.” Tag it with the system (`--system SLUG`). Number the steps, say how to check each worked and how to back out. Reviewed every 90 days. |
| **POLICY** | A mandatory rule or constraint, its authority and where it applies. | Preserve a requirement: “Existing-note mutations require an approved plan digest.” Record the authoritative source and scope. A retrieved policy note remains historical evidence; verify that the rule is current and applicable. |

The suggested contents above guide authoring; they are not additional required metadata fields or automatic workflow engines.

## Choosing between related types

- **KNOWLEDGE vs RESEARCH:** use research for the investigation and its uncertainties; use knowledge for an established finding. Link a finding to the research that supports it.
- **KNOWLEDGE vs CONTEXT:** use knowledge for a reusable fact or explanation; use context for the circumstances of a particular situation.
- **DECISION vs PLAN:** use decision for a choice already made and why; use plan for the intended work. Link the plan to decisions it implements.
- **PLAN vs STATUS:** use plan for what is intended; use status for what is true or accomplished as of a particular time. Completing a step does not automatically change a plan note's type.
- **MAP vs KNOWLEDGE or CONTEXT:** a map is the whole-repo orientation, one per repo; a single fact about one module is knowledge, and the circumstances around a project are context.
- **MAP vs BRIEF vs CONTEXT:** a map is how the code is organized (one per repo); a brief is what the project is, who is on it, where it stands and what was decided (one per project folder, kept current); context is the circumstances around one situation.
- **RUNBOOK vs KNOWLEDGE or PLAN:** a runbook is a repeatable procedure someone will follow again; a fact about how something works is knowledge, and one-off intended work is a plan.
- **PREFERENCE vs POLICY:** use preference for a desired default that permits exceptions; use policy for a requirement backed by an identified authority. Do not promote a preference into a requirement without an explicit decision.
- **BUG vs RISK:** use bug for an observed defect; use risk for a potential problem. Link a bug to risks arising from it, or a risk to a bug that later demonstrates it.

Choose the note's primary purpose rather than assigning several types to one note. Split distinct durable claims into linked notes when they need independent updates. Types do not create additional retrieval domains or broaden search scope.

## Note layout

Every note file, and its Obsidian copy byte for byte, has the same layout (`core/note_format.py`):

```markdown
---
title: "Webcam fails with 0xA00F4244"
type: "bug"
status: "active"
tags: ["concern/bug", "platform/windows", "subject/user"]
context: ""
id: "…"
domain: "sandbox"
created: "…"
updated: "…"
schema_version: 3
capture_key: null
sha256: "…"
---

# 🎯 Webcam fails with 0xA00F4244

## 📌 Executive Summary
One or two sentences: the fact, decision or problem.

---

## 📝 Key Details
* **Cause:** Bold the key word, keep sentences short.

## 🗂️ Resource Links
* [[Another note]] or https://…

## 🟩 Action Items
- [ ] An open step
- [x] A finished step

> ℹ️ **Important Note:**
> A critical caveat or warning.
```

- The frontmatter lists `title, type, status, tags, context` first and bookkeeping after them. Optional bookkeeping appears only when set: `files` (after `context`), and after `updated`: `review_by`, `confirmed` (the dates `confirm` recorded, at most 8, newest last), `supersedes`, `superseded_by`, then `origin` after `capture_key`, then `repo` on maps and briefs: the git identity of the folder `files[0]` names (its `origin` remote normalized to `host/owner/repo`, or `file:` plus the resolved local path), set by capture and update and not editable. Clones and worktrees of one origin share one map and one brief; a map or brief matches by identity first, then by folder. A note whose folder has no origin carries no `repo`.
- The `# 🎯` heading always comes from the `title` property. It is not stored in the body, so to rename a note, change `title`.
- The Executive Summary is required. Every other section is optional, and empty sections are dropped.
- **Action Items are only for `plan`, `bug` and `risk` notes.** Capturing them on another type, or changing a note to another type while they exist, is refused. Lasting facts go in Key Details.
- A plain-text body, from an older note or a quick capture, becomes the Executive Summary the next time the note is written.
- Text inside a section is kept as written, and `##` sections of your own are kept in place.
- Build the body from flags (`--summary`, `--detail`, `--link`, `--action`, `--done`, `--important`) on `capture` or `update`. Each flag replaces only its own section; on `update`, `--add-detail`, `--add-link` and `--add-action` append to a section and `--set-detail` replaces one labelled bullet, so the rest need not be restated. You can also pass formatted Markdown with `--content-file` / `--body-file`.
- Search and session-start snippets show the text without headings or list markup.

## Type, lifecycle status and tags

Type answers “what kind of information is this?” Lifecycle `status` answers “is this note active, resolved, archived or superseded?” They are independent. For example, `type: "status"` with `status: "active"` means an active current-state note. Notes that track work (plan, bug and risk) are resolved when the work is finished: `resolve REF` moves the note to the archive in one journaled, undoable step, and `reopen REF` brings it back. A resolved bug remains `type: "bug"`, with a `BUG` filename prefix and `status: "resolved"`; the same holds for plan and risk notes. Other types leave the active set through an archive plan.

Tags describe the subject and concern, optionally the platform and technology. Every new note still requires registered `subject/` and `concern/` tags. Type names do not automatically become tags; use `taxonomy` to inspect the existing vocabulary. A knowledge note correcting an earlier misunderstanding can use `subject/project` and `concern/correction`.

A project's charter is a `context` note tagged `project/<slug>` (with `subject/project` and `concern/knowledge`); `project-plan create` writes it and `projects.json` links to it.

A system's passport is a `context` note tagged `system/<slug>` (with `subject/project` and `concern/architecture`); `system-plan create` writes it and `systems.json` links to it.

A change's brief is a `plan` note tagged `project/<slug>` (with `subject/project` and `concern/workflow`); `change-plan create` writes it, or adopts an existing plan note, and `changes.json` links to it.

Date status snapshots in the body and preserve the information needed to interpret them later. When a snapshot becomes misleading, use an approved update, archive or supersession plan. The system does not automatically expire status notes or treat a plan, risk or policy as an executable instruction.

## Capture and existing-note conversion

CLI and JSON capture accept the lowercase values `knowledge`, `decision`, `preference`, `context`, `status`, `bug`, `plan`, `risk`, `research`, `policy`, `map`, `runbook` and `brief`. The default is `knowledge`. For example, after binding the project:

```powershell
py -3.15 living_memory.py capture --domain my-project --type risk --subject project --concern architecture "Older clients during migration" "Older clients may keep writing to the previous store. Upgrade clients before switching locations."
```

Existing `progress` and `correction` notes remain readable. New captures must use `status` and `knowledge`, respectively. `note-type-plan` previews conversion across the store:

| Existing classification | Conversion |
| --- | --- |
| Type `progress` | Type `status`, prefix `STATUS` |
| Type `correction` | Type `knowledge`, prefix `KNOWLEDGE`; preserve existing correction tags |
| Legacy prefix `BUG` or `RESOLVED` | Type `bug`, prefix `BUG`; preserve lifecycle status |
| Other supported types | Keep the type and derive its uppercase prefix |

Review with `inspect-plan ID`, then apply the exact approved digest using `apply ID --expected-plan-sha256 HASH`. Conversion preserves stable IDs, creation timestamps, lifecycle statuses and tags, updates filenames and affected local Markdown links, and rebuilds derived indexes. It does not rewrite historical transaction journals. Existing-note conversion requires its own reviewed plan; changing the software does not silently migrate notes.
