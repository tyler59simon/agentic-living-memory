"""Explicit, reviewed procedural learning. JSON records are canonical; approved runbooks are projections.

No model calls, transcript collection, implicit success inference or ranking changes. See LEARNING.md.
"""
from __future__ import annotations
import copy
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .repository import (canonical_json, confined, digest, read_json, now,
                         store_lock, transaction, serialize, SETTLE_NS)

KINDS = ('tasks', 'attempts', 'evidence', 'candidates', 'procedures', 'versions', 'uses', 'feedback')
OUTCOMES = ('succeeded', 'partial', 'failed', 'abandoned', 'unknown')
USAGES = ('selected', 'used', 'partially_used', 'ignored', 'inapplicable')
RESULTS = ('pass', 'fail', 'unknown')
METHODS = ('tool_check', 'user_acceptance', 'external_check', 'agent_assertion')
CONTRIBUTIONS = ('helpful', 'harmful', 'neutral', 'unknown')
CANDIDATE_STATES = ('draft', 'deferred', 'admitted', 'rejected')
PROCEDURE_STATES = ('active', 'suspended', 'retired')
# Proposal kinds share the candidate/version lifecycle; later releases add kinds (e.g. writing preferences).
PROPOSAL_KINDS = ('procedure',)
BASE_TAGS = ('subject/project', 'concern/workflow')
EXCERPT_LIMIT = 8192
ATTEMPT_LIMIT = 65536
NO_EXCLUSIONS = 'No additional exclusions stated.'
# Admission needs at least one stated exclusion: a runbook without one gets followed in lookalike cases it does not fit.
NEEDS_EXCLUSIONS = 'No exclusions: name at least one situation where this procedure must not be used (do_not_use_when)'
ID = re.compile(r'[a-f0-9-]{8,64}(?:\.[1-9][0-9]*)?\Z')
PREFIX = re.compile(r'[a-f0-9]{8,63}\Z')
DOMAIN = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]*\Z')


# ---------------------------------------------------------------- records

def rel(domain, kind, ident):
    if not DOMAIN.fullmatch(domain or '') or kind not in KINDS or not ID.fullmatch(ident or ''):
        raise ValueError('Invalid learning reference')
    return f'domains/{domain}/learning/{kind}/{ident}.json'


# Validated records this process has read: {path: (mtime_ns, size, record)}. Hooks call inventory() on every
# prompt, and learned runbooks are checked against their manifests, versions and evidence each time.
_RECORDS = {}


def _load(root, domain, kind, ident):
    path = confined(root, rel(domain, kind, ident))
    try:
        stat = path.stat()
    except FileNotFoundError:
        raise ValueError('Missing learning record: '+kind+'/'+ident) from None
    key = str(path)
    hit = _RECORDS.get(key)
    if hit and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
        return copy.deepcopy(hit[2])
    data = path.read_bytes()
    try:
        value = json.loads(data.decode('utf-8-sig'))
    except (UnicodeError, ValueError):
        raise ValueError('Invalid learning record: '+kind+'/'+ident) from None
    if not isinstance(value, dict) or value.get('schema') != 1 or value.get('domain') != domain or value.get('id') != ident:
        raise ValueError('Invalid learning record: '+kind+'/'+ident)
    _validate_record(kind, value)
    value = dict(value, revision=digest(data))
    if time.time_ns()-stat.st_mtime_ns > SETTLE_NS:
        _RECORDS[key] = (stat.st_mtime_ns, stat.st_size, copy.deepcopy(value))
    return value


def resolve(root, domain, kind, ident):
    """Full record ID for an ID or a unique 8+ character prefix (the short form hints show)."""
    ident = str(ident or '').strip()
    if ID.fullmatch(ident) and confined(root, rel(domain, kind, ident)).exists():
        return ident
    versioned = re.fullmatch(r'([a-f0-9]{8,64})\.([1-9][0-9]*)', ident)
    if PREFIX.fullmatch(ident) or (versioned and kind == 'versions'):
        folder = confined(root, f'domains/{domain}/learning/{kind}')
        pattern = f'{versioned[1]}*.{versioned[2]}.json' if versioned else ident+'*.json'
        found = [p.stem for p in folder.glob(pattern)] if folder.is_dir() else []
        if not versioned:
            found = [f for f in found if '.' not in f]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ValueError(f'Ambiguous learning reference {ident}: {len(found)} {kind} match')
    if not ID.fullmatch(ident):
        raise ValueError('Invalid learning reference')
    raise ValueError('Missing learning record: '+kind+'/'+ident)


def get(root, domain, kind, ident):
    return _load(root, domain, kind, resolve(root, domain, kind, ident))


def scan(root, domain, kind):
    """(records, problems): one bad file is reported, never allowed to hide the others."""
    folder = confined(root, f'domains/{domain}/learning/{kind}')
    if not folder.is_dir():
        return [], []
    good, bad = [], []
    for p in sorted(folder.glob('*.json')):
        try:
            good.append(_load(root, domain, kind, p.stem))
        except (ValueError, OSError) as exc:
            bad.append(dict(path=f'domains/{domain}/learning/{kind}/{p.name}', problem=str(exc)))
    return good, bad


def rows(root, domain, kind):
    return scan(root, domain, kind)[0]


def config(root, domain=None):
    value = read_json(Path(root)/'learning.json', {'schema': 1, 'enabled': True, 'domains': {}})
    if (not isinstance(value, dict) or not isinstance(value.get('enabled'), bool) or not isinstance(value.get('domains'), dict)
            or any(not isinstance(v, bool) for v in value['domains'].values())):
        raise ValueError('Invalid learning configuration: learning.json')
    return dict(value, effective=value['enabled'] and value['domains'].get(domain, True))


def _enabled(root, domain):
    try:
        return config(root, domain)['effective']
    except ValueError:
        return False


def _clean(value):
    return {k: v for k, v in value.items() if k != 'revision'}


def _text(value, name, maximum=16000):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > maximum:
        raise ValueError('Invalid '+name+(f' (1..{maximum} bytes of text)' if isinstance(value, str) and value.strip() else ' (text required)'))
    return value.strip()


def _strings(value, name, empty=False, single_line=False):
    if not isinstance(value, list) or (not value and not empty) or len(value) > 100:
        raise ValueError('Invalid '+name+(' (a list of strings)' if empty else ' (a nonempty list of strings)'))
    items = [_text(v, name) for v in value]
    if single_line and any('\n' in v for v in items):
        raise ValueError(f'Each {name} item must be one line')
    return items


def _safe(value):
    from .repository import find_secret
    if find_secret(canonical_json(value)):
        raise ValueError('Credential-looking content rejected before learning persistence; redact it and resubmit')


def _validate_record(kind, value):
    """Reject malformed canonical records before they can influence retrieval."""
    try:
        if kind == 'tasks':
            _text(value['objective'], 'objective'); _text(value['family'], 'family', 200)
            _strings(value['acceptance_criteria'], 'acceptance_criteria')
        elif kind == 'attempts':
            if (value['outcome'] not in OUTCOMES or value['state'] not in ('open', 'closed') or not ID.fullmatch(value['task_id'])
                    or not isinstance(value['environment'], dict)):
                raise ValueError('Invalid attempt')
        elif kind == 'evidence':
            if not ID.fullmatch(value['attempt_id']):
                raise ValueError('Invalid evidence')
            if value.get('deleted'):
                _text(value['deleted_at'], 'deleted_at')
                return
            _text(value['excerpt'], 'excerpt', EXCERPT_LIMIT); _text(value['criterion'], 'criterion')
            if value['result'] not in RESULTS or value['method'] not in METHODS:
                raise ValueError('Invalid evidence')
            if value['content_hash'] != digest(value['excerpt'].encode()):
                raise ValueError('Evidence content hash mismatch')
        elif kind in ('candidates', 'versions'):
            if value.get('kind', 'procedure') not in PROPOSAL_KINDS:
                raise ValueError('Unknown proposal kind')
            _content(value['content']); _strings(value['evidence_ids'], 'evidence_ids', empty=True)
            _strings(value.get('tags', []), 'tags', empty=True)
            refs = value['step_evidence']
            if not isinstance(refs, list) or len(refs) != len(value['content']['steps']):
                raise ValueError('Invalid step evidence')
            for row in refs:
                _strings(row, 'step_evidence', empty=True)
            if kind == 'candidates' and value['state'] not in CANDIDATE_STATES:
                raise ValueError('Invalid candidate state')
            if kind == 'versions' and value['content_hash'] != digest(canonical_json(value['content']).encode()):
                raise ValueError('Procedure content hash mismatch')
        elif kind == 'procedures':
            if value['state'] not in PROCEDURE_STATES or not isinstance(value['current_version'], int) or value['current_version'] < 1:
                raise ValueError('Invalid procedure')
            path = value['note_path']
            if not any(path.startswith(f'domains/{value["domain"]}/{folder}/') for folder in ('notes', 'archive')) or '..' in Path(path).parts:
                raise ValueError('Invalid procedure note path')
            from .repository import parse_text
            n = parse_text(value['approved_note'])
            if not n or n['id'] != value['note_id'] or n.get('learning_procedure') != value['id'] or n.get('learning_version') != value['current_version']:
                raise ValueError('Invalid approved snapshot')
        elif kind == 'uses':
            if (value['usage'] not in USAGES or not isinstance(value['version'], int) or value['version'] < 1
                    or (value.get('attempt_id') is not None and not ID.fullmatch(value['attempt_id']))):
                raise ValueError('Invalid use')
        elif kind == 'feedback':
            if value['outcome'] not in OUTCOMES or value['contribution'] not in CONTRIBUTIONS:
                raise ValueError('Invalid feedback')
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid '+kind+' learning record') from exc


def _record(domain, payload):
    return dict(schema=1, id=uuid.uuid4().hex, domain=domain, created_at=now(), **payload)


def _commit(root, domain, kind, value, expected=None, extra=None):
    value = _clean(value)
    _safe(value)
    _validate_record(kind, value)
    path = rel(domain, kind, value['id'])
    transaction(root, {path: canonical_json(value)}, 'learning_'+kind, {path: expected})
    return dict(dict(ok=True, status='saved', record=get(root, domain, kind, value['id'])), **(extra or {}))


def _idempotent(root, domain, kind, payload):
    """The original reply when this idempotency key was already used with the same payload."""
    key = _text(payload['idempotency_key'], 'idempotency_key', 200)
    fingerprint = digest(canonical_json(payload).encode())
    for old in rows(root, domain, kind):
        if old.get('idempotency_key') == key:
            if old.get('request_hash') != fingerprint:
                raise ValueError('Idempotency conflict: payload differs')
            return old
    return None


def _keyed(record, payload):
    if payload.get('idempotency_key'):
        record.update(idempotency_key=_text(payload['idempotency_key'], 'idempotency_key', 200),
                      request_hash=digest(canonical_json(payload).encode()))
    return record


# ---------------------------------------------------------------- procedure content and its runbook

LIST_FIELDS = (('use_when', 'Use when'), ('do_not_use_when', 'Do not use when'), ('steps', 'Procedure'), ('done_when', 'Done when'))
LABELS = {label for _, label in LIST_FIELDS} | {'Fallback'}


def _content(payload, new=False):
    if not isinstance(payload, dict):
        raise ValueError('content must be an object with title, body, use_when, do_not_use_when, steps, done_when, fallback')
    result = {}
    for field in ('title', 'body', 'fallback'):
        result[field] = _text(payload.get(field), field, 200 if field == 'title' else 16000)
    if '\n' in result['title']:
        raise ValueError('title must be one line')
    for field, _ in LIST_FIELDS:
        result[field] = _strings(payload.get(field, [] if field == 'do_not_use_when' else None), field,
                                 empty=field == 'do_not_use_when', single_line=new)
    return result


def body(content):
    """Runbook body for approved content. List fields render one item per sub-bullet so edits parse back exactly."""
    from .note_format import render
    def listed(values, numbered=False):
        return '\n'.join(f'  {i+1}. {v}' if numbered else f'  - {v}' for i, v in enumerate(values))
    exclusions = content['do_not_use_when']
    details = ['**Use when:**\n'+listed(content['use_when']),
               '**Do not use when:** '+NO_EXCLUSIONS if not exclusions else '**Do not use when:**\n'+listed(exclusions),
               '**Procedure:**\n'+listed(content['steps'], numbered=True),
               '**Done when:**\n'+listed(content['done_when']),
               '**Fallback:** '+content['fallback']]
    return render(content['body'], details)


def _items(text, approved, field):
    text = text.strip()
    if field == 'do_not_use_when' and text == NO_EXCLUSIONS:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    marker = re.compile(r'^(?:[-*+]|\d+\.)\s+')
    if len(lines) == 1 and not marker.match(lines[0]):
        # Earlier projections joined items with '; '. Unchanged text keeps the approved items exactly.
        return list(approved) if lines[0] == '; '.join(approved) else [lines[0]]
    return [marker.sub('', line) for line in lines]


def parse_body(text, approved):
    """Content from an edited runbook body; fields the edit left alone keep their approved values."""
    from .note_format import sections
    parts = sections(text)
    content = dict(approved, body=(parts.get('summary') or text).strip())
    details = parts.get('details', '')
    if not details:
        return content
    labels = {label.strip(): value for label, value in
              re.findall(r'(?ms)^\* \*\*([^*]+):\*\*[ \t]*(.*?)(?=^\* \*\*|\Z)', details)}
    if set(labels) != LABELS:
        raise ValueError('A learned runbook keeps the Key Details labels '+', '.join(sorted(LABELS))
                         +'; change fields with learn candidate update or Hivemind Review')
    for field, label in LIST_FIELDS:
        content[field] = _items(labels[label], approved[field], field)
    content['fallback'] = labels['Fallback'].strip()
    return content


# ---------------------------------------------------------------- evaluation and admission

def _tags(root, value):
    from .notes import taxonomy
    tags = _strings(value or [], 'tags', empty=True)
    unknown = sorted(set(tags)-taxonomy(root))
    if unknown:
        raise ValueError('Unregistered tags: '+', '.join(unknown))
    return sorted(set(tags))


def _candidate(root, domain, payload):
    kind = payload.get('kind', 'procedure')
    if kind not in PROPOSAL_KINDS:
        raise ValueError('Unknown proposal kind: '+str(kind))
    content = _content(payload.get('content'), new=True)
    ids = list(dict.fromkeys(get(root, domain, 'evidence', i)['id']
                             for i in _strings(payload.get('evidence_ids', []), 'evidence_ids', empty=True)))
    step_evidence = payload.get('step_evidence')
    defaulted = step_evidence is None
    if defaulted:
        step_evidence = [list(ids) for _ in content['steps']]
    if not isinstance(step_evidence, list) or len(step_evidence) != len(content['steps']):
        raise ValueError('step_evidence must hold one evidence list per procedure step (omit it to cite all evidence for every step)')
    step_evidence = [[get(root, domain, 'evidence', i)['id'] for i in _strings(v, 'step_evidence', empty=True)] for v in step_evidence]
    project = payload.get('project')
    if project is not None:
        from .notes import taxonomy
        if not isinstance(project, str) or 'project/'+project not in taxonomy(root):
            raise ValueError('Unknown project scope')
    procedure_id = payload.get('procedure_id')
    if procedure_id:
        current = get(root, domain, 'procedures', procedure_id)
        procedure_id = current['id']
        if project != current.get('project'):
            raise ValueError('Procedure revisions cannot change project scope')
    return dict(kind=kind, content=content, evidence_ids=ids, step_evidence=step_evidence,
                step_evidence_defaulted=defaulted and bool(ids), project=project, procedure_id=procedure_id,
                tags=_tags(root, payload.get('tags')), state='draft',
                contradictions=_strings(payload.get('contradictions', []), 'contradictions', empty=True))


def evaluate(root, domain, candidate):
    """Evidence structure and coverage. It cannot prove that a procedure caused success."""
    errors, warnings, dependencies = [], [], {}
    evidence = []
    for ident in candidate['evidence_ids']:
        try:
            e = get(root, domain, 'evidence', ident)
        except ValueError:
            errors.append('Evidence unavailable: '+ident); continue
        dependencies[rel(domain, 'evidence', e['id'])] = e['revision']
        if e.get('deleted'):
            errors.append('Evidence was deleted: '+ident); continue
        evidence.append(e)
    attempts = {}
    for e in evidence:
        try:
            a = get(root, domain, 'attempts', e['attempt_id'])
        except ValueError:
            errors.append('Attempt unavailable: '+e['attempt_id']); continue
        attempts[a['id']] = a
        dependencies[rel(domain, 'attempts', a['id'])] = a['revision']
    families, tasks, task_families = set(), set(), set()
    for a in attempts.values():
        try:
            t = get(root, domain, 'tasks', a['task_id'])
        except ValueError:
            errors.append('Task unavailable: '+a['task_id']); continue
        dependencies[rel(domain, 'tasks', t['id'])] = t['revision']
        families.add(a['attempt_family_id']); tasks.add(t['id']); task_families.add(t['family'])
        if t.get('project') != candidate.get('project'):
            errors.append('Evidence project differs from candidate scope')
        if a['outcome'] != 'succeeded':
            errors.append('Supporting attempt is not closed successfully: '+a['id'])
        for criterion in t['acceptance_criteria']:
            checks = [e for e in evidence if e['attempt_id'] == a['id'] and e['criterion'] == criterion and not e.get('corrected_by')]
            if not any(e['result'] == 'pass' and e['method'] != 'agent_assertion' for e in checks):
                errors.append('Missing checked criterion: '+criterion)
            if any(e['result'] == 'fail' for e in checks):
                errors.append('Contradictory criterion: '+criterion)
    if not attempts:
        errors.append('No supporting task evidence')
    if len(task_families) > 1:
        errors.append('Supporting tasks have different task families')
    if any(e.get('corrected_by') for e in evidence):
        errors.append('Evidence has been corrected; select its replacement')
    if any(e['result'] == 'fail' for e in evidence):
        errors.append('Contradicting evidence requires resolution')
    if any(not refs or any(i not in candidate['evidence_ids'] for i in refs) for refs in candidate['step_evidence']):
        errors.append('Each procedure step needs supporting evidence references')
    if candidate.get('contradictions'):
        errors.append('Resolve recorded contradictions before admission')
    if not candidate['content'].get('do_not_use_when'):
        errors.append(NEEDS_EXCLUSIONS)
    if not _enabled(root, domain):
        errors.append('Learning is disabled for this domain')
    if len(tasks) < 2:
        warnings.append('Provisional: only one distinct supporting task; no causal benefit established')
    if candidate.get('step_evidence_defaulted'):
        warnings.append('Every step cites all supporting evidence by default; confirm each step is actually supported')
    # Similarity is an inspection lead, never an automatic merge decision.
    from .repository import inventory
    matches = [dict(id=n['id'], title=n['title']) for n in inventory(domain, root)
               if n['title'].casefold() == candidate['content']['title'].casefold()
               and n.get('learning_procedure') != candidate.get('procedure_id')]
    if matches:
        errors.append('Existing note has this title; reconcile it explicitly')
    return dict(ok=True, eligible=not errors, errors=list(dict.fromkeys(errors)), warnings=warnings, matches=matches,
                supporting_tasks=len(tasks), attempt_families=len(families), dependencies=dependencies)


def _admission_plan(root, domain, c, c_revision):
    """A reviewed plan that publishes candidate `c` (saved at c_revision, or None when the plan creates it)."""
    from .governance import plan
    from .notes import build_note, taxonomy
    if c['state'] not in ('draft', 'deferred'):
        raise ValueError('Candidate is not awaiting review')
    report = evaluate(root, domain, c)
    # A rollback republishes an approved version unchanged, including versions approved before exclusions were required.
    errors = [e for e in report['errors'] if not (e == NEEDS_EXCLUSIONS and c.get('restored_from') is not None)]
    if errors:
        raise ValueError('Candidate not eligible: '+'; '.join(errors))
    pid = c.get('procedure_id') or uuid.uuid4().hex
    previous = get(root, domain, 'procedures', pid) if c.get('procedure_id') else None
    if previous and previous['state'] == 'retired':
        raise ValueError('Procedure is retired; propose a new procedure instead')
    number = previous['current_version']+1 if previous else 1
    vid = pid+'.'+str(number)
    version = dict(schema=1, id=vid, domain=domain, created_at=now(), kind=c.get('kind', 'procedure'), procedure_id=pid,
                   version=number, content=c['content'], evidence_ids=c['evidence_ids'], tags=c.get('tags', []),
                   step_evidence=c['step_evidence'], project=c.get('project'),
                   source_candidate=c['id'], maturity='corroborated' if report['supporting_tasks'] > 1 else 'provisional')
    version['content_hash'] = digest(canonical_json(version['content']).encode())
    if c.get('restored_from') is not None:
        version['restored_from'] = c['restored_from']
    tags = sorted(set(BASE_TAGS) | set(c.get('tags', [])) | ({'project/'+c['project']} if c.get('project') else set()))
    if not set(tags) <= taxonomy(root):
        raise ValueError('Runbook tags must be registered: '+', '.join(sorted(set(tags)-taxonomy(root))))
    expected = dict(report['dependencies'])
    expected[rel(domain, 'candidates', c['id'])] = c_revision
    if previous:
        expected[rel(domain, 'procedures', pid)] = previous['revision']
        note_path = confined(root, previous['note_path'])
        from .repository import parse
        note = parse(note_path)
        if not note:
            raise ValueError('Procedure note is missing or malformed; restore it from backup first')
        expected[previous['note_path']] = digest(note_path.read_bytes())
        note.update(title=c['content']['title'], body=body(c['content']), tags=tags, updated=now())
    else:
        note_path, note, _ = build_note(c['content']['title'], body(c['content']), tags, domain, root, note_type='runbook')
    note.update(learning_procedure=pid, learning_version=number)
    note_text = serialize(note)
    note_rel = Path(note_path).relative_to(root).as_posix()
    manifest = dict(schema=1, id=pid, domain=domain, created_at=previous['created_at'] if previous else now(),
                    kind=c.get('kind', 'procedure'), current_version=number, title=c['content']['title'],
                    project=c.get('project'), state='active', note_id=note['id'], note_path=note_rel, approved_note=note_text)
    admitted = dict(_clean(c), state='admitted', procedure_id=pid, admitted_version=number)
    changes = {rel(domain, 'versions', vid): canonical_json(version),
               rel(domain, 'procedures', pid): canonical_json(manifest), note_rel: note_text,
               rel(domain, 'candidates', c['id']): canonical_json(admitted)}
    p = Path(root)/'tag_taxonomy.json'
    expected['tag_taxonomy.json'] = digest(p.read_bytes())
    return plan(root, changes, 'learning_admit', [domain],
                dict(candidate_id=c['id'], procedure_id=pid, version=number, title=c['content']['title'],
                     **{k: report[k] for k in ('warnings', 'supporting_tasks', 'attempt_families')}),
                guards={'read_set': expected, 'learning_enabled': [domain],
                        'learning_admission': {'domain': domain, 'title': c['content']['title'], 'procedure_id': pid}})


def supported(root, domain, procedure):
    """True while the current version's evidence is present, uncorrected and undeleted."""
    try:
        version = get(root, domain, 'versions', procedure['id']+'.'+str(procedure['current_version']))
        if not version['evidence_ids']:
            return False
        for ident in version['evidence_ids']:
            e = get(root, domain, 'evidence', ident)
            if e.get('corrected_by') or e.get('deleted'):
                return False
        return True
    except (ValueError, OSError, KeyError):
        return False


# ---------------------------------------------------------------- serving approved runbooks

def published(root, domain):
    """{note_id: manifest} for every learned runbook, and the set of those that may be served."""
    if not (Path(root)/'domains'/domain/'learning'/'procedures').is_dir():
        return {}, set()
    manifests = {p['note_id']: p for p in rows(root, domain, 'procedures')}
    servable = {note_id for note_id, p in manifests.items() if p['state'] == 'active' and supported(root, domain, p)}
    return manifests, servable


def approved_notes(root, domain, notes, actionable=False):
    """Serve approved bytes even if an external editor changed or removed metadata.

    actionable: drop suspended, retired and unsupported procedures (what search and context should offer).
    Learning being disabled does not unpublish approved procedures; `learn procedure suspend` does."""
    if not (Path(root)/'domains'/domain/'learning'/'procedures').is_dir():
        return notes
    from .repository import parse_text
    manifests, servable = published(root, domain)
    output = []
    for note in notes:
        p = manifests.get(note['id'])
        if not p:
            # Unapproved copies, or runbooks whose manifest is unreadable, cannot impersonate a procedure.
            if not note.get('learning_procedure'):
                output.append(note)
            continue
        if actionable and note['id'] not in servable:
            continue
        if 'learning_state' in note:
            output.append(note); continue    # already the approved snapshot
        approved = parse_text(p['approved_note'], note['path'])
        if approved is None:
            continue
        approved.update(revision=note['revision'], status=note.get('status', 'active'),
                        learning_state=p['state'] if p['state'] != 'active' or note['id'] in servable else 'needs_review',
                        learning_pending_edit=serialize(note) != p['approved_note'])
        output.append(approved)
    return output


def use_hint(procedure_id, version=None):
    short = procedure_id[:8]
    return (f"Learned procedure {short}{' v'+str(version) if version else ''}: after relying on it (or deciding not to), "
            f"CMD learn use report --procedure {short} --usage used|partially_used|ignored|inapplicable")


# ---------------------------------------------------------------- edits to published runbooks

def stage_edit(root, domain, note, values, preview=False):
    """An ordinary update of a learned runbook becomes a review candidate; the published note stays intact."""
    pid = note.get('learning_procedure')
    if not pid:
        return None
    if set(values)-{'title', 'body'}:
        raise ValueError('Learned runbook edits accept title and body; lifecycle changes use learn procedure suspend/resume/retire-plan')
    if not _enabled(root, domain):
        raise ValueError('Learning is disabled for this domain, so edits to a learned runbook cannot be staged for review')
    p = get(root, domain, 'procedures', pid)
    if p['state'] == 'retired':
        raise ValueError('Procedure is retired; propose a new procedure instead')
    v = get(root, domain, 'versions', pid+'.'+str(p['current_version']))
    content = dict(v['content'])
    if 'body' in values:
        content = parse_body(str(values['body']), content)
    if 'title' in values:
        content['title'] = str(values['title']).strip()
    if content == v['content']:
        return dict(ok=True, status='unchanged', id=note['id'], domain=domain)
    old = v['content']['steps']
    if len(old) == len(content['steps']):
        refs = [list(r) for r in v['step_evidence']]
    else:
        by_text = dict(zip(old, v['step_evidence']))
        refs = [list(by_text.get(step, [])) for step in content['steps']]
    payload = dict(content=content, evidence_ids=v['evidence_ids'], step_evidence=refs, procedure_id=pid,
                   project=p.get('project'), tags=v.get('tags', []))
    candidate = _record(domain, _candidate(root, domain, payload))
    if preview:
        return dict(ok=True, status='preview', plan_id=None, id=note['id'], domain=domain, proposal=candidate['content'],
                    hint='Learned runbook edits are staged as review proposals; run update to stage this one.')
    return _commit(root, domain, 'candidates', candidate, extra=dict(
        id=note['id'], domain=domain, status='proposed', candidate_id=candidate['id'],
        hint=f'Staged for review; the approved version stays published. Inspect: CMD learn candidate evaluate {candidate["id"][:8]}'))


def capture_edit(root, domain, ident, payload):
    p = get(root, domain, 'procedures', ident)
    _revision(p, payload)
    path = confined(root, p['note_path'])
    from .repository import parse
    raw = parse(path)
    if not raw:
        raise ValueError('Cannot stage a malformed note; restore the approved version from backup first')
    raw['learning_procedure'] = p['id']
    result = stage_edit(root, domain, raw, dict(title=raw['title'], body=raw['body']))
    transaction(root, {p['note_path']: p['approved_note']}, 'learning_restore_projection', {p['note_path']: raw['revision']})
    return result


# ---------------------------------------------------------------- integrity

def problems(root, domain):
    found = []
    for kind in KINDS:
        try:
            records, bad = scan(root, domain, kind)
        except (ValueError, OSError) as exc:
            found.append(dict(path=f'domains/{domain}/learning/{kind}', problem=str(exc))); continue
        found += bad
        for record in records:
            try:
                if kind == 'attempts': get(root, domain, 'tasks', record['task_id'])
                if kind == 'evidence': get(root, domain, 'attempts', record['attempt_id'])
                if kind == 'versions':
                    for ident in record['evidence_ids']: get(root, domain, 'evidence', ident)
                if kind == 'procedures':
                    get(root, domain, 'versions', record['id']+'.'+str(record['current_version']))
                    path = confined(root, record['note_path'])
                    if not path.exists() or path.read_text(encoding='utf-8-sig') != record['approved_note']:
                        found.append(dict(path=record['note_path'],
                                          problem=f"approved projection differs; stage the edit with learn procedure capture-edit {record['id'][:8]}"))
            except (ValueError, KeyError, OSError) as exc:
                found.append(dict(path=rel(domain, kind, record['id']), problem=str(exc)))
    return found


def referenced(root, domain):
    """(evidence IDs, attempt IDs) that candidates, versions, uses or feedback still depend on."""
    evidence = set()
    for kind in ('candidates', 'versions', 'feedback'):
        for r in rows(root, domain, kind):
            evidence |= set(r.get('evidence_ids', []))
            for refs in r.get('step_evidence', []):
                evidence |= set(refs)
    records = {e['id']: e for e in rows(root, domain, 'evidence')}
    for ident in list(evidence):
        for link in ('corrects', 'corrected_by'):
            if records.get(ident, {}).get(link):
                evidence.add(records[ident][link])
    attempts = {records[i]['attempt_id'] for i in evidence if i in records}
    attempts |= {u['attempt_id'] for u in rows(root, domain, 'uses') if u.get('attempt_id')}
    by_id = {a['id']: a for a in rows(root, domain, 'attempts')}
    pending = list(attempts)
    while pending:
        retry = by_id.get(pending.pop(), {}).get('retry_of')
        if retry and retry not in attempts:
            attempts.add(retry); pending.append(retry)
    return evidence, attempts


# ---------------------------------------------------------------- operations

def _revision(record, payload):
    if record['revision'] != payload.get('expected_revision'):
        raise ValueError('Stale revision: learning record changed (pass the expected_revision it returned; re-read it first)')


def _domain_ok(root, domain):
    if not domain or not DOMAIN.fullmatch(domain):
        raise ValueError('Domain required: pass --domain or --cwd for a bound project')
    from .common import load_bindings
    if domain not in load_bindings(root).get('domains', {}) and not (Path(root)/'domains'/domain/'notes').exists():
        raise ValueError('Unknown learning domain: '+domain)


def _task(root, domain, payload):
    project = payload.get('project')
    if project:
        from .notes import taxonomy
        if 'project/'+str(project) not in taxonomy(root):
            raise ValueError('Unknown project scope')
    return _record(domain, dict(objective=_text(payload.get('objective'), 'objective'),
                                acceptance_criteria=_strings(payload.get('acceptance_criteria'), 'acceptance_criteria', single_line=True),
                                family=_text(payload.get('family'), 'family', 200), project=project or None))


def _evidence(domain, attempt_id, criteria, item, existing_bytes):
    if not isinstance(item, dict):
        raise ValueError('Each evidence item must be an object')
    criterion = item.get('criterion')
    if criterion is None and len(criteria) == 1:
        criterion = criteria[0]
    if criterion not in criteria:
        raise ValueError('Unknown acceptance criterion: '+str(criterion)+' (use one of the task criteria exactly)')
    if item.get('result') not in RESULTS:
        raise ValueError('Evidence result must be one of: '+', '.join(RESULTS))
    if item.get('method') not in METHODS:
        raise ValueError('Verification method must be one of: '+', '.join(METHODS))
    excerpt = _text(item.get('excerpt'), 'excerpt', EXCERPT_LIMIT)
    if existing_bytes+len(excerpt.encode()) > ATTEMPT_LIMIT:
        raise ValueError('Attempt evidence exceeds 64 KiB')
    return _record(domain, dict(attempt_id=attempt_id, criterion=criterion, result=item['result'], method=item['method'],
                                observer=_text(item.get('observer'), 'observer', 200), excerpt=excerpt, source=item.get('source'),
                                limitations=_text(item.get('limitations'), 'limitations'), content_hash=digest(excerpt.encode())))


def _attempt_bytes(root, domain, attempt_id):
    return sum(len(e['excerpt'].encode()) for e in rows(root, domain, 'evidence') if e['attempt_id'] == attempt_id and not e.get('deleted'))


def op_record(root, domain, ident, payload):
    """Task, closed attempt and evidence in one transaction: the usual way to record verified work."""
    if payload.get('idempotency_key'):
        old = _idempotent(root, domain, 'tasks', payload)
        if old:
            attempts = [a for a in rows(root, domain, 'attempts') if a['task_id'] == old['id']]
            ids = {a['id'] for a in attempts}
            return dict(ok=True, status='existing', task=old, attempts=attempts,
                        evidence=[e for e in rows(root, domain, 'evidence') if e['attempt_id'] in ids])
    task = _keyed(_task(root, domain, payload), payload)
    if payload.get('outcome') not in OUTCOMES:
        raise ValueError('outcome must be one of: '+', '.join(OUTCOMES))
    if not isinstance(payload.get('environment', {}), dict):
        raise ValueError('environment must be an object')
    attempt = _record(domain, dict(task_id=task['id'], attempt_family_id=uuid.uuid4().hex, retry_of=None, source_origin_id=None,
                                   environment=payload.get('environment', {}),
                                   session_refs=_strings(payload.get('session_refs', []), 'session_refs', empty=True),
                                   outcome=payload['outcome'], state='closed', closed_at=now(), reason=str(payload.get('reason') or '')))
    items = payload.get('evidence')
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError('evidence must list 1..20 checks: {criterion?, result, method, observer, excerpt, limitations, source?}')
    evidence, used = [], 0
    for item in items:
        e = _evidence(domain, attempt['id'], task['acceptance_criteria'], item, used)
        used += len(e['excerpt'].encode()); evidence.append(e)
    changes = {}
    for kind, value in [('tasks', task), ('attempts', attempt)]+[('evidence', e) for e in evidence]:
        _safe(value); _validate_record(kind, value)
        changes[rel(domain, kind, value['id'])] = canonical_json(value)
    transaction(root, changes, 'learning_record', {path: None for path in changes})
    ids = [e['id'] for e in evidence]
    eligible = payload['outcome'] == 'succeeded' and any(e['method'] != 'agent_assertion' and e['result'] == 'pass' for e in evidence)
    hint = ("Propose the procedure for review: CMD learn candidate create --title '...' --summary '...' --use-when '...' "
            "--step '...' --done-when '...' --fallback '...' "+' '.join('--evidence '+i[:8] for i in ids)) if eligible else \
           'Recorded. Only a succeeded attempt with a passing non-assertion check can support a procedure.'
    return dict(ok=True, status='saved', task=get(root, domain, 'tasks', task['id']),
                attempt=get(root, domain, 'attempts', attempt['id']),
                evidence=[get(root, domain, 'evidence', i) for i in ids], hint=hint)


def op_task_create(root, domain, ident, payload):
    return _commit(root, domain, 'tasks', _keyed(_task(root, domain, payload), payload))


def op_attempt_start(root, domain, ident, payload):
    if not isinstance(payload.get('environment', {}), dict):
        raise ValueError('environment must be an object')
    t = get(root, domain, 'tasks', payload.get('task_id'))
    previous = get(root, domain, 'attempts', payload['retry_of']) if payload.get('retry_of') else None
    if previous and previous['task_id'] != t['id']:
        raise ValueError('Retry belongs to another task')
    origin = payload.get('source_origin_id')
    if origin:
        origin = _text(origin, 'source_origin_id', 200)
        existing = next((a for a in rows(root, domain, 'attempts') if a.get('source_origin_id') == origin), None)
        if existing:
            if existing['task_id'] != t['id']:
                raise ValueError('Origin conflict: imported attempt belongs to another task')
            return dict(ok=True, status='existing', record=existing)
    record = _record(domain, dict(task_id=t['id'], attempt_family_id=previous['attempt_family_id'] if previous else uuid.uuid4().hex,
                                  retry_of=previous['id'] if previous else None, source_origin_id=origin,
                                  environment=payload.get('environment', {}),
                                  session_refs=_strings(payload.get('session_refs', []), 'session_refs', empty=True),
                                  outcome='unknown', state='open'))
    return _commit(root, domain, 'attempts', _keyed(record, payload))


def op_attempt_close(root, domain, ident, payload):
    old = get(root, domain, 'attempts', ident)
    _revision(old, payload)
    if old['state'] != 'open':
        raise ValueError('Attempt is already closed')
    if payload.get('outcome') not in OUTCOMES:
        raise ValueError('outcome must be one of: '+', '.join(OUTCOMES))
    record = dict(_clean(old), outcome=payload['outcome'], state='closed', closed_at=now(), reason=str(payload.get('reason') or ''))
    return _commit(root, domain, 'attempts', record, old['revision'])


def op_evidence_add(root, domain, ident, payload):
    a = get(root, domain, 'attempts', payload.get('attempt_id'))
    t = get(root, domain, 'tasks', a['task_id'])
    record = _evidence(domain, a['id'], t['acceptance_criteria'], payload, _attempt_bytes(root, domain, a['id']))
    return _commit(root, domain, 'evidence', _keyed(record, payload))


def _suspend_dependents(root, domain, evidence_id, reason, changes, expected):
    suspended = []
    for p in rows(root, domain, 'procedures'):
        if p['state'] != 'active':
            continue
        try:
            v = get(root, domain, 'versions', p['id']+'.'+str(p['current_version']))
        except ValueError:
            continue
        if evidence_id in v['evidence_ids']:
            changes[rel(domain, 'procedures', p['id'])] = canonical_json(dict(_clean(p), state='suspended', suspended_reason=reason))
            expected[rel(domain, 'procedures', p['id'])] = p['revision']
            suspended.append(p['id'])
    return suspended


def op_evidence_correct(root, domain, ident, payload):
    old = get(root, domain, 'evidence', ident)
    _revision(old, payload)
    if old.get('deleted'):
        raise ValueError('Evidence was deleted and cannot be corrected')
    if old.get('corrected_by'):
        raise ValueError('Evidence was already corrected by '+old['corrected_by']+'; correct that record instead')
    a = get(root, domain, 'attempts', payload.get('attempt_id', old['attempt_id']))
    if old['attempt_id'] != a['id']:
        raise ValueError('Correction belongs to another attempt')
    t = get(root, domain, 'tasks', a['task_id'])
    used = _attempt_bytes(root, domain, a['id'])-len(old['excerpt'].encode())
    record = _evidence(domain, a['id'], t['acceptance_criteria'], payload, used)
    record['corrects'] = old['id']
    _safe(record); _validate_record('evidence', record)
    changes = {rel(domain, 'evidence', old['id']): canonical_json(dict(_clean(old), corrected_by=record['id'])),
               rel(domain, 'evidence', record['id']): canonical_json(record)}
    expected = {rel(domain, 'evidence', old['id']): old['revision'], rel(domain, 'evidence', record['id']): None}
    suspended = _suspend_dependents(root, domain, old['id'], 'evidence corrected: '+old['id'], changes, expected)
    transaction(root, changes, 'learning_evidence_correct', expected)
    return dict(ok=True, status='saved', record=get(root, domain, 'evidence', record['id']), suspended=suspended)


def op_evidence_delete_plan(root, domain, ident, payload):
    from .governance import plan
    old = get(root, domain, 'evidence', ident)
    _revision(old, payload)
    if old.get('deleted'):
        raise ValueError('Evidence is already deleted')
    # A tombstone keeps references explainable; the excerpt, observer, limitations and source are removed.
    tomb = dict(schema=1, id=old['id'], domain=domain, created_at=old['created_at'], attempt_id=old['attempt_id'],
                deleted=True, deleted_at=now(), corrects=old.get('corrects'), corrected_by=old.get('corrected_by'))
    changes = {rel(domain, 'evidence', old['id']): canonical_json(tomb)}
    read_set = {rel(domain, 'evidence', old['id']): old['revision']}
    suspended = _suspend_dependents(root, domain, old['id'], 'evidence deleted: '+old['id'], changes, read_set)
    return plan(root, changes, 'learning_evidence_delete', [domain],
                {'evidence_id': old['id'], 'suspended': suspended,
                 'warning': 'Dependent procedures are suspended; a tombstone keeps references explainable. Journals and backups keep old data until their retention ends.'},
                guards={'read_set': read_set})


def _exclusion_warning(record):
    """A reply warning, so the agent that wrote the candidate adds its exclusions while it still knows them."""
    if record['content']['do_not_use_when']:
        return {}
    return dict(warnings=[NEEDS_EXCLUSIONS+'. Admission refuses it until you add one: CMD learn candidate update '
                          +record['id']+' --expected-revision '+record['revision']
                          +" --do-not-use-when 'a lookalike symptom with another cause, or a state where a step does harm'"])


def op_candidate_create(root, domain, ident, payload):
    reply = _commit(root, domain, 'candidates', _keyed(_record(domain, _candidate(root, domain, payload)), payload),
                    extra=dict(hint='Inspect eligibility: CMD learn candidate evaluate ID. The user approves it; agents never apply admission plans.'))
    return dict(reply, **_exclusion_warning(reply['record']))


def op_candidate_update(root, domain, ident, payload):
    old = get(root, domain, 'candidates', ident)
    _revision(old, payload)
    if old['state'] not in ('draft', 'deferred'):
        raise ValueError('Candidate is already finalized; create a new candidate')
    merged = {k: old.get(k) for k in ('kind', 'content', 'evidence_ids', 'project', 'procedure_id', 'tags', 'contradictions')}
    merged.update({k: v for k, v in payload.items() if k != 'expected_revision'})
    if isinstance(payload.get('content'), dict):
        # Fields not given keep their current values, so flags can change one field at a time.
        merged['content'] = dict(old['content'], **payload['content'])
    keep = ('step_evidence' not in payload and 'evidence_ids' not in payload and isinstance(merged['content'], dict)
            and isinstance(merged['content'].get('steps'), list) and len(merged['content']['steps']) == len(old['step_evidence']))
    if keep:
        merged['step_evidence'] = old['step_evidence']
    record = dict(_clean(old), **_candidate(root, domain, merged))
    if keep:
        record['step_evidence_defaulted'] = old.get('step_evidence_defaulted', False)
    reply = _commit(root, domain, 'candidates', record, old['revision'])
    return dict(reply, **_exclusion_warning(reply['record']))


def op_candidate_finalize(state):
    def op(root, domain, ident, payload):
        old = get(root, domain, 'candidates', ident)
        _revision(old, payload)
        if old['state'] not in ('draft', 'deferred'):
            raise ValueError('Candidate is already finalized')
        return _commit(root, domain, 'candidates', dict(_clean(old), state=state, reviewed_at=now(),
                                                        review_reason=str(payload.get('reason') or '')), old['revision'])
    return op


def op_candidate_evaluate(root, domain, ident, payload):
    return evaluate(root, domain, get(root, domain, 'candidates', ident))


def op_candidate_promote_plan(root, domain, ident, payload):
    c = get(root, domain, 'candidates', ident)
    if c['revision'] != payload.get('expected_revision'):
        raise ValueError('Stale revision: candidate changed')
    return _admission_plan(root, domain, c, c['revision'])


def op_procedure_state(action):
    def op(root, domain, ident, payload):
        old = get(root, domain, 'procedures', ident)
        _revision(old, payload)
        if old['state'] == 'retired':
            raise ValueError('Procedure is retired')
        if action == 'resume':
            v = get(root, domain, 'versions', old['id']+'.'+str(old['current_version']))
            c = dict(content=v['content'], evidence_ids=v['evidence_ids'], step_evidence=v['step_evidence'],
                     project=v.get('project'), procedure_id=old['id'])
            report = evaluate(root, domain, c)
            # Resume rechecks evidence; a version approved before exclusions were required still resumes.
            errors = [e for e in report['errors'] if e not in ('Learning is disabled for this domain', NEEDS_EXCLUSIONS)]
            if errors:
                raise ValueError('Procedure evidence requires review: '+'; '.join(errors))
        record = dict(_clean(old), state='suspended' if action == 'suspend' else 'active')
        record.pop('suspended_reason', None)
        if action == 'suspend':
            record['suspended_reason'] = str(payload.get('reason') or 'suspended by reviewer')
        return _commit(root, domain, 'procedures', record, old['revision'])
    return op


def op_procedure_rollback_plan(root, domain, ident, payload):
    p = get(root, domain, 'procedures', ident)
    _revision(p, payload)
    if p['state'] == 'retired':
        raise ValueError('Procedure is retired')
    v = get(root, domain, 'versions', p['id']+'.'+str(payload.get('version')))
    proposal = _record(domain, _candidate(root, domain, dict(content=v['content'], project=p.get('project'), tags=v.get('tags', []),
                                                             evidence_ids=v['evidence_ids'], step_evidence=v['step_evidence'],
                                                             procedure_id=p['id'])))
    proposal['restored_from'] = v['version']
    _safe(proposal); _validate_record('candidates', proposal)
    # The candidate is written by the plan itself, so an unapproved rollback leaves nothing behind.
    return _admission_plan(root, domain, proposal, None)


def op_procedure_retire_plan(root, domain, ident, payload):
    from .governance import plan
    from .repository import parse_text
    p = get(root, domain, 'procedures', ident)
    _revision(p, payload)
    if p['state'] == 'retired':
        raise ValueError('Procedure is already retired')
    note = parse_text(p['approved_note'])
    note.update(status='archived', updated=now())
    target = f'domains/{domain}/archive/{Path(p["note_path"]).name}'
    if confined(root, target).exists():
        raise ValueError('Archive filename collision: '+target)
    text = serialize(note)
    manifest = dict(_clean(p), state='retired', note_path=target, approved_note=text, retired_at=now(),
                    retired_reason=str(payload.get('reason') or ''))
    manifest.pop('suspended_reason', None)
    source = confined(root, p['note_path'])
    changes = {p['note_path']: None, target: text, rel(domain, 'procedures', p['id']): canonical_json(manifest)}
    read_set = {rel(domain, 'procedures', p['id']): p['revision'],
                p['note_path']: digest(source.read_bytes()) if source.exists() else None}
    return plan(root, changes, 'learning_retire', [domain], {'procedure_id': p['id'], 'title': p['title']},
                guards={'read_set': read_set})


def op_procedure_promote_plan(root, domain, ident, payload):
    """Copy a procedure's current evidence into another domain (default _global) as a draft for review there."""
    from .common import canonical_domain, load_bindings
    from .governance import plan
    p = get(root, domain, 'procedures', ident)
    _revision(p, payload)
    target = canonical_domain(str(payload.get('target') or '_global'), root)
    if target == domain:
        raise ValueError('Target domain is the procedure\'s own domain')
    if target not in load_bindings(root).get('domains', {}) and not (Path(root)/'domains'/target).is_dir():
        raise ValueError('Unknown target domain: '+target)
    v = get(root, domain, 'versions', p['id']+'.'+str(p['current_version']))
    changes, read_set = {}, {rel(domain, 'procedures', p['id']): p['revision']}
    def copy_record(kind, record):
        read_set[rel(domain, kind, record['id'])] = record['revision']
        path = rel(target, kind, record['id'])
        if confined(root, path).exists():
            return
        value = dict(_clean(record), domain=target, copied_from=f'{domain}:{record["id"]}')
        _validate_record(kind, value)
        changes[path] = canonical_json(value)
    for eid in v['evidence_ids']:
        e = get(root, domain, 'evidence', eid)
        a = get(root, domain, 'attempts', e['attempt_id'])
        copy_record('evidence', e); copy_record('attempts', a); copy_record('tasks', get(root, domain, 'tasks', a['task_id']))
    candidate = _record(target, dict(kind=v.get('kind', 'procedure'), content=v['content'], evidence_ids=v['evidence_ids'],
                                     step_evidence=v['step_evidence'], step_evidence_defaulted=False, project=v.get('project'),
                                     procedure_id=None, tags=v.get('tags', []), state='draft', contradictions=[],
                                     promoted_from={'domain': domain, 'procedure_id': p['id'], 'version': v['version']}))
    _safe(candidate); _validate_record('candidates', candidate)
    changes[rel(target, 'candidates', candidate['id'])] = canonical_json(candidate)
    return plan(root, changes, 'learning_promote', [target],
                {'procedure_id': p['id'], 'title': p['title'], 'target': target, 'candidate_id': candidate['id'],
                 'next': f'After applying, review the candidate in {target}; retire the original once the copy is approved.'},
                guards={'read_set': read_set})


def op_use_report(root, domain, ident, payload):
    p = get(root, domain, 'procedures', payload.get('procedure_id'))
    number = payload.get('version') or p['current_version']
    v = get(root, domain, 'versions', p['id']+'.'+str(number))
    if payload.get('usage') not in USAGES:
        raise ValueError('usage must be one of: '+', '.join(USAGES))
    attempt = None
    if payload.get('attempt_id'):
        attempt = get(root, domain, 'attempts', payload['attempt_id'])
        if get(root, domain, 'tasks', attempt['task_id']).get('project') != v.get('project'):
            raise ValueError('Use project differs from procedure scope')
    record = _record(domain, dict(attempt_id=attempt['id'] if attempt else None, procedure_id=p['id'], version=v['version'],
                                  usage=payload['usage'], deviations=str(payload.get('deviations') or ''),
                                  session=payload.get('session'), provenance='explicit_agent_report'))
    return _commit(root, domain, 'uses', _keyed(record, payload), extra=dict(
        hint=f'When the outcome is known: CMD learn feedback add --use {record["id"][:8]} --outcome succeeded|partial|failed'))


def op_feedback_add(root, domain, ident, payload):
    u = get(root, domain, 'uses', payload.get('use_id'))
    attempt = get(root, domain, 'attempts', u['attempt_id']) if u.get('attempt_id') else None
    if attempt:
        if attempt['state'] != 'closed':
            raise ValueError('Close the attempt before recording outcome feedback')
        outcome = attempt['outcome']
    else:
        outcome = payload.get('outcome')
        if outcome not in OUTCOMES:
            raise ValueError('This use has no attempt; give outcome: '+', '.join(OUTCOMES))
    if payload.get('corrects'):
        prior = get(root, domain, 'feedback', payload['corrects'])
        if prior['use_id'] != u['id']:
            raise ValueError('Feedback correction belongs to another use')
    contribution = payload.get('contribution', 'unknown')
    if contribution not in CONTRIBUTIONS:
        raise ValueError('contribution must be one of: '+', '.join(CONTRIBUTIONS))
    evidence = [get(root, domain, 'evidence', e)['id'] for e in _strings(payload.get('evidence_ids', []), 'evidence_ids', empty=True)]
    for eid in evidence:
        if attempt and get(root, domain, 'evidence', eid)['attempt_id'] != attempt['id']:
            raise ValueError('Feedback evidence belongs to another attempt')
    if contribution != 'unknown' and (not evidence or not payload.get('reason')):
        raise ValueError('A contribution other than unknown requires evidence and a reason')
    record = _record(domain, dict(use_id=u['id'], outcome=outcome, contribution=contribution, evidence_ids=evidence,
                                  reason=str(payload.get('reason') or ''), corrects=payload.get('corrects'),
                                  outcome_source='attempt' if attempt else 'agent_report'))
    return _commit(root, domain, 'feedback', _keyed(record, payload))


def op_task_prune_plan(root, domain, ident, payload):
    """Remove tasks (with their attempts and evidence) that nothing references, older than `days`."""
    from .governance import plan
    days = payload.get('days', 90)
    if not isinstance(days, int) or days < 1:
        raise ValueError('days must be a positive integer')
    cutoff = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    _, kept_attempts = referenced(root, domain)
    attempts = rows(root, domain, 'attempts')
    evidence = rows(root, domain, 'evidence')
    changes, read_set, pruned_attempts = {}, {}, []
    for t in rows(root, domain, 'tasks'):
        mine = [a for a in attempts if a['task_id'] == t['id']]
        if str(t['created_at']) >= cutoff or any(a['id'] in kept_attempts for a in mine):
            continue
        for kind, record in [('tasks', t)]+[('attempts', a) for a in mine]+[('evidence', e) for e in evidence if e['attempt_id'] in {a['id'] for a in mine}]:
            changes[rel(domain, kind, record['id'])] = None
            read_set[rel(domain, kind, record['id'])] = record['revision']
        pruned_attempts += [a['id'] for a in mine]
    if not changes:
        return dict(ok=True, plan_id=None, summary={'tasks': 0, 'days': days})
    return plan(root, changes, 'learning_prune', [domain],
                {'tasks': sum(1 for path in changes if '/tasks/' in path), 'files': len(changes), 'days': days},
                guards={'read_set': read_set, 'learning_unreferenced': {'domain': domain, 'attempts': pruned_attempts}})


def op_config_status(root, domain, ident, payload):
    return dict(ok=True, config=config(root, domain))


def op_config_set(root, domain, ident, payload):
    if not isinstance(payload.get('enabled'), bool):
        raise ValueError('config set requires enabled: true or false (--enable or --disable)')
    c = config(root); c.pop('effective', None)
    if payload.get('global'):
        c['enabled'] = payload['enabled']
    else:
        if not domain:
            raise ValueError('Domain required; pass --global to change every domain')
        c['domains'][domain] = payload['enabled']
    result = transaction(root, {'learning.json': canonical_json(c)}, 'learning_config')
    return dict(result, config=config(root, domain))


def op_read(kind):
    def op(root, domain, ident, payload):
        record = get(root, domain, kind, ident)
        result = dict(ok=True, record=record)
        if kind == 'procedures' and record['state'] == 'active':
            result['hint'] = use_hint(record['id'], record['current_version'])
        return result
    return op


GUIDE = [
    'When: after a verified fix or workflow that is likely to recur in this environment, record it, then propose a procedure for the user to review.',
    "1. CMD learn record --objective '...' --family 'area.kind' --criterion '...' --result pass --method tool_check --observer 'pytest' "
    "--excerpt 'what the check showed' --limitations 'what it did not cover' --outcome succeeded",
    "2. CMD learn candidate create --title '...' --summary '...' --use-when '...' --do-not-use-when '...' [--do-not-use-when ...] "
    "--step '...' [--step ...] --done-when '...' --fallback '...' --evidence ID",
    '   Do not use when: the cases where these steps would be wrong, such as a lookalike symptom with a different cause or a state '
    'where a step does harm. Make the fallback cover them. A candidate without one is not eligible.',
    '3. CMD learn candidate evaluate ID: eligibility, errors and warnings (fix with learn candidate update ID). The user approves '
    'in Hivemind Review, or applies promote-plan; agents never apply it.',
    '4. Hits on learned procedures name their ID: CMD learn use report --procedure ID --usage used|partially_used|ignored|inapplicable, '
    'then CMD learn feedback add --use ID --outcome ...',
    'Inspect: CMD learn ENTITY list | read ID. Controls: learn procedure suspend|resume|retire-plan|rollback-plan|promote-plan ID; learn config status|set.',
    'Methods: tool_check, user_acceptance, external_check; agent_assertion never supports admission. Full JSON payloads: --payload-file FILE or stdin. See LEARNING.md.',
]


def op_guide(root, domain, ident, payload):
    return dict(ok=True, guide=GUIDE)


KIND_OF = dict(task='tasks', attempt='attempts', evidence='evidence', candidate='candidates',
               procedure='procedures', version='versions', use='uses', feedback='feedback')
# (entity, action) -> (handler, read-only, blocked while learning is disabled, idempotency kind)
OPERATIONS = {
    ('guide', 'show'): (op_guide, True, False, None),
    ('config', 'status'): (op_config_status, True, False, None),
    ('config', 'set'): (op_config_set, False, False, None),
    ('record', 'create'): (op_record, False, True, None),
    ('task', 'create'): (op_task_create, False, True, 'tasks'),
    ('task', 'prune-plan'): (op_task_prune_plan, False, False, None),
    ('attempt', 'start'): (op_attempt_start, False, True, 'attempts'),
    ('attempt', 'close'): (op_attempt_close, False, False, None),
    ('evidence', 'add'): (op_evidence_add, False, True, 'evidence'),
    ('evidence', 'correct'): (op_evidence_correct, False, False, None),
    ('evidence', 'delete-plan'): (op_evidence_delete_plan, False, False, None),
    ('candidate', 'create'): (op_candidate_create, False, True, 'candidates'),
    ('candidate', 'update'): (op_candidate_update, False, True, None),
    ('candidate', 'reject'): (op_candidate_finalize('rejected'), False, False, None),
    ('candidate', 'defer'): (op_candidate_finalize('deferred'), False, False, None),
    ('candidate', 'evaluate'): (op_candidate_evaluate, True, False, None),
    ('candidate', 'promote-plan'): (op_candidate_promote_plan, False, True, None),
    ('procedure', 'suspend'): (op_procedure_state('suspend'), False, False, None),
    ('procedure', 'resume'): (op_procedure_state('resume'), False, False, None),
    ('procedure', 'capture-edit'): (capture_edit, False, True, None),
    ('procedure', 'rollback-plan'): (op_procedure_rollback_plan, False, True, None),
    ('procedure', 'retire-plan'): (op_procedure_retire_plan, False, False, None),
    ('procedure', 'promote-plan'): (op_procedure_promote_plan, False, True, None),
    ('use', 'report'): (op_use_report, False, False, 'uses'),
    ('feedback', 'add'): (op_feedback_add, False, False, 'feedback'),
}
for _entity, _kind in KIND_OF.items():
    OPERATIONS[(_entity, 'read')] = (op_read(_kind), True, False, None)
    OPERATIONS[(_entity, 'list')] = (None, True, False, None)
DEFAULT_ACTION = {None: 'show', 'guide': 'show', 'config': 'status', 'record': 'create'}
ENTITIES = ['guide', 'record', 'config']+list(KIND_OF)


def operation(entity, action):
    action = action or DEFAULT_ACTION.get(entity)
    entity = entity or 'guide'
    spec = OPERATIONS.get((entity, action))
    if not spec:
        actions = sorted(a for e, a in OPERATIONS if e == entity)
        raise ValueError(f'Unsupported learning operation: {entity} {action}. Actions for {entity}: '+', '.join(actions))
    return entity, action, spec


def is_read(entity, action):
    """True for operations viewers and subagents may run."""
    try:
        return operation(entity, action)[2][1]
    except ValueError:
        return False


# Flag name -> payload key. Content flags fill payload['content']; --criterion depends on the operation.
FLAG_KEYS = dict(objective='objective', family='family', project='project', key='idempotency_key', task='task_id',
                 attempt='attempt_id', retry_of='retry_of', result='result', method='method', observer='observer',
                 excerpt='excerpt', limitations='limitations', source='source', outcome='outcome', reason='reason',
                 expected_revision='expected_revision', procedure='procedure_id', learn_version='version', usage='usage',
                 deviations='deviations', use='use_id', contribution='contribution', evidence='evidence_ids', enabled='enabled',
                 global_='global', target='target', tag='tags', days='days', session='session', kind='kind')
CONTENT_FLAGS = dict(title='title', summary='body', use_when='use_when', do_not_use_when='do_not_use_when',
                     step='steps', done_when='done_when', fallback='fallback')


def flag_payload(entity, action, values):
    """Payload from command-line flags (values: argparse namespace as a dict). Empty when no flag was given."""
    payload, content = {}, {}
    for flag, key in FLAG_KEYS.items():
        if values.get(flag) is not None:
            payload[key] = values[flag]
    for flag, key in CONTENT_FLAGS.items():
        if values.get(flag) is not None:
            content[key] = values[flag]
    if content:
        payload['content'] = content
    criteria = values.get('criterion')
    if criteria:
        if entity == 'evidence':
            payload['criterion'] = criteria[-1]
        else:
            payload['acceptance_criteria'] = criteria
    if entity == 'record':
        check = {k: payload.pop(k) for k in ('result', 'method', 'observer', 'excerpt', 'limitations', 'source') if k in payload}
        if check:
            check.setdefault('observer', 'agent')
            criteria = payload.get('acceptance_criteria') or [None]
            payload['evidence'] = [dict(check, criterion=c) if c else dict(check) for c in criteria]
    return payload


def dispatch(root, domain, entity, action, ident=None, payload=None, all_domains=False):
    """Single contract shared by CLI, stdio bridge and Hivemind."""
    payload = payload or {}
    root = Path(root)
    entity, action, (handler, read, gated, idem_kind) = operation(entity, action)
    if not isinstance(payload, dict):
        raise ValueError('Learning payload must be a JSON object')
    with store_lock(root):
        if action == 'list':
            kind = KIND_OF[entity]
            domains = sorted(p.name for p in (root/'domains').iterdir() if p.is_dir() and DOMAIN.fullmatch(p.name)) \
                if all_domains and (root/'domains').is_dir() else [d for d in [domain] if d]
            records, bad = [], []
            for d in domains:
                good, broken = scan(root, d, kind)
                records += good; bad += broken
            result = dict(ok=True, records=records)
            if bad:
                result['problems'] = bad
            return result
        if entity not in ('guide', 'config'):
            _domain_ok(root, domain)
        if not read:
            if gated and not _enabled(root, domain):
                raise ValueError('Learning is disabled for this domain; approved procedures stay published. '
                                 'Enable it with: CMD learn config set --enable')
            _safe(payload)
            if idem_kind and payload.get('idempotency_key'):
                old = _idempotent(root, domain, idem_kind, payload)
                if old:
                    return dict(ok=True, status='existing', record=old)
        return handler(root, domain, ident, payload)


# ---------------------------------------------------------------- export / import

def export_records(root, domain, procedure_ids):
    """Full dependency closure for selected procedures; explicit export only."""
    selected = {}
    def add(kind, ident):
        if not ident or (kind, ident) in selected:
            return
        r = get(root, domain, kind, ident); selected[(kind, ident)] = _clean(r)
        if kind == 'procedures':
            for v in rows(root, domain, 'versions'):
                if v['procedure_id'] == ident: add('versions', v['id'])
        elif kind == 'versions':
            for eid in r['evidence_ids']: add('evidence', eid)
            add('candidates', r.get('source_candidate'))
        elif kind == 'candidates':
            for eid in r['evidence_ids']: add('evidence', eid)
        elif kind == 'evidence':
            add('attempts', r['attempt_id']); add('evidence', r.get('corrects')); add('evidence', r.get('corrected_by'))
        elif kind == 'attempts':
            add('tasks', r['task_id']); add('attempts', r.get('retry_of'))
        elif kind == 'uses':
            add('attempts', r.get('attempt_id'))
        elif kind == 'feedback':
            add('uses', r['use_id'])
            for eid in r['evidence_ids']: add('evidence', eid)
    for pid in procedure_ids:
        add('procedures', pid)
    for u in rows(root, domain, 'uses'):
        if u['procedure_id'] in procedure_ids: add('uses', u['id'])
    for f in rows(root, domain, 'feedback'):
        if ('uses', f['use_id']) in selected: add('feedback', f['id'])
    return {f'learning/{kind}/{ident}.json': canonical_json(value).encode() for (kind, ident), value in selected.items()}


def import_closure(records, procedure_id):
    """The bundle records one imported procedure depends on, validated: {'kind/id': record}."""
    def find(kind, ident):
        return records.get(f'learning/{kind}/{ident}.json')
    chosen = {}
    def add(kind, ident):
        r = find(kind, ident) if ident else None
        if r is None or f'{kind}/{ident}' in chosen:
            return
        if not isinstance(r, dict) or r.get('id') != ident:
            raise ValueError(f'Bundle learning record is malformed: {kind}/{ident}')
        _validate_record(kind, r)
        chosen[f'{kind}/{ident}'] = r
        if kind == 'versions':
            for eid in r['evidence_ids']: add('evidence', eid)
        elif kind == 'evidence':
            add('attempts', r['attempt_id'])
        elif kind == 'attempts':
            add('tasks', r['task_id'])
    for name, r in records.items():
        if name.startswith('learning/versions/') and isinstance(r, dict) and r.get('procedure_id') == procedure_id:
            add('versions', r['id'])
    return chosen
