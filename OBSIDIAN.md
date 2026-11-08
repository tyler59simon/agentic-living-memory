# Managed Obsidian overlay

`overlay-build VAULT --domain DOMAIN` builds this top-level layout:

| Folder | Contents |
| --- | --- |
| `Domains/DOMAIN/` | Managed copies of active notes. New notes created here are captured. |
| `Archive/DOMAIN/` | Managed copies of retired notes. |
| `Dashboards/` | Home, Taxonomy, Stale Notes, Timeline, Domain Overview, Archive, and one Sync Status page per domain. Links are Obsidian wikilinks. |
| `Canvas/DOMAIN.canvas` | Four columns of active notes. An edge means one note links to another with `[[Title]]` (label `link`) or supersedes it; shared tags do not draw edges. Obsidian's reformatting on open is not an edit. |
| `Templates/` | One template per note type. Seeded once; your edits are kept and the folder is never scanned. |
| `.living-memory/` | Vault identity (`vault.json`) and the `copies.json`/`generated.json` ownership manifests (hidden by Obsidian). |

## When sync runs

The installed Claude Code `SessionEnd` hook runs `overlay-sync-all`, which syncs every domain that has a managed vault, continues past a failing domain, and records the outcome in the store's `hooks/last-sync.json`. Codex has no session-end event, so a Codex session start runs the same sync when the last one is more than 15 minutes old. Any agent write to memory also refreshes that domain's vault. Run `overlay-sync-all` yourself after editing in Obsidian without an agent session. `--primary DOMAIN` chooses which domain the shared dashboards describe; by default it is the domain with the most notes.

Each Sync Status page starts with the time of the last sync that changed something and what it did, or the error if it failed. A sync that finds nothing to do rewrites no file and journals nothing; the time of the latest run is in `hooks/last-sync.json`. Journals of regenerated views and sync bookkeeping (`overlay_views`, `overlay_sync`, `overlay_state`) are deleted after 14 days by session housekeeping; journals of note edits are kept.

## Editing in the vault

Vault copies use the same layout as canonical notes, described in [NOTE_TYPES.md](NOTE_TYPES.md#note-layout): properties, a `# 🎯 Title` heading, then the Executive Summary, Key Details, Resource Links, Action Items and Important Note sections.
- The heading follows the `title` property, so editing the heading has no effect.
- Templates follow the same layout. Sections you leave unfilled are dropped when the note is captured.
- A section edit that breaks the layout rules is listed under "Couldn't save". Examples: removing the Executive Summary, or adding Action Items to a knowledge note.

The vault is your own editing surface, so routine edits need no approval:

- **Applied at sync:** edits to an active note's title, body, tags, type, context or review_by, and valid new notes in `Domains/DOMAIN/`. Each is one journaled transaction (`overlay_auto_update` / `overlay_capture`) listed under "Recently applied" with its undo command. A title or type change renames the canonical file and rewrites Markdown links that point to it.
- **Still being edited:** a file saved in the last 30 seconds is left alone and picked up at the next sync, so a sync never races an open editor.
- **Approved by checkbox:** deleting an active note's file, or setting `status` to `archived` or `resolved`. Sync creates a plan and lists it on `Dashboards/Sync Status - DOMAIN.md` as `- [ ] Action: Title <!-- lm:PLAN_ID -->`. Ticking the box applies it at the next sync, and it then appears under "Recently applied" with an undo command. Only plans that this domain's vault sync created can be approved this way. Restoring a deleted file cancels the deletion.
- **Renaming a file** in Obsidian is followed by its `id`: no deletion is proposed and the file gets its memory name back. To rename a note, change its `title` property.
- **Properties memory does not store** (`aliases`, `cssclasses`, your own keys) stay in the vault copy and are carried over when memory updates the note.
- **New notes from a template** are captured once their text replaces the template prompt. A note inserted from a template needs no `id`: sync assigns one, takes the domain from the folder, captures it, and replaces the vault file with the canonically named copy. Frontmatter may use Obsidian's YAML block lists for tags.
- **Not from the vault:** supersede, restoring an archived note, taxonomy expansion and bulk changes. Use the script or ask an agent.

## Conflicts and recovery

- **Changed on both sides** (you edited a note while an agent updated it): memory's version is written to the vault, your version is saved under the store's `domains/DOMAIN/staged/`, and the Sync Status page offers `- [ ] Keep my vault version`. Ticking it applies your saved version at the next sync. Nothing is ever stuck: later edits apply normally.
- **A rename blocked by another file** (memory renames a note's copy, but a file it does not manage already has the new name): that file is left alone, the copy keeps its old name, and the status page lists the conflict until the file is moved or renamed.
- **New notes saved again during a sync** are left in place and captured with the newer text at the next sync.
- **Edited dashboards or canvases** are preserved and no longer regenerated; the status page lists them. Delete the file to get a fresh copy. A dashboard edit never stops a sync.
- **Edits that fail validation** (unknown tags, credentials, a changed `id`, missing frontmatter) leave canonical memory untouched and are reported under "Couldn't save" until the file changes again. Edits to archived copies are saved aside and the copy is restored.
- **A new, moved or rebuilt vault** is recognised by its `.living-memory/vault.json` identity: notes are copied in and nothing is proposed for deletion. If many managed copies disappear at once (for example a deleted `Domains/DOMAIN` folder), they are restored instead of proposed for deletion.
- `overlay-teardown-plan VAULT` proposes deletion only of unchanged managed files, including the vault identity; apply that plan with `--root VAULT`. After teardown, agent writes no longer rebuild the vault until you run `overlay-build` again.

Every vault edit is also kept under the canonical store's staged directory. The PreToolUse hook denies agent file edits under the vault's `Domains/`, `Archive/` and `Dashboards/` folders, so agent changes go through scripts. Overlay ownership is logical, not enforced by OS permissions. Vaults built with the earlier single `Living Memory/` folder migrate on the next sync or build.

A vault's six shared dashboards describe the most recently built domain, and each domain also has its own Sync Status page; per-domain notes and canvases coexist. Sync across the canonical store and vault uses separate durable transactions; a failed second phase is repaired by rerunning sync.
