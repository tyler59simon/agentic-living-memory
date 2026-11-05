# Recovery and history

Every canonical/configuration mutation records preimages, postimages, hashes, and durable intent under STORE/.operations/OPERATION/journal.json before writes. A single OS store lock serializes cooperating writers. A crash can leave a partially applied operation; it is recoverable, not instantly atomic across the entire filesystem.

Run `doctor` to discover pending operations, `history` to inspect receipts, and `recover OPERATION --direction forward` to finish or `--direction backward` to restore. Recovery preflights every touched path against known pre/post hashes before the first write, preserves unknown user edits, validates journal content, and can resume after another interruption. Rebuild derived indexes/views after external repair; CLI recovery attempts reconciliation for affected domains. Canonical Markdown remains authoritative if derived regeneration fails.

A completed operation can be reversed through `undo OPERATION`, followed by inspection and approved application of the resulting plan. It rejects later conflicting edits. Keep journals/backups private: they contain historical note and configuration preimages. Maintenance telemetry reset does not delete these recovery records.

Direct install recovery is rooted at CODEX_HOME. Use `living_memory.py --root CODEX_HOME history` and `recover` there for package/hook operations. The standalone installer lifecycle retains legacy receipts, durable package intents, and restartable rollback; rerunning its approved Python API plan resumes an interrupted operation. Receipt paths and hashes must remain intact.

Do not edit ownership records to suppress conflicts. Save and review modified files, then create a new plan. Corrupt JSON is preserved and reported rather than replaced. Recovering an older version may also require its compatible store schema; this migration intentionally preserves canonical note bytes.
