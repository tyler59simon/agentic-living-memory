# Controlled faceted tags

New capture requires at least one registered `subject/` and `concern/` tag. Optional `platform/` and `technology/` tags add implementation context. Use `taxonomy` to list allowed values. An absent taxonomy can be initialized by the approved migration plan; `taxonomy-plan TAG...` proposes expansions.

The `project/` facet marks the work a note belongs to: `capture --project SLUG` adds `project/SLUG`. Project tags are created only by `project-plan create`, which registers the tag, a `projects.json` entry and a charter note in one reviewed plan; `taxonomy-plan` refuses `project/*`, and `retag-plan` accepts a `project/*` target only for a registered slug.

Existing legacy tags are preserved during migration. `retag-plan` accepts a JSON object mapping old tags to new faceted values and updates affected notes and taxonomy in one reviewed transaction. A single-note retag is a normal note plan. Archives and superseded notes retain stable identities and remain available to history and to searches with an explicit status filter (`--filter status=superseded`); other filters narrow active notes only.

Note types describe the purpose of a note; tags describe what it concerns. See [note types and use cases](NOTE_TYPES.md). PROGRESS has merged into STATUS. CORRECTION is represented by type KNOWLEDGE with the existing `concern/correction` tag; no new type-specific tags are created automatically. Type STATUS is separate from lifecycle status (active, resolved, archived, superseded).
