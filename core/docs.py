"""Notebook pages (Notes Studio, NS-2A): a record kind of their own, apart from notes.

A page is free-form UTF-8 text kept byte for byte under `domains/D/notebooks/<nb>/`; attachments live in the notebook's
`assets/` folder and staged agent edits in `.proposals/`. Every write is one journaled transaction whose kind starts
with `doc_`, undone with `undo OPERATION`. Pages are never notes: no list, review, maintenance, SessionStart or wiki
path reads them. Design: docs/notebooks-design.md."""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path

from .common import canonical_domain, load_bindings
from .repository import atomic_write, blob_store, digest, find_secret, now, read_json, store_lock, transaction

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
    if not (Path(root) / "domains" / name).is_dir():
        raise ValueError(f"not_found: no domain {name}")
    return name


def _is_link(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


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
        twin = next((n for n in names if n.casefold() == part.casefold() and n != part), None)
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
    # A small index beside the journal, so retention need not read every journal's page text (prune_journals).
    try:
        atomic_write(Path(root) / ".operations" / operation_id / "doc.json",
                     json.dumps({"created": now(), "paths": sorted(changes)}, ensure_ascii=False) + "\n")
    except OSError:
        pass   # retention falls back to the journal
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
    rows = []
    with store_lock(root):
        for folder in (root / ".operations").glob(DOC_OP_PREFIX + "*"):
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
        removed = 0
        for created, folder, _ in rows:
            if folder.name in newest and created >= cutoff:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def _journal_meta(domain, nb, meta):
    return {"domain": domain, "notebook": nb, "uid": meta.get("uid")}


def _files(folder):
    """Every file under `folder` (relative, `/`-separated), never following a link."""
    out = []
    for current, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not _is_link(Path(current) / d)]
        for name in names:
            full = Path(current) / name
            if not _is_link(full):
                out.append(full.relative_to(folder).as_posix())
    return sorted(out)


def _pages(folder):
    return [p for p in _files(folder) if p.split("/")[0] not in RESERVED and p.casefold().endswith(".md")]


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
            pending = sum(1 for p in (folder / ".proposals").glob("*.json") if (read_json(p, {}) or {}).get("status") == "pending") if (folder / ".proposals").is_dir() else 0
            rows.append({"domain": domain, "notebook": folder.name, "title": meta.get("title"), "visibility": meta.get("visibility", "searchable"),
                         "private": bool(meta.get("private")), "revision": revision, "pages": len(_pages(folder)), "assets": len(assets),
                         "asset_bytes": sum((folder / "assets" / a).stat().st_size for a in assets), "proposals": pending,
                         "updated": meta.get("updated")})
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


def update_notebook(root, domain, nb, expected=None, title=None, visibility=None, private=None, order=None, icons=None, pinned=None, settings=None):
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, meta, revision = _open(root, domain, nb, include_private=True)
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


def delete_notebook(root, domain, nb, with_pages=False):
    """Delete a notebook. With pages it needs --with-pages and comes back as a plan to approve, like note deletion."""
    from .governance import plan
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private=True)
        folder = _resolve(root, domain, nb)
        pages = _pages(folder)
        if pages and not with_pages:
            raise ValueError(f"Notebook {nb} holds {len(pages)} pages; pass --with-pages to plan deleting them too")
        changes = {_rel(domain, nb, f): None for f in _files(folder)}
        summary = {"domain": domain, "notebook": nb, "pages": len(pages), "files": len(changes)}
        if pages:
            return plan(root, changes, "doc_notebook_delete", (), summary)
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


def create_page(root, domain, nb, path, body=b""):
    root = Path(root).resolve()
    warnings = _check_body(body)
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private=True)
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


def write_page(root, domain, nb, path, body, expected):
    root = Path(root).resolve()
    warnings = _check_body(body)
    if not expected:
        raise ValueError("--expected-revision is required: read the page first")
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private=True)
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


def delete_page(root, domain, nb, path, expected):
    root = Path(root).resolve()
    if not expected:
        raise ValueError("--expected-revision is required: read the page first")
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private=True)
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


def _rewrite_links(text, moved, unique_after):
    """Rewrite [[...]] links whose target is a moved page: a full path (with or without .md) becomes the new full path;
    a bare name that changed becomes the new name when that is unique in the notebook, else the new path."""
    by_path = {_stem(old).casefold(): _stem(new) for old, new in moved.items()}
    by_name = {}
    for old, new in moved.items():
        if _basename(old).casefold() != _basename(new).casefold():
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


def move(root, domain, nb, source, target, rewrite_links=False):
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, meta, _ = _open(root, domain, nb, include_private=True)
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
        case_only = source.casefold() == target.casefold()
        if not is_page and case_only:
            raise ValueError("Change the letter case of a section in two steps, through another name")
        if (target + "/").casefold().startswith((source + "/").casefold()) and not case_only:
            raise ValueError("A section cannot move into itself")
        if not case_only:
            _clash(root, domain, nb, target)
        if dst.exists() and not case_only:
            raise ValueError(f"{target} already exists")
        files = [source] if is_page else [f"{source}/{f}" for f in _files(src)]
        moved = {f: target + f[len(source):] for f in files}
        changes, expected = {}, {}
        for old, new in moved.items():
            data = _resolve(root, domain, nb, old).read_bytes()
            changes[_rel(domain, nb, old)] = None
            changes[_rel(domain, nb, new)] = data
            expected[_rel(domain, nb, old)] = digest(data)
        pages = {p for p in _pages(folder) if p not in moved} | {new for new in moved.values() if new.casefold().endswith(".md")}
        rewritten = []
        if rewrite_links:
            names = {}
            for p in pages:
                names[_basename(p).casefold()] = names.get(_basename(p).casefold(), 0) + 1
            unique_after = lambda name: names.get(name.casefold(), 0) == 1
            page_moves = {o: n for o, n in moved.items() if o.casefold().endswith(".md")}
            for page in sorted(pages):
                rel = _rel(domain, nb, page)
                data = changes[rel] if rel in changes else _resolve(root, domain, nb, page).read_bytes()
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                new_text = _rewrite_links(text, page_moves, unique_after)
                if new_text != text:
                    if rel not in changes:
                        expected[rel] = digest(data)
                    changes[rel] = new_text.encode("utf-8")
                    rewritten.append(page)
            if len(rewritten) > MAX_REWRITE:
                raise ValueError(f"--rewrite-links would change {len(rewritten)} pages; at most {MAX_REWRITE} are rewritten in one move")
        # Pending proposals follow the pages they would change.
        proposals = folder / ".proposals"
        for file in sorted(proposals.glob("*.json")) if proposals.is_dir() else []:
            value = read_json(file, None)
            if isinstance(value, dict) and value.get("status") == "pending" and value.get("path") in moved:
                value["path"] = moved[value["path"]]
                rel = _rel(domain, nb, f".proposals/{file.name}")
                expected[rel] = digest(file.read_bytes())
                changes[rel] = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        receipt = _commit(root, changes, "doc_move", _journal_meta(domain, nb, meta),
                          {"domain": domain, "notebook": nb, "from": source, "to": target, "pages": len(files), "rewritten": rewritten}, expected)
        if not is_page and not case_only:
            _prune_empty(src, folder)
        return receipt


def _prune_empty(path, stop):
    """Remove `path` and its parents up to `stop` while they are empty folders (a moved section leaves its folder)."""
    for current, dirs, files in sorted(os.walk(path, topdown=False), key=lambda w: -len(w[0])):
        if not os.listdir(current):
            try:
                os.rmdir(current)
            except OSError:
                pass


def attach(root, domain, nb, source, name=None):
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
        domain, nb, meta, _ = _open(root, domain, nb, include_private=True)
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
        receipt.update(id=pid, status="proposed", next=f"The user reviews it in Hivemind (doc accept {pid} or doc reject {pid})")
        if warnings:
            receipt.setdefault("warnings", []).extend(warnings)
        return receipt


def _find_proposal(root, pid):
    if not isinstance(pid, str) or not PROPOSAL_ID.fullmatch(pid):
        raise ValueError("A proposal id is 12 hexadecimal characters")
    for file in Path(root).glob(f"domains/*/notebooks/*/.proposals/{pid}.json"):
        domain, nb = file.parts[-5], file.parts[-3]
        _resolve(root, domain, nb, f".proposals/{pid}.json")
        value = read_json(file, None)
        if isinstance(value, dict):
            return domain, nb, file, value
    raise ValueError(f"not_found: no proposal {pid}")


def proposals(root, domains, nb=None, include_private=False, status="pending", limit=50):
    rows = []
    for domain in domains:
        base = Path(root) / "domains" / domain / "notebooks"
        for file in base.glob("*/.proposals/*.json") if base.is_dir() else []:
            notebook = file.parts[-3]
            if nb and notebook != nb:
                continue
            try:
                _, _, meta, _ = _open(root, domain, notebook, include_private)
            except ValueError:
                continue
            value = read_json(file, None)
            if not isinstance(value, dict) or (status != "all" and value.get("status") != status):
                continue
            rows.append({k: value.get(k) for k in ("id", "domain", "notebook", "path", "base_revision", "note", "created", "status", "reason", "settled")}
                        | {"size": len(str(value.get("body") or "").encode())})
    rows.sort(key=lambda r: str(r["created"]), reverse=True)
    return {"ok": True, "proposals": rows[:limit]}


def show_proposal(root, pid, include_private=False):
    domain, nb, _, value = _find_proposal(root, pid)
    _open(root, domain, nb, include_private)
    return {"ok": True, "proposal": value}


def accept(root, pid, expected=None):
    """Write a proposal's text to its page. Refused when the page changed since the proposal's base revision."""
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, file, value = _find_proposal(root, pid)
        domain, nb, meta, _ = _open(root, domain, nb, include_private=True)
        if value.get("status") != "pending":
            raise ValueError(f"Proposal {pid} is {value.get('status')}")
        path = clean_path(value["path"])
        full = _resolve(root, domain, nb, path)
        current = digest(full.read_bytes()) if full.is_file() else None
        if current != value.get("base_revision"):
            raise ValueError(f"stale_revision: {path} changed since the proposal was made; read both and write the merge")
        if expected is not None and current != expected:
            raise ValueError(f"stale_revision: {path} is not at revision {expected[:12]}")
        body = str(value.get("body") or "").encode("utf-8")
        rel, prel = _rel(domain, nb, path), _rel(domain, nb, f".proposals/{pid}.json")
        if current is None:
            _clash(root, domain, nb, path)
        settled = dict(value, status="accepted", settled=now())
        receipt = _commit(root, {rel: body, prel: _proposal_text(settled)}, "doc_accept", _journal_meta(domain, nb, meta),
                          {"domain": domain, "notebook": nb, "path": path, "proposal": pid}, {rel: current, prel: digest(file.read_bytes())})
        receipt["revision"] = digest(body)
        return receipt


def reject(root, pid, reason=None):
    root = Path(root).resolve()
    with store_lock(root):
        domain, nb, file, value = _find_proposal(root, pid)
        domain, nb, meta, _ = _open(root, domain, nb, include_private=True)
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


def private_scope(root):
    """(path prefixes, uids) of every notebook that is or ever was private: their operations are redacted from history,
    diff and inspect-plan unless the caller passes --include-private. A damaged registry redacts every notebook."""
    root = Path(root)
    prefixes, uids = set(), set()
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


def is_private_path(path, scope):
    path = str(path)
    if EVERY_NOTEBOOK in scope[0] and NOTEBOOK_PATH.match(path):
        return True
    return any(path.startswith(prefix) for prefix in scope[0] if prefix != EVERY_NOTEBOOK)


def private_read(paths, cwd, root, search):
    """The folder of a private notebook a file-reading tool would reach: a path inside one, or for a search tool (Glob, Grep)
    a folder that holds one. None when there is none."""
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
            if path == folder or path.is_relative_to(folder) or (search and folder.is_relative_to(path)):
                return folder
    return None


def private_journal(journal, scope):
    """Whether an operation touched a private notebook: by the uid it recorded, or by any path it changed."""
    meta = journal.get("meta") if isinstance(journal, dict) else None
    if isinstance(meta, dict) and meta.get("uid") in scope[1]:
        return True
    return any(is_private_path(e.get("path", ""), scope) for e in journal.get("effects") or [] if isinstance(e, dict))


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


def health(root):
    """Doctor warnings about notebooks: damaged notebook.json or privacy registry, links inside a notebook, and notebooks
    past 80% of a limit."""
    root = Path(root)
    warnings = []
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
