"""Notebook pages (Notes Studio, NS-2A): a record kind of their own, apart from notes.

A page is free-form UTF-8 text kept byte for byte under `domains/D/notebooks/<nb>/`; attachments live in the notebook's
`assets/` folder and staged agent edits in `.proposals/`. Every write is one journaled transaction whose kind starts
with `doc_`, undone with `undo OPERATION`. Pages are never notes: no list, review, maintenance, SessionStart or wiki
path reads them. Design: docs/notebooks-design.md."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from .common import canonical_domain, load_bindings
from .paths import is_os_junk, name_within, remove_empty_dir, same_name
from .repository import atomic_write, blob_store, digest, find_secret, is_link_or_junction, now, read_json, store_lock, transaction

MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_PAGES = 20_000
MAX_ASSET_TOTAL = 2 * 1024 ** 3
MAX_META_BYTES = 256 * 1024
MAX_DEPTH = 8
MAX_REWRITE = 200
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,39}")
VISIBILITY = ("hidden", "searchable", "injectable")
RESERVED = {"notebook.json", "assets", ".proposals"}
BAD_CHARS = set('/\\:*?"<>|')
DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
PROPOSAL_ID = re.compile(r"[a-f0-9]{12}")
PRIVACY = "notebook-privacy.json"
LINK = re.compile(r"\[\[([^\[\]|#\n]+)((?:#[^\[\]|\n]*)?(?:\|[^\[\]\n]*)?)\]\]")
PAGES_GONE = "not_found"


# ----------------------------------------------------------------------------------------------- names and paths

def _check_part(part):
    if not 1 <= len(part) <= 120:
        raise ValueError("Each part of a path is 1 to 120 characters")
    if any(c in BAD_CHARS or ord(c) < 32 or ord(c) == 127 for c in part):
        raise ValueError(f'"{part}" holds a character a page name cannot have (/ \\ : * ? " < > | or a control character)')
    if part != part.strip() or part.endswith("."):
        raise ValueError(f'"{part}" starts or ends with a space, or ends with a dot')
    if part.split(".")[0].casefold() in DEVICES:
        raise ValueError(f'"{part}" is a Windows device name')


def clean_path(path, page=True):
    """A page (`page`) or folder path inside a notebook, checked part by part, with `/` between parts."""
    if not isinstance(path, str) or not path:
        raise ValueError("A path is required")
    if "\\" in path:
        raise ValueError("Use / between folders")
    parts = path.split("/")
    if len(parts) > MAX_DEPTH:
        raise ValueError(f"Pages nest at most {MAX_DEPTH} levels deep")
    for part in parts:
        _check_part(part)
    if parts[0].casefold() in RESERVED:
        raise ValueError(f"{parts[0]} is reserved at the top of a notebook")
    if page and not parts[-1].casefold().endswith(".md"):
        raise ValueError("Page names end in .md")
    return "/".join(parts)


def _slug(nb):
    if not isinstance(nb, str) or not SLUG.fullmatch(nb):
        raise ValueError("A notebook id is 2 to 40 lowercase letters, digits or hyphens, starting with a letter or digit")
    return nb


def _domain(root, domain):
    if not domain:
        raise ValueError("A domain is required")
    name = canonical_domain(domain, root)
    if not (Path(root) / "domains" / name).is_dir() and not is_notebook_only(root, name):
        raise ValueError(f"not_found: no domain {name}")
    return name


_is_link = is_link_or_junction


def _rel(domain, nb, path=""):
    return f"domains/{domain}/notebooks/{nb}" + (f"/{path}" if path else "")


def _resolve(root, domain, nb, path=""):
    """The absolute path of `path` inside the notebook. Every part from `domains` down must not be a link or junction:
    `repository.confined` misses junctions, and an alias could reach another notebook or the rest of the store."""
    root = Path(root)
    cursor = root
    for part in ["domains", domain, "notebooks", nb] + (path.split("/") if path else []):
        cursor = cursor / part
        if _is_link(cursor):
            raise ValueError("Links and junctions are not allowed inside a notebook")
    return cursor


def _clash(root, domain, nb, path):
    """Refuse a new page or folder whose name, or the name of a folder on its way, differs only in letter case from one
    already there: names are unique ignoring case within a folder, so the notebook works the same on every system."""
    folder = _resolve(root, domain, nb)
    for part in path.split("/"):
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            return
        twin = next((n for n in names if same_name(n, part) and n != part), None)
        if twin:
            raise ValueError(f'"{part}" differs only in letter case from "{twin}", which already exists')
        folder = folder / part


# ----------------------------------------------------------------------------------------------- notebooks

def _meta_text(meta):
    return json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _read_meta(root, domain, nb):
    path = _resolve(root, domain, nb, "notebook.json")
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ValueError(f"not_found: no notebook {nb} in {domain}") from None
    try:
        meta = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError(f"{_rel(domain, nb, 'notebook.json')} is damaged; run doctor") from None
    if not isinstance(meta, dict):
        raise ValueError(f"{_rel(domain, nb, 'notebook.json')} is damaged; run doctor")
    return meta, digest(data)


def _open(root, domain, nb, include_private=False):
    """(domain, notebook id, meta, revision) of a notebook the caller may see. A private notebook looks missing unless
    `include_private`, so its name is not given away either."""
    domain, nb = _domain(root, domain), _slug(nb)
    meta, revision = _read_meta(root, domain, nb)
    if meta.get("private") and not include_private:
        raise ValueError(f"not_found: no notebook {nb} in {domain}")
    return domain, nb, meta, revision


def _visibility(value):
    if value not in VISIBILITY:
        raise ValueError("Visibility is hidden, searchable or injectable")
    return value


def _title(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError("A notebook title is 1 to 200 characters on one line")
    return value.strip()


def _privacy(root, meta, domain, nb):
    """notebook-privacy.json with this notebook recorded, or None when it already is: every notebook that was ever private
    stays listed, deleted ones included, so its old operations stay redacted (design section 6)."""
    registry = read_json(Path(root) / PRIVACY, None) or {"version": 1, "notebooks": []}
    if not isinstance(registry, dict) or not isinstance(registry.get("notebooks"), list):
        raise ValueError(f"{PRIVACY} is damaged; run doctor")
    if any(isinstance(e, dict) and e.get("uid") == meta["uid"] and e.get("domain") == domain and e.get("notebook") == nb for e in registry["notebooks"]):
        return None
    registry["notebooks"].append({"uid": meta["uid"], "domain": domain, "notebook": nb, "since": now()})
    return json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


DOC_OP_PREFIX = "doc-"
KEEP_VERSIONS = 100
KEEP_DAYS = 90


def _commit(root, changes, kind, meta, summary, expected=None):
    operation_id = DOC_OP_PREFIX + uuid.uuid4().hex
    receipt = transaction(root, changes, kind, expected, operation_id, meta=meta)
    record_index(root, operation_id, changes)   # retention reads this instead of every journal's page text
    if meta and meta.get("domain") and meta.get("notebook") and not meta.get("folder_notebook"):   # a folder notebook has no store index
        from .docs_index import after_write
        after_write(root, meta["domain"], meta["notebook"])   # search sees the change at once; a missed reindex is caught by the next walk
    receipt.update(kind=kind, summary=summary, undo=f"undo {operation_id}")
    return receipt


def prune_journals(root, keep=KEEP_VERSIONS, days=KEEP_DAYS):
    """Retention for notebook operations: a finished `doc-` operation is deleted, with its attachment blobs, once it is not among
    the newest `keep` operations of any file it changed, or once it is older than `days`, whichever comes first. Page journals
    hold the page text before and after, so without a bound a page saved often would grow the store without limit.
    Returns how many were removed."""
    import shutil
    from datetime import timedelta, timezone
    from . import clock
    from .common import parse_iso
    from .repository import PENDING, _TAIL_STATE, FINAL_STATES
    root = Path(root).resolve()
    cutoff = clock.now() - timedelta(days=days)
    operations = root / ".operations"
    # Scan without store_lock so a large .operations/ folder never stalls hooks; re-check each candidate under lock.
    orphans = []
    for folder in sorted(operations.glob("*")) if operations.is_dir() else []:
        try:
            if folder.is_dir() and not (folder / PENDING).exists() and not (folder / "journal.json").exists() and \
                    {p.name for p in folder.iterdir()} <= {"blobs", "doc.json"}:
                orphans.append(folder)
        except OSError:
            continue
    for folder in orphans:
        with store_lock(root):
            # Under the store lock no write is between staging and its marker, so a folder with neither marker nor journal holds
            # only attachment bytes a crashed write staged; nothing refers to them.
            try:
                if folder.is_dir() and not (folder / PENDING).exists() and not (folder / "journal.json").exists() and \
                        {p.name for p in folder.iterdir()} <= {"blobs", "doc.json"}:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                continue
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


def _journal_meta(domain, nb, meta):
    return {"domain": domain, "notebook": nb, "uid": meta.get("uid")}


def _files(folder):
    """Every file under `folder` (relative, `/`-separated), never following a link, `folder` itself included."""
    if _is_link(folder):
        return []
    out = []
    for current, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not _is_link(Path(current) / d)]
        for name in names:
            full = Path(current) / name
            if not _is_link(full) and not is_os_junk(name):
                out.append(full.relative_to(folder).as_posix())
    return sorted(out)


def _pages(folder):
    return [p for p in _files(folder) if p.split("/")[0] not in RESERVED and p.casefold().endswith(".md")]


def _proposal_files(root, domain, nb):
    """The proposal files of a notebook, read only through a `.proposals` folder that is not a link or junction."""
    try:
        folder = _resolve(root, domain, nb, ".proposals")
    except ValueError:
        return []
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.json") if PROPOSAL_ID.fullmatch(p.stem) and not _is_link(p))


def is_notebook_only(root, domain):
    entry = (load_bindings(Path(root)).get("domains") or {}).get(domain)
    return isinstance(entry, dict) and bool(entry.get("notebook_only"))


def notebooks(root, domains, include_hidden=False, include_private=False):
    rows = []
    for domain in domains:
        base = Path(root) / "domains" / domain / "notebooks"
        if not base.is_dir() or _is_link(base):
            continue
        for folder in sorted(p for p in base.iterdir() if p.is_dir() and not _is_link(p) and SLUG.fullmatch(p.name)):
            try:
                meta, revision = _read_meta(root, domain, folder.name)
            except ValueError as exc:
                if str(exc).startswith("not_found"):
                    continue
                rows.append({"domain": domain, "notebook": folder.name, "error": str(exc)})
                continue
            if (meta.get("private") and not include_private) or (meta.get("visibility") == "hidden" and not include_hidden):
                continue
            assets = [p for p in _files(folder / "assets")] if (folder / "assets").is_dir() else []
            pending = sum(1 for p in _proposal_files(root, domain, folder.name) if (_read_proposal(p) or {}).get("status") == "pending")
            rows.append({"domain": domain, "notebook": folder.name, "title": meta.get("title"), "visibility": meta.get("visibility", "searchable"),
                         "private": bool(meta.get("private")), "revision": revision, "pages": len(_pages(folder)), "assets": len(assets),
                         "asset_bytes": sum((folder / "assets" / a).stat().st_size for a in assets), "proposals": pending,
                         "updated": meta.get("updated"), "settings": meta.get("settings") if isinstance(meta.get("settings"), dict) else {}})
    return {"ok": True, "notebooks": rows}


def create_notebook(root, domain, nb, title, visibility="searchable", private=False):
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb = _domain(root, domain), _slug(nb)
        folder = _resolve(root, domain, nb)
        if folder.exists() and _files(folder):
            raise ValueError(f"Notebook {nb} already exists in {domain}")
        stamp = now()
        meta = {"version": 1, "uid": uuid.uuid4().hex, "title": _title(title), "order": [], "icons": {}, "pinned": [],
                "visibility": _visibility(visibility), "private": bool(private), "created": stamp, "updated": stamp}
        changes = {_rel(domain, nb, "notebook.json"): _meta_text(meta)}
        if meta["private"]:
            changes[PRIVACY] = _privacy(root, meta, domain, nb)
        return _commit(root, changes, "doc_notebook", _journal_meta(domain, nb, meta),
                       {"domain": domain, "notebook": nb, "created": True}, {_rel(domain, nb, "notebook.json"): None})


def update_notebook(root, domain, nb, expected=None, title=None, visibility=None, private=None, order=None, icons=None, pinned=None, settings=None,
                    include_private=False):
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, meta, revision = _open(root, domain, nb, include_private)
        if expected is not None and expected != revision:
            raise ValueError(f"stale_revision: notebook.json of {nb} changed; read it again")
        new = dict(meta)
        if title is not None:
            new["title"] = _title(title)
        if visibility is not None:
            new["visibility"] = _visibility(visibility)
        if private is not None:
            new["private"] = bool(private)
        if order is not None:
            if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
                raise ValueError("order is a list of paths")
            new["order"] = order
        if icons is not None:
            if not isinstance(icons, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in icons.items()):
                raise ValueError("icons maps paths to icon names")
            new["icons"] = icons
        if pinned is not None:
            if not isinstance(pinned, list) or not all(isinstance(x, str) for x in pinned):
                raise ValueError("pinned is a list of paths")
            new["pinned"] = pinned
        if settings is not None:
            if not isinstance(settings, dict):
                raise ValueError("settings is a JSON object")
            new["settings"] = {**(meta.get("settings") if isinstance(meta.get("settings"), dict) else {}), **settings}
        if new == meta:
            return {"ok": True, "unchanged": True, "revision": revision}
        new["updated"] = now()
        text = _meta_text(new)
        if len(text.encode()) > MAX_META_BYTES:
            raise ValueError("notebook.json would be larger than 256 KiB")
        changes = {_rel(domain, nb, "notebook.json"): text}
        if new.get("private"):
            privacy = _privacy(root, new, domain, nb)
            if privacy:
                changes[PRIVACY] = privacy
        return _commit(root, changes, "doc_notebook", _journal_meta(domain, nb, new),
                       {"domain": domain, "notebook": nb, "updated": sorted(k for k in new if new.get(k) != meta.get(k) and k != "updated")},
                       {_rel(domain, nb, "notebook.json"): revision})


def delete_notebook(root, domain, nb, with_pages=False, include_private=False):
    """Delete a notebook. With pages it needs --with-pages and comes back as a plan to approve, like note deletion."""
    from .governance import plan
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private)
        folder = _resolve(root, domain, nb)
        pages = _pages(folder)
        if pages and not with_pages:
            raise ValueError(f"Notebook {nb} holds {len(pages)} pages; pass --with-pages to plan deleting them too")
        files = _files(folder)
        changes = {_rel(domain, nb, f): None for f in files}
        summary = {"domain": domain, "notebook": nb, "pages": len(pages), "files": len(changes)}
        if pages:
            # The plan must delete the notebook as it is when applied: a page saved meanwhile makes it stale (D3).
            return plan(root, changes, "doc_notebook_delete", (), summary, guards={"inventory": {_rel(domain, nb): files}})
        return _commit(root, changes, "doc_notebook_delete", _journal_meta(domain, nb, meta), summary)


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


def _page_info(path, rel):
    info = path.stat()
    data = path.read_bytes()
    return {"path": rel, "kind": "page", "size": info.st_size, "revision": digest(data), "updated": _iso(info.st_mtime)}


def _iso(mtime):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def list_pages(root, domain, nb, section=None, include_private=False):
    root = Path(root).resolve()
    domain, nb, meta, _ = _open(root, domain, nb, include_private)
    folder = _resolve(root, domain, nb)
    start = folder
    if section:
        section = clean_path(section, page=False)
        start = _resolve(root, domain, nb, section)
        if not start.is_dir():
            raise ValueError(f"not_found: no section {section}")
    rows = []
    for current, dirs, names in os.walk(start):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if not _is_link(here / d) and not (here == folder and d in RESERVED))
        for d in dirs:
            rows.append({"path": (here / d).relative_to(folder).as_posix(), "kind": "folder"})
        for name in sorted(names):
            full = here / name
            if _is_link(full) or (here == folder and name in RESERVED) or not name.casefold().endswith(".md"):
                continue
            rows.append(_page_info(full, full.relative_to(folder).as_posix()))
    return {"ok": True, "domain": domain, "notebook": nb, "title": meta.get("title"), "entries": rows}


def read_page(root, domain, nb, path, max_chars=None, include_private=False):
    root = Path(root).resolve()
    domain, nb, _, _ = _open(root, domain, nb, include_private)
    path = clean_path(path)
    full = _resolve(root, domain, nb, path)
    try:
        data = full.read_bytes()
    except FileNotFoundError:
        raise ValueError(f"not_found: no page {path}") from None
    text = data.decode("utf-8", errors="replace")
    result = {"ok": True, "domain": domain, "notebook": nb, "path": path, "text": text, "revision": digest(data), "size": len(data),
              "updated": _iso(full.stat().st_mtime)}
    if max_chars is not None and len(text) > max_chars:
        result.update(text=text[:max_chars], truncated=True)
    return result


def _count_check(folder, adding):
    if len(_pages(folder)) + adding > MAX_PAGES:
        raise ValueError(f"A notebook holds at most {MAX_PAGES} pages")


def create_page(root, domain, nb, path, body=b"", include_private=False):
    root = Path(root).resolve()
    warnings = _check_body(body)
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private)
        path = clean_path(path)
        full = _resolve(root, domain, nb, path)
        _clash(root, domain, nb, path)   # first: on Windows a twin differing in case also "exists"
        if full.exists():
            raise ValueError(f"{path} already exists")
        _count_check(_resolve(root, domain, nb), 1)
        rel = _rel(domain, nb, path)
        receipt = _commit(root, {rel: body}, "doc_create", _journal_meta(domain, nb, meta), {"domain": domain, "notebook": nb, "path": path},
                          {rel: None})
        receipt["revision"] = digest(body)
        if warnings:
            receipt.setdefault("warnings", []).extend(warnings)
        return receipt


def write_page(root, domain, nb, path, body, expected, include_private=False):
    root = Path(root).resolve()
    warnings = _check_body(body)
    if not expected:
        raise ValueError("--expected-revision is required: read the page first")
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private)
        path = clean_path(path)
        full = _resolve(root, domain, nb, path)
        if not full.exists():
            raise ValueError(f"not_found: no page {path}")
        current = digest(full.read_bytes())
        if current != expected:
            raise ValueError(f"stale_revision: {path} changed since revision {expected[:12]}; read it again")
        rel = _rel(domain, nb, path)
        if current == digest(body):
            return {"ok": True, "unchanged": True, "revision": current}
        receipt = _commit(root, {rel: body}, "doc_write", _journal_meta(domain, nb, meta), {"domain": domain, "notebook": nb, "path": path},
                          {rel: expected})
        receipt["revision"] = digest(body)
        if warnings:
            receipt.setdefault("warnings", []).extend(warnings)
        return receipt


def delete_page(root, domain, nb, path, expected, include_private=False):
    root = Path(root).resolve()
    if not expected:
        raise ValueError("--expected-revision is required: read the page first")
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private)
        path = clean_path(path)
        full = _resolve(root, domain, nb, path)
        if not full.is_file():
            raise ValueError(f"not_found: no page {path}")
        if digest(full.read_bytes()) != expected:
            raise ValueError(f"stale_revision: {path} changed since revision {expected[:12]}; read it again")
        rel = _rel(domain, nb, path)
        return _commit(root, {rel: None}, "doc_delete", _journal_meta(domain, nb, meta), {"domain": domain, "notebook": nb, "path": path},
                       {rel: expected})


def _stem(path):
    return path[:-3] if path.casefold().endswith(".md") else path


def _basename(path):
    return _stem(path).rsplit("/", 1)[-1]


def _rewrite_links(text, moved, unique_after, unique_before=lambda name: True):
    """Rewrite [[...]] links whose target is a moved page: a full path (with or without .md) becomes the new full path;
    a bare name that changed becomes the new name when that is unique in the notebook, else the new path. A bare name is
    rewritten only when it named one page before the move, so a link to an equal name elsewhere is left alone."""
    by_path = {_stem(old).casefold(): _stem(new) for old, new in moved.items()}
    by_name = {}
    for old, new in moved.items():
        if _basename(old).casefold() != _basename(new).casefold() and unique_before(_basename(old)):
            by_name.setdefault(_basename(old).casefold(), []).append(new)

    def swap(match):
        target, rest = match[1].strip(), match[2]
        key = _stem(target).casefold()
        if "/" in target and key in by_path:
            return f"[[{by_path[key]}{rest}]]"
        if "/" not in target and key in by_name and len(by_name[key]) == 1:
            new = by_name[key][0]
            name = _basename(new)
            return f"[[{name if unique_after(name) else _stem(new)}{rest}]]"
        return match[0]
    return LINK.sub(swap, text)


def move(root, domain, nb, source, target, rewrite_links=False, include_private=False):
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private)
        folder = _resolve(root, domain, nb)
        is_page = source.casefold().endswith(".md")
        source, target = clean_path(source, page=is_page), clean_path(target, page=is_page)
        if source == target:
            return {"ok": True, "unchanged": True}
        src, dst = _resolve(root, domain, nb, source), _resolve(root, domain, nb, target)
        if not src.exists():
            raise ValueError(f"not_found: no {'page' if is_page else 'section'} {source}")
        if is_page != src.is_file():
            raise ValueError(f"{source} is a {'section' if is_page else 'page'}")
        # On a case-insensitive disk a case-only rename is one file, which a journal cannot undo with its old spelling (R1).
        if same_name(source, target):
            raise ValueError(f"Change the letter case of a {'page' if is_page else 'section'} in two steps, through another name")
        if name_within(target, source):
            raise ValueError("A section cannot move into itself")
        _clash(root, domain, nb, target)
        if dst.exists():
            raise ValueError(f"{target} already exists")
        files = [source] if is_page else [f"{source}/{f}" for f in _files(src)]
        moved = {f: target + f[len(source):] for f in files}
        for new in moved.values():
            clean_path(new, page=new.casefold().endswith(".md"))   # every expanded path, not only the two given (R3)
        changes, expected, before = {}, {}, {}
        for old, new in moved.items():
            data = _resolve(root, domain, nb, old).read_bytes()
            changes[_rel(domain, nb, old)] = None
            changes[_rel(domain, nb, new)] = data
            expected[_rel(domain, nb, old)] = digest(data)
            before[new] = data
        existing = _pages(folder)
        pages = {p for p in existing if p not in moved} | {new for new in moved.values() if new.casefold().endswith(".md")}
        page_moves = {o: n for o, n in moved.items() if o.casefold().endswith(".md")}
        rewritten = []
        if rewrite_links:
            def counts(paths):
                out = {}
                for p in paths:
                    out[_basename(p).casefold()] = out.get(_basename(p).casefold(), 0) + 1
                return out
            names_before, names_after = counts(existing), counts(pages)
            unique_before = lambda name: names_before.get(name.casefold(), 0) == 1
            unique_after = lambda name: names_after.get(name.casefold(), 0) == 1
            for page in sorted(pages):
                rel = _rel(domain, nb, page)
                data = changes[rel] if rel in changes else _resolve(root, domain, nb, page).read_bytes()
                before.setdefault(page, data)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                new_text = _rewrite_links(text, page_moves, unique_after, unique_before)
                if new_text != text:
                    if rel not in changes:
                        expected[rel] = digest(data)
                    changes[rel] = new_text.encode("utf-8")
                    rewritten.append(page)
            if len(rewritten) > MAX_REWRITE:
                raise ValueError(f"--rewrite-links would change {len(rewritten)} pages; at most {MAX_REWRITE} are rewritten in one move")
        # Pending proposals follow what they would change, a new page inside a moved section too (R2). When the move rewrote
        # the links of the page a proposal is based on, the proposal's text gets the same rewrite and the new base revision.
        for file in _proposal_files(root, domain, nb):
            value = _read_proposal(file)
            if not isinstance(value, dict) or value.get("status") != "pending" or not isinstance(value.get("path"), str):
                continue
            old_path = value["path"]
            new_path = moved.get(old_path) or (target + old_path[len(source):] if not is_page and old_path.startswith(source + "/") else old_path)
            if new_path != old_path:
                try:
                    clean_path(new_path)
                except ValueError as exc:
                    raise ValueError(f"Pending proposal {value.get('id')} for {old_path} would move to {new_path}: {exc}; reject or accept it first") from None
            updated = dict(value, path=new_path)
            new_rel = _rel(domain, nb, new_path)
            if new_rel in changes and new_path in before and value.get("base_revision") == digest(before[new_path]):
                updated["base_revision"] = digest(changes[new_rel])
                if rewrite_links:
                    updated["body"] = _rewrite_links(str(value.get("body") or ""), page_moves, unique_after, unique_before)
            if updated != value:
                rel = _rel(domain, nb, f".proposals/{file.name}")
                expected[rel] = digest(file.read_bytes())
                changes[rel] = _proposal_text(updated)
        receipt = _commit(root, changes, "doc_move", _journal_meta(domain, nb, meta),
                          {"domain": domain, "notebook": nb, "from": source, "to": target, "pages": len(files), "rewritten": rewritten}, expected)
        if not is_page:
            _prune_empty(src, folder)
        return receipt


def _prune_empty(path, stop):
    """Remove `path` and its parents up to `stop` while they are empty folders (a moved section leaves its folder)."""
    for current, dirs, files in sorted(os.walk(path, topdown=False), key=lambda w: -len(w[0])):
        remove_empty_dir(current)


def attach(root, domain, nb, source, name=None, include_private=False):
    """Copy a file into the notebook's assets as `<sha8>-<name>`. Living Memory reads the file itself, so Hivemind's
    bridge never carries the bytes. An identical attachment already there is reused."""
    root = Path(root).resolve()
    source = Path(source)
    if not source.is_file():
        raise ValueError(f"not_found: no file {source}")
    if source.stat().st_size > MAX_ASSET_BYTES:
        raise ValueError(f"An attachment is at most {MAX_ASSET_BYTES // (1024 * 1024)} MiB")
    data = source.read_bytes()
    if len(data) > MAX_ASSET_BYTES:
        raise ValueError(f"An attachment is at most {MAX_ASSET_BYTES // (1024 * 1024)} MiB")
    name = name or source.name
    _check_part(name)
    sha = digest(data)
    asset = f"{sha[:8]}-{name}"
    _check_part(asset)
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private)
        full = _resolve(root, domain, nb, f"assets/{asset}")
        link = f"assets/{asset}"
        if full.exists():
            if digest(full.read_bytes()) == sha:
                return {"ok": True, "path": link, "created": False, "sha256": sha, "size": len(data)}
            raise ValueError(f"{link} already exists with other bytes; attach it under another name")
        assets = _resolve(root, domain, nb, "assets")
        used = sum((assets / f).stat().st_size for f in _files(assets)) if assets.is_dir() else 0
        if used + len(data) > MAX_ASSET_TOTAL:
            raise ValueError("This notebook's attachments would pass 2 GiB")
        rel = _rel(domain, nb, link)
        receipt = _commit(root, {rel: data}, "doc_attach", _journal_meta(domain, nb, meta),
                          {"domain": domain, "notebook": nb, "path": link, "size": len(data)}, {rel: None})
        receipt.update(path=link, created=True, sha256=sha, size=len(data))
        return receipt


# ----------------------------------------------------------------------------------------------- history

def history(root, domain, nb, path, limit=20, version=None, include_private=False):
    """Journal entries that touched the page, newest first; with `version`, the page's text as that operation left it."""
    import base64
    root = Path(root).resolve()
    domain, nb, _, _ = _open(root, domain, nb, include_private)
    path = clean_path(path)
    rel = _rel(domain, nb, path)
    needle = json.dumps(rel)[1:-1].encode()
    rows = []
    scope = None if include_private else private_scope(root)
    for journal in (root / ".operations").glob("*/journal.json"):
        if version and journal.parent.name != version:
            continue
        try:
            raw = journal.read_bytes()
            if needle not in raw:
                continue
            value = json.loads(raw)
        except (OSError, ValueError):
            continue
        if scope and journal_private(root, value, scope):
            continue
        for effect in value.get("effects") or []:
            if effect.get("path") != rel:
                continue
            if version:
                side = effect.get("after") if effect.get("after") is not None else effect.get("before")
                text = base64.b64decode(side).decode("utf-8", errors="replace") if side is not None else ""
                return {"ok": True, "operation_id": value.get("id"), "path": path, "deleted": effect.get("after") is None, "text": text,
                        "revision": effect.get("after_hash") or effect.get("before_hash")}
            rows.append({"operation_id": value.get("id"), "kind": value.get("kind"), "state": value.get("state"), "created": value.get("created"),
                         "before_revision": effect.get("before_hash"), "after_revision": effect.get("after_hash")})
    if version:
        raise ValueError(f"not_found: operation {version} did not change {path}")
    rows.sort(key=lambda r: str(r["created"]), reverse=True)
    return {"ok": True, "domain": domain, "notebook": nb, "path": path, "history": rows[:limit]}


# ----------------------------------------------------------------------------------------------- proposals

def _proposal_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def propose(root, domain, nb, path, body, base_revision=None, note=None, session=None, include_private=False):
    """Stage an edit for the user to accept or reject (agents may run it). A proposal for a new page has no base."""
    import hashlib
    root = Path(root).resolve()
    warnings = _check_body(body)
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise ValueError("A proposal note is at most 2000 characters")
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private)
        path = clean_path(path)
        full = _resolve(root, domain, nb, path)
        current = digest(full.read_bytes()) if full.is_file() else None
        if current is not None and base_revision is None:
            raise ValueError("--base-revision is required for a page that exists: read it first")
        if base_revision is not None and current != base_revision:
            raise ValueError(f"stale_revision: {path} changed since revision {base_revision[:12]}; read it again")
        pid = uuid.uuid4().hex[:12]
        value = {"schema": 1, "id": pid, "domain": domain, "notebook": nb, "uid": meta.get("uid"), "path": path, "base_revision": base_revision,
                 "body": body.decode("utf-8"), "note": note, "created": now(), "status": "pending",
                 "session": hashlib.sha256(session.encode()).hexdigest()[:16] if session else None}
        rel = _rel(domain, nb, f".proposals/{pid}.json")
        receipt = _commit(root, {rel: _proposal_text(value)}, "doc_propose", _journal_meta(domain, nb, meta),
                          {"domain": domain, "notebook": nb, "path": path, "proposal": pid}, {rel: None})
        receipt.update(id=pid, status="proposed", next=f"The user reviews it in Hivemind (proposals accept {pid} or proposals reject {pid})")
        if warnings:
            receipt.setdefault("warnings", []).extend(warnings)
        return receipt


def _read_proposal(file):
    try:
        value = read_json(file, None)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _find_proposal(root, pid):
    if not isinstance(pid, str) or not PROPOSAL_ID.fullmatch(pid):
        raise ValueError("A proposal id is 12 hexadecimal characters")
    for file in Path(root).glob(f"domains/*/notebooks/*/.proposals/{pid}.json"):
        domain, nb = file.parts[-5], file.parts[-3]
        try:
            _resolve(root, domain, nb, f".proposals/{pid}.json")
        except ValueError:
            continue
        value = _read_proposal(file)
        if value is not None:
            return domain, nb, file, value
    for file in Path(root).glob(f"domains/*/folder-proposals/*/{pid}.json"):   # a folder notebook's page (folder_docs)
        domain, nb = file.parts[-4], file.parts[-2]
        cursor = Path(root)
        if any(_is_link(cursor := cursor / part) for part in ("domains", domain, "folder-proposals", nb)) or _is_link(file):
            continue
        value = _read_proposal(file)
        if value is not None and value.get("kind") == "folder":
            return domain, nb, file, value
    raise ValueError(f"not_found: no proposal {pid}")


def proposals(root, domains, nb=None, include_private=False, status="pending", limit=50, session=None):
    """Page proposals: those of the store's notebooks and of folder notebooks (folder_docs). session limits them to one session."""
    from . import folder_docs
    wanted = digest(session.encode())[:16] if session else None
    rows = folder_docs.rows(root, domains, nb, include_private, status, session)
    for domain in domains:
        base = Path(root) / "domains" / domain / "notebooks"
        folders = sorted(p.name for p in base.iterdir() if p.is_dir() and SLUG.fullmatch(p.name)) if base.is_dir() and not _is_link(base) else []
        for file in [f for notebook in folders if not nb or notebook == nb for f in _proposal_files(root, domain, notebook)]:
            notebook = file.parts[-3]
            try:
                _, _, meta, _ = _open(root, domain, notebook, include_private)
            except ValueError:
                continue
            value = _read_proposal(file)
            if not isinstance(value, dict) or (status != "all" and value.get("status") != status) or (wanted and value.get("session") != wanted):
                continue
            rows.append({k: value.get(k) for k in ("id", "domain", "notebook", "path", "base_revision", "note", "created", "status", "reason", "settled", "edited")}
                        | {"size": len(str(value.get("body") or "").encode())})
    rows.sort(key=lambda r: str(r["created"]), reverse=True)
    return {"ok": True, "proposals": rows[:limit]}


def show_proposal(root, pid, include_private=False):
    domain, nb, _, value = _find_proposal(root, pid)
    if value.get("kind") == "folder":
        from . import folder_docs
        folder_docs.check_visible(root, value, include_private)
    else:
        _open(root, domain, nb, include_private)
    return {"ok": True, "proposal": value}


def accept(root, pid, expected=None, include_private=False, body=None):
    """Write a proposal's text to its page. Refused when the page changed since the proposal's base revision.

    With `body` (`--edited`), the user's merge of the proposal is written instead and the proposal is settled as accepted
    and edited in the same operation. The page must then be at `expected`, the revision the merge was made against, or not
    exist when `expected` is None (NS-2C review, D3)."""
    root = Path(root).resolve()
    warnings = _check_body(body) if body is not None else []
    with store_lock(root):
        domain, nb, file, value = _find_proposal(root, pid)
        if value.get("kind") == "folder":
            from . import folder_docs
            return folder_docs.accept(root, pid, file, value, expected, include_private, body)
        domain, nb, meta, _ = _open(root, domain, nb, include_private)
        if value.get("status") != "pending":
            raise ValueError(f"Proposal {pid} is {value.get('status')}")
        path = clean_path(value["path"])
        full = _resolve(root, domain, nb, path)
        current = digest(full.read_bytes()) if full.is_file() else None
        if body is not None:
            if current != expected:
                raise ValueError(f"stale_revision: {path} changed since the merge was made; read it again and merge again"
                                 if expected else f"stale_revision: {path} exists now; read it and merge again")
        elif current != value.get("base_revision"):
            raise ValueError(f"stale_revision: {path} changed since the proposal was made; read both and write the merge")
        elif expected is not None and current != expected:
            raise ValueError(f"stale_revision: {path} is not at revision {expected[:12]}")
        edited = body is not None
        body = body if edited else str(value.get("body") or "").encode("utf-8")
        rel, prel = _rel(domain, nb, path), _rel(domain, nb, f".proposals/{pid}.json")
        if current is None:
            _clash(root, domain, nb, path)
        settled = dict(value, status="accepted", settled=now(), **({"edited": True} if edited else {}))
        receipt = _commit(root, {rel: body, prel: _proposal_text(settled)}, "doc_accept", _journal_meta(domain, nb, meta),
                          {"domain": domain, "notebook": nb, "path": path, "proposal": pid, **({"edited": True} if edited else {})},
                          {rel: current, prel: digest(file.read_bytes())})
        receipt["revision"] = digest(body)
        if warnings:
            receipt.setdefault("warnings", []).extend(warnings)
        return receipt


def reject(root, pid, reason=None, include_private=False):
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, file, value = _find_proposal(root, pid)
        if value.get("kind") == "folder":
            from . import folder_docs
            return folder_docs.reject(root, pid, file, value, reason, include_private)
        domain, nb, meta, _ = _open(root, domain, nb, include_private)
        if value.get("status") != "pending":
            raise ValueError(f"Proposal {pid} is {value.get('status')}")
        prel = _rel(domain, nb, f".proposals/{pid}.json")
        settled = dict(value, status="rejected", settled=now(), reason=(reason or "")[:200] or None)
        return _commit(root, {prel: _proposal_text(settled)}, "doc_reject", _journal_meta(domain, nb, meta),
                       {"domain": domain, "notebook": nb, "proposal": pid}, {prel: digest(file.read_bytes())})


# ----------------------------------------------------------------------------------------------- privacy

EVERY_NOTEBOOK = "*"
NOTEBOOK_PATH = re.compile(r"domains/[^/]+/notebooks/")


def _private_meta(path):
    """Whether a notebook.json marks its notebook private. A damaged one counts as private, so damage never shows a page."""
    try:
        meta = read_json(path, None)
    except ValueError:
        return True
    return not isinstance(meta, dict) or bool(meta.get("private"))


def private_folders(root):
    """The folders of the notebooks that are private now."""
    return [p.parent for p in Path(root).glob("domains/*/notebooks/*/notebook.json") if _private_meta(p)]


def private_scope(root, changes=None):
    """(path prefixes, uids) of every notebook that is or ever was private: their operations are redacted from history,
    diff and inspect-plan unless the caller passes --include-private. A damaged registry redacts every notebook.
    changes: a plan's changes, whose incoming notebook.json files and registry count as well (a restore from another store)."""
    root = Path(root)
    prefixes, uids = set(), set()
    for rel, text in (changes or {}).items():
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if rel == PRIVACY:
            for e in value.get("notebooks") or [] if isinstance(value, dict) else []:
                if isinstance(e, dict) and e.get("domain") and e.get("notebook"):
                    prefixes.add(f"domains/{e['domain']}/notebooks/{e['notebook']}/")
                    if e.get("uid"):
                        uids.add(e["uid"])
        elif NOTEBOOK_PATH.match(rel) and rel.endswith("/notebook.json") and (not isinstance(value, dict) or value.get("private")):
            prefixes.add(rel[: -len("notebook.json")])
            if isinstance(value, dict) and value.get("uid"):
                uids.add(value["uid"])
    try:
        registry = read_json(root / PRIVACY, None) or {}
        entries = registry.get("notebooks") if isinstance(registry, dict) else None
        if registry and not isinstance(entries, list):
            raise ValueError(PRIVACY)
        entries = entries or []
    except ValueError:
        prefixes.add(EVERY_NOTEBOOK)
        entries = []
    for e in entries:
        if isinstance(e, dict) and e.get("domain") and e.get("notebook"):
            prefixes.add(f"domains/{e['domain']}/notebooks/{e['notebook']}/")
            if e.get("uid"):
                uids.add(e["uid"])
    for folder in private_folders(root):
        prefixes.add(f"domains/{folder.parts[-3]}/notebooks/{folder.name}/")
        try:
            meta = read_json(folder / "notebook.json", None)
        except ValueError:
            meta = None
        if isinstance(meta, dict) and meta.get("uid"):
            uids.add(meta["uid"])
    return prefixes, uids


FOLDER_PROPOSALS = re.compile(r"domains/[^/]+/folder-proposals/")   # staged edits of folder notebook pages (folder_docs): their text is outside the store
BOOKKEEPING = re.compile(r"notebook-folders\.json|domains/[^/]+/notebook-sync\.json|domains/[^/]+/folder-proposals/.+")


def is_private_path(path, scope):
    path = str(path).replace("\\", "/").casefold()
    if path == PRIVACY:
        return bool(scope[0])
    if BOOKKEEPING.fullmatch(path):   # folder notebook paths and mirrored page names: Hivemind's business, shown with --include-private (NS-2B)
        return True
    if EVERY_NOTEBOOK in scope[0] and NOTEBOOK_PATH.match(path):
        return True
    return any(path.startswith(prefix.casefold()) for prefix in scope[0] if prefix != EVERY_NOTEBOOK)


def private_read(paths, cwd, root, search):
    """The folder of a private notebook a file-reading tool would reach: a path inside one, or for a search tool (Glob, Grep)
    a folder that holds one. None when there is none."""
    from .paths import is_within
    folders = private_folders(root)
    if not folders:
        return None
    for raw in paths:
        try:
            path = Path(os.path.expandvars(raw)).expanduser()
            path = (path if path.is_absolute() else Path(cwd) / path).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        for folder in folders:
            folder = folder.resolve()
            if is_within(path, folder) or (search and is_within(folder, path)):
                return folder
    return None


def redact_summary(summary, scope):
    """A plan or operation summary without the notebooks and paths that `scope` makes private: a store path inside one, a
    `D/NB` notebook name, or (when the summary names a private notebook) its notebook, path and link fields."""
    if not isinstance(summary, dict):
        return summary

    def private_text(text):
        if not isinstance(text, str):
            return False
        if is_private_path(text, scope):
            return True
        parts = text.split("/")
        return len(parts) == 2 and is_private_path(f"domains/{parts[0]}/notebooks/{parts[1]}/", scope)

    def clean(value):
        if isinstance(value, list):
            return [clean(v) for v in value if not private_text(v)]
        if isinstance(value, dict):
            return redact_summary(value, scope)
        return value
    named = summary.get("domain") and summary.get("notebook") and \
        is_private_path(f"domains/{summary['domain']}/notebooks/{summary['notebook']}/", scope)
    out = {}
    for key, value in summary.items():
        if named and key in ("notebook", "path", "from", "to", "rewritten", "proposal", "title"):
            continue
        if private_text(value):
            continue
        out[key] = clean(value)
    return out


def merge_privacy(root, text):
    """The privacy registry `text` plus every entry the live registry has: it only grows, so undo and restore never expose
    what a notebook held while it was private."""
    try:
        given = json.loads(text) if text else {}
    except ValueError:
        given = {}
    try:
        live = read_json(Path(root) / PRIVACY, None) or {}
    except ValueError:
        live = {}
    entries = [e for e in (given.get("notebooks") if isinstance(given, dict) and isinstance(given.get("notebooks"), list) else []) if isinstance(e, dict)]
    for e in live.get("notebooks") or [] if isinstance(live, dict) else []:
        if isinstance(e, dict) and e not in entries:
            entries.append(e)
    return json.dumps({"version": 1, "notebooks": entries}, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


META_PATH = re.compile(r"(domains/[^/]+/notebooks/[^/]+)/notebook\.json")
MEMBER_PATH = re.compile(r"(domains/[^/]+/notebooks/[^/]+)/.+")


def check_notebook_metadata(root, changes):
    """Refuse changes that delete a notebook's notebook.json while that notebook keeps files the changes do not delete: they
    would sit in a folder no `doc` command opens (an undo of a notebook create, a delete plan made before a later page)."""
    gone = {str(k).replace("\\", "/").casefold() for k, v in changes.items() if v is None}
    for rel in sorted(str(k).replace("\\", "/") for k, v in changes.items() if v is None):
        match = META_PATH.fullmatch(rel)
        if not match:
            continue
        folder = Path(root) / match[1]
        left = [f for f in (_files(folder) if folder.is_dir() else []) if f"{match[1]}/{f}".casefold() not in gone]
        left += [str(k) for k, v in changes.items() if v is not None and str(k).replace("\\", "/").startswith(match[1] + "/")]
        if left:
            raise ValueError(f"Notebook conflict: {match[1]} still holds {len(left)} files this change keeps, and deleting its "
                             "notebook.json would strand them; delete the notebook as a whole instead")
    written = {str(k).replace("\\", "/") for k, v in changes.items() if v is not None}
    for rel in sorted(written):
        match = MEMBER_PATH.fullmatch(rel)
        if match and rel != f"{match[1]}/notebook.json" and f"{match[1]}/notebook.json" not in written \
                and not (Path(root) / match[1] / "notebook.json").is_file():
            raise ValueError(f"Notebook conflict: {match[1]} has no notebook.json (it was deleted meanwhile), so {rel} would be stranded")


def refuse_notes_in(root, domain):
    """Notes never go to a notebook-only domain, whatever command chose it."""
    if is_notebook_only(root, domain):
        raise ValueError(f"{domain} holds notebooks only; notes go to another domain (pass --domain)")


def is_notebook_journal(effects):
    """Whether an operation changed only notebook files (and the privacy registry): a notebook operation, whatever its kind."""
    paths = [e.get("path") for e in effects or [] if isinstance(e, dict)]
    here = lambda p: bool(NOTEBOOK_PATH.match(p) or FOLDER_PROPOSALS.match(p))
    return bool(paths) and all(isinstance(p, str) and (here(p) or p == PRIVACY) for p in paths) and any(here(p) for p in paths)


def record_index(root, operation_id, paths, created=None):
    """The small doc.json beside a notebook operation's journal that retention reads instead of the journal."""
    try:
        target = Path(root) / ".operations" / operation_id / "doc.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, json.dumps({"created": created or now(), "paths": sorted(paths)}, ensure_ascii=False) + "\n")
    except OSError:
        pass   # retention falls back to the journal


def journal_scope(root, journal):
    """private_scope of the live store plus the notebook.json files and registry held in the operation's own before and after
    images: a restore of a private notebook that stopped before any file landed is private already."""
    import base64
    prefixes, uids = (set(s) for s in private_scope(root))
    for side in ("before", "after"):
        images = {}
        for e in journal.get("effects") or [] if isinstance(journal, dict) else []:
            path = e.get("path") if isinstance(e, dict) else None
            if isinstance(path, str) and not e.get("blob") and e.get(side) and (path == PRIVACY or path.endswith("/notebook.json")):
                try:
                    images[path] = base64.b64decode(e[side]).decode("utf-8", errors="replace")
                except ValueError:
                    images[path] = "{"   # unreadable counts as private
        if images:
            more = private_scope(root, images)
            prefixes |= set(more[0])
            uids |= set(more[1])
    return prefixes, uids


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


def journal_private(root, journal, scope=None, authorize=False):
    """Whether an operation touched a private notebook, by the live store or by its own images (journal_scope). authorize:
    see private_journal."""
    if private_journal(journal, scope if scope is not None else private_scope(root), authorize):
        return True
    if authorize and isinstance(journal, dict) and _folder_proposal_private(root, journal):
        return True
    own = any(isinstance(e, dict) and isinstance(e.get("path"), str) and (e["path"] == PRIVACY or e["path"].endswith("/notebook.json"))
              for e in journal.get("effects") or [] if isinstance(journal, dict))
    return own and private_journal(journal, journal_scope(root, journal), authorize)


def guarded_path(path, scope, kind=None):
    """Whether changing `path` needs --include-private: a path inside a private notebook. The bookkeeping files (the folder
    registry, the mirror's state) are only hidden from display; a registry change that would reopen a hidden or private
    folder is refused separately (folder_policy.loosens)."""
    return is_private_path(path, scope) and not BOOKKEEPING.fullmatch(str(path).replace("\\", "/").casefold())


def private_journal(journal, scope, authorize=False):
    """Whether an operation touched a private notebook: by the uid it recorded, or by any path it changed. authorize: judge
    whether it may be undone or recovered without --include-private (guarded_path), not whether it may be shown."""
    meta = journal.get("meta") if isinstance(journal, dict) else None
    if isinstance(meta, dict) and meta.get("uid") in scope[1]:
        return True
    kind = journal.get("kind") if isinstance(journal, dict) else None
    test = (lambda p: guarded_path(p, scope, kind)) if authorize else (lambda p: is_private_path(p, scope))
    return any(test(e.get("path", "")) for e in journal.get("effects") or [] if isinstance(e, dict) and e.get("path") != PRIVACY)


# ----------------------------------------------------------------------------------------------- notebook-only domains

def init_notebook_domain(name, root):
    """A domain that holds notebooks only: no folder binding, no notes."""
    from .common import safe_domain
    from .init_domain import _read_bindings
    from .repository import canonical_json
    root = Path(root).resolve()
    name = safe_domain(name)
    with store_lock(root):
        settings, read = _read_bindings(root, {"version": 2, "domains": {}, "search_scope": {"global_domains": ["_global"], "foreign_domains": []}})
        if not isinstance(settings, dict) or not isinstance(settings.get("domains", {}), dict):
            raise ValueError("Invalid bindings preserved")
        entry = settings.get("domains", {}).get(name)
        if isinstance(entry, dict) and entry.get("notebook_only"):
            # Bound already (an interrupted create, a restore or a relocation): make sure its folder is there.
            (root / "domains" / name / "notebooks").mkdir(parents=True, exist_ok=True)
            return {"ok": True, "domain": name, "kind": "notebook", "existing": True}
        if name in settings.get("domains", {}) or (root / "domains" / name).exists():
            raise ValueError(f"Domain {name} already exists")
        settings.setdefault("domains", {})[name] = {"prefixes": [], "notebook_only": True}
        settings["version"] = 2
        scope = settings.setdefault("search_scope", {})
        scope.setdefault("global_domains", ["_global"])
        scope.setdefault("foreign_domains", [])
        receipt = transaction(root, {"bindings.json": canonical_json(settings)}, "bind_domain", expected={"bindings.json": read})
        (root / "domains" / name / "notebooks").mkdir(parents=True, exist_ok=True)
        return {"ok": True, "domain": name, "kind": "notebook", "operation_id": receipt.get("operation_id")}


def health(root, include_private=False):
    """Doctor warnings about notebooks: damaged notebook.json or privacy registry, links inside a notebook, and notebooks
    past 80% of a limit. A notebook that is or may be private is not named unless include_private."""
    root = Path(root)
    warnings = []
    scope = None if include_private else private_scope(root)
    try:
        from .folder_policy import load
        load(root)
    except ValueError as exc:
        warnings.append(f"{exc}: no source excerpt or folder notebook page is served until it is repaired")
    try:
        read_json(root / PRIVACY, None)
    except ValueError:
        warnings.append(f"{PRIVACY} cannot be read; every notebook is redacted from history until it is repaired")
    for base in sorted(root.glob("domains/*/notebooks")):
        if _is_link(base):
            warnings.append(f"{base.relative_to(root).as_posix()} is a link or junction; notebooks there are ignored")
            continue
        for folder in sorted(p for p in base.iterdir() if p.is_dir()):
            name = f"{folder.parts[-3]}/{folder.name}"
            if scope and is_private_path(f"domains/{name.replace('/', '/notebooks/', 1)}/", scope):
                name = f"a private notebook in {folder.parts[-3]} (doctor --include-private names it)"
            if _is_link(folder):
                warnings.append(f"notebook {name} is a link or junction and is ignored")
                continue
            try:
                read_json(folder / "notebook.json", None)
            except ValueError:
                warnings.append(f"notebook {name}: notebook.json cannot be read; agents treat the notebook as private until it is repaired")
            linked = [d for d, _, _ in os.walk(folder) if _is_link(Path(d))] + [str(p) for p in folder.rglob("*") if _is_link(p)]
            if linked:
                warnings.append(f"notebook {name} holds {len(set(linked))} link(s) or junction(s), which Living Memory skips")
            pages = len(_pages(folder))
            if pages > MAX_PAGES * 0.8:
                warnings.append(f"notebook {name}: {pages} pages, near the limit of {MAX_PAGES}")
            assets = folder / "assets"
            used = sum((assets / f).stat().st_size for f in _files(assets)) if assets.is_dir() else 0
            if used > MAX_ASSET_TOTAL * 0.8:
                warnings.append(f"notebook {name}: {used // (1024 * 1024)} MiB of attachments, near the limit of {MAX_ASSET_TOTAL // (1024 ** 3)} GiB")
    return warnings


def page_count(root, domain):
    base = Path(root) / "domains" / domain / "notebooks"
    if not base.is_dir():
        return 0
    return sum(len(_pages(folder)) for folder in base.iterdir() if folder.is_dir() and not _is_link(folder))


def blob_bytes(root, operation_id, sha):
    """The bytes of an attachment an operation moved aside (for undo and history)."""
    path = blob_store(root, operation_id) / sha
    return path.read_bytes() if path.exists() else None
