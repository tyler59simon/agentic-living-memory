# Security

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the repository's Security tab and choose "Report a vulnerability". Do not open a public issue for a security problem. Include the version (`alm --version`), your platform and the steps to reproduce.

## What Living Memory protects, and what it does not

Living Memory is a single-user, local store. It runs only when a hook or a CLI command runs; there is no daemon and no server.

- **Governance is cooperative.** Agents change memory through the `alm` CLI, which checks revisions, journals every write and makes it undoable. Hooks steer agents to that path and refuse some direct edits, but any process running as your user can still edit the store's files. Living Memory is not a sandbox against a hostile agent or local malware.
- **Data stays on your computer by default.** The store is plain files in the folder you chose at setup. Nothing is uploaded.
- **Network use is opt-in.** Only these reach the network, and only when you run them:
  - `semantic install` downloads the embedding model and checks its hashes.
  - Confluence and Notion sources (`source-sync` on a remote source) fetch pages over https. The token is read from the environment variable the source names and is never stored.
- **Model calls are opt-in.** Note maintenance (`maintenance enable`) and the agents started by `scripts/ingest_run.py` run the `claude` CLI, which sends note text and file contents to the model provider. Both are off until you turn them on.
- **Nothing is erased automatically.** Journals, undoable deletes and backups keep note text. Do not store secrets in notes; capture refuses text that looks like a vendor credential, but that check is a pattern match.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the stated limits.
