"""Durable-statement signal detection, settlement sidecar, coverage hashing, and prompt nudges (REL-D3)."""
from __future__ import annotations

import re

from .common import atomic_write
from .repository import canonical_json, read_json

MACHINE_PROMPT = re.compile(r'\A\s*(?:<(?:agent-message|task-notification|system-reminder)\b|\[SYSTEM NOTIFICATION)')

IGNORED = re.compile(r'```.*?(?:```|\Z)|<(system-reminder|agent-message|task-notification)\b.*?(?:</\1>|\Z)', re.S | re.I)
QUOTED = re.compile(r'<pasted_content\b[^>]*>(.*?)(?:</pasted_content>|\Z)|^[ \t]*>[ \t]*([^\r\n]*)$', re.S | re.M | re.I)
QUOTED_CHARS = 2000
STATEMENT_BREAK = re.compile(r'(?<=[.!?])\s+|\n+')
TRANSIENT = re.compile(r"\b(?:for now|for the moment|this time|just this once|right now|for this (?:task|pr|change|commit|session|turn))\b", re.I)

SIGNALS = (
    ('remember', re.compile(r"\b(?:remember (?:that|this|we|it)|keep in mind|for (?:the )?record|don'?t forget(?! to\b))\b", re.I)),
    ('decision', re.compile(
        r"\b(?:let'?s (?:go with|use)|we(?:'ll| will) (?:go with|use)|we decided|i(?:'ve)? decided|go with option|we(?:'ve| have)? (?:agreed|chosen|settled on)"
        r"|the decision is|effective (?:immediately|today|from|as of)|(?<!\byou )(?:is|are) (?:not )?(?:allowed|permitted|prohibited)|(?:we|the team) must (?:never|not|always|stay|remain))\b|\bdecision:",
        re.I,
    )),
    ('correction', re.compile(r"^\s*(?:no|nope)\s*[,.!:;—-]|^\s*actually,?\s+(?:it'?s|it is|that'?s|the|we|i|you)\b|\bthat'?s (?:wrong|not right|incorrect|not what i)\b|\byou(?:'re| are| were) wrong\b|\bi meant\b", re.I)),
    ('preference', re.compile(
        r"\b(?:i(?:'d)? prefer|i'?d rather|i (?:hate|don'?t like)|from now on|going forward|in (?:the )?future|please (?:don'?t|do not|never|always)|stop doing|don'?t ever|by default,? use"
        r"|(?:you should|you must|we) (?:always|never)|i (?:always|never) (?:use|want|like|do))\b|^\s*(?:always|never)\s+\w",
        re.I,
    )),
)
DIRECTIVE_STRONG = re.compile(r"\b(?:decided|decision|directive|policy|effective (?:immediately|today|from|as of)|must never|must not|may not)\b", re.I)
DIRECTIVE_WEAK = tuple(
    re.compile(p, re.I)
    for p in (
        r"\bmust (?:be|use|stay|remain|always)\b",
        r"\b(?:replac(?:es|ing)|supersed(?:es|ing)|no longer)\b",
        r"\b(?:going forward|from now on|in future)\b",
        r"\b(?:all|every|each) [\w -]{0,40}\b(?:must|shall)\b",
    )
)
PRIORITY = ('remember', 'decision', 'directive', 'correction', 'preference')
SIGNAL_TEXT = {
    'remember': ('asked you to remember something', '--type TYPE --concern CONCERN'),
    'correction': ('corrected you', '--type knowledge --concern correction'),
    'preference': ('stated a preference', '--type preference --concern preference'),
    'decision': ('made a decision', '--type decision --concern decision'),
    'directive': ('shared a directive (a memo, ticket or decision record)', '--type decision --concern decision --link SOURCE'),
}
BLOCKING_SIGNALS = frozenset({'remember', 'decision', 'directive'})

SIGNALS_SEEN = '.signals-seen.json'
MAX_SIGNALS_SEEN = 500
MAX_SIGNAL_WORDS = 12
MIN_COVER_WORDS = 2

SIGNAL_STOP = frozenset(
    '''a an the and or but of to in on at by for with from into as so if then than that this these those there here
is are was were be been being am do does did done it its we i you they he she our my your their me us them not no yes ok please just
also too very will would should can could may might must shall let lets ll ve re
remember keep mind forget record prefer rather like hate decided decide decision agreed chosen settled going forward future now
always never actually wrong meant team effective immediately today allowed permitted prohibited'''.split()
)

NUDGE_KINDS = ('preference', 'correction', 'decision')
NUDGE_NOTE_TYPES = frozenset({'preference', 'policy', 'decision', 'knowledge'})
NUDGE_NOTES = 2


def _statements(text):
    return [s for s in STATEMENT_BREAK.split(text) if s.strip() and not s.rstrip().endswith('?') and not TRANSIENT.search(s)]


def signals_of(prompt, skip=None):
    """[(kind, the statement that carries it)], one per kind the user's own words carry, highest priority first."""
    if not isinstance(prompt, str) or MACHINE_PROMPT.match(prompt):
        return []
    plain = IGNORED.sub(' ', prompt)
    statements = [s for s in _statements(QUOTED.sub(' ', plain)) if not (skip and skip(s))]
    found = {}
    for kind, pattern in SIGNALS:
        hit = next((s for s in statements if pattern.search(s)), None)
        if hit:
            found[kind] = hit
    quoted = [s for s in _statements('\n'.join(m[1] or m[2] or '' for m in QUOTED.finditer(plain))[:QUOTED_CHARS]) if not (skip and skip(s))]
    strong = next((s for s in quoted if DIRECTIVE_STRONG.search(s)), None)
    weak = [(s, p) for s in quoted for p in DIRECTIVE_WEAK if p.search(s)]
    if strong:
        found['directive'] = strong
    elif len({p for _, p in weak}) >= 2:
        found['directive'] = weak[0][0]
    return [(kind, found[kind]) for kind in PRIORITY if kind in found]


def signal_of(prompt, skip=None):
    """(kind, statement) of the highest-priority kind signals_of finds, else (None, None)."""
    found = signals_of(prompt, skip)
    return found[0] if found else (None, None)


def durable_signal(prompt):
    """The kind of durable statement the user's own words carry, else None."""
    return signal_of(prompt)[0]


def _statement_hash(salt, statement):
    from .private import _hash, _words
    return _hash(salt, _words(statement))


def signals_seen(root, domain):
    """(raised, hashed): raised(statement) is True for a statement settled before; hashed(statement) is its salted hash."""
    path = root / 'domains' / domain / SIGNALS_SEEN
    row = read_json(path, {}) or {}
    row = row if isinstance(row, dict) else {}
    raised = lambda statement: bool(row.get('salt')) and _statement_hash(row['salt'], statement) in (row.get('seen') or [])

    def hashed(statement):
        if not row.get('salt'):
            import secrets
            row['salt'] = secrets.token_hex(16)
            try:
                atomic_write(path, canonical_json(row))
            except OSError:
                pass
        return _statement_hash(row['salt'], statement)

    return raised, hashed


def settle_signal(root, domain, value):
    """Remember a statement's hash once its turn is settled."""
    if value:
        settle_signals(root, domain, [value])


def settle_signals(root, domain, values):
    """Remember one or more statement hashes in a single read/write of .signals-seen.json (REL-P5)."""
    clean = [v for v in values if v]
    if not clean or not domain:
        return
    try:
        path = root / 'domains' / domain / SIGNALS_SEEN
        row = read_json(path, {}) or {}
        if not row.get('salt'):
            return
        before = list(row.get('seen', []))
        seen = list(before)
        for value in clean:
            seen = [h for h in seen if h != value] + [value]
        seen = seen[-MAX_SIGNALS_SEEN:]
        if seen != before:
            atomic_write(path, canonical_json({**row, 'seen': seen}))
    except (OSError, ValueError, AttributeError):
        pass


def content_hashes(salt, text, limit=None):
    """Salted hashes of the distinct content words of a text, in order."""
    from .private import _hash, _words
    from .search import stem
    words = dict.fromkeys(stem(w) for w in _words(text) if w not in SIGNAL_STOP and len(w) > 1)
    return [_hash(salt, [w]) for w in list(words)[:limit]]


def covers_signal(root, refs, state):
    """True when one of the notes a turn's receipts touched shares at least min(2, n) content words with the armed statement."""
    from .notes import find_note
    wanted = set(state.get('signal_words') or [])
    salt = state.get('signal_salt')
    for ref in dict.fromkeys(refs):
        try:
            domain, note_id = ref.split(':', 1)
            note = find_note(note_id, domain, root)[1]
        except (ValueError, OSError):
            continue
        if len(wanted & set(content_hashes(salt, (note.get('title') or '') + ' ' + (note.get('body') or '')))) >= min(MIN_COVER_WORDS, len(wanted)):
            return True
    return False


def signal_notes(statement, domain, root, bindings):
    """Up to NUDGE_NOTES saved notes that the statement may change, through the relevance gate. Fails open: []."""
    try:
        from .common import global_domains
        from .prompt_context import relevance_gate, scope_domains
        from .search import search_domains
        domains = list(dict.fromkeys([domain, *scope_domains(domain, bindings), *global_domains(root)]))
        hits = search_domains(statement, domains, 6, root, keep=relevance_gate(statement, root), ensure_fresh=False)
        return [h for h in hits if h.get('type') in NUDGE_NOTE_TYPES and not h.get('sources')][:NUDGE_NOTES]
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.signal_notes', exc, root)
        return []


def signal_nudge(signal, session, kinds=None, notes=None):
    """The one line a prompt with a durable statement adds to its own context."""
    said, hint = SIGNAL_TEXT[signal]
    said = [SIGNAL_TEXT[k][0] for k in kinds if k in SIGNAL_TEXT] if kinds and signal in kinds else [said]
    said = said[0] if len(said) == 1 else ', '.join(said[:-1]) + ' and ' + said[-1]
    line = (
        f"Living Memory: the user {said} in this prompt. If it should outlast this session, update its note or "
        f"CMD capture --session {session} {hint} --title '...' --summary '...' before you finish; if not, nothing is needed."
    )
    if notes:
        from .notes import short_ref
        names = ' or '.join(f"{short_ref(h['domain'], h['id'])} {h['title'][:70]}" for h in notes)
        line += f" It may change {names}; update that note instead of capturing a new one."
    return line
