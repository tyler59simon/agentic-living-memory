"""Canonical inventory and recoverable filesystem transactions for script operations."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .common import atomic_write, file_lock, memory_root, retry_sharing

STATUSES = {"active", "resolved", "archived", "superseded"}
FACETS = ("subject", "concern", "platform", "technology")
_locks = threading.local()
# Hook processes lower this so they fail fast inside the host's hook timeout.
LOCK_TIMEOUT = 30
_EDGE = r"(?<![A-Za-z0-9])"
SECRET_PATTERNS = [(name, re.compile(pattern, flags)) for name, pattern, flags in (
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", 0),
    ("API key", _EDGE + r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}", 0),
    ("Stripe key", _EDGE + r"[rs]k_(?:live|test)_[A-Za-z0-9]{16,}", 0),
    ("AWS access key", _EDGE + r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])", 0),
    ("GitHub token", _EDGE + r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})", 0),
    ("GitLab token", _EDGE + r"glpat-[A-Za-z0-9_-]{20,}", 0),
    ("npm token", _EDGE + r"npm_[A-Za-z0-9]{36}", 0),
    ("Slack token", _EDGE + r"xox[abprs]-[A-Za-z0-9-]{10,}", 0),
    ("Google API key", _EDGE + r"AIza[0-9A-Za-z_-]{35}", 0),
    ("JWT", _EDGE + r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", 0),
    ("bearer token", r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*", re.I),
    ("password in URL", r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]{3,}@", re.I),
    ("Azure account key", r"\bAccountKey=[A-Za-z0-9+/=]{20,}", re.I),
)]
# key = value assignments: DB_PASSWORD=..., client_secret: ..., api-key=..., aws_secret_access_key = ...
ASSIGNMENT = re.compile(
    _EDGE + r"(?:[A-Za-z0-9]+[_.-])*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)"
    r"\s*[:=]\s*[\"']?([^\s\"',;]{8,})", re.I)
PLACEHOLDER = re.compile(r"^(?:<.*>|\$\{?.*|%.*%|\{\{.*|.*(?:redacted|example|changeme|placeholder|your[_-]).*|\*+|x{6,}|.*os\.environ.*|.*process\.env.*|.*getenv.*)$", re.I)


def find_secret(text):
    """(kind, offset) of the first credential-looking value, else None. Placeholders are fine."""
    for name, pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            return name, match.start()
    for match in ASSIGNMENT.finditer(text):
        value = match[1]
        if PLACEHOLDER.match(value):
            continue
        # Prose ("secret: configured in Key Vault") has no digits or symbols; real values do.
        if re.search(r"[0-9]", value) or re.search(r"[^A-Za-z]", value):
            return "credential assignment", match.start()
    return None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_json(path, default=None):
    try:
        value = json.loads(retry_sharing(lambda: Path(path).read_text(encoding="utf-8-sig")))
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid or unreadable configuration preserved: {path}") from exc
    return value


@contextmanager
def store_lock(root, allow_retired=False, timeout=None):
    root = Path(root).resolve()
    key = str(root)
    held = getattr(_locks, "held", set())
    if key in held:
        yield
        return
    with file_lock(root / ".store.lock", timeout=LOCK_TIMEOUT if timeout is None else timeout):
        from .storage import check_root
        if not allow_retired:
            check_root(root)
        _locks.held = held | {key}
        try:
            yield
        finally:
            _locks.held = held


def confined(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Path must be relative and confined to the store")
    path = root / rel
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError("Symbolic links are not allowed in the store")
        cursor = cursor.parent
    path.resolve().relative_to(root)
    return path


def parse(path):
    try:
        data = retry_sharing(lambda: Path(path).read_bytes())
        raw = data.decode("utf-8-sig")
    except (OSError, UnicodeError):
        return None
    note = parse_text(raw, path)
    if note is not None:
        # Revisions are compared with exact file bytes in transactions, BOM included.
        note["revision"] = digest(data)
    return note


TEXT_KEYS = ("title", "context")


def _scalar(value):
    """Plain YAML scalar: strip one layer of quotes, undoing YAML's '' escape."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def parse_text(raw, path=""):
    original = raw
    # Editors on Windows (and git autocrlf) may rewrite line endings; the note is the same.
    raw = raw.lstrip("\ufeff").replace("\r\n", "\n")
    match = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n(.*)\Z", raw, re.S)
    if not match:
        return None
    meta = {}
    block_key = None
    block_keys = set()
    for line in match[1].splitlines():
        # YAML block lists ("key:" then "  - item") as written by Obsidian's property editor.
        if block_key and line.lstrip().startswith("- "):
            meta[block_key].append(line.lstrip()[2:].strip().strip("\"'"))
            continue
        block_key = None
        if line.endswith(":") and " " not in line.strip():
            block_key = line.strip()[:-1]
            block_keys.add(block_key)
            meta[block_key] = []
            continue
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        try:
            meta[key] = json.loads(value)
        except json.JSONDecodeError:
            # Accept the scalar/list subset used in note frontmatter.
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                meta[key] = [_scalar(x) for x in value[1:-1].split(",") if x.strip()]
            else:
                meta[key] = _scalar(value)
        if key in TEXT_KEYS and meta[key] is not None and not isinstance(meta[key], str):
            # "title: 2024" or "title: true" is still a title.
            meta[key] = value.strip()
    for key in block_keys:
        if meta.get(key) == [] and key != "tags":
            meta[key] = ""
    if not isinstance(meta.get("title"), str):
        return None
    if meta.get('schema_version', 1) == 3:
        # Prefix is a derived read/search field, never a second canonical value.
        if 'prefix' in meta:
            return None
        from .note_types import prefix_for
        try:meta['prefix'] = prefix_for(meta.get('type'), legacy=True)
        except ValueError:return None
    from .note_format import strip_title
    # The "# 🎯 Title" heading is derived from the title property; it is never part of the body.
    body = strip_title(match[2])
    meta.update(body=body, path=str(path), digest=digest(body.encode()), revision=digest(original.encode()))
    return meta


# Frontmatter order: the properties a person reads first, then bookkeeping.
FRONT_KEYS = ("title", "type", "status", "tags", "context")
SYSTEM_KEYS = ("id", "domain", "created", "updated", "review_by", "supersedes", "superseded_by", "schema_version", "capture_key")


def serialize(note):
    from .note_format import format_body, TITLE_MARK
    meta = {k: v for k, v in note.items() if k not in {"body", "path", "digest", "revision"}}
    meta["schema_version"] = note.get('schema_version', 2)
    if meta['schema_version'] == 3:
        from .note_types import prefix_for
        prefix_for(meta.get('type'), legacy=True)
        meta.pop('prefix', None)
    body = format_body(note["body"])
    meta.pop("sha256", None)
    ordered = {k: meta.pop(k) for k in FRONT_KEYS + SYSTEM_KEYS if k in meta}
    ordered.update(meta)
    ordered["sha256"] = digest(body.encode())
    title = " ".join(str(note.get("title", "")).split())
    front = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in ordered.items())
    return "---\n" + front + "\n---\n\n" + TITLE_MARK + title + "\n\n" + body + "\n"


def _scan(domain, root=None, retired=False):
    """Notes plus problems. A bad file is reported, never allowed to hide or break the domain."""
    from .common import canonical_domain
    root = Path(root or memory_root()).resolve()
    name = canonical_domain(domain,root)
    result = []
    found = []
    directories = [root / "domains" / name / "notes"]
    if retired:
        directories.append(root / "domains" / name / "archive")
    seen = {}
    for directory in directories:
        if directory.is_symlink():
            raise ValueError("Canonical directory cannot be a symbolic link")
        for path in sorted(directory.rglob("*.md")):
            confined(root, path.relative_to(root))
            note = parse(path)
            rel = path.relative_to(root).as_posix()
            if note is None:
                found.append({"path": rel, "problem": "unreadable frontmatter"})
                continue
            note_id = note.get("id")
            if not isinstance(note_id, str) or not note_id:
                found.append({"path": rel, "problem": "missing id"})
                continue
            if note_id in seen:
                found.append({"path": rel, "problem": f"duplicate id {note_id} (also {seen[note_id]})"})
                continue
            seen[note_id] = rel
            if retired or note.get("status", "active") == "active":
                result.append(note)
    return result, found


def inventory(domain, root=None, retired=False):
    return _scan(domain, root, retired)[0]


def problems(domain, root=None):
    """Canonical files that inventory skipped, for doctor and session start."""
    return _scan(domain, root, True)[1]


def validate(note, domain, root=None, new=False):
    if not isinstance(note.get("id"), str) or not note["id"]:
        raise ValueError("Note requires a stable ID")
    if not note.get("title", "").strip() or not note.get("body", "").strip():
        raise ValueError("Note title and body are required")
    if note.get("domain") != domain or note.get("status") not in STATUSES:
        raise ValueError("Invalid domain or lifecycle status")
    if new or note.get('schema_version') == 3:
        from .note_types import prefix_for
        prefix = prefix_for(note.get('type'), legacy=not new)
        if note.get('schema_version') != 3 or note.get('prefix') != prefix:
            raise ValueError('Prefix must be derived from type; use capture or the note-type-plan migration')
        if note.get('status') == 'resolved' and note['type'] != 'bug':
            raise ValueError('Only bug notes may have resolved status')
        if note.get('path'):
            from .notes import slug
            expected = f"{prefix} - {slug(note['title'])}-{note['id'][:8]}.md"
            if Path(note['path']).name != expected:
                raise ValueError('Filename must match type and title; use a reviewed update/rename plan')
    from .note_format import check
    check(note["body"], note.get("type", "knowledge"))
    tags = note.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise ValueError("Tags must be a string list")
    taxonomy = read_json(Path(root or memory_root()) / "tag_taxonomy.json", {"tags": []})
    known = taxonomy.get("tags", []) if isinstance(taxonomy, dict) else taxonomy
    if isinstance(known, dict):
        known = list(known)
    if new:
        for required in ("subject", "concern"):
            if not any(t.startswith(required + "/") for t in tags):
                raise ValueError(f"Capture requires a registered {required}/ tag")
        if any(t.split("/", 1)[0] not in FACETS or "/" not in t for t in tags):
            raise ValueError("Tags must use subject/, concern/, platform/, or technology/ facets")
    if known and set(tags) - set(known):
        raise ValueError("Unknown tags require an approved taxonomy plan: " + ", ".join(sorted(set(tags) - set(known))))
    if new and not known:
        raise ValueError("Initialize the controlled taxonomy before capturing notes")
    found = find_secret("\n".join(str(note.get(k) or "") for k in ("title", "body", "context")))
    if found:
        raise ValueError(f"Potential credential detected ({found[0]} at character {found[1]}); remove it or replace it with a placeholder such as <REDACTED>")
    if note.get("status") == "superseded" and not note.get("superseded_by"):
        raise ValueError("Supersession requires a successor note ID")


def _bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _encode(data):
    return base64.b64encode(data).decode() if data is not None else None


def _decode(data):
    return base64.b64decode(data) if data is not None else None


def _write(path, data):
    if data is None:
        if path.exists():
            retry_sharing(path.unlink)
    else:
        # Canonical/configuration files are UTF-8. Atomic write fsyncs first.
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        atomic_write(path, data.decode("utf-8"))
        os.chmod(path, mode)


def transaction(root, changes, kind, expected=None, operation_id=None):
    """Durable intent precedes every write. Recovery accepts only known states."""
    root = Path(root).resolve()
    operation_id = operation_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation_id):
        raise ValueError("Invalid operation ID")
    journal_path = root / ".operations" / operation_id / "journal.json"
    with store_lock(root):
        if journal_path.exists():
            journal = read_json(journal_path)
            if journal["kind"] != kind:
                raise ValueError("Idempotency identity is already in use")
            requested = {str(Path(k).as_posix()): digest(v.encode() if isinstance(v, str) else v) for k, v in changes.items()}
            if requested != {e["path"]: e["after_hash"] for e in journal["effects"]}:
                raise ValueError("Idempotency payload differs")
            return recover(root, operation_id, "forward")
        roll_forward_pending(root)
        effects = []
        for rel, content in sorted(changes.items()):
            path = confined(root, rel)
            before = _bytes(path)
            if expected is not None and str(rel) in expected and digest(before) != expected[str(rel)]:
                raise ValueError(f"Stale revision: {rel}")
            after = content.encode() if isinstance(content, str) else content
            effects.append({"path": str(Path(rel).as_posix()), "before": _encode(before), "after": _encode(after),
                            "before_hash": digest(before), "after_hash": digest(after)})
        journal = {"schema": 1, "id": operation_id, "kind": kind, "created": now(), "state": "prepared", "effects": effects}
        atomic_write(journal_path, canonical_json(journal))
        atomic_write(journal_path.with_name(PENDING), "pending\n")
        return recover(root, operation_id, "forward")


PENDING = "pending"


def roll_forward_pending(root):
    """Finish operations a crashed or interrupted process left half-applied.

    Each journal has a marker while it is incomplete, so this costs one glob when nothing
    is pending. A journal whose files changed since is left for `recover --abandon`."""
    results = []
    for marker in sorted(Path(root).glob(f".operations/*/{PENDING}")):
        operation_id = marker.parent.name
        try:
            results.append(recover(root, operation_id, "forward"))
        except ValueError as exc:
            results.append({"ok": False, "operation_id": operation_id, "error": str(exc)})
    return results


def abandon(root, operation_id):
    """Stop treating an operation as pending without touching any note file."""
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation_id):
        raise ValueError("Invalid operation ID")
    journal_path = root / ".operations" / operation_id / "journal.json"
    with store_lock(root):
        journal = read_json(journal_path)
        if not journal:
            raise ValueError("Operation is unavailable")
        if journal["state"] in {"committed", "rolled_back"}:
            raise ValueError(f"Operation is already {journal['state']}")
        journal["state"] = "abandoned"
        journal["completed"] = now()
        atomic_write(journal_path, canonical_json(journal))
        marker = journal_path.with_name(PENDING)
        if marker.exists():
            retry_sharing(marker.unlink)
        return {"ok": True, "operation_id": operation_id, "state": "abandoned",
                "files": [effect["path"] for effect in journal["effects"]]}


def recover(root, operation_id, direction="forward"):
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation_id):
        raise ValueError("Invalid operation ID")
    journal_path = root / ".operations" / operation_id / "journal.json"
    with store_lock(root):
        journal = read_json(journal_path)
        if not journal:
            raise ValueError("Operation is unavailable")
        desired = "after" if direction == "forward" else "before"
        final_state = "committed" if direction == "forward" else "rolled_back"
        if journal["state"] == final_state:
            marker = journal_path.with_name(PENDING)
            if marker.exists():
                retry_sharing(marker.unlink)
            return {"ok": True, "operation_id": operation_id, "state": final_state, "idempotent": True}
        if journal["state"] == "abandoned":
            raise ValueError("Operation was abandoned")
        # Validate ALL resources before changing ANY resource.
        for effect in journal["effects"]:
            path = confined(root, effect["path"])
            current = digest(_bytes(path))
            if current not in {effect["before_hash"], effect["after_hash"]}:
                raise ValueError(f"Recovery conflicts with a user edit: {effect['path']}")
            for side in ("before", "after"):
                if digest(_decode(effect[side])) != effect[side + "_hash"]:
                    raise ValueError("Journal content failed integrity validation")
        journal["state"] = "applying" if direction == "forward" else "rolling_back"
        atomic_write(journal_path, canonical_json(journal))
        effects = journal["effects"] if direction == "forward" else list(reversed(journal["effects"]))
        # Write new files before removing old ones: an interrupted rename then leaves a
        # duplicate (reported, recoverable) rather than a note that has vanished.
        effects = sorted(effects, key=lambda effect: effect[desired] is None)
        for effect in effects:
            path = confined(root, effect["path"])
            if digest(_bytes(path)) != effect[desired + "_hash"]:
                _write(path, _decode(effect[desired]))
        journal["state"] = final_state
        journal["completed"] = now()
        atomic_write(journal_path, canonical_json(journal))
        marker = journal_path.with_name(PENDING)
        if marker.exists():
            retry_sharing(marker.unlink)
        return {"ok": True, "operation_id": operation_id, "state": final_state, "idempotent": False}


def reconcile(domain, root=None, sync_overlay=True):
    from .search import rebuild_index
    root = Path(root or memory_root()).resolve()
    with store_lock(root):
        all_notes = inventory(domain, root, retired=True)
        directory = root / "domains" / domain
        for retired, name in ((False, "INDEX.md"), (True, "ARCHIVE.md")):
            selected = [n for n in all_notes if (n.get("status", "active") != "active") == retired]
            text = f"# {'Retired' if retired else 'Active'} Memory\n\n"
            text += "\n".join(f"- [{n['title']}]({Path(n['path']).relative_to(directory).as_posix()}) — {', '.join(n.get('tags', []))}" for n in selected) + "\n"
            atomic_write(directory / name, text)
        rebuild_index(domain, root)
        atomic_write(directory / ".derived-dirty", "clean\n")
        state = read_json(directory / "sync-state.json", {})
        if sync_overlay and state.get("vault"):
            from .sync import build_overlay, is_managed
            if is_managed(state["vault"]):
                build_overlay(domain, Path(state["vault"]), root, trigger="memory change")

