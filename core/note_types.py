"""The note type is the source of truth for human-readable prefixes and review horizons.

The thirteen built-in types are fixed. Custom types live in the store's `note_types.json` registry (core/type_registry.py) and state
their own sections, action items, review horizon, default tags and summary prompt; `definition` answers for both kinds."""
import json, os, re
from datetime import date, timedelta, timezone
from pathlib import Path

from . import clock
from .common import parse_iso

NOTE_TYPES = ('knowledge', 'decision', 'preference', 'context', 'status', 'bug', 'plan', 'risk', 'research', 'policy', 'map', 'runbook', 'brief')
# One active note per folder, keyed by the first `files` entry (core/maps.py): a repo's codebase map, a project folder's brief.
KEYED_TYPES = ('map', 'brief')
LEGACY_TYPES = {'correction': 'knowledge', 'progress': 'status'}


# A type name: a built-in, or a custom type's name. Custom names are letters and digits only, so the prefix is one plain word.
TYPE_NAME = re.compile(r'[a-z][a-z0-9]{2,23}')


def prefix_for(note_type, legacy=False, root=None):
    """The filename prefix of a type. With a store root, a registered custom type (active or retired) is valid too; with
    root='any', any well-formed name is (loading never drops a note whose custom type is not registered, see repository.parse)."""
    if note_type in NOTE_TYPES or (legacy and note_type in LEGACY_TYPES):
        return note_type.upper()
    if root == 'any' and isinstance(note_type, str) and TYPE_NAME.fullmatch(note_type):
        return note_type.upper()
    if root not in (None, 'any') and note_type in custom_types(root):
        return note_type.upper()
    raise ValueError('Invalid note type; expected ' + ', '.join(type_names(root) if root not in (None, 'any') else NOTE_TYPES))


def effective_type(note):
    # Legacy BUG/RESOLVED prefixes were the only way to express a bug.
    if note.get('schema_version', 1) < 3 and note.get('prefix') in {'BUG', 'RESOLVED'}:
        return 'bug'
    selected = note.get('type', 'knowledge')
    selected = LEGACY_TYPES.get(selected, selected)
    prefix_for(selected, root='any')
    return selected


def normalize(note, selected=None):
    note = dict(note)
    note['type'] = effective_type(note) if selected is None else selected
    note['prefix'] = prefix_for(note['type'], root='any')
    note['schema_version'] = 3
    return note


# Time-sensitive types go stale: they get a review date at capture and on every body update.
# A codebase map drifts as the code moves, so it is checked more often than durable knowledge; a runbook drifts as operations change;
# a project brief carries the current phase and blockers, so it is checked as often as a status.
REVIEW_DAYS = {'status': 30, 'plan': 30, 'bug': 60, 'risk': 90, 'map': 90, 'runbook': 90, 'brief': 30}
# Notes without a review date are due once they have not changed for this long.
STALE_DAYS = 180


def default_review_by(note_type, today=None, root=None):
    found = definition(LEGACY_TYPES.get(note_type, note_type), root)
    days = found['review_days'] if found else None
    if not days:
        return None
    return ((today or clock.today()) + timedelta(days=days)).isoformat()


def last_confirmed(note):
    """The latest `confirm` date, or None."""
    dates = [d for d in note.get('confirmed') or [] if isinstance(d, str) and len(d) == 10]
    return max(dates) if dates else None


def verified_at(note):
    """The verification baseline of a note: when its facts were last checked, as an ISO timestamp. `checked` when the note has it (a
    confirm, a body rewrite, or the baseline a cosmetic edit preserved), else the legacy baseline, `updated` or `created`. Not `max(updated,
    checked)`: a title or type edit moves `updated` but vouches for nothing."""
    return str(note.get('checked') or note.get('updated') or note.get('created') or '')


def keep_baseline(note):
    """For an edit that moves `updated` without checking the facts (a title, type or link rewrite, a move, a lifecycle change): write the
    note's old verification baseline into `checked`, so the new `updated` does not become it by accident. Only notes whose verification
    is read need it: a map or brief, or a note with digest sources or file hashes. Mutates and returns the note."""
    if not note.get('checked') and (note.get('type') in KEYED_TYPES or note.get('file_hashes') or note.get('sources')):
        baseline=verified_at(note)
        if baseline:note['checked']=baseline
    return note


def effective_date(note):
    """When the note was last known true: its last content change or confirmation, whichever is later."""
    try:
        updated = parse_iso(str(note.get('updated') or note.get('created') or ''))
    except ValueError:
        updated = None
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    # Review dates, confirmations and `today` are local calendar days, so the note's age is counted in
    # local days too; its UTC date runs a day ahead in the evening west of Greenwich.
    found = updated.astimezone().date() if updated else None
    confirmed = last_confirmed(note)
    if confirmed:
        try:
            found = max(filter(None, (found, date.fromisoformat(confirmed))))
        except ValueError:
            pass
    return found


def review_due(note, today=None, root=None):
    """True when an active note should be checked: its review date passed, or it is old.
    A confirmation counts as fresh: `confirm` resets the age a note without a review date is judged by."""
    if note.get('status', 'active') != 'active':
        return False
    today = today or clock.today()
    review = str(note.get('review_by') or '')[:10]
    if review:
        return review < today.isoformat()
    known = effective_date(note)
    if known is None:
        return False
    note_type = LEGACY_TYPES.get(note.get('type'), note.get('type'))
    found = None if note_type in NOTE_TYPES else definition(note_type, root)
    horizon = REVIEW_DAYS.get(note_type) or (found or {}).get('review_days') or STALE_DAYS
    return known + timedelta(days=horizon) < today


def review_order(notes, read_refs=(), domain=None, today=None):
    """Due notes, most neglected first: never confirmed and not in read_refs ('domain:id' strings the usage
    diary saw read or used; it keeps 30 days) ahead of the rest, then the oldest review date or effective date."""
    today = today or clock.today()
    read = set(read_refs)
    def key(note):
        confirmed = last_confirmed(note)
        neglected = not confirmed and f"{domain or note.get('domain')}:{note.get('id')}" not in read
        when = str(note.get('review_by') or '')[:10] or (effective_date(note) or today).isoformat()
        return (not neglected, when, str(note.get('title', '')))
    return sorted(notes, key=key)


# Default tags a capture gets when it names no subject/ or concern/ tag (used only when the taxonomy has the tag).
SUBJECT_BY_TYPE = {'preference': 'subject/user'}
CONCERN_BY_TYPE = {'decision': 'concern/decision', 'preference': 'concern/preference', 'bug': 'concern/bug', 'knowledge': 'concern/knowledge', 'policy': 'concern/workflow', 'map': 'concern/architecture', 'runbook': 'concern/workflow', 'brief': 'concern/knowledge', 'plan': 'concern/workflow', 'context': 'concern/knowledge', 'status': 'concern/knowledge', 'risk': 'concern/constraint', 'research': 'concern/knowledge'}
# The summary line of each template: what the opening sentences of that type say.
TEMPLATE_PROMPTS = {
    'knowledge': 'The fact, in one or two plain sentences. Add sections named after what they explain.',
    'decision': 'The choice, who made it and when.',
    'preference': 'Whose preference it is, the default they want, and where it applies.',
    'context': 'The situation this note explains, in one or two sentences.',
    'status': 'Where things stand, as of a stated date.',
    'bug': 'What goes wrong, where, and since when.',
    'plan': 'The goal, and whether the plan is proposed or approved.',
    'risk': 'What could go wrong, what would set it off, and who owns it.',
    'research': 'The question, and the answer so far.',
    'policy': 'The rule, who requires it, and where it applies.',
    'map': 'What the repo is for, in one or two sentences.',
    'runbook': 'What the procedure does.',
    'brief': 'What the project is and where it stands.'}
# One line per built-in type, as NOTE_TYPES.md describes it (listed by `types`).
DESCRIPTIONS = {
    'knowledge': 'Established facts, explanations, reusable findings and corrected understandings.',
    'decision': 'A choice that has been made, its rationale and relevant tradeoffs.',
    'preference': "A person's desired approach or default, allowing exceptions when appropriate.",
    'context': 'Background and circumstances needed to interpret a particular project or situation.',
    'status': 'Meaningful current state, progress, completed milestones, blockers and outcomes, as of a stated time.',
    'bug': 'An observed defect: actual behavior that differs from expected behavior.',
    'plan': 'Intended future steps toward a goal, including dependencies and how success will be checked.',
    'risk': 'A possible future problem, its consequences and potential mitigation.',
    'research': 'Investigations, sources, experiments, competing explanations and open questions.',
    'policy': 'A mandatory rule or constraint, its authority and where it applies.',
    'map': "One repo's codebase orientation: purpose, layout, entry points, key modules, data flow, build and test.",
    'runbook': 'An operational procedure for a system: when to use it, preconditions, steps, verification and rollback.',
    'brief': 'The living project brief: what the project is, who owns what, where it stands and what was decided.'}

REGISTRY = 'note_types.json'
_cache = {}


def _registry_path(root):
    if root is None:
        from .common import memory_root
        root = memory_root()
    return Path(root) / REGISTRY


def custom_types(root=None):
    """{name: entry} for every registered custom type, active or retired. A missing or unreadable registry, or no store at all, has
    none here (doctor reports an unreadable registry); the file is re-read when it changes."""
    try:
        path = _registry_path(root)
        stat = os.stat(path)
    except (OSError, ValueError):
        return {}
    key = str(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    if key in _cache and _cache[key][0] == stamp:
        return _cache[key][1]
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        found = {t['name']: t for t in data.get('types', []) if isinstance(t, dict) and isinstance(t.get('name'), str) and t['name'] not in NOTE_TYPES}
    except (OSError, ValueError, AttributeError):
        found = {}
    _cache[key] = (stamp, found)
    return found


def builtin_definition(name):
    from .note_format import ACTION_TYPES, TYPE_SECTIONS
    return {'name': name, 'builtin': True, 'status': 'active', 'description': DESCRIPTIONS[name], 'summary': TEMPLATE_PROMPTS[name],
            'sections': list(TYPE_SECTIONS.get(name, ())), 'actions': name in ACTION_TYPES, 'review_days': REVIEW_DAYS.get(name),
            'concern': CONCERN_BY_TYPE.get(name), 'subject': SUBJECT_BY_TYPE.get(name, 'subject/project')}


def definition(name, root=None):
    """What a type means for capture and layout: description, summary prompt, sections, actions (action items, resolve and the
    resolved status), review_days, default concern and subject. None for an unknown name."""
    if name in NOTE_TYPES:
        return builtin_definition(name)
    if not isinstance(name, str) or not TYPE_NAME.fullmatch(name):
        return None
    entry = custom_types(root).get(name)
    if entry is None:
        return None
    return {'name': name, 'builtin': False, 'status': entry.get('status', 'active'), 'description': entry.get('description', ''),
            'summary': entry.get('summary', ''), 'sections': list(entry.get('sections') or []), 'actions': bool(entry.get('actions')),
            'review_days': entry.get('review_days'), 'concern': entry.get('concern'), 'subject': entry.get('subject') or 'subject/project'}


def type_names(root=None, active=True):
    """Built-in types, then custom types (only active ones unless active is False)."""
    custom = sorted(n for n, e in custom_types(root).items() if not active or e.get('status', 'active') == 'active')
    return tuple(NOTE_TYPES) + tuple(custom)


def takes_actions(name, root=None):
    """True when notes of this type may have action items, be resolved and reopened (plan, bug, risk, and custom types that say so)."""
    if name in NOTE_TYPES:
        from .note_format import ACTION_TYPES
        return name in ACTION_TYPES
    found = definition(name, root)
    return bool(found and found['actions'])


def check_type(name, root=None, new=True):
    """The type's definition; ValueError unless a write may use it: a built-in or an active custom type, or for an existing note
    of that type (new=False) a retired one."""
    found = definition(name, root)
    if found is None:
        raise ValueError(f'Invalid note type: {name}; use one of ' + ', '.join(type_names(root)))
    if new and found['status'] != 'active':
        raise ValueError(f'Note type {name} is retired; reactivate it with type update, or use one of ' + ', '.join(type_names(root)))
    return found
