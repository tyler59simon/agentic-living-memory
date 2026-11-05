# Maintenance

There are no background engines and no usage telemetry. Maintenance is the notes themselves plus a few explicit commands:

- `validate` and `doctor` report notes that fail to parse, duplicate IDs, and pending operations.
- `remap-plan` moves notes between domains with stable IDs as a reviewed, undoable plan.
- `merge-plan`, `retag-plan` and `taxonomy-plan` handle bulk changes through reviewed plans.

Stores created by earlier releases may contain an `improvement/` folder (categorical search/read events and engine state). Nothing reads it any more; it is safe to delete by hand.
