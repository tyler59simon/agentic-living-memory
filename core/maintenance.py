"""Automatic note maintenance: candidate selection, the evidence packet, the executor and the service function `run`.

`candidates` and `packet` are a free, read-only preview (no model call, no usage events, no allowance). The model only recommends;
`apply` is the one automatic writer: it re-reads every note, pin, action item and source before it commits through the existing
journaled note change sets, one decision at a time, each with a deterministic operation id. Provider handling lives in maintenance_agent."""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from .clock import now as clock_now
from . import maintenance_agent as agent
from . import maintenance_state as ms
from .repository import canonical_json, digest, redact, store_lock, find_secret, transaction

PACKET_MAX = 128 * 1024
BODY_MAX = 24 * 1024
EXCERPTS = 4
RUN_ID_ROOM = 100
EXCERPT_CHARS = 1200
FILE_EXCERPT_CHARS = 16000     # a file up to this size is sent whole
OUTLINE_CHARS = 3000           # the outline of a larger file
FILE_READ_MAX = 1 << 20
EXCLUDED = ('preference', 'policy')            # normative: never reviewed by the model
LIMITED = ('map', 'brief', 'runbook')          # kept, deferred, confirmed or updated; never retired
COUNT_KEYS = ('updated', 'confirmed', 'archived', 'superseded', 'resolved', 'kept', 'deferred', 'skipped', 'distinct')
APPLIED_KEY = {'update': 'updated', 'confirm': 'confirmed', 'archive': 'archived', 'supersede': 'superseded', 'resolve': 'resolved', 'distinct': 'distinct'}
ARCHIVE_RULES = {'status': ('expired_status',), 'action': ('cancelled_work', 'obsolete_content', 'obsolete_source_claim'),
                 'other': ('obsolete_content', 'obsolete_source_claim')}
STATE_PRIORITY = {'changed': 0, 'missing': 1, 'unavailable': 2, 'unchecked': 3, 'current': 4}


def _clock(now):
    return (lambda: ms._aware(now)) if now is not None else clock_now


def _type(note):
    from .note_types import effective_type
    return effective_type(note)


def _excluded(note):
    return _type(note) in EXCLUDED


def _completed(note):
    from .note_format import action_items, ACTION_TYPES
    if _type(note) not in ACTION_TYPES:
        return False
    items = action_items(note.get('body', ''))
    return bool(items) and all(done for done, _ in items)


def permitted(note, kinds=(), pair_same=False, has_successor=False):
    """The actions the policy table (spec 2.1) allows for this note as it is now. Usage alone (a noisy row with no other signal) never retires."""
    if 'replaced' in kinds:
        return _replaced_permitted(note, has_successor)
    out = _permitted(note, kinds, pair_same, has_successor)
    return [a for a in out if a not in ('archive', 'supersede')] if kinds and set(kinds) <= {'noisy'} else out


def _replaced_permitted(note, has_successor):
    """The actions for the older note of a queued pair: it may be recorded as distinct from the newer one, and superseded by it where the policy table
    allows a supersede (never a pinned or limited note, which the reviewer may still record distinct). Normative notes never get here: queued_pairs drops them."""
    if not has_successor:
        return ['keep', 'defer']
    out = ['keep', 'defer', 'distinct']
    if not _excluded(note) and 'supersede' in _permitted(note, ['replaced'], True, True):
        out.append('supersede')
    return out


def _permitted(note, kinds, pair_same, has_successor):
    from .note_format import ACTION_TYPES
    t = _type(note)
    out = ['keep', 'defer']
    if t in EXCLUDED:
        return []
    out += ['confirm', 'update']
    if note.get('pinned') is True or t in LIMITED:
        return out
    if t in ACTION_TYPES:
        if _completed(note):
            out.append('resolve')
        out.append('archive')
    elif t == 'status':
        if 'due' in kinds:
            out.append('archive')
    else:
        out.append('archive')
        if pair_same and has_successor:
            out.append('supersede')
    return out


def refusal(note, action):
    """The reason code for an action the policy table does not allow."""
    t = _type(note)
    if t in EXCLUDED:
        return 'protected_type'
    if action in ('resolve', 'archive', 'supersede'):
        if note.get('pinned') is True:
            return 'pinned'
        if t in LIMITED:
            return 'protected_type'
    if action == 'resolve':
        from .note_format import ACTION_TYPES
        return 'open_actions' if t in ACTION_TYPES else 'not_permitted'
    if action in ('archive', 'supersede'):
        return 'evidence_incomplete'
    return 'not_permitted'


# ---- source evidence

def _root_present(root, domain, path, bindings):
    """Whether the folder a stored source path lives in is there, so a file absent from it was really removed: every folder bound to the domain
    for a relative path, the registered source's folder for a `name:rel` path. A moved, unmounted or deregistered root leaves the file unavailable."""
    from . import digests
    from .common import domain_prefixes
    named = digests.NAMED.match(path)
    if named:
        spec = digests._specs(root, domain, bindings).get(named[1])
        return bool(spec) and Path(spec['path']).is_dir()
    prefixes = domain_prefixes(domain, bindings)
    return bool(prefixes) and all(p.is_dir() for p in prefixes)


def _file_entries(root, note, domain, bindings, currency):
    """Every file this note's currency depends on: tracked hashes and sources, and the cited files a map or brief reports as changed."""
    from . import digests, maps
    from .note_types import KEYED_TYPES
    found = {}
    for item in (note.get('sources') or []) + (note.get('file_hashes') or []):
        path = item.get('path')
        if not path or path in found:
            continue
        absolute, known = digests.locate(root, domain, path, bindings)
        sha = digests.file_sha256(absolute) if absolute else None
        # missing means deleted (the reviewer may archive a digest for it), so the folder it lived in must still be there.
        state = ('missing' if known and _root_present(root, domain, path, bindings) else 'unavailable') if absolute is None else 'unavailable' if sha is None else 'current' if sha == item.get('sha256') else 'changed'
        found[path] = {'path': path, 'state': state, 'sha256': sha, 'abs': str(absolute) if absolute else None, 'kind': 'tracked'}
    if _type(note) in KEYED_TYPES and currency:
        repo = maps.repo_of(note, domain, bindings)
        for rel in currency.get('changed') or []:
            if rel in found:
                continue
            absolute = (Path(repo) / rel) if repo else None
            sha = digests.file_sha256(absolute) if absolute is not None and absolute.is_file() else None
            found[rel] = {'path': rel, 'state': 'changed' if sha else 'unavailable', 'sha256': sha, 'abs': str(absolute) if sha else None, 'kind': 'cited'}
    return sorted(found.values(), key=lambda f: (STATE_PRIORITY[f['state']], f['path']))


def _fingerprint(files, kinds):
    return digest(canonical_json([[[f['path'], f['state'], f['sha256']] for f in files], sorted(kinds)]).encode())


def _pair_same(note, other, score):
    from .notes import _shingles, NEAR_BODY, NEAR_TITLE
    from .worklist import DUPLICATE_TITLE
    x, y = _shingles(note.get('body', '')), _shingles(other.get('body', ''))
    body = len(x & y) / len(x | y) if x and y else 0.0
    return body >= NEAR_BODY and (score < DUPLICATE_TITLE or score >= NEAR_TITLE)


# ---- candidates

def _level(kinds, note, completed):
    if 'stale' in kinds:
        return 1
    if completed:
        return 2
    if _type(note) == 'status' and 'due' in kinds:
        return 3
    return next((n for n, k in ((4, 'duplicate'), (5, 'due'), (6, 'interim'), (7, 'warned'), (8, 'noisy')) if k in kinds), 10 if 'replaced' in kinds else 9)


def candidates(root, cfg, state, now=None, limit=10):
    """The next batch, in priority order (spec Phase 4), round-robin over the enabled domains from `domain_cursor`. Read-only: no model,
    no usage event, no reservation. Each candidate carries its note as it is now."""
    from .common import load_bindings
    from .repository import inventory
    from .worklist import worklist, currency_of
    from .note_types import verified_at
    root = Path(root)
    now = ms._aware(now)
    domains = list(cfg['domains'])
    notes = {}
    for d in domains:
        try:
            for n in inventory(d, root):
                notes[f"{d}:{n['id']}"] = dict(n, domain=d)
        except (OSError, ValueError):
            continue
    rows = worklist(root, domains, today=now.astimezone().date(), working_tree=True, limit=10 ** 6, notes=list(notes.values()))['items']
    bindings = load_bindings(root)
    seen = {r['ref'] for r in rows}
    for ref, note in notes.items():
        if ref not in seen and _completed(note):
            try:
                currency = currency_of(note, root, bindings)
            except Exception:
                currency = None
            rows.append({'ref': ref, 'kinds': [], 'reason': 'every action item is checked', **({'currency': currency} if currency else {})})
    queues, queued = {}, set()
    for position, row in enumerate(rows):
        note = notes.get(row['ref'])
        if note is None or _excluded(note):
            continue
        completed = _completed(note)
        files = _file_entries(root, note, note['domain'], bindings, row.get('currency'))
        fingerprint = _fingerprint(files, row['kinds'])
        if ms.retry_hold(state, row['ref'], note['revision'], now):
            continue
        pair, same = None, False
        if row.get('pair') and row['pair']['ref'] in notes and notes[row['pair']['ref']]['domain'] == note['domain']:
            pair = notes[row['pair']['ref']]
            same = _pair_same(note, pair, row['pair']['score'])
        cand = {'ref': row['ref'], 'domain': note['domain'], 'title': note.get('title'), 'type': _type(note), 'revision': note['revision'],
                'level': _level(row['kinds'], note, completed), 'kinds': list(row['kinds']), 'reason': row['reason'], 'pinned': note.get('pinned') is True,
                'currency': row.get('currency'), 'pair': pair['id'] if pair else None, 'pair_note': pair, 'pair_same': same, 'fingerprint': fingerprint, 'files': files, 'note': note,
                'permitted': permitted(note, row['kinds'], same, pair is not None)}
        queues.setdefault(note['domain'], []).append((cand['level'], verified_at(note), position, row['ref'], cand))
        queued.add(row['ref'])
    # A row held by retry_hold is not queued, so an older note of a queued pair still gets its replaced review.
    for position, cand in enumerate(replaced_candidates(root, state, notes, now, queued, set(domains)), len(rows)):
        queues.setdefault(cand['domain'], []).append((cand['level'], verified_at(cand['note']), position, cand['ref'], cand))
    for queue in queues.values():
        queue.sort(key=lambda q: q[:4])
    start = domains.index(state.get('domain_cursor')) if state.get('domain_cursor') in domains else 0
    order = domains[start:] + domains[:start]
    chosen = []
    while len(chosen) < limit and any(queues.get(d) for d in order):
        for d in order:
            if queues.get(d) and len(chosen) < limit:
                chosen.append(queues[d].pop(0)[4])
    return chosen


def queued_pairs(root, notes, domains):
    """([(pair, older note, newer note)] still worth a review, [(new, old)] that are not). A pair is valid while both notes are active, in one
    domain, and neither is marked distinct from the other. Pairs of a domain outside `domains` are neither (maintenance does not cover it)."""
    from .notes import distinct
    valid, invalid = [], []
    for p in ms.queue(root)['pairs']:
        new_domain, old_domain = p['new'].partition(':')[0], p['old'].partition(':')[0]
        if new_domain not in domains and old_domain not in domains:
            continue
        new, old = notes.get(p['new']), notes.get(p['old'])
        if (new_domain != old_domain or new is None or old is None or new.get('status', 'active') != 'active' or old.get('status', 'active') != 'active'
                or _excluded(new) or _excluded(old) or distinct(new, old)):
            invalid.append((p['new'], p['old']))
        else:
            valid.append((p, old, new))
    return valid, invalid


def replaced_hold(ref, successor):
    """The retry key of a note's replaced review by one successor. It is separate from the note's own key and from its other pairs', so
    deferring one review never lifts, or overwrites, another's hold. It starts with `#`, which no domain name can, so it is never a note ref."""
    return f'#replaced:{ref}>{successor}'


def legacy_replaced_hold(ref):
    """The key replaced reviews were held under before they were held per pair; honored while it matches, so an upgrade does not lift it."""
    return f'{ref}#replaced'


def replaced_candidates(root, state, notes, now, taken, domains):
    """Candidates of kind `replaced`: the older note of each queued pair, with the newer one as its successor. One per older note, and none for a
    note another row already reviews in this batch. They rank after every other kind."""
    out, used = [], set(taken)
    for p, old, new in queued_pairs(root, notes, domains)[0]:
        if p['old'] in used:
            continue
        fingerprint = digest(canonical_json(['replaced', p['new'], new['revision']]).encode())
        if (ms.retry_hold(state, replaced_hold(p['old'], p['new']), old['revision'], now, fingerprint)
                or ms.retry_hold(state, legacy_replaced_hold(p['old']), old['revision'], now, fingerprint)):
            continue
        used.add(p['old'])
        out.append({'ref': p['old'], 'domain': old['domain'], 'title': old.get('title'), 'type': _type(old), 'revision': old['revision'], 'level': 10,
                    'kinds': ['replaced'], 'reason': f"queued as possibly replaced by {p['new']} \"{new.get('title')}\"", 'pinned': old.get('pinned') is True,
                    'currency': None, 'pair': new['id'], 'pair_note': new, 'pair_same': True, 'fingerprint': fingerprint, 'files': [], 'note': old,
                    'hold': replaced_hold(p['old'], p['new']), 'permitted': permitted(old, ['replaced'], True, True)})
    return out


def _active_notes(root, domains):
    from .repository import inventory
    notes = {}
    for d in domains:
        try:
            for n in inventory(d, root):
                notes[f"{d}:{n['id']}"] = dict(n, domain=d)
        except (OSError, ValueError):
            continue
    return notes


def prune_queue(root, cfg):
    """Drop the queued pairs that are no longer valid (a retired, missing or distinct note). Not part of the read-only preview."""
    ms.queue_drop(root, queued_pairs(root, _active_notes(root, cfg['domains']), set(cfg['domains']))[1])


BACKFILL_SECONDS = 45


def backfill_related(root, cfg, now=None, seconds=BACKFILL_SECONDS):
    """Queue the pairs capture would have queued, for the notes already in the store: the same check and bar, run once. It scans the active decision,
    policy and preference notes of the enabled domains for at most `seconds` and saves its place, so a large store finishes over several runs.
    The later-created note of a pair is `new`. Returns the number of pairs added, or None when the backfill was already done."""
    from .notes import scan_related, related_scan_ids, owed_pair
    from .repository import inventory
    progress = ms.queue(root)['backfill']
    if progress['done']:
        return None
    plan, loaded = [], {}
    for d in cfg['domains']:
        try:
            loaded[d] = inventory(d, root)
        except (OSError, ValueError):
            continue
        plan += [(d, i) for i in related_scan_ids(d, root, loaded[d])]
    stop, position, pairs = time.monotonic() + seconds, progress['next'], []
    todo = plan[position:]
    for d in dict.fromkeys(d for d, _ in todo):
        by_id = {n['id']: n for n in loaded[d]}
        for note, hits in scan_related(d, root, None, loaded[d], {i for dd, i in todo if dd == d}):
            top = owed_pair(note, hits, d, root)
            if top:
                other = by_id.get(top['id'], note)
                new, old = (note, other) if str(note.get('created') or '') >= str(other.get('created') or '') else (other, note)
                pairs.append({'new': f"{d}:{new['id']}", 'old': f"{d}:{old['id']}"})
            position += 1
            if time.monotonic() >= stop:
                break
        if time.monotonic() >= stop:
            break
    added = ms.queue_add(root, pairs, now) if pairs else 0
    ms.queue_backfill(root, position >= len(plan), position, now)
    return added


def next_cursor(cfg, selected):
    """The domain after the last one a batch drew from, where the next run starts."""
    domains = cfg['domains']
    if not selected or selected[-1]['domain'] not in domains:
        return None
    return domains[(domains.index(selected[-1]['domain']) + 1) % len(domains)]


# ---- packet

def _scrub(value):
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _window(text, body):
    """(excerpt, first line, last line), about EXCERPT_CHARS around the line sharing the most identifiers with the note, else the file head."""
    lines = text.splitlines() or ['']
    words = {w.lower() for w in re.findall(r'[A-Za-z_][A-Za-z0-9_]{3,}', body)}
    scores = [len(words & {w.lower() for w in re.findall(r'[A-Za-z_][A-Za-z0-9_]{3,}', line)}) for line in lines]
    best = max(range(len(lines)), key=lambda i: (scores[i], -i)) if max(scores) else 0
    lo = hi = best
    size = len(lines[best][:EXCERPT_CHARS])
    while size < EXCERPT_CHARS:
        grew = False
        for step in (1, -1):
            nxt = hi + 1 if step == 1 else lo - 1
            if 0 <= nxt < len(lines) and size + len(lines[nxt]) + 1 <= EXCERPT_CHARS:
                size += len(lines[nxt]) + 1
                hi, lo = (nxt, lo) if step == 1 else (hi, nxt)
                grew = True
        if not grew:
            break
    return '\n'.join(lines[lo:hi + 1])[:EXCERPT_CHARS], lo + 1, hi + 1


OUTLINE_LINE = re.compile(r'\s*(?:(?:async\s+)?def\s|class\s|function\s|#{1,6}\s)')


def _outline(text):
    """Lines that start a definition or a Markdown heading, as 'N: line', at most OUTLINE_CHARS."""
    out, size = [], 0
    for n, line in enumerate(text.splitlines(), 1):
        if OUTLINE_LINE.match(line):
            item = f'{n}: {line.strip()[:120]}'
            if size + len(item) + 1 > OUTLINE_CHARS:
                break
            out.append(item)
            size += len(item) + 1
    return '\n'.join(out)


def _read_text(path):
    try:
        with open(path, 'rb') as f:
            raw = f.read(FILE_READ_MAX)
    except OSError:
        return None
    return None if b'\0' in raw else raw.decode('utf-8', 'replace')


def packet(root, cands, run_id):
    """(packet text, manifest). The text is JSON of at most PACKET_MAX bytes, all of it redacted. The manifest is private: it maps each
    evidence id (E1, E2, ...) to the real note revision or file hash, and records what each candidate may do."""
    from .note_format import action_items
    manifest = {'version': 1, 'run_id': run_id, 'evidence': {}, 'candidates': {}, 'deferred': {}, 'dropped': [], 'keys': {}}
    empty = 2 + RUN_ID_ROOM + 40   # fixed room for the id, so a trial packet drops the same notes as the real one
    blocks, size, counter = [], empty, 0

    def new_id():
        nonlocal counter
        counter += 1
        return f'E{counter}'
    for cand in cands:
        note, ref = cand['note'], cand['ref']
        body = note.get('body', '')
        if len(body.encode('utf-8')) > BODY_MAX:
            manifest['deferred'][ref] = {'reason_code': 'note_too_large', 'revision': cand['revision'], 'fingerprint': cand['fingerprint'], 'hold': cand.get('hold', ref)}
            continue
        mark = counter
        for whole_ok in (True, False):
            counter = mark
            evidence, entries = {}, []
            own = new_id()
            evidence[own] = {'candidate': ref, 'kind': 'note', 'ref': ref, 'revision': cand['revision'], 'role': 'candidate'}
            entries.append({'id': own, 'kind': 'note', 'role': 'candidate'})
            successors, successor_refs = [], {}
            pair = cand.get('pair_note')
            if pair is not None and len(pair.get('body', '').encode('utf-8')) <= BODY_MAX and pair.get('status', 'active') == 'active':
                pref = f"{cand['domain']}:{pair['id']}"
                sid = new_id()
                evidence[sid] = {'candidate': ref, 'kind': 'note', 'ref': pref, 'revision': pair['revision'], 'role': 'successor'}
                successor_refs[pref] = pair['revision']
                successors.append({'evidence_id': sid, 'ref': pref, 'revision': pair['revision'], 'title': pair.get('title'), 'type': _type(pair), 'created': pair.get('created'),
                                   'updated': pair.get('updated'), 'body': pair.get('body', '')})
            for f in cand['files'][:EXCERPTS]:
                fid = new_id()
                record = {'candidate': ref, 'kind': 'file', 'path': f['path'], 'state': f['state'], 'sha256': f['sha256'], 'abs': f['abs'], 'lines': None}
                entry = {'id': fid, 'kind': 'file', 'path': f['path'], 'state': f['state']}
                text = _read_text(f['abs']) if f['abs'] and f['state'] in ('current', 'changed') else None
                if text is not None:
                    if whole_ok and len(text) <= FILE_EXCERPT_CHARS:
                        entry['excerpt'], first, last = text, 1, max(1, len(text.splitlines()))
                        entry['whole'] = True
                    else:
                        entry['excerpt'], first, last = _window(text, body)
                        entry['whole'] = False
                        entry['outline'] = _outline(text)
                    record['lines'] = entry['lines'] = [first, last]
                evidence[fid] = record
                entries.append(entry)
            permits = permitted(note, cand['kinds'], cand['pair_same'], bool(successors))
            key = f"C{len(manifest['keys']) + 1}"
            block = {'key': key, 'title': note.get('title'), 'type': cand['type'], 'status': note.get('status', 'active'),
                     'pinned': cand['pinned'], 'tags': note.get('tags') or [], 'created': note.get('created'), 'updated': note.get('updated'),
                     'checked': note.get('checked'), 'confirmed': note.get('confirmed') or [], 'review_by': note.get('review_by'), 'files': note.get('files') or [],
                     'body': body, 'action_items': [{'done': d, 'text': t} for d, t in action_items(body)], 'review_kinds': cand['kinds'],
                     'digest': bool(note.get('sources')), 'review_reason': cand['reason'], 'currency': cand['currency'], 'permitted_actions': permits, 'evidence': entries, 'successors': successors}
            text = json.dumps(_scrub(block), ensure_ascii=False, separators=(',', ':'))
            need = len(text.encode('utf-8')) + 1
            # With whole files the block either fits now, or would fit a later packet (dropped, retried next run). Only a block too large for
            # any packet is rebuilt with outlines and windows, so it is not dropped on every run.
            if size + need <= PACKET_MAX or (whole_ok and empty + need <= PACKET_MAX):
                break
        if size + need > PACKET_MAX:
            counter = mark
            manifest['dropped'].append(ref)
            continue
        size += len(text.encode('utf-8')) + 1
        blocks.append(text)
        manifest['keys'][key] = ref
        manifest['evidence'].update(evidence)
        manifest['candidates'][ref] = {'key': key, 'revision': cand['revision'], 'domain': cand['domain'], 'kinds': cand['kinds'], 'pair_same': cand['pair_same'], 'permitted': permits,
                                       'evidence_ids': list(evidence), 'successors': successor_refs, 'fingerprint': cand['fingerprint'], 'hold': cand.get('hold', ref),
                                       'pair': next(iter(successor_refs), None) if 'replaced' in cand['kinds'] else None,
                                       'files': {f['path']: {'state': f['state'], 'sha256': f['sha256'], 'kind': f['kind']} for f in cand['files']},
                                       'currency_state': (cand['currency'] or {}).get('state'), 'type': cand['type']}
    text = json.dumps({'run_id': run_id}, separators=(',', ':'))[:-1] + ',"candidates":[' + ','.join(blocks) + ']}' if blocks else ''
    return text, manifest


# ---- executor

def _decision_record(root, run, ref, action, result, code, rule=None, reason=None):
    changes, expected, op, _ = ms.decision_changes(root, run, ref, {'action': action, 'result': result, 'reason_code': code, 'rule': rule, 'reason': reason})
    transaction(root, changes, 'maintenance_decision', expected=expected, operation_id=op)


def _close(root, run, ref, action, result, code, info, now, hold=False, rule=None, reason=None):
    """Record a decision that changes no note, with a retry hold when asked (the same revision is not reviewed again for 7, then 14, then 30 days)."""
    out = {'ref': ref, 'action': action, 'result': result, 'reason_code': code}
    try:
        _decision_record(root, run, ref, action, result, code, rule, reason)
        if hold and info:
            ms.set_retry(root, info.get('hold', ref), info['revision'], info['fingerprint'], code or result, now, strict=info.get('hold', ref) != ref)
    except (OSError, ValueError, TimeoutError, ms.Unavailable):
        out['record_failed'] = True
    return out


def _patched(body, patches):
    spans = []
    for p in patches:
        i = body.find(p['find'])
        if not p['find'] or i < 0 or body.find(p['find'], i + 1) >= 0:
            raise ValueError('patch find text must occur exactly once')
        if not p['replace'].strip() or len(p['replace']) > agent.REPLACE_MAX or find_secret(p['replace']):
            raise ValueError('patch replacement is not acceptable')
        spans.append((i, i + len(p['find']), p['replace']))
    spans.sort()
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        raise ValueError('patches overlap')
    out, at = [], 0
    for start, end, text in spans:
        out += [body[at:start], text]
        at = end
    return ''.join(out) + body[at:]


def _protected_view(note):
    from .note_format import action_items, sections, named_sections
    body = note.get('body', '')
    found = sections(body)
    exclusions = next((t for h, t in named_sections(body) if h.casefold() == 'when not to use'), None)
    paths = sorted(s['path'] for s in note.get('sources') or [])
    return (action_items(body), found.get('important'), found.get('links'), exclusions, note.get('title'), note.get('type'), sorted(note.get('tags') or []),
            note.get('files'), note.get('commands'), note.get('pinned'), paths, note.get('status'))


def _recheck(root, domain, ev, bindings):
    """None when the evidence is still what the packet showed, else a reason code."""
    from . import digests, notes
    if ev['kind'] == 'note':
        rdomain, _, rid = ev['ref'].partition(':')
        try:
            _, current = notes.find_note(rid, rdomain, root)
        except ValueError:
            return 'stale_revision'
        return None if current.get('status', 'active') == 'active' and current['revision'] == ev['revision'] else 'stale_revision'
    if ev['abs']:
        return None if digests.file_sha256(ev['abs']) == ev['sha256'] else 'source_changed'
    absolute, _ = digests.locate(root, domain, ev['path'], bindings)
    if absolute is not None:
        return 'source_changed'
    return None if _root_present(root, domain, ev['path'], bindings) else 'source_unavailable'


def _skip_code(exc):
    text = str(exc)
    if text.lower().startswith('stale'):
        return 'skipped', 'stale_revision'
    if text.startswith('too_many_link_updates'):
        return 'deferred', 'too_many_link_updates'
    return 'rejected', 'apply_failed'


def _decide(root, run, ref, dec, manifest, now, deadline, clock):
    from . import notes
    from .common import load_bindings
    cand, action = manifest['candidates'][ref], dec['action']

    def end(result, code=None, hold=False):
        return _close(root, run, ref, action, result, code, cand, now, hold, dec.get('rule'), redact(dec.get('reason') or '')[0])
    if deadline is not None and clock() >= deadline:
        return end('skipped', 'timeout')
    domain, _, note_id = ref.partition(':')
    with store_lock(root):
        try:
            current = ms.config(root)
        except ValueError:
            return end('skipped', 'config_changed')
        if not current['enabled']:
            return end('skipped', 'disabled')
        if ms.config_revision(current) != run['config_revision'] or domain not in current['domains']:
            return end('skipped', 'config_changed')
        try:
            _, note = notes.find_note(note_id, domain, root)
        except ValueError:
            return end('skipped', 'stale_revision')
        if note.get('status', 'active') != 'active' or note['revision'] != cand['revision']:
            return end('skipped', 'stale_revision')
        replaced = 'replaced' in cand['kinds']
        if _excluded(note):
            return end('rejected', 'protected_type', True)
        if action not in permitted(note, cand['kinds'], cand['pair_same'], bool(cand['successors'])):
            return end('rejected', refusal(note, action), True)
        if action == 'keep':
            return end('kept', None, True)
        if action == 'defer':
            code = dec.get('code') or 'model_defer'
            return end('deferred', code, code == 'model_defer')
        ids = dec['evidence_ids']
        if any(i not in cand['evidence_ids'] for i in ids) or not ids and action != 'resolve':
            return end('rejected', 'evidence_incomplete', True)
        evidence = [manifest['evidence'][i] for i in ids]
        bindings = load_bindings(root)
        for ev in evidence:
            code = _recheck(root, domain, ev, bindings)
            if code:
                return end('skipped' if code == 'stale_revision' else 'deferred', code, code != 'stale_revision')
        tracked = cand['files']
        covered = {ev['path'] for ev in evidence if ev['kind'] == 'file'}
        full = set(tracked) <= covered and all(f['state'] in ('current', 'changed') for f in tracked.values())
        t = cand['type']
        rule, values, retire = dec['rule'], {}, action in ('resolve', 'archive', 'supersede')
        if action == 'resolve':
            rule = rule or 'completed_work'
            ok = rule == 'completed_work'
        elif action == 'archive':
            from .note_format import ACTION_TYPES
            ok = rule in ARCHIVE_RULES['status' if t == 'status' else 'action' if t in ACTION_TYPES else 'other']
        elif action == 'supersede':
            rule = rule or 'replaced_by_successor'
            successor = dec['successor']
            ok = rule in ('replaced_by_successor', 'duplicate') and successor in cand['successors'] and successor.partition(':')[0] == domain and successor != ref
            # The successor is checked whether or not the model cited it: it must still be active and unchanged since the packet.
            if ok and _recheck(root, domain, {'kind': 'note', 'ref': successor, 'revision': cand['successors'][successor]}, bindings):
                return end('skipped', 'stale_revision')
            values = {'superseded_by': successor.partition(':')[2]} if ok else {}
        elif action == 'distinct':
            successor = next(iter(cand['successors']), None) if len(cand['successors']) == 1 else None
            why = ' '.join(redact(dec['reason'])[0].split())[:200]
            ok = replaced and successor is not None and dec['successor'] in (None, successor) and len(why) >= 8
            if ok and _recheck(root, domain, {'kind': 'note', 'ref': successor, 'revision': cand['successors'][successor]}, bindings):
                return end('skipped', 'stale_revision')
        elif action == 'confirm':
            ok = full and not dec['patches']
            if ok and t in ('map', 'brief'):
                # Currency is computed again now (git and working tree): every changed cited file must be one the evidence covered.
                from .worklist import currency_of
                try:
                    fresh = currency_of(dict(note, domain=domain), root, bindings, working_tree=True)
                except Exception:
                    fresh = None
                ok = bool(fresh) and fresh.get('state') in ('current', 'changed') and set(fresh.get('changed') or []) <= covered
                if not ok:
                    return end('deferred', 'source_changed', True)
        else:
            ok = bool(dec['patches'])
        if action != 'update' and dec['patches']:
            ok = False
        if not ok:
            return end('rejected', 'evidence_incomplete' if action == 'confirm' else 'invalid_report', True)
        try:
            if action == 'update':
                try:
                    values = {'body': _patched(note['body'], dec['patches'])}
                except ValueError:
                    return end('rejected', 'invalid_report', True)
            rchanges, rexpected, op, _ = ms.decision_changes(root, run, ref, {'action': action, 'result': 'applied', 'reason_code': None, 'rule': rule,
                                                                         'reason': redact(dec['reason'])[0][:agent.REASON_MAX]})
            marks = []
            for ev in evidence:
                if ev['kind'] == 'note':
                    marks.append({'kind': 'note', 'ref': ev['ref'], 'revision': ev['revision']})
                elif ev['sha256'] and len(ev['path']) <= 300:
                    marks.append({'kind': 'file', 'path': ev['path'], 'sha256': ev['sha256']})
            retirement = {'rule': rule, 'reason': redact(dec['reason'])[0][:agent.REASON_MAX], 'evidence': marks[:agent.EVIDENCE_MAX], 'run_id': run['id'],
                          'operation_id': op} if retire else None
            if action == 'distinct':
                # The record lives on the newer note, so the older one is left exactly as it is.
                newer_id = successor.partition(':')[2]
                newer = notes.find_note(newer_id, domain, root)[1]
                changes, expected, _ = notes._change_set(newer_id, domain, 'update', root, cand['successors'][successor], actor='maintenance',
                                                         distinct_from=notes.distinct_with(newer, domain, note_id, why, 'maintenance'))
            else:
                changes, expected, _ = notes._change_set(note_id, domain, action, root, cand['revision'], rehash=bool(tracked) and full, actor='maintenance',
                                                         retirement=retirement, **values)
            if action == 'update':
                rel = Path(note['path']).relative_to(root).as_posix()
                after = notes.parse_changed(changes[rel], note)
                if _protected_view(after) != _protected_view(note):
                    return end('deferred', 'protected_change', True)
            changes.update(rchanges)
            expected.update(rexpected)
            receipt = notes._commit_many([domain], changes, action, root, expected, operation_id=op)
        except ValueError as exc:
            result, code = _skip_code(exc)
            return end(result, code, result != 'skipped')
        out = {'ref': ref, 'action': action, 'result': 'applied', 'reason_code': None, 'operation_id': op}
        if receipt.get('warning'):
            out['warning'] = receipt['warning']
        return out


def _counts(results):
    counts = {k: 0 for k in COUNT_KEYS}
    for r in results:
        if r['result'] == 'applied':
            counts[APPLIED_KEY[r['action']]] += 1
        elif r['result'] == 'kept':
            counts['kept'] += 1
        elif r['result'] in ('deferred', 'rejected'):
            counts['deferred'] += 1
        elif r['result'] == 'skipped':
            counts['skipped'] += 1
    return counts


def _settle_pairs(root, manifest, results):
    """A queued pair the reviewer decided (superseded, recorded distinct, or kept as it is) leaves the queue; a deferred, rejected or skipped one stays."""
    gone = []
    for r in results:
        info = manifest['candidates'].get(r['ref'])
        if info and info.get('pair') and (r['result'] == 'kept' or r['result'] in ('applied', 'already_applied') and r['action'] in ('supersede', 'distinct')):
            gone.append((info['pair'], r['ref']))
    try:
        ms.queue_drop(root, gone)
    except (OSError, ValueError, TimeoutError):
        pass   # the next run's prune drops it


def apply(root, run, report, manifest, cfg=None, now=None, deadline=None):
    """Execute a report. Returns {ok, results, counts, warnings}; a structurally invalid report changes nothing. Each decision is atomic on its
    own; one already recorded for this run is never applied twice."""
    root = Path(root).resolve()
    clock = _clock(now)
    try:
        report = agent.validate_report(report, run['id'], manifest)
    except agent.ReportError as exc:
        return {'ok': False, 'reason_code': 'invalid_report', 'detail': str(exc), 'results': [], 'counts': _counts([]), 'warnings': []}
    results = []
    decided = ms._current(root, run)['decisions']
    for ref, info in manifest['deferred'].items():
        if ref not in decided:
            results.append(_close(root, run, ref, 'defer', 'deferred', info['reason_code'], info, now, True))
    for dec in report['decisions']:
        if dec['ref'] in decided:
            results.append({'ref': dec['ref'], 'action': dec['action'], 'result': 'already_applied', 'reason_code': None})
            continue
        try:
            results.append(_decide(root, run, dec['ref'], dec, manifest, now, deadline, clock))
        except (OSError, TimeoutError, ms.Unavailable) as exc:
            results.append({'ref': dec['ref'], 'action': dec['action'], 'result': 'skipped', 'reason_code': 'apply_failed', 'error': str(exc)[:200]})
    _settle_pairs(root, manifest, results)
    return {'ok': True, 'results': results, 'counts': _counts([r for r in results if r['result'] != 'already_applied']),
            'warnings': [f"{r['ref']}: {r['warning']}" for r in results if r.get('warning')]}


# ---- service

def _skipped(reason, when=None):
    return {'ok': True, 'status': 'skipped', 'skip_reason': reason, 'next_eligible_at': when}


def run(root, dry_run=False, adapter=None, now=None, allow_disabled=False, domains=None):
    """One synchronous bounded review: worker lock, recovery, eligibility, candidates, reservation, packet, one provider call, then the
    decisions (not for a dry run). Returns counts shaped like the spec's `last_run`; a routine skip is a successful no-op.
    A manual dry run may pass `allow_disabled` (with `domains` when none are configured): it runs as if enabled, every limit still applies,
    and the config file is not touched."""
    root = Path(root).resolve()
    clock = _clock(now)
    start = clock()
    try:
        with ms.worker_lock(root):
            ms.recover_runs(root, start)
            try:
                cfg = ms.config(root)
            except ValueError:
                return _skipped('disabled')
            override = allow_disabled and dry_run and not cfg['enabled']
            if override:
                if not (cfg['domains'] or domains):
                    return _skipped('disabled')
                cfg = ms.validate(dict(cfg, enabled=True, domains=cfg['domains'] or list(domains)), root, allow_global=True)
            verdict = ms.eligibility(root, start, cfg)
            if not verdict['eligible']:
                return _skipped(verdict['skip_reason'], verdict['next_eligible_at'])
            if not dry_run:   # free, and it may clear notes this run would otherwise pay to review
                from . import rebaseline
                rebaseline.before_review(root, cfg['domains'])
            try:
                backfill_related(root, cfg, start)
                prune_queue(root, cfg)
            except Exception as exc:   # advice for the reviewer; a failure here never stops the run
                from .logs import swallowed
                swallowed('maintenance.queue', exc, root)
            st = ms.state(root, start)
            remaining = cfg['max_notes_per_day'] - st['reviewed'] - st['reserved_notes']
            selected = candidates(root, cfg, st, start, min(remaining, agent.BATCH_MAX))
            # Notes that would not fit in the packet are left out before the reservation, so they cost no allowance.
            dropped = set(packet(root, selected, '')[1]['dropped'])
            selected = [c for c in selected if c['ref'] not in dropped]
            if not selected:
                return _skipped('no_candidates')
            record = ms.reserve(root, cfg, len(selected), start, selected=[{'ref': c['ref'], 'revision': c['revision']} for c in selected], allow_disabled=override)
            selected = selected[:record['reserved_notes']]
            cursor = next_cursor(cfg, selected)
            if cursor:
                ms.set_cursor(root, cursor, start)
            text, manifest = packet(root, selected, record['id'])
            if not manifest['candidates']:
                for ref, info in manifest['deferred'].items():
                    ms.set_retry(root, info.get('hold', ref), info['revision'], info['fingerprint'], info['reason_code'], clock(), strict=info.get('hold', ref) != ref)
                ms.release(root, record, 'no_candidates', clock())
                return _skipped('no_candidates')
            adapter = adapter or agent.review
            check = getattr(adapter, 'check', None)
            verdict = check(cfg) if check else {'ok': True}
            if not verdict['ok']:
                ms.release(root, record, verdict['reason_code'], clock())
                return {'ok': True, 'status': 'failed', 'reason_code': verdict['reason_code'], 'id': record['id'], 'reviewed': 0}
            record = ms.launch(root, record, clock())
            deadline = start + timedelta(seconds=cfg['timeout_seconds'])
            budget = (deadline - clock()).total_seconds()
            timeout = budget - min(30.0, 0.1 * cfg['timeout_seconds'])
            if timeout < 1:
                out = {'ok': False, 'reason_code': 'timeout', 'usage_micro': None}
            else:
                try:
                    out = adapter({'cfg': cfg, 'packet': text, 'manifest': manifest, 'run': record, 'timeout': timeout})
                except Exception as exc:
                    out = {'ok': False, 'reason_code': 'provider_error', 'usage_micro': None, 'detail': type(exc).__name__}
            usage = out.get('usage_micro')
            report = None
            if out.get('ok'):
                try:
                    report = agent.validate_report(out.get('report'), record['id'], manifest)
                except agent.ReportError as exc:
                    out = {'ok': False, 'reason_code': 'invalid_report', 'usage_micro': usage, 'detail': str(exc)}
            reviewed = len(manifest['candidates']) + len(manifest['deferred'])
            base = {'ok': True, 'id': record['id'], 'reviewed': reviewed, 'dropped': len(manifest['dropped']), 'usage_micro': usage}
            if not out.get('ok'):
                ms.finish(root, record, usage, 'failed', clock(), out['reason_code'])
                return {**base, 'status': 'failed', 'reason_code': out['reason_code'], 'detail': out.get('detail'), **{k: 0 for k in COUNT_KEYS}}
            if dry_run:
                ms.finish(root, record, usage, 'dry_run', clock())
                proposed = {}
                for d in report['decisions']:
                    proposed[d['action']] = proposed.get(d['action'], 0) + 1
                # The decisions come back (not stored) so the dry-run gate can inspect every recommendation.
                return {**base, 'status': 'dry_run', 'reason_code': None, 'proposed': proposed, 'decisions': report['decisions'], **{k: 0 for k in COUNT_KEYS}}
            record = ms.applying(root, record, clock())
            try:
                done = apply(root, record, report, manifest, cfg, now, deadline)
            except Exception as exc:
                ms.finish(root, record, usage, 'failed', clock(), 'apply_failed')
                return {**base, 'status': 'failed', 'reason_code': 'apply_failed', 'detail': type(exc).__name__, **{k: 0 for k in COUNT_KEYS}}
            ms.finish(root, record, usage, 'completed', clock())
            return {**base, 'status': 'completed', 'reason_code': None, **done['counts'], 'results': done['results'], 'warnings': done['warnings']}
    except ms.MaintenanceSkip as exc:
        return _skipped(exc.reason)


# ---- commands: status, preview, enable, disable, history

def _recent(root, limit=None):
    """Run records, newest first, at most `limit` (all when None). Day folders are read newest first, so a small limit stays cheap."""
    base = Path(root) / ms.RUNS
    out = []
    try:
        folders = sorted((f for f in base.iterdir() if f.is_dir()), reverse=True) if base.exists() else []
        for folder in folders:
            try:
                datetime.strptime(folder.name, '%Y-%m-%d')
            except ValueError:
                continue
            out += sorted((ms._read_run(p, folder.name) for p in folder.glob('*.json')), key=lambda r: ms._parse(r['attempted_at']), reverse=True)
            if limit and len(out) >= limit:
                break
    except OSError as exc:
        raise ms.Unavailable('damaged runs directory') from exc
    return out[:limit] if limit else out


def _summary(run):
    out = {'id': run['id'], 'status': run['state'], 'day': run['day'], 'attempted_at': run['attempted_at'], 'finished_at': run.get('finished_at'),
           'reason_code': run.get('reason_code'), 'model': run.get('model'), 'reviewed': 0 if run.get('released') else len(run['selected']),
           'charged_usd': ms.format_usd(run['charged_micro']), 'cost_complete': run.get('cost_complete', True)}
    return {**out, **_counts(list(run['decisions'].values()))}


def status(root, now=None):
    """The spec's status shape. Reads configuration, checkpoint and run records; no model call and no candidate scan."""
    now, errors = ms._aware(now), []
    try:
        cfg = ms.config(root)
    except ValueError as exc:
        cfg = ms.validate(ms.DEFAULTS)
        errors.append(f'config_invalid: {exc}')
    verdict = ms.eligibility(root, now, cfg)
    acc, last = None, None
    try:
        acc = ms.accounting(root, now)[0]
        last = next(iter(_recent(root, 1)), None)
    except ms.Unavailable as exc:
        errors.append(f'accounting_unavailable: {exc}')
    return {'ok': True, 'enabled': cfg['enabled'], 'domains': cfg['domains'], 'policy_version': cfg['policy_version'], 'config_revision': ms.config_revision(cfg),
            'limits': {k: cfg[k] for k in ('model', 'max_notes_per_day', 'timeout_seconds', 'daily_budget_usd', 'min_interval_seconds')}, 'enabled_at': cfg['enabled_at'],
            'accounting': {'day': acc['day'], 'reviewed': acc['reviewed'], 'reserved_notes': acc['reserved_notes'], 'reserved_usd': ms.format_usd(acc['reserved_micro']),
                           'charged_usd': ms.format_usd(acc['charged_micro']), 'cost_complete': acc['cost_complete']} if acc else None,
            'blocked': ms.active_block(acc, cfg) if acc else None, 'running': ms.running(root), 'eligible': verdict['eligible'], 'next_eligible_at': verdict['next_eligible_at'],
            'skip_reason': verdict['skip_reason'], 'last_run': _summary(last) if last else None, 'errors': errors}


def preview(root, limit=10, now=None, domains=None):
    """The next batch as it would be chosen, with the policy's action limits and a trial packet. No reservation, usage event or model call.
    Uses the configured domains, or `domains` when none are configured (so a store can be inspected before it is enabled)."""
    now = ms._aware(now)
    cfg = ms.config(root)
    cfg = dict(cfg, domains=cfg['domains'] or list(domains or []))
    if not cfg['domains']:
        raise ValueError('No maintenance domains: enable one, or name --domain D')
    try:
        st = ms.state(root, now)
    except ms.Unavailable as exc:
        raise ValueError(f'accounting_unavailable: {exc}') from exc
    selected = candidates(root, cfg, st, now, max(1, min(int(limit), 50)))
    text, manifest = packet(root, selected, '')
    rows = []
    for c in selected:
        ref = c['ref']
        rows.append({'ref': ref, 'title': c['title'], 'type': c['type'], 'level': c['level'], 'kinds': c['kinds'], 'permitted': c['permitted'],
                     'protected': None if 'archive' in c['permitted'] else refusal(c['note'], 'archive'), 'bytes': len(c['note'].get('body', '').encode('utf-8')),
                     'in_packet': ref in manifest['candidates']})
    return {'ok': True, 'domains': cfg['domains'], 'candidates': rows, 'too_large': sorted(manifest['deferred']), 'dropped': manifest['dropped'],
            'estimated_packet_bytes': len(text.encode('utf-8'))}


def _config_write(root, new, revision):
    from .common import global_domains
    return ms.set_config(root, new, revision, allow_global=bool(set(new['domains']) & set(global_domains(root))))


def enable(root, domains, allow_global=False, settings=None, now=None):
    """Authorize maintenance: the named domains join the configured ones, `settings` replace the matching settings, accounting is untouched."""
    from .common import global_domains
    cfg = ms.config(root)
    named = [d for d in domains if d in set(global_domains(root))]
    if named and not allow_global:
        raise ValueError(f'{named[0]} is a global domain; name it explicitly with --domain to include it')
    new = {**cfg, **(settings or {}), 'domains': list(dict.fromkeys([*cfg['domains'], *domains])), 'enabled': True}
    if not new['domains']:
        raise ValueError('Name a domain with --domain D, or use --all-domains')
    if not cfg['enabled'] or cfg['enabled_at'] is None:
        new['enabled_at'] = ms._iso(ms._aware(now))
    if new != cfg:
        _config_write(root, new, ms.config_revision(cfg))
    return {**status(root, now), 'changed': new != cfg}


def disable(root, now=None):
    cfg = ms.config(root)
    new = {**cfg, 'enabled': False}
    if new != cfg:
        _config_write(root, new, ms.config_revision(cfg))
    return {**status(root, now), 'changed': new != cfg}


def _run_path(root, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
        raise ValueError('Invalid run id')
    found = sorted((Path(root) / ms.RUNS).glob(f'*/{run_id}.json'))
    if not found:
        raise ValueError(f'Unknown run: {run_id}')
    return found[0]


def history(root, limit=10, run_id=None):
    """Run summaries, newest first; with `run_id`, that run's per-candidate decisions, and for a retired note the reason it was given."""
    from . import notes
    try:
        if run_id is None:
            return {'ok': True, 'runs': [_summary(r) for r in _recent(root, max(1, min(int(limit), 200)))]}
        run = ms._read_run(_run_path(root, run_id))
    except ms.Unavailable as exc:
        raise ValueError(f'accounting_unavailable: {exc}') from exc
    rows = []
    for ref, d in run['decisions'].items():
        row = {'ref': ref, 'action': d.get('action'), 'result': d.get('result'), 'reason_code': d.get('reason_code'), 'operation_id': d.get('operation_id'),
               'rule': d.get('rule'), 'reason': d.get('reason')}
        if not d.get('reason') and d.get('result') == 'applied' and d.get('action') in ('resolve', 'archive', 'supersede'):
            domain, _, note_id = ref.partition(':')
            try:
                retirement = notes.find_note(note_id, domain, root)[1].get('last_retirement') or {}
            except ValueError:
                retirement = {}
            if retirement.get('operation_id') == d.get('operation_id'):
                row.update(rule=retirement.get('rule'), reason=retirement.get('reason'))
        rows.append(row)
    return {'ok': True, 'run': _summary(run), 'selected': [s.get('ref') for s in run['selected']], 'decisions': rows}


def doctor_warnings(root):
    """What `doctor` reports about maintenance (the provider is probed only when it is enabled)."""
    out = []
    try:
        cfg = ms.config(root)
    except ValueError as exc:
        return [f'maintenance.json is malformed ({exc}); maintenance stays off until it is fixed']
    if cfg['policy_version'] != ms.POLICY_VERSION:
        out.append(f"maintenance.json has policy_version {cfg['policy_version']}, which this version does not know; maintenance does not run")
    try:
        acc = ms.accounting(root)[0]
        open_runs = [r['id'] for r in _recent(root) if r['state'] in ('reserved', 'running', 'applying')]
        if open_runs and not ms.running(root):
            out.append(f"{len(open_runs)} maintenance run(s) never finished and no worker is running ({', '.join(open_runs[:3])}); the next run settles them")
        if ms.active_block(acc, cfg):
            out.append(f"maintenance is blocked ({acc['blocked']['reason']}); change the budget with maintenance enable to clear it")
    except ms.Unavailable as exc:
        out.append(f'accounting_unavailable: maintenance cannot read its run records ({exc}), so it will not run')
    if cfg['enabled']:
        found = agent.provider(cfg)
        if not found['ok']:
            out.append(f"the maintenance reviewer is unavailable ({found['reason_code']}{': ' + found['detail'] if found.get('detail') else ''})")
    return out


# ---- scheduling and the session summary

SPAWN_COOLDOWN = 600


def _spawn(root):
    """Start `maintenance run --scheduled` detached, as sources.spawn does for source-sync."""
    script = Path(__file__).resolve().parents[1] / 'living_memory.py'
    argv = [sys.executable, str(script), '--root', str(root), 'maintenance', 'run', '--scheduled']
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'close_fds': True, 'cwd': tempfile.gettempdir()}   # not the store: a running process would block moving it
    if os.name == 'nt':
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, and out of the host's job object when it allows (CREATE_BREAKAWAY_FROM_JOB)
        try:
            subprocess.Popen(argv, creationflags=flags | 0x01000000, **options)
        except OSError:
            subprocess.Popen(argv, creationflags=flags, **options)
    else:
        subprocess.Popen(argv, start_new_session=True, **options)


def schedule(root, *, subagent=False, plan=False, compact=False, now=None):
    """The SessionStart check: start a detached worker when maintenance is due. Returns 'spawned' or the reason nothing started. Never raises and
    never waits. It reads only the config, the checkpoint and recent run records (no notes, git or provider); the worker rechecks everything."""
    from .logs import swallowed
    try:
        from .runtime import headless
        if subagent:
            return 'subagent'
        if headless():
            return 'headless'
        if plan:
            return 'plan_mode'
        if compact:
            return 'compact'
        if os.environ.get('LIVING_MEMORY_NO_BACKGROUND'):
            return 'background_disabled'
        if os.environ.get('LIVING_MEMORY_MAINTENANCE_CHILD'):
            return 'maintenance_child'
        cfg = ms.config(root)
        if not cfg['enabled']:
            return 'disabled'
        verdict = ms.eligibility(root, now, cfg)
        if not verdict['eligible']:
            return verdict['skip_reason']
        if ms.running(root):
            return 'already_running'
        marker = Path(root) / 'hooks' / 'maintenance.spawned'
        try:
            if time.time() - float(marker.read_text()) < SPAWN_COOLDOWN:
                return 'cooldown'
        except (OSError, ValueError):
            pass
        _spawn(root)
        from .common import atomic_write
        atomic_write(marker, str(time.time()))
        return 'spawned'
    except Exception as exc:
        swallowed('maintenance.schedule', exc, root)
        return 'error'


def session_lines(root, told_run=None, told_blocked=None):
    """(lines, run id told, blocked reason told) for the SessionStart prefix: the latest completed run's summary once per run, and one line
    when maintenance is enabled but blocked. Nothing unless maintenance is enabled."""
    try:
        cfg = ms.config(root)
    except ValueError:
        return [], told_run, told_blocked
    if not cfg['enabled']:
        return [], told_run, told_blocked
    lines, reason, latest = [], None, None
    try:
        recent = _recent(root, 20)
        latest = next((r for r in recent if r['state'] == 'completed'), None)
        acc = ms.accounting(root)[0]
        if ms.active_block(acc, cfg):
            reason = acc['blocked']['reason']
        elif recent and recent[0]['state'] == 'failed' and recent[0].get('reason_code') in ('provider_unavailable', 'provider_unsupported'):
            reason = recent[0]['reason_code']
    except ms.Unavailable:
        reason = 'accounting_unavailable'
    if latest and latest['id'] != told_run:
        c = _counts(list(latest['decisions'].values()))
        lines.append(f"Memory maintenance: checked {len(latest['selected'])}, updated {c['updated']}, retired {c['archived'] + c['superseded'] + c['resolved']}, deferred {c['deferred']}. "
                     'maintenance history shows reasons; restore REF brings archived notes back.')
        told_run = latest['id']
    if reason and not told_blocked:
        lines.append(f'Memory maintenance is blocked: {reason}. maintenance status shows details; notes are not changed until it clears.')
        told_blocked = reason
    return lines, told_run, told_blocked


def log_run(root, result, scheduled=False):
    """One metadata-only operational log line for a `maintenance run`: ids, counts and codes, never a packet or prompt."""
    from . import logs
    model = cost_complete = None
    if result.get('id'):
        try:
            record = ms._read_run(_run_path(root, result['id']))
            model, cost_complete = record.get('model'), record.get('cost_complete')
        except (ValueError, ms.Unavailable):
            pass
    logs.write('info', 'internal', 'maintenance.run', root, run_id=result.get('id'), status=result.get('status'), skip_reason=result.get('skip_reason'),
               reason_code=result.get('reason_code'), scheduled=scheduled, counts={k: result[k] for k in COUNT_KEYS if k in result} or None,
               reviewed=result.get('reviewed'), model=model, cost_complete=cost_complete, usage_micro=result.get('usage_micro'))
