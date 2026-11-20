"""Custom note type registry (note_types.json): store-wide types that users define, next to the thirteen built-in ones.

A custom type states its description, summary prompt, sections, whether it takes action items, its review horizon and its default
concern and subject tags (core/note_types.definition reads them). Types are created and changed only through `type` (direct,
journaled, undoable); a type is retired, never deleted, so notes of it keep working."""
from __future__ import annotations
from pathlib import Path
from .note_types import NOTE_TYPES, LEGACY_TYPES, TYPE_NAME, REGISTRY, definition, builtin_definition, custom_types
from .repository import read_json, canonical_json, find_secrets, store_lock, now

STATUSES = ('active', 'retired')
# Legacy prefixes a name must not take: RESOLVED marked resolved bugs before schema 3.
RESERVED = set(NOTE_TYPES) | set(LEGACY_TYPES) | {'resolved'}
LAYOUT_HEADINGS = ('summary', 'important', 'details', 'links', 'action items')
MAX_SECTIONS = 8
REVIEW_RANGE = (7, 730)
EDITABLE = {'description', 'summary', 'sections', 'actions', 'review_days', 'concern', 'subject'}
FIELDS = {'create': {'name'} | EDITABLE, 'update': {'name', 'status'} | EDITABLE, 'retire': {'name'}}


def _path(root):
    return Path(root) / REGISTRY


def load(root):
    """The registry as {'version':1,'types':[...]}; a missing file is an empty registry."""
    data = read_json(_path(root), {'version': 1, 'types': []})
    if not isinstance(data, dict) or not isinstance(data.get('types'), list) or any(not isinstance(t, dict) or not isinstance(t.get('name'), str) for t in data['types']):
        raise ValueError(f'Invalid or unreadable configuration preserved: {_path(root)}')
    return data


def _text(value, field, low, high):
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise ValueError(f'{field} must be {low}..{high} characters')
    return value.strip()


def _sections(value):
    from .note_format import _letters
    if not isinstance(value, list) or len(value) > MAX_SECTIONS:
        raise ValueError(f'sections must be a list of at most {MAX_SECTIONS} headings')
    out, seen = [], set()
    for heading in value:
        heading = _text(heading, 'section heading', 1, 40)
        key = _letters(heading)
        if not key:
            raise ValueError(f'section heading {heading!r} needs letters')
        if key in LAYOUT_HEADINGS or key in ('executive summary', 'key details', 'resource links', 'important note'):
            raise ValueError(f'section heading {heading!r} is a layout section every note already has')
        if key in seen:
            raise ValueError(f'section heading {heading!r} is listed twice')
        seen.add(key)
        out.append(heading)
    return out


def _tag(value, facet, known):
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(facet + '/'):
        raise ValueError(f'{facet} must be a {facet}/ tag or null')
    if value not in known:
        raise ValueError(f'{value} is not in the taxonomy; add it with taxonomy add first')
    return value


def _clean(values, base, root):
    """Validated registry fields from a payload's values, layered over base (an entry)."""
    from .notes import taxonomy
    entry = dict(base)
    if 'description' in values:
        entry['description'] = _text(values['description'], 'description', 1, 300)
    if 'summary' in values:
        entry['summary'] = _text(values['summary'] if values['summary'] is not None else '', 'summary', 0, 200)
    if 'sections' in values:
        entry['sections'] = _sections(values['sections'] if values['sections'] is not None else [])
    if 'actions' in values:
        if not isinstance(values['actions'], bool):
            raise ValueError('actions must be true or false')
        entry['actions'] = values['actions']
    if 'review_days' in values:
        days = values['review_days']
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not REVIEW_RANGE[0] <= days <= REVIEW_RANGE[1]):
            raise ValueError(f'review_days must be null or a whole number of days from {REVIEW_RANGE[0]} to {REVIEW_RANGE[1]}')
        entry['review_days'] = days
    if 'concern' in values or 'subject' in values:
        known = taxonomy(root)
        if 'concern' in values:
            entry['concern'] = _tag(values['concern'], 'concern', known)
        if 'subject' in values:
            entry['subject'] = _tag(values['subject'], 'subject', known)
    if 'status' in values:
        if values['status'] not in STATUSES:
            raise ValueError('status must be one of ' + ', '.join(STATUSES))
        entry['status'] = values['status']
    found = find_secrets('\n'.join([entry.get('description', ''), entry.get('summary', '')] + list(entry.get('sections', []))))
    if found:
        raise ValueError(f'Potential credential detected ({found[0][0]}) in the type fields; remove it')
    return entry


def _registry(entries):
    return canonical_json({'version': 1, 'types': sorted(entries, key=lambda t: t['name'])})


def _domains(root):
    base = Path(root) / 'domains'
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def notes_by_type(root):
    """{type: [notes]} for every note in the store (all domains, any status) whose type is neither built-in nor legacy."""
    from .repository import inventory
    found = {}
    for domain in _domains(root):
        try:
            notes = inventory(domain, root, retired=True)
        except (OSError, ValueError):
            continue
        for note in notes:
            if note.get('type') and note.get('type') not in NOTE_TYPES and note.get('type') not in LEGACY_TYPES:
                found.setdefault(note.get('type'), []).append(note)
    return found


def _with_actions(notes):
    from .note_format import action_items
    return [n for n in notes if n.get('status') == 'resolved' or action_items(n.get('body') or '')]


def check_registry(root, entries):
    """ValueError when a registry holding `entries` would leave notes invalid: a note of a type it does not register, or a note
    with action items (or resolved) of a type that takes none. Used by `type update` and by undo of a type write."""
    by_name = {e['name']: e for e in entries}
    for name, notes in sorted(notes_by_type(root).items(), key=lambda kv: str(kv[0])):
        entry = by_name.get(name)
        if entry is None:
            raise ValueError(f'{len(notes)} note(s) use type {name}, which would no longer be registered (for example {notes[0].get("domain")}:{str(notes[0].get("id"))[:8]})')
        if not entry.get('actions'):
            acting = _with_actions(notes)
            if acting:
                raise ValueError(f'{len(acting)} note(s) of type {name} have action items or are resolved, so {name} must keep taking action items (for example {acting[0].get("domain")}:{str(acting[0].get("id"))[:8]})')


def type_write(root, action, payload):
    """A direct, journaled write creating, updating or retiring one custom type (kinds type_create, type_update, type_retire)."""
    from .governance import commit
    root = Path(root).resolve()
    if action not in FIELDS:
        raise ValueError('type action must be create, update or retire')
    if not isinstance(payload, dict):
        raise ValueError('Payload must be a JSON object')
    extra = set(payload) - FIELDS[action]
    if extra:
        raise ValueError('Unknown payload fields: ' + ', '.join(sorted(extra)))
    name = payload.get('name')
    if not isinstance(name, str) or not TYPE_NAME.fullmatch(name):
        raise ValueError('name must match [a-z][a-z0-9]{2,23}: lowercase letters and digits, starting with a letter')
    if name in RESERVED:
        raise ValueError(f'{name} is a built-in or legacy note type; pick another name')
    with store_lock(root):
        entries = load(root)['types']
        current = next((t for t in entries if t['name'] == name), None)
        others = [t for t in entries if t['name'] != name]
        stamp = now()
        if action == 'create':
            if current:
                raise ValueError(f'Note type {name} already exists' + (' (retired; reactivate it with type update and status active)' if current.get('status') == 'retired' else ''))
            if 'description' not in payload:
                raise ValueError('create requires description')
            base = {'name': name, 'status': 'active', 'description': '', 'summary': '', 'sections': [], 'actions': False, 'review_days': None, 'concern': None, 'subject': None, 'created': stamp, 'updated': stamp}
            entry = _clean({k: v for k, v in payload.items() if k != 'name'}, base, root)
            return commit(root, {REGISTRY: _registry(others + [entry])}, 'type_create', [], {'name': name, 'files': 1})
        if not current:
            raise ValueError(f'Unknown note type {name}')
        values = {'status': 'retired'} if action == 'retire' else {k: v for k, v in payload.items() if k != 'name'}
        if not values:
            raise ValueError('update requires at least one of: ' + ', '.join(sorted(FIELDS['update'] - {'name'})))
        entry = _clean(values, current, root)
        entry['updated'] = stamp
        if current.get('actions') and not entry.get('actions'):
            check_registry(root, others + [entry])
        return commit(root, {REGISTRY: _registry(others + [entry])}, 'type_' + action, [], {'name': name, 'fields': sorted(values), 'files': 1})


def listing(root, include_retired=False):
    """Every type with its definition: the built-ins, then custom types (retired ones only with include_retired), and warnings
    for notes whose type is not registered."""
    root = Path(root)
    load(root)
    rows = [builtin_definition(name) for name in NOTE_TYPES]
    for name, entry in sorted(custom_types(root).items()):
        if include_retired or entry.get('status', 'active') == 'active':
            row = definition(name, root)
            row.update({k: entry[k] for k in ('created', 'updated') if k in entry})
            rows.append(row)
    registered = set(custom_types(root))
    warnings = [f'{len(notes)} note(s) use type {name}, which is not registered; register it with type create or retype them'
                for name, notes in sorted(notes_by_type(root).items(), key=lambda kv: str(kv[0])) if name not in registered]
    return {'ok': True, 'types': rows, 'warnings': warnings}
