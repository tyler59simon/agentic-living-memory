"""Shared helpers for the pages of folder notebooks (core/folder_docs.py): staged page edits, journaled commits, journal
retention and the privacy of their operations. Hosted notebooks, which kept their pages inside the store, were retired in CON-18.

A staged page edit is a record under `domains/D/folder-proposals/<nb>/<id>.json`. Every write is one journaled transaction
whose kind starts with `doc_`, undone with `undo OPERATION`. Pages are never notes: no list, review, maintenance, SessionStart
or wiki path reads them."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from .repository import atomic_write, find_secret, is_link_or_junction, now, read_json, store_lock, transaction

MAX_PAGE_BYTES = 4 * 1024 * 1024
BAD_CHARS = set('/\\:*?"<>|')
DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
PROPOSAL_ID = re.compile(r"[a-f0-9]{12}")


# ----------------------------------------------------------------------------------------------- names

def _check_part(part):
    if not 1 <= len(part) <= 120:
        raise ValueError("Each part of a path is 1 to 120 characters")
    if any(c in BAD_CHARS or ord(c) < 32 or ord(c) == 127 for c in part):
        raise ValueError(f'"{part}" holds a character a page name cannot have (/ \\ : * ? " < > | or a control character)')
    if part != part.strip() or part.endswith("."):
        raise ValueError(f'"{part}" starts or ends with a space, or ends with a dot')
    if part.split(".")[0].casefold() in DEVICES:
        raise ValueError(f'"{part}" is a Windows device name')


_is_link = is_link_or_junction


# ----------------------------------------------------------------------------------------------- journaled commits

DOC_OP_PREFIX = "doc-"
KEEP_VERSIONS = 100
KEEP_DAYS = 90


def _commit(root, changes, kind, meta, summary, expected=None):
    operation_id = DOC_OP_PREFIX + uuid.uuid4().hex
    receipt = transaction(root, changes, kind, expected, operation_id, meta=meta)
    record_index(root, operation_id, changes)   # retention reads this instead of every journal's page text
    receipt.update(kind=kind, summary=summary, undo=f"undo {operation_id}")
    return receipt


def prune_journals(root, keep=KEEP_VERSIONS, days=KEEP_DAYS):
    """Retention for notebook operations: a finished `doc-` operation is deleted once it is not among the newest `keep`
    operations of any file it changed, or once it is older than `days`, whichever comes first. A folder proposal's journal holds
    the page text before and after, so without a bound a page edited often would grow the store without limit.
    Returns how many were removed."""
    import shutil
    from datetime import timedelta, timezone
    from . import clock
    from .common import parse_iso
    from .repository import PENDING, _TAIL_STATE, FINAL_STATES
    root = Path(root).resolve()
    cutoff = clock.now() - timedelta(days=days)
    operations = root / ".operations"
    rows = []
    folders = (set(operations.glob(DOC_OP_PREFIX + "*")) | {p.parent for p in operations.glob("*/doc.json")}) if operations.is_dir() else set()
    for folder in sorted(folders):
        journal = folder / "journal.json"
        if (folder / PENDING).exists() or not journal.is_file():
            continue
        try:
            with journal.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 64))
                match = _TAIL_STATE.search(handle.read())
            index = read_json(folder / "doc.json", None)
            if not isinstance(index, dict):
                value = read_json(journal)
                index = {"created": value.get("created"), "paths": [e.get("path") for e in value.get("effects") or []]}
                match = match or re.search(rb'"state":"([a-z_]+)"', json.dumps(value).encode())
            created = parse_iso(str(index.get("created")))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if not match or match[1].decode() not in FINAL_STATES:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        rows.append((created, folder, [p for p in index.get("paths") or [] if isinstance(p, str)]))
    newest = set()
    by_path = {}
    for created, folder, paths in rows:
        for path in paths:
            by_path.setdefault(path, []).append((created, folder.name))
    for ops in by_path.values():
        newest |= {name for _, name in sorted(ops, reverse=True)[:keep]}
    candidates = [folder for created, folder, _ in rows if not (folder.name in newest and created >= cutoff)]
    removed = 0
    for folder in candidates:
        with store_lock(root):
            journal = folder / "journal.json"
            if not folder.is_dir() or (folder / PENDING).exists() or not journal.is_file():
                continue
            try:
                with journal.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    handle.seek(max(0, handle.tell() - 64))
                    match = _TAIL_STATE.search(handle.read())
                if not match:
                    value = read_json(journal)
                    match = re.search(rb'"state":"([a-z_]+)"', json.dumps(value).encode())
            except (OSError, ValueError, TypeError):
                continue
            if not match or match[1].decode() not in FINAL_STATES:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


# ----------------------------------------------------------------------------------------------- pages

def _check_body(body):
    if len(body) > MAX_PAGE_BYTES:
        raise ValueError(f"A page is at most {MAX_PAGE_BYTES // (1024 * 1024)} MiB")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("A page is UTF-8 text") from None
    found = find_secret(text)
    return [f"possible credential ({found[0]} at character {found[1]}); pages keep it as written"] if found else []


def _iso(mtime):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ----------------------------------------------------------------------------------------------- proposals

def _proposal_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _read_proposal(file):
    try:
        value = read_json(file, None)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _find_proposal(root, pid):
    """(domain, notebook, file, record) of a folder notebook page proposal. Domain and notebook come from where the file is
    stored, never from the record's own fields."""
    if not isinstance(pid, str) or not PROPOSAL_ID.fullmatch(pid):
        raise ValueError("A proposal id is 12 hexadecimal characters")
    for file in Path(root).glob(f"domains/*/folder-proposals/*/{pid}.json"):
        domain, nb = file.parts[-4], file.parts[-2]
        cursor = Path(root)
        if any(_is_link(cursor := cursor / part) for part in ("domains", domain, "folder-proposals", nb)) or _is_link(file):
            continue
        value = _read_proposal(file)
        if value is not None and value.get("kind") == "folder":
            return domain, nb, file, value
    raise ValueError(f"not_found: no proposal {pid}")


def proposals(root, domains, nb=None, include_private=False, status="pending", limit=50, session=None):
    """Page proposals of the folder notebooks of these domains (folder_docs.rows). session limits them to one session."""
    from . import folder_docs
    rows = folder_docs.rows(root, domains, nb, include_private, status, session)
    rows.sort(key=lambda r: str(r["created"]), reverse=True)
    return {"ok": True, "proposals": rows[:limit]}


def show_proposal(root, pid, include_private=False):
    from . import folder_docs
    _, _, _, value = _find_proposal(root, pid)
    folder_docs.check_visible(root, value, include_private)
    return {"ok": True, "proposal": value}


def accept(root, pid, expected=None, include_private=False, body=None):
    """Write a proposal's text to its page. Refused when the page changed since the proposal's base revision.

    With `body` (`--edited`), the user's merge of the proposal is written instead and the proposal is settled as accepted
    and edited in the same operation. The page must then be at `expected`, the revision the merge was made against, or not
    exist when `expected` is None."""
    from . import folder_docs
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, file, value = _find_proposal(root, pid)
        return folder_docs.accept(root, pid, file, value, expected, include_private, body)


def reject(root, pid, reason=None, include_private=False):
    from . import folder_docs
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, file, value = _find_proposal(root, pid)
        return folder_docs.reject(root, pid, file, value, reason, include_private)


# ----------------------------------------------------------------------------------------------- privacy

FOLDER_PROPOSALS = re.compile(r"domains/[^/]+/folder-proposals/")   # staged edits of folder notebook pages (folder_docs): their text is outside the store
# The folder registry and the proposals are Hivemind's bookkeeping. notebook-sync.json is the retired Obsidian page mirror's state: old journals still name it.
BOOKKEEPING = re.compile(r"notebook-folders\.json|domains/[^/]+/notebook-sync\.json|domains/[^/]+/folder-proposals/.+")


def is_private_path(path):
    """Whether `path` is bookkeeping about folder notebooks: shown in history and diff only with --include-private."""
    return bool(BOOKKEEPING.fullmatch(str(path).replace("\\", "/").casefold()))


def redact_summary(summary):
    """An operation summary without the bookkeeping paths it names."""
    if not isinstance(summary, dict):
        return summary

    def clean(value):
        if isinstance(value, list):
            return [clean(v) for v in value if not is_private_path(v)]
        if isinstance(value, dict):
            return redact_summary(value)
        return value
    return {key: clean(value) for key, value in summary.items() if not is_private_path(value)}


def is_notebook_journal(effects):
    """Whether an operation changed only folder notebook proposals: a notebook operation, whatever its kind."""
    paths = [e.get("path") for e in effects or [] if isinstance(e, dict)]
    return bool(paths) and all(isinstance(p, str) and FOLDER_PROPOSALS.match(p) for p in paths)


def record_index(root, operation_id, paths, created=None):
    """The small doc.json beside a notebook operation's journal that retention reads instead of the journal."""
    try:
        target = Path(root) / ".operations" / operation_id / "doc.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, json.dumps({"created": created or now(), "paths": sorted(paths)}, ensure_ascii=False) + "\n")
    except OSError:
        pass   # retention falls back to the journal


def _folder_proposal_private(root, journal):
    """Whether any folder-proposal effect in `journal` belongs to a private folder notebook or cannot be decoded (fail closed)."""
    import base64
    from . import folder_docs
    effects = [e for e in (journal.get("effects") or []) if isinstance(e, dict) and FOLDER_PROPOSALS.match(str(e.get("path", "")))]
    if not effects:
        return False
    for e in effects:
        seen = False
        for side in ("after", "before"):
            raw = e.get(side)
            if raw is None:
                continue
            seen = True
            try:
                record = json.loads(base64.b64decode(raw))
            except (ValueError, TypeError):
                return True
            if not isinstance(record, dict) or folder_docs.hidden(root, record, include_private=False):
                return True
        if not seen:
            return True
    return False


def journal_private(root, journal, authorize=False):
    """Whether an operation touched a private folder notebook. Without `authorize` (what history shows) any bookkeeping path
    counts. With it (whether the operation may be undone or recovered without --include-private) only a folder proposal of a
    private or unreadable folder does: the registry and the proposals are otherwise open to the main actor."""
    if not isinstance(journal, dict):
        return False
    if authorize:
        return _folder_proposal_private(root, journal)
    return any(is_private_path(e.get("path", "")) for e in journal.get("effects") or [] if isinstance(e, dict))
