"""The note type is the source of truth for human-readable prefixes and review horizons."""
from datetime import date, datetime, timedelta, timezone

NOTE_TYPES = ('knowledge', 'decision', 'preference', 'context', 'status', 'bug', 'plan', 'risk', 'research', 'policy')
LEGACY_TYPES = {'correction': 'knowledge', 'progress': 'status'}


def prefix_for(note_type, legacy=False):
    if note_type not in NOTE_TYPES and not (legacy and note_type in LEGACY_TYPES):
        raise ValueError('Invalid note type; expected ' + ', '.join(NOTE_TYPES))
    return note_type.upper()


def effective_type(note):
    # Legacy BUG/RESOLVED prefixes were the only way to express a bug.
    if note.get('schema_version', 1) < 3 and note.get('prefix') in {'BUG', 'RESOLVED'}:
        return 'bug'
    selected = note.get('type', 'knowledge')
    selected = LEGACY_TYPES.get(selected, selected)
    prefix_for(selected)
    return selected


def normalize(note, selected=None):
    note = dict(note)
    note['type'] = effective_type(note) if selected is None else selected
    note['prefix'] = prefix_for(note['type'])
    note['schema_version'] = 3
    return note


# Time-sensitive types go stale: they get a review date at capture and on every body update.
REVIEW_DAYS = {'status': 30, 'plan': 30, 'bug': 60, 'risk': 90}
# Notes without a review date are due once they have not changed for this long.
STALE_DAYS = 180


def default_review_by(note_type, today=None):
    days = REVIEW_DAYS.get(LEGACY_TYPES.get(note_type, note_type))
    if not days:
        return None
    return ((today or date.today()) + timedelta(days=days)).isoformat()


def review_due(note, today=None):
    """True when an active note should be checked: its review date passed, or it is old."""
    if note.get('status', 'active') != 'active':
        return False
    today = today or date.today()
    review = str(note.get('review_by') or '')[:10]
    if review:
        return review < today.isoformat()
    try:
        updated = datetime.fromisoformat(str(note.get('updated') or note.get('created') or '').replace('Z', '+00:00'))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    note_type = LEGACY_TYPES.get(note.get('type'), note.get('type'))
    horizon = REVIEW_DAYS.get(note_type, STALE_DAYS)
    return updated.date() + timedelta(days=horizon) < today
