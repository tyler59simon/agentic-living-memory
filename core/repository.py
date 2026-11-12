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
FACETS = ("subject", "concern", "platform", "technology", "project", "system")
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
    ("Hugging Face token", _EDGE + r"hf_[A-Za-z0-9]{30,}", 0),
    ("Docker token", _EDGE + r"dckr_pat_[A-Za-z0-9_-]{20,}", 0),
    ("Linear key", _EDGE + r"lin_api_[A-Za-z0-9]{30,}", 0),
    ("Notion token", _EDGE + r"(?:ntn_[A-Za-z0-9]{40,}|secret_[A-Za-z0-9]{40,})", 0),
    ("SendGrid key", _EDGE + r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", 0),
    ("DigitalOcean token", _EDGE + r"do[por]_v1_[a-f0-9]{64}", 0),
    ("Shopify token", _EDGE + r"shp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}", 0),
    ("PyPI token", _EDGE + r"pypi-AgE[A-Za-z0-9_-]{50,}", 0),
    ("Databricks token", _EDGE + r"dapi[a-f0-9]{32}", 0),
    ("Vault token", _EDGE + r"hv[sb]\.[A-Za-z0-9_-]{24,}", 0),
    ("Groq key", _EDGE + r"gsk_[A-Za-z0-9]{40,}", 0),
    ("xAI key", _EDGE + r"xai-[A-Za-z0-9]{40,}", 0),
    ("Supabase token", _EDGE + r"sbp_[a-f0-9]{40}", 0),
    ("Sentry token", _EDGE + r"sntry[us]_[A-Za-z0-9_=+/-]{40,}", 0),
    ("Slack webhook", r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{16,}", 0),
    ("Discord webhook", r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}", re.I),
    ("Google OAuth token", _EDGE + r"ya29\.[A-Za-z0-9_-]{20,}", 0),
    ("GitLab token", _EDGE + r"gl(?:dt|rt)-[A-Za-z0-9_-]{20,}", 0),
    # Basic credentials are base64; prose after "Basic" has no digits or symbols.
    ("basic auth credentials", r"\bBasic\s+(?=[A-Za-z0-9+/]*[0-9+/])[A-Za-z0-9+/]{16,}={0,2}", 0),
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


PRIVATE_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def find_secrets(text):
    """Every credential-looking span as (kind, start, end), in text order, without overlaps.
    For key = value assignments only the value is covered, so the key name stays readable."""
    spans = []
    for name, pattern in SECRET_PATTERNS:
        for m in pattern.finditer(text):
            end = m.end()
            if name == "private key":
                # The header only marks the block: cover the key material through its END line (or the rest of the text).
                closing = PRIVATE_KEY_END.search(text, m.end())
                end = closing.end() if closing else len(text)
            spans.append((name, m.start(), end))
    for match in ASSIGNMENT.finditer(text):
        value = match[1]
        if not PLACEHOLDER.match(value) and (re.search(r"[0-9]", value) or re.search(r"[^A-Za-z]", value)):
            spans.append(("credential assignment", match.start(1), match.end(1)))
    result = []
    for span in sorted(spans, key=lambda s: (s[1], -s[2])):
        if not result or span[1] >= result[-1][2]:
            result.append(span)
    return result


def redact(text):
    """(text with each credential replaced by <redacted type='KIND'/>, kinds replaced)."""
    spans = find_secrets(text)
    for kind, start, end in reversed(spans):
        text = text[:start] + f"<redacted type='{kind}'/>" + text[end:]
    return text, [kind for kind, _, _ in spans]


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
FRONT_KEYS = ("title", "type", "status", "tags", "context", "files", "commands", "sources")
SYSTEM_KEYS = ("id", "domain", "created", "updated", "review_by", "confirmed", "supersedes", "superseded_by", "schema_version", "capture_key", "origin", "repo")
MAX_FILES = 30


def clean_files(value):
    """The `files` property as a list of forward-slash paths (Obsidian may store one path as text)."""
    if value in (None, ""):
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        raise ValueError("files must be a list of paths")
    result = []
    for item in items:
        text = str(item).strip().replace("\\", "/")
        if text.startswith("./"):
            text = text[2:]
        if not text:
            continue
        if len(text) > 300 or "\n" in text:
            raise ValueError("files entries must be single paths of at most 300 characters")
        result.append(text)
    return list(dict.fromkeys(result))[:MAX_FILES]


def clean_commands(value):
    """The `commands` property as a list of single-line command strings, at most MAX_COMMANDS of MAX_CHARS each."""
    from .commands import MAX_COMMANDS, MAX_CHARS
    if value in (None, ""):
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        raise ValueError("commands must be a list of strings")
    result = list(dict.fromkeys(" ".join(item.split()) for item in items if item.strip()))
    if len(result) > MAX_COMMANDS or any(len(item) > MAX_CHARS for item in result):
        raise ValueError(f"commands holds at most {MAX_COMMANDS} commands of {MAX_CHARS} characters each")
    return result


MAX_SOURCES = 20
SHA256 = re.compile(r"[0-9a-f]{64}")


def check_sources(value):
    """The `sources` property must be a list of at most MAX_SOURCES {path, sha256} objects (path text of at most 300 characters, sha256 64 hex digits)."""
    if not isinstance(value, list) or len(value) > MAX_SOURCES:
        raise ValueError(f"sources must be a list of at most {MAX_SOURCES} {{path, sha256}} objects")
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"} or not isinstance(item["path"], str) or not item["path"].strip()
                or len(item["path"]) > 300 or "\n" in item["path"] or not isinstance(item["sha256"], str) or not SHA256.fullmatch(item["sha256"])):
            raise ValueError("sources entries are {path (text of at most 300 characters), sha256 (64 hex digits)}")


def clean_sources(value):
    """The `sources` property as a validated list, one entry per path (a later entry replaces an earlier one), forward slashes."""
    if value in (None, ""):
        return []
    if isinstance(value, list):
        value = [dict(item, path=item["path"].strip().replace("\\", "/")) if isinstance(item, dict) and isinstance(item.get("path"), str) else item for item in value]
    check_sources(value)
    return list({item["path"]: {"path": item["path"], "sha256": item["sha256"]} for item in value}.values())


def serialize(note):
    from .note_format import format_body, TITLE_MARK
    meta = {k: v for k, v in note.items() if k not in {"body", "path", "digest", "revision", "learning_state", "learning_pending_edit"}}
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


# Parsed notes this process has already read, per canonical directory: {directory: {path: (mtime_ns, size, note)}}.
# One command scans a domain several times (search, duplicate checks, the index rebuild, session context), and
# a long-lived `serve` process scans it on every request; re-reading and re-hashing every file each time made
# latency grow with the store. A file is re-read whenever its size or modification time differs.
_PARSED = {}
# A file changed this recently is not remembered: where timestamps are coarse, a second change could look the same.
SETTLE_NS = 2_000_000_000


def _note_files(root, directory):
    """[(path, stat)] for the Markdown files under a canonical directory, in path order.

    Every directory is checked with confined() and every entry for being a symbolic link, which keeps what
    confined() guaranteed per file (no link anywhere between the store root and the note) at one check per
    directory, using the listing's own data for each file."""
    found = []
    pending = [directory]
    while pending:
        current = pending.pop()
        confined(root, current.relative_to(root))
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise ValueError("Symbolic links are not allowed in the store")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif os.path.normcase(entry.name).endswith(".md"):
                    found.append((Path(entry.path), entry.stat(follow_symlinks=False)))
    return sorted(found, key=lambda item: item[0])


def _remembered(directory, files):
    """[(path, note or None)] for these files, parsing only those not already read at this size and time."""
    import time
    known = _PARSED.get(str(directory), {})
    current = {}
    notes = []
    for path, stat in files:
        key = str(path)
        hit = known.get(key)
        if hit and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
            current[key] = hit
            note = hit[2]
        else:
            note = parse(path)
            if time.time_ns() - stat.st_mtime_ns > SETTLE_NS:
                current[key] = (stat.st_mtime_ns, stat.st_size, note)
        # Callers change the notes they are given, so each gets its own copy.
        notes.append((path, {k: (v.copy() if isinstance(v, (list, dict)) else v) for k, v in note.items()} if note is not None else None))
    _PARSED[str(directory)] = current
    return notes


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
        if not directory.is_dir():
            continue
        for path, note in _remembered(directory, _note_files(root, directory)):
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
    notes=_scan(domain, root, retired)[0]
    from .learning import approved_notes
    return approved_notes(Path(root or memory_root()),domain,notes,actionable=not retired)


def problems(domain, root=None):
    """Canonical files that inventory skipped, for doctor and session start."""
    return _scan(domain, root, True)[1]


def validate(note, domain, root=None, new=False, extra_tags=()):
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
        if note.get('status') == 'resolved' and note['type'] not in ('plan', 'bug', 'risk'):
            raise ValueError('Only plan, bug and risk notes may have resolved status')
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
    if "files" in note and (not isinstance(note["files"], list) or any(not isinstance(f, str) for f in note["files"])):
        raise ValueError("files must be a list of paths")
    if "sources" in note:
        check_sources(note["sources"])
    if "repo" in note and not isinstance(note["repo"], str):
        raise ValueError("repo must be text (the git identity of the folder a map or brief is about)")
    if "commands" in note:
        from .commands import MAX_COMMANDS, MAX_CHARS
        if not isinstance(note["commands"], list) or any(not isinstance(c, str) or not c.strip() or len(c) > MAX_CHARS for c in note["commands"]) or len(note["commands"]) > MAX_COMMANDS:
            raise ValueError(f"commands must be a list of at most {MAX_COMMANDS} strings of {MAX_CHARS} characters each")
    if "confirmed" in note and (not isinstance(note["confirmed"], list) or any(not isinstance(d, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in note["confirmed"])):
        raise ValueError("confirmed must be a list of YYYY-MM-DD dates")
    taxonomy = read_json(Path(root or memory_root()) / "tag_taxonomy.json", {"tags": []})
    known = taxonomy.get("tags", []) if isinstance(taxonomy, dict) else taxonomy
    if isinstance(known, dict):
        known = list(known)
    if known and extra_tags:
        known = list(known) + list(extra_tags)
    if new:
        for required in ("subject", "concern"):
            if not any(t.startswith(required + "/") for t in tags):
                options = sorted(t for t in known if t.startswith(required + "/"))
                raise ValueError(f"Capture requires a registered {required}/ tag; pass --{required}" + (" with one of: " + ", ".join(options) if options else ""))
        if any(t.split("/", 1)[0] not in FACETS or "/" not in t for t in tags):
            raise ValueError("Tags must use subject/, concern/, platform/, technology/, project/, or system/ facets")
    if known and set(tags) - set(known):
        raise ValueError("Unknown tags require an approved taxonomy plan: " + ", ".join(sorted(set(tags) - set(known))))
    if new and not known:
        raise ValueError("Initialize the controlled taxonomy before capturing notes")
    found = find_secret("\n".join([str(note.get(k) or "") for k in ("title", "body", "context")] + [c for c in note.get("commands") or [] if isinstance(c, str)]))
    if found:
        raise ValueError(f"Potential credential detected ({found[0]} at character {found[1]}); remove it, replace it with a placeholder such as <REDACTED>, or pass --redact")
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


def _fold_case_renames(root, changes, expected):
    """(changes, expected, renames) with each "delete Note.md + write note.md" pair that names ONE file reduced
    to the write. On a case-insensitive filesystem the two spellings are the same file, so writing the new text
    and then deleting the old name would delete the note. renames lists (old, new) so the file can take the new
    spelling; the old name's expected revision carries over to the new one."""
    changes = dict(changes)
    expected = dict(expected) if expected is not None else None
    renames = []
    for old in [rel for rel, content in changes.items() if content is None]:
        for new in [rel for rel, content in changes.items() if content is not None]:
            if str(old) == str(new) or str(old).casefold() != str(new).casefold():
                continue
            try:
                same = os.path.samefile(root / old, root / new)
            except OSError:
                same = False
            if same:
                del changes[old]
                renames.append((old, new))
                if expected is not None and str(old) in expected:
                    expected.setdefault(str(new), expected.pop(str(old)))
                break
    return changes, expected, renames


def transaction(root, changes, kind, expected=None, operation_id=None, skip_unchanged=False):
    """Durable intent precedes every write. Recovery accepts only known states.

    skip_unchanged (derived files only): leave out files that already hold these exact bytes, and
    write no journal at all when nothing is left, so a sync that changed nothing leaves no trace."""
    root = Path(root).resolve()
    operation_id = operation_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation_id):
        raise ValueError("Invalid operation ID")
    journal_path = root / ".operations" / operation_id / "journal.json"
    with store_lock(root):
        changes, expected, renames = _fold_case_renames(root, changes, expected)
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
            if skip_unchanged and before == after:
                continue
            effects.append({"path": str(Path(rel).as_posix()), "before": _encode(before), "after": _encode(after),
                            "before_hash": digest(before), "after_hash": digest(after)})
        if skip_unchanged and not effects:
            return {"ok": True, "operation_id": None, "state": "unchanged", "kind": kind}
        journal = {"schema": 1, "id": operation_id, "kind": kind, "created": now(), "state": "prepared", "effects": effects}
        atomic_write(journal_path, canonical_json(journal))
        atomic_write(journal_path.with_name(PENDING), "pending\n")
        for old, new in renames:
            # Same file, new spelling: the content is untouched, so an interruption here loses nothing.
            retry_sharing(lambda: os.rename(root / old, root / new))
        return recover(root, operation_id, "forward")


PENDING = "pending"
# Journals of regenerated vault views and sync bookkeeping. Nothing undoes them, so old ones only take space.
# Journals of note edits (including overlay_capture, overlay_auto_update, overlay_delete) are never pruned.
BOOKKEEPING_KINDS = frozenset({"overlay_sync", "overlay_state", "overlay_views"})


FINAL_STATES = frozenset({"committed", "rolled_back", "abandoned"})
# canonical_json sorts keys, and "state" sorts last, so a journal ends with its state.
_TAIL_STATE = re.compile(rb'"state":"([a-z_]+)"\}\s*$')


def unfinished_operations(root):
    """(IDs of operations that never reached a final state, IDs whose journal cannot be read).

    Only the last bytes of each journal are read. A store keeps every note-edit journal, with the notes'
    before and after text inside, so parsing them all made each health check slower with every write."""
    pending, unreadable = [], []
    for path in sorted(Path(root).glob(".operations/*/journal.json")):
        state = None
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 64))
                match = _TAIL_STATE.search(handle.read())
            state = match[1].decode() if match else read_json(path)["state"]
        except (OSError, ValueError, KeyError, TypeError):
            unreadable.append(path.parent.name)
            continue
        if state not in FINAL_STATES:
            pending.append(path.parent.name)
    return pending, unreadable


def prune_journals(root, days=14, kinds=BOOKKEEPING_KINDS):
    """Delete committed journals of the given kinds older than `days`; returns how many were removed."""
    from datetime import timedelta
    import shutil
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    with store_lock(Path(root).resolve()):
        for journal_path in sorted(Path(root).glob(".operations/*/journal.json")):
            folder = journal_path.parent
            if (folder / PENDING).exists():
                continue
            value = read_json(journal_path, None)
            if not isinstance(value, dict) or value.get("kind") not in kinds or value.get("state") != "committed":
                continue
            try:
                created = datetime.fromisoformat(str(value.get("created")))
            except ValueError:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
    return removed


def prune_plans(root, applied_days=1, unapplied_days=30):
    """Delete plan files: applied ones (committed journal) older than `applied_days`, never-applied ones older
    than `unapplied_days`. A plan whose journal is in any other state, or unreadable, stays. Returns the count removed."""
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc)
    removed = 0
    with store_lock(Path(root).resolve()):
        for plan_path in sorted(Path(root).glob(".plans/*.json")):
            if not re.fullmatch("[a-f0-9]{32}", plan_path.stem):
                continue
            try:
                value = read_json(plan_path, None)
                created = datetime.fromisoformat(str(value.get("created")))
            except (ValueError, AttributeError):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            folder = Path(root) / ".operations" / plan_path.stem
            if folder.exists():
                try:
                    journal = read_json(folder / "journal.json", None)
                except ValueError:
                    continue
                if not isinstance(journal, dict) or journal.get("state") != "committed" or (folder / PENDING).exists():
                    continue
                cutoff = now_utc - timedelta(days=applied_days)
            else:
                cutoff = now_utc - timedelta(days=unapplied_days)
            if created < cutoff:
                plan_path.unlink(missing_ok=True)
                removed += 1
    return removed


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

