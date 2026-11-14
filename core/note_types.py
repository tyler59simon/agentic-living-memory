"""The note type is the source of truth for human-readable prefixes and review horizons."""
from datetime import date, datetime, timedelta, timezone

NOTE_TYPES = ('knowledge', 'decision', 'preference', 'context', 'status', 'bug', 'plan', 'risk', 'research', 'policy', 'map', 'runbook', 'brief')
# One active note per folder, keyed by the first `files` entry (core/maps.py): a repo's codebase map, a project folder's brief.
KEYED_TYPES = ('map', 'brief')
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
# A codebase map drifts as the code moves, so it is checked more often than durable knowledge; a runbook drifts as operations change;
# a project brief carries the current phase and blockers, so it is checked as often as a status.
REVIEW_DAYS = {'status': 30, 'plan': 30, 'bug': 60, 'risk': 90, 'map': 90, 'runbook': 90, 'brief': 30}
# Notes without a review date are due once they have not changed for this long.
STALE_DAYS = 180


def default_review_by(note_type, today=None):
    days = REVIEW_DAYS.get(LEGACY_TYPES.get(note_type, note_type))
    if not days:
        return None
    return ((today or date.today()) + timedelta(days=days)).isoformat()


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
        updated = datetime.fromisoformat(str(note.get('updated') or note.get('created') or '').replace('Z', '+00:00'))
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


def review_due(note, today=None):
    """True when an active note should be checked: its review date passed, or it is old.
    A confirmation counts as fresh: `confirm` resets the age a note without a review date is judged by."""
    if note.get('status', 'active') != 'active':
        return False
    today = today or date.today()
    review = str(note.get('review_by') or '')[:10]
    if review:
        return review < today.isoformat()
    known = effective_date(note)
    if known is None:
        return False
    note_type = LEGACY_TYPES.get(note.get('type'), note.get('type'))
    horizon = REVIEW_DAYS.get(note_type, STALE_DAYS)
    return known + timedelta(days=horizon) < today


def review_order(notes, read_refs=(), domain=None, today=None):
    """Due notes, most neglected first: never confirmed and not in read_refs ('domain:id' strings the usage
    diary saw read or used; it keeps 30 days) ahead of the rest, then the oldest review date or effective date."""
    today = today or date.today()
    read = set(read_refs)
    def key(note):
        confirmed = last_confirmed(note)
        neglected = not confirmed and f"{domain or note.get('domain')}:{note.get('id')}" not in read
        when = str(note.get('review_by') or '')[:10] or (effective_date(note) or today).isoformat()
        return (not neglected, when, str(note.get('title', '')))
    return sorted(notes, key=key)
