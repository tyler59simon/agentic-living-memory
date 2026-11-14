"""Automatic note maintenance: candidate selection, the evidence packet, the executor and the service function `run`.

`candidates` and `packet` are a free, read-only preview (no model call, no usage events, no allowance). The model only recommends;
`apply` is the one automatic writer: it re-reads every note, pin, action item and source before it commits through the existing
journaled note change sets, one decision at a time, each with a deterministic operation id. Provider handling lives in maintenance_agent."""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import maintenance_agent as agent
from . import maintenance_state as ms
from .repository import canonical_json, digest, redact, store_lock, find_secret, transaction

PACKET_MAX = 128 * 1024
BODY_MAX = 24 * 1024
EXCERPTS = 4
EXCERPT_CHARS = 1200
FILE_READ_MAX = 1 << 20
EXCLUDED = ('preference', 'policy')            # normative: never reviewed by the model
LIMITED = ('map', 'brief', 'runbook')          # kept, deferred, confirmed or updated; never retired
COUNT_KEYS = ('updated', 'confirmed', 'archived', 'superseded', 'resolved', 'kept', 'deferred', 'skipped')
APPLIED_KEY = {'update': 'updated', 'confirm': 'confirmed', 'archive': 'archived', 'supersede': 'superseded', 'resolve': 'resolved'}
ARCHIVE_RULES = {'status': ('expired_status',), 'action': ('cancelled_work', 'obsolete_content', 'obsolete_source_claim'),
                 'other': ('obsolete_content', 'obsolete_source_claim')}
STATE_PRIORITY = {'changed': 0, 'missing': 1, 'unavailable': 2, 'unchecked': 3, 'current': 4}


def _clock(now):
    return (lambda: ms._aware(now)) if now is not None else (lambda: datetime.now(timezone.utc))


def _type(note):
    from .note_types import effective_type
    return effective_type(note)


def _excluded(note):
    return _type(note) in EXCLUDED or bool(note.get('learning_procedure'))


def _completed(note):
    from .note_format import action_items, ACTION_TYPES
    if _type(note) not in ACTION_TYPES:
        return False
    items = action_items(note.get('body', ''))
    return bool(items) and all(done for done, _ in items)


def permitted(note, kinds=(), pair_same=False, has_successor=False):
    """The actions the policy table (spec 2.1) allows for this note as it is now."""
    from .note_format import ACTION_TYPES
    t = _type(note)
    out = ['keep', 'defer']
    if t in EXCLUDED or note.get('learning_procedure'):
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
    if t in EXCLUDED or note.get('learning_procedure'):
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
        state = ('missing' if known else 'unavailable') if absolute is None else 'unavailable' if sha is None else 'current' if sha == item.get('sha256') else 'changed'
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
    return next((n for n, k in ((4, 'duplicate'), (5, 'due'), (6, 'interim'), (7, 'warned'), (8, 'noisy')) if k in kinds), 9)


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
    queues = {}
    for position, row in enumerate(rows):
        note = notes.get(row['ref'])
        if note is None or _excluded(note):
            continue
        completed = _completed(note)
        files = _file_entries(root, note, note['domain'], bindings, row.get('currency'))
        fingerprint = _fingerprint(files, row['kinds'])
        if ms.retry_hold(state, row['ref'], note['revision'], fingerprint, now):
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
    manifest = {'version': 1, 'run_id': run_id, 'evidence': {}, 'candidates': {}, 'deferred': {}, 'dropped': []}
    blocks, size, counter = [], 2 + len(run_id) + 40, 0

    def new_id():
        nonlocal counter
        counter += 1
        return f'E{counter}'
    for cand in cands:
        note, ref = cand['note'], cand['ref']
        body = note.get('body', '')
        if len(body.encode('utf-8')) > BODY_MAX:
            manifest['deferred'][ref] = {'reason_code': 'note_too_large', 'revision': cand['revision'], 'fingerprint': cand['fingerprint']}
            continue
        mark, evidence, entries = counter, {}, []
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
            successors.append({'evidence_id': sid, 'ref': pref, 'revision': pair['revision'], 'title': pair.get('title'), 'type': _type(pair), 'body': pair.get('body', '')})
        for f in cand['files'][:EXCERPTS]:
            fid = new_id()
            record = {'candidate': ref, 'kind': 'file', 'path': f['path'], 'state': f['state'], 'sha256': f['sha256'], 'abs': f['abs'], 'lines': None}
            entry = {'id': fid, 'kind': 'file', 'path': f['path'], 'state': f['state']}
            text = _read_text(f['abs']) if f['abs'] and f['state'] in ('current', 'changed') else None
            if text is not None:
                entry['excerpt'], first, last = _window(text, body)
                record['lines'] = entry['lines'] = [first, last]
            evidence[fid] = record
            entries.append(entry)
        permits = permitted(note, cand['kinds'], cand['pair_same'], bool(successors))
        block = {'ref': ref, 'revision': cand['revision'], 'title': note.get('title'), 'type': cand['type'], 'status': note.get('status', 'active'),
                 'pinned': cand['pinned'], 'tags': note.get('tags') or [], 'created': note.get('created'), 'updated': note.get('updated'),
                 'checked': note.get('checked'), 'confirmed': note.get('confirmed') or [], 'review_by': note.get('review_by'), 'files': note.get('files') or [],
                 'body': body, 'action_items': [{'done': d, 'text': t} for d, t in action_items(body)], 'review_kinds': cand['kinds'],
                 'review_reason': cand['reason'], 'currency': cand['currency'], 'permitted_actions': permits, 'evidence': entries, 'successors': successors}
        text = json.dumps(_scrub(block), ensure_ascii=False, separators=(',', ':'))
        if size + len(text.encode('utf-8')) + 1 > PACKET_MAX:
            counter = mark
            manifest['dropped'].append(ref)
            continue
        size += len(text.encode('utf-8')) + 1
        blocks.append(text)
        manifest['evidence'].update(evidence)
        manifest['candidates'][ref] = {'revision': cand['revision'], 'domain': cand['domain'], 'kinds': cand['kinds'], 'pair_same': cand['pair_same'], 'permitted': permits,
                                       'evidence_ids': list(evidence), 'successors': successor_refs, 'fingerprint': cand['fingerprint'],
                                       'files': {f['path']: {'state': f['state'], 'sha256': f['sha256'], 'kind': f['kind']} for f in cand['files']},
                                       'currency_state': (cand['currency'] or {}).get('state'), 'type': cand['type']}
    text = json.dumps({'run_id': run_id}, separators=(',', ':'))[:-1] + ',"candidates":[' + ','.join(blocks) + ']}' if blocks else ''
    return text, manifest


# ---- executor

def _decision_record(root, run, ref, action, result, code):
    changes, expected, op, _ = ms.decision_changes(root, run, ref, {'action': action, 'result': result, 'reason_code': code})
    transaction(root, changes, 'maintenance_decision', expected=expected, operation_id=op)


def _close(root, run, ref, action, result, code, info, now, hold=False):
    """Record a decision that changes no note, with a retry hold when asked (the same revision and evidence are not reviewed again for 7 days)."""
    out = {'ref': ref, 'action': action, 'result': result, 'reason_code': code}
    try:
        _decision_record(root, run, ref, action, result, code)
        if hold and info:
            ms.set_retry(root, ref, info['revision'], info['fingerprint'], code or result, now)
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
    return 'source_changed' if absolute is not None else None


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
        return _close(root, run, ref, action, result, code, cand, now, hold)
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
        if _excluded(note):
            return end('rejected', 'protected_type', True)
        if action not in permitted(note, cand['kinds'], cand['pair_same'], bool(cand['successors'])):
            return end('rejected', refusal(note, action), True)
        if action == 'keep':
            return end('kept', None, True)
        if action == 'defer':
            return end('deferred', 'model_defer', True)
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
            values = {'superseded_by': successor.partition(':')[2]} if ok else {}
        elif action == 'confirm':
            ok = full and not dec['patches'] and (t not in ('map', 'brief') or cand['currency_state'] in ('current', 'changed'))
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
            rchanges, rexpected, op, _ = ms.decision_changes(root, run, ref, {'action': action, 'result': 'applied', 'reason_code': None})
            marks = []
            for ev in evidence:
                if ev['kind'] == 'note':
                    marks.append({'kind': 'note', 'ref': ev['ref'], 'revision': ev['revision']})
                elif ev['sha256'] and len(ev['path']) <= 300:
                    marks.append({'kind': 'file', 'path': ev['path'], 'sha256': ev['sha256']})
            retirement = {'rule': rule, 'reason': redact(dec['reason'])[0][:agent.REASON_MAX], 'evidence': marks[:agent.EVIDENCE_MAX], 'run_id': run['id'],
                          'operation_id': op} if retire else None
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
    return {'ok': True, 'results': results, 'counts': _counts([r for r in results if r['result'] != 'already_applied']),
            'warnings': [f"{r['ref']}: {r['warning']}" for r in results if r.get('warning')]}


# ---- service

def _skipped(reason, when=None):
    return {'ok': True, 'status': 'skipped', 'skip_reason': reason, 'next_eligible_at': when}


def run(root, dry_run=False, adapter=None, now=None):
    """One synchronous bounded review: worker lock, recovery, eligibility, candidates, reservation, packet, one provider call, then the
    decisions (not for a dry run). Returns counts shaped like the spec's `last_run`; a routine skip is a successful no-op."""
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
            verdict = ms.eligibility(root, start, cfg)
            if not verdict['eligible']:
                return _skipped(verdict['skip_reason'], verdict['next_eligible_at'])
            st = ms.state(root, start)
            remaining = cfg['max_notes_per_day'] - st['reviewed'] - st['reserved_notes']
            selected = candidates(root, cfg, st, start, remaining)
            if not selected:
                return _skipped('no_candidates')
            record = ms.reserve(root, cfg, len(selected), start, selected=[{'ref': c['ref'], 'revision': c['revision']} for c in selected])
            selected = selected[:record['reserved_notes']]
            cursor = next_cursor(cfg, selected)
            if cursor:
                ms.set_cursor(root, cursor, start)
            text, manifest = packet(root, selected, record['id'])
            if not manifest['candidates']:
                for ref, info in manifest['deferred'].items():
                    ms.set_retry(root, ref, info['revision'], info['fingerprint'], info['reason_code'], clock())
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
                return {**base, 'status': 'dry_run', 'reason_code': None, 'proposed': proposed, **{k: 0 for k in COUNT_KEYS}}
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

