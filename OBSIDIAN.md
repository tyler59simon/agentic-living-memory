# Managed Obsidian overlay

`overlay-build VAULT --domain DOMAIN` builds this top-level layout:

| Folder | Contents |
| --- | --- |
| `Domains/DOMAIN/` | Managed copies of active notes. New notes created here are captured. |
| `Archive/DOMAIN/` | Managed copies of retired notes. |
| `Dashboards/` | Home, Taxonomy, Stale Notes, Timeline, Domain Overview, Recap, Archive, and one Sync Status page per domain. Links are Obsidian wikilinks. `Recap.md` is the `recap` command's page for the last seven days of the domain; it is computed once per local day and cached in `.cache/recap-DOMAIN.json`, and shows "Recap unavailable" if the recap fails. |
| `Canvas/DOMAIN.canvas` | Four columns of active notes. An edge means one note links to another with `[[Title]]` (label `link`) or supersedes it; shared tags do not draw edges. Obsidian's reformatting on open is not an edit. |
| `Templates/` | One template per note type. Seeded once; your edits are kept and the folder is never scanned. |
| `.living-memory/` | Vault identity (`vault.json`) and the `copies.json`/`generated.json` ownership manifests (hidden by Obsidian). |

## When sync runs

The installed Claude Code `SessionEnd` hook runs `overlay-sync-all`, which syncs every domain that has a managed vault, continues past a failing domain, and records the outcome in the store's `hooks/last-sync.json`. Codex has no session-end event, so a Codex session start runs the same sync when the last one is more than 15 minutes old. Any agent write to memory also refreshes that domain's vault: a background process syncs it once writes pause for 2 seconds, so the vault catches up a few seconds after the write returns (with `LIVING_MEMORY_NO_BACKGROUND=1`, before it returns). Run `overlay-sync-all` yourself after editing in Obsidian without an agent session. `--primary DOMAIN` chooses which domain the shared dashboards describe; by default it is the domain with the most notes.

Each Sync Status page starts with the time of the last sync that changed something and what it did, or the error if it failed. A sync that finds nothing to do rewrites no file and journals nothing; the time of the latest run is in `hooks/last-sync.json`. Journals of regenerated views and sync bookkeeping (`overlay_views`, `overlay_sync`, `overlay_state`) are deleted after 14 days by session housekeeping; journals of note edits are kept.

## Editing in the vault

Vault copies use the same layout as canonical notes, described in [NOTE_TYPES.md](NOTE_TYPES.md#note-layout): properties, a `# Title` heading, then the summary, an Important caveat, the type's own sections, Details, Links and Action items.
- The heading follows the `title` property, so editing the heading has no effect.
- Templates follow the same layout. Sections you leave unfilled are dropped when the note is captured.
- A section edit that breaks the layout rules is listed under "Couldn't save". Examples: removing the summary, or adding action items to a knowledge note.

The vault is your own editing surface, so edits there need no approval:

- **Applied at sync:** edits to an active note's title, body, tags, type, context or review_by, and valid new notes in `Domains/DOMAIN/`. Each is one journaled transaction (`overlay_auto_update` / `overlay_capture`) listed under "Recently applied" with its undo command. A title or type change renames the canonical file and rewrites Markdown links that point to it.
- **Still being edited:** a file saved in the last 30 seconds is left alone and picked up at the next sync, so a sync never races an open editor.
- **Deleted, archived or resolved:** deleting an active note's file, or setting `status` to `archived` or `resolved`, is applied at the next sync directly, as one journaled transaction, and listed under "Recently applied" on `Dashboards/Sync Status - DOMAIN.md` with its undo command. Restoring a deleted file before that sync cancels the deletion; afterwards the undo command brings the note back.
- **Renaming a file** in Obsidian is followed by its `id`: it is not treated as a deletion and the file gets its memory name back. To rename a note, change its `title` property.
- **Properties memory does not store** (`aliases`, `cssclasses`, your own keys) stay in the vault copy and are carried over when memory updates the note.
- **New notes from a template** are captured once their text replaces the template prompt. A note inserted from a template needs no `id`: sync assigns one, takes the domain from the folder, captures it, and replaces the vault file with the canonically named copy. Frontmatter may use Obsidian's YAML block lists for tags.
- **Not from the vault:** supersede, restoring an archived note, taxonomy expansion and bulk changes. Use the script or ask an agent.

## Notebooks

Living Memory no longer mirrors notebook pages into the vault (hosted notebooks were retired on 2026-11-24). Copies an earlier version left in `Notebooks/<domain>/<notebook>/` are plain files now: they stay, agents may not write them, and they are never indexed as a source. A leftover `domains/<domain>/notebook-sync.json` is ignored and removed by the next sync. Folder notebooks are folders you keep yourself, outside the vault. See [NOTEBOOKS.md](NOTEBOOKS.md#obsidian).

## Conflicts and recovery

- **Changed on both sides** (you edited a note while an agent updated it): memory's version is written to the vault, your version is saved under the store's `domains/DOMAIN/staged/`, and the Sync Status page offers `- [ ] Keep my vault version`. Ticking it applies your saved version at the next sync. Nothing is ever stuck: later edits apply normally.
- **Deleted in memory after you edited it** (an agent's `delete` or `merge` removed a note whose vault copy you had changed): your version is saved under `domains/DOMAIN/staged/` once, the copy stays in the vault, and the status page lists the conflict until you delete that copy. To bring the note back, capture it again from the saved version.
- **A rename blocked by another file** (memory renames a note's copy, but a file it does not manage already has the new name): that file is left alone, the copy keeps its old name, and the status page lists the conflict until the file is moved or renamed.
- **New notes saved again during a sync** are left in place and captured with the newer text at the next sync.
- **Edited dashboards or canvases** are preserved and no longer regenerated; the status page lists them. Delete the file to get a fresh copy. A dashboard edit never stops a sync.
- **Edits that fail validation** (unknown tags, credentials, a changed `id`, missing frontmatter) leave canonical memory untouched and are reported under "Couldn't save" until the file changes again. Edits to archived copies are saved aside and the copy is restored.
- **A new, moved or rebuilt vault** is recognised by its `.living-memory/vault.json` identity: notes are copied in and nothing is deleted. If many managed copies disappear at once (for example a deleted `Domains/DOMAIN` folder), they are restored instead of deleted.
- `overlay-teardown VAULT` deletes only unchanged managed files, including the vault identity, in one write journaled in the vault (the harness asks the user first); undo it with `undo OPERATION` and `--root VAULT`. After teardown, agent writes no longer rebuild the vault until you run `overlay-build` again.

Every vault edit is also kept under the canonical store's staged directory. The PreToolUse hook denies agent file edits under the vault's `Domains/`, `Archive/`, `Dashboards/` and `Notebooks/` folders, so agent changes go through scripts. Overlay ownership is logical, not enforced by OS permissions. Vaults built with the earlier single `Living Memory/` folder migrate on the next sync or build.

The global domain gets its own folder once you run `overlay-build VAULT --domain _global`; from then on it syncs like any other domain. When a note is promoted to the global domain, the next sync of its old domain removes the old copy if you had not edited it (an edited copy is kept and reported as a conflict), and the global domain's sync writes the new copy under `Domains/_global/`.

A vault's seven shared dashboards describe the most recently built domain, and each domain also has its own Sync Status page; per-domain notes and canvases coexist. Sync across the canonical store and vault uses separate durable transactions; a failed second phase is repaired by rerunning sync.
