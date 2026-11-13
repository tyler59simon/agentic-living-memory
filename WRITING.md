# Writing notes

A note is read cold, often months later, by an agent or a person who was not there when it was written. Often they see only the first sentence, in a session-start line or a search snippet. Write for that reader: say the thing plainly, in the words you would use to tell a colleague, and stop.

The rules below adapt two sources to memory notes: [humanizer](https://github.com/blader/humanizer) v4.0.0 (MIT) and Wikipedia's [Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing). Both describe how model text drifts toward the most likely phrasing for the widest audience, so it comes out generic and inflated. Notes are reference text, so they stay neutral and plain: no humor, no opinions, no asides.

`capture` and `update` check for most of these habits and return `style:` and `shape:` lines in `quality_warnings`. The warnings are advisory: the note is saved either way.

## The summary

The summary is the opening paragraph, before any heading. It carries the note on its own.

- Open with the fact, the decision or the problem. The first sentence must make sense with nothing around it.
- Say who decided and when, with an absolute date: "On 2026-11-12 the user chose ...". Never "yesterday" or "recently".
- One or two sentences, about 60 words at most. Put the rest in sections.
- Don't restate the title, and don't announce the note ("This note records ...").

Before:

> On 2026-11-11 the user approved a 7-phase remediation of Living Memory driven by the alm-simulation-benchmark findings: capture load-bearing knowledge from docs and code as concise pointer notes, block Stop once per reply everywhere when docs, decisions or described files change, a living project brief per project, command hints and better long-prompt retrieval, ...

After:

> On 2026-11-11 the user approved a seven-phase fix of Living Memory, based on what the alm-simulation-benchmark runs found. The phases are listed under Why; the deferred work is under Alternatives considered.

## Sections by type

Every note starts with the summary and an optional caveat (`--important`, shown as `> **Important:** ...` right after the summary). After that, the type decides the shape.

| Type | The summary says | Sections, in this order |
|---|---|---|
| knowledge | the fact | short sections named after what they explain, written as paragraphs |
| decision | the choice, who made it, when | Why; Alternatives considered |
| bug | the symptom | Cause (or a sentence starting "Hypothesis:"); Fix; How to check |
| runbook | what the procedure does | When to use; When not to use; Steps (numbered); Check; Roll back |
| research | the question and the answer so far | Findings; Sources; Open questions |
| map, brief | what the repo or project is | Details, with the fixed bold labels (see [NOTE_TYPES.md](NOTE_TYPES.md)) |
| preference, context, status, plan, risk, policy | the point | Details |

Write a section with `--section 'Heading' 'Text'` on `capture` or `update`. On `update` it replaces only that section. `--drop-section 'Heading'` removes one. Links go under Links (`--link`), and open steps of a plan, bug or risk go under Action items (`--action`).

## Action items for a dashboard

Any plan, bug or risk note can be shown as a dashboard (`views add REF`, see [SCRIPT_REFERENCE.md](SCRIPT_REFERENCE.md)). The dashboard reads an Action item's text by one convention, and a note that does not follow it still shows, only without tags and IDs.

- Start the text with one or more bracket groups, each a comma-separated list of tags: `[P2] [ALM] ...` or `[ALM, HM] [approved] ...`. The dashboard turns each tag into a badge and a filter chip. `P1`, `P2` and `P3` set the priority order.
- Next comes an ID if the item has one: capitals, digits and hyphens, with at least one hyphen or digit (`CUR-1`, `HM-P4`, `MAP-REKEY`, `UX-Q2`). A plain capitalized word such as `API` is not an ID.
- The rest is the item's title: `- [ ] [P2] [ALM] HOOK-4 Cap the total UserPromptSubmit context below the hook-context limit`.
- Put the longer explanation in a Details bullet whose bold label is the ID: `- **HOOK-4:** the UserPromptSubmit path in core/runtime.py ...`. Indented lines under the bullet belong to it. The dashboard shows that text when the item's card is opened, and leaves the bullet out of the Details list. Write one such bullet per ID; an item with no ID or no bullet simply has no detail.

Keep the title to one line and the Details bullet to the facts the reader needs to start the work.

A knowledge note reads as a short article. Name each section after what it explains ("Updating a stale PR branch"), not with a generic label ("Details", "Notes", "Key points").

## Sentences

- **One name per thing.** If you call it "the install plan" once, don't switch to "the deployment manifest" and "the plan file". Readers take a new name as a new thing.
- **Plain verbs.** "is", "are", "has". Not "serves as", "stands as", "functions as", "boasts", "features".
- **Say who acts.** "The hook refuses the write", not "the write is refused". "The user chose", not "it was decided".
- **One fact per bullet**, in a full sentence. Split a bullet that strings facts together with semicolons.
- **Explain project words once**, or link to the note that does. "alm-simulation-benchmark (the simulation harness in sandbox/alm-simulation-benchmark)".
- **Keep exact text exact.** Commands, paths, errors, IDs and quotations go in backticks and are never paraphrased.
- **State doubt once.** A cause you haven't confirmed is a hypothesis: say so in one sentence, and update the note when you know. Don't stack qualifiers ("could potentially possibly").
- **Vary sentence length.** A list of same-shaped sentences reads like a form.

## What to cut

Each of these makes a note longer without adding a fact. The section numbers point to humanizer v4.0.0.

| Habit | Example | Instead |
|---|---|---|
| Contrast that only adds weight (§1) | "This is not a cache, it's a source of truth." | "The index is rebuilt from the notes." |
| Closer that restates (§2) | "That is the real win." | End on the last fact. |
| Run-up before the point (§4) | "Here's the thing: ..." | Make the point. |
| Arguing with no one (§5) | "To be clear, this is not about speed." | Leave it out, or state the actual claim. |
| Forced triad (§6) | "fast, reliable and scalable" | Name only what is true and matters. |
| Em or en dash (§8) | "The plan — once approved — applies." | Comma, colon, period or parentheses. |
| Stock AI words (§12) | delve, pivotal, crucial, seamless, robust (figurative), additionally, underscore, showcase, testament | The plain word, or nothing. |
| Inflated significance (§13) | "a pivotal step that reflects a broader shift" | The fact without the claim. |
| -ing tail (§15) | "..., ensuring consistency across harnesses." | Say what it does, or cut it. |
| Bold for emphasis (§19) | "the **single local store** is shared" | No bold. A leading label (`**Cause:**`) is fine. |
| Emoji, Title Case headings, rules (§20) | "## 🚀 Next Steps", `---` | "## Next steps". No rules. |
| Chat residue (§22) | "Let me know if ...", "I hope this helps" | Leave it out. |
| Writing about the note (§25) | "This note was added to replace ...", "summarized below" | Write about the subject. |
| Re-explaining what the reader has (§26) | restating the problem before the decision | Lead with the decision. |

A word on the list is not wrong in itself. "Key" in "API key", "robust" in a statistics note and "gate" in "the release gate" are literal. The warnings skip those words for that reason.

## Rewriting a note

Rewrite prose and structure only. Keep every fact, name, number, date, path, command, ID and link target. Never change the title: `[[links]]` and filenames depend on it. If the old text lost something (a summary cut off mid-sentence, for example), find it in the source or ask; don't guess.

Several notes are rewritten together through one reviewed plan:

```
living_memory.py rewrite-plan --payload-file rewrite.json
living_memory.py inspect-plan PLAN_ID --full
living_memory.py apply PLAN_ID --expected-plan-sha256 DIGEST
```

`rewrite.json` is a list of `{"ref": "domain:id", "revision": "...", "body": "..."}`, with the revision each new body was written from. A rewrite keeps each note's `updated` and `review_by`, because the facts did not change. Maps, briefs and learned runbooks are refused: maps and briefs keep their labels, and learned runbooks change through learning review.
