"""Explicit, reviewed procedural learning. JSON is canonical; indexes are disposable.

No model calls, transcript collection, implicit success inference or ranking changes.
"""
from __future__ import annotations
import hashlib
import re
import uuid
from pathlib import Path
from .repository import (canonical_json, confined, digest, read_json, now,
                         store_lock, transaction, serialize)

KINDS = ('tasks', 'attempts', 'evidence', 'candidates', 'procedures', 'versions', 'uses', 'feedback')
READS = {'read', 'list', 'evaluate', 'status'}
OUTCOMES = {'succeeded', 'partial', 'failed', 'abandoned', 'unknown'}
USAGES = {'selected', 'used', 'partially_used', 'ignored', 'inapplicable'}
ID = re.compile(r'[a-f0-9-]{8,64}(?:\.[1-9][0-9]*)?\Z')
DOMAIN = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]*\Z')


def rel(domain, kind, ident):
    if not DOMAIN.fullmatch(domain or '') or kind not in KINDS or not ID.fullmatch(ident or ''):
        raise ValueError('Invalid learning reference')
    return f'domains/{domain}/learning/{kind}/{ident}.json'


def get(root, domain, kind, ident):
    path = confined(root, rel(domain, kind, ident))
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError('Missing learning record: '+kind+'/'+str(ident))
    if value.get('schema') != 1 or value.get('domain') != domain or value.get('id') != ident:
        raise ValueError('Invalid learning record: '+kind+'/'+str(ident))
    _validate_record(kind,value)
    return dict(value, revision=digest(path.read_bytes()))


def rows(root, domain, kind):
    folder = confined(root, f'domains/{domain}/learning/{kind}')
    return [get(root, domain, kind, p.stem) for p in sorted(folder.glob('*.json'))] if folder.exists() else []


def config(root, domain=None):
    value = read_json(Path(root)/'learning.json', {'schema': 1, 'enabled': True, 'domains': {}})
    if not isinstance(value.get('enabled'), bool) or not isinstance(value.get('domains'), dict) or any(not isinstance(v, bool) for v in value['domains'].values()):
        raise ValueError('Invalid learning configuration')
    return dict(value, effective=value['enabled'] and value['domains'].get(domain, True))


def _clean(value):
    return {k: v for k, v in value.items() if k != 'revision'}


def _text(value, name, maximum=16000):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > maximum:
        raise ValueError('Invalid '+name)
    return value.strip()


def _strings(value, name, empty=False):
    if not isinstance(value, list) or (not value and not empty) or len(value) > 100:
        raise ValueError('Invalid '+name)
    return [_text(v, name) for v in value]


def _safe(value):
    from .repository import find_secret
    if find_secret(canonical_json(value)):
        raise ValueError('Credential-looking content rejected before learning persistence')


def _validate_record(kind,value):
    """Reject malformed canonical records before they can influence retrieval."""
    try:
        if kind=='tasks':
            _text(value['objective'],'objective');_text(value['family'],'family',200)
            _strings(value['acceptance_criteria'],'acceptance_criteria')
        elif kind=='attempts':
            if value['outcome'] not in OUTCOMES or value['state'] not in ('open','closed') or not ID.fullmatch(value['task_id']) or not isinstance(value['environment'],dict):raise ValueError('Invalid attempt')
        elif kind=='evidence':
            _text(value['excerpt'],'excerpt',8192);_text(value['criterion'],'criterion')
            if value['result'] not in ('pass','fail','unknown') or value['method'] not in ('tool_check','user_acceptance','external_check','agent_assertion'):raise ValueError('Invalid evidence')
            if value['content_hash']!=digest(value['excerpt'].encode()):raise ValueError('Evidence content hash mismatch')
        elif kind in ('candidates','versions'):
            _content(value['content']);_strings(value['evidence_ids'],'evidence_ids',empty=True)
            refs=value['step_evidence']
            if not isinstance(refs,list) or len(refs)!=len(value['content']['steps']):raise ValueError('Invalid step evidence')
            for row in refs:_strings(row,'step_evidence',empty=True)
            if kind=='candidates' and value['state'] not in ('draft','deferred','admitted','rejected'):raise ValueError('Invalid candidate state')
            if kind=='versions' and value['content_hash']!=digest(canonical_json(value['content']).encode()):raise ValueError('Procedure content hash mismatch')
        elif kind=='procedures':
            if value['state'] not in ('active','suspended') or not isinstance(value['current_version'],int) or value['current_version']<1:raise ValueError('Invalid procedure')
            prefix=f'domains/{value["domain"]}/notes/'
            if not value['note_path'].startswith(prefix) or '..' in Path(value['note_path']).parts:raise ValueError('Invalid procedure note path')
            from .repository import parse_text
            n=parse_text(value['approved_note'])
            if not n or n['id']!=value['note_id'] or n.get('learning_procedure')!=value['id'] or n.get('learning_version')!=value['current_version']:raise ValueError('Invalid approved snapshot')
        elif kind=='uses':
            if value['usage'] not in USAGES or not isinstance(value['version'],int) or value['version']<1:raise ValueError('Invalid use')
        elif kind=='feedback':
            if value['outcome'] not in OUTCOMES or value['contribution'] not in ('helpful','harmful','neutral','unknown'):raise ValueError('Invalid feedback')
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError('Invalid '+kind+' learning record') from exc


def _record(domain, payload):
    return dict(schema=1, id=uuid.uuid4().hex, domain=domain, created_at=now(), **payload)


def _commit(root, domain, kind, value, expected=None, key=None, request=None):
    value = _clean(value)
    if key:
        key = _text(key, 'idempotency_key', 200)
        fingerprint = digest(canonical_json(request).encode())
        for old in rows(root, domain, kind):
            if old.get('idempotency_key') == key:
                if old.get('request_hash') != fingerprint:
                    raise ValueError('Idempotency conflict: payload differs')
                return dict(ok=True, status='existing', record=old)
        value.update(idempotency_key=key, request_hash=fingerprint)
    _safe(value)
    _validate_record(kind,value)
    path = rel(domain, kind, value['id'])
    transaction(root, {path: canonical_json(value)}, 'learning_'+kind,
                {path: expected}, operation_id=None)
    return dict(ok=True, status='saved', record=get(root, domain, kind, value['id']))


def _content(payload):
    fields = ('title', 'body', 'use_when', 'do_not_use_when', 'steps', 'done_when', 'fallback')
    result = {k: payload.get(k) for k in fields}
    for field in ('title', 'body', 'fallback'):
        result[field] = _text(result[field], field)
    for field in ('use_when', 'do_not_use_when', 'steps', 'done_when'):
        result[field] = _strings(result[field], field, empty=field == 'do_not_use_when')
    return result


def body(content):
    from .note_format import render
    details = ['**Use when:** '+ '; '.join(content['use_when']),
               '**Do not use when:** '+('; '.join(content['do_not_use_when']) or 'No additional exclusions stated.'),
               '**Procedure:**\n'+ '\n'.join(f'{i+1}. {step}' for i, step in enumerate(content['steps'])),
               '**Done when:** '+ '; '.join(content['done_when']),
               '**Fallback:** '+content['fallback']]
    return render(content['body'], details)


def evaluate(root, domain, candidate):
    errors, warnings, dependencies = [], [], {}
    evidence = []
    for ident in candidate['evidence_ids']:
        e = get(root, domain, 'evidence', ident)
        evidence.append(e)
        dependencies[rel(domain, 'evidence', ident)] = e['revision']
    attempts = {}
    for e in evidence:
        a = get(root, domain, 'attempts', e['attempt_id'])
        attempts[a['id']] = a
        dependencies[rel(domain, 'attempts', a['id'])] = a['revision']
    families, tasks, task_families = set(), set(), set()
    for a in attempts.values():
        t = get(root, domain, 'tasks', a['task_id'])
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
    if len(tasks) < 2:
        warnings.append('Provisional: only one distinct supporting task; no causal benefit established')
    # Similarity is an inspection lead, never an automatic merge decision.
    from .repository import inventory
    matches = [dict(id=n['id'], title=n['title']) for n in inventory(domain, root)
               if n['title'].casefold() == candidate['content']['title'].casefold()
               and n.get('learning_procedure') != candidate.get('procedure_id')]
    if matches:
        errors.append('Existing note has this title; reconcile it explicitly')
    return dict(ok=True, eligible=not errors, errors=errors, warnings=warnings, matches=matches,
                supporting_tasks=len(tasks), attempt_families=len(families), dependencies=dependencies)


def _candidate(root, domain, payload):
    content = _content(payload['content'])
    ids = _strings(payload.get('evidence_ids', []), 'evidence_ids', empty=True)
    step_evidence = payload.get('step_evidence', [[] for _ in content['steps']])
    if not isinstance(step_evidence, list) or len(step_evidence) != len(content['steps']):
        raise ValueError('step_evidence must match procedure steps')
    step_evidence = [_strings(v, 'step_evidence', empty=True) for v in step_evidence]
    project = payload.get('project')
    if project is not None:
        from .notes import taxonomy
        if not isinstance(project, str) or 'project/'+project not in taxonomy(root):
            raise ValueError('Unknown project scope')
    for ident in ids:
        get(root, domain, 'evidence', ident)
    procedure_id = payload.get('procedure_id')
    if procedure_id:
        current = get(root, domain, 'procedures', procedure_id)
        if project != current.get('project'):
            raise ValueError('Procedure revisions cannot change project scope')
    return dict(content=content, evidence_ids=list(dict.fromkeys(ids)), step_evidence=step_evidence,
                project=project, procedure_id=procedure_id, state='draft',
                contradictions=_strings(payload.get('contradictions', []), 'contradictions', empty=True))


def promote_plan(root, domain, candidate_id, expected_revision):
    from .governance import plan
    from .notes import build_note, taxonomy
    c = get(root, domain, 'candidates', candidate_id)
    if c['revision'] != expected_revision:
        raise ValueError('Stale revision: candidate changed')
    if c['state'] not in ('draft', 'deferred'):
        raise ValueError('Candidate is not awaiting review')
    report = evaluate(root, domain, c)
    if not report['eligible']:
        raise ValueError('Candidate not eligible: '+'; '.join(report['errors']))
    pid = c.get('procedure_id') or uuid.uuid4().hex
    previous = get(root, domain, 'procedures', pid) if c.get('procedure_id') else None
    number = previous['current_version']+1 if previous else 1
    vid = pid+'.'+str(number)
    version = dict(schema=1, id=vid, domain=domain, created_at=now(), procedure_id=pid,
                   version=number, content=c['content'], evidence_ids=c['evidence_ids'],
                   step_evidence=c['step_evidence'], project=c.get('project'),
                   source_candidate=c['id'], maturity='corroborated' if report['supporting_tasks'] > 1 else 'provisional')
    version['content_hash'] = digest(canonical_json(version['content']).encode())
    if c.get('restored_from') is not None:version['restored_from']=c['restored_from']
    tags = ['subject/project', 'concern/workflow']
    if c.get('project'): tags.append('project/'+c['project'])
    if not set(tags) <= taxonomy(root):
        raise ValueError('Runbook requires registered subject/project and concern/workflow tags')
    expected = dict(report['dependencies'])
    expected[rel(domain, 'candidates', c['id'])] = c['revision']
    if previous:
        expected[rel(domain, 'procedures', pid)] = previous['revision']
        note_path = confined(root, previous['note_path'])
        from .repository import parse
        note = parse(note_path)
        if not note: raise ValueError('Procedure note is missing or malformed')
        expected[previous['note_path']] = digest(note_path.read_bytes())
        note.update(title=c['content']['title'], body=body(c['content']), updated=now())
    else:
        note_path, note, _ = build_note(c['content']['title'], body(c['content']), tags,
                                       domain, root, note_type='runbook')
    note.update(learning_procedure=pid, learning_version=number)
    note_text = serialize(note)
    note_rel = Path(note_path).relative_to(root).as_posix()
    manifest = dict(schema=1, id=pid, domain=domain, created_at=previous['created_at'] if previous else now(),
                    current_version=number, title=c['content']['title'], project=c.get('project'), state='active', note_id=note['id'],
                    note_path=note_rel, approved_note=note_text)
    changes = {rel(domain, 'versions', vid): canonical_json(version),
               rel(domain, 'procedures', pid): canonical_json(manifest), note_rel: note_text,
               rel(domain, 'candidates', c['id']): canonical_json(dict(_clean(c), state='admitted', procedure_id=pid, admitted_version=number))}
    # A global/domain disable between review and approval also makes the review stale.
    for path in ('learning.json',):
        p = Path(root)/path; expected[path] = digest(p.read_bytes()) if p.exists() else None
    p = Path(root)/'tag_taxonomy.json'; expected['tag_taxonomy.json'] = digest(p.read_bytes())
    return plan(root, changes, 'learning_admit', [domain],
                dict(candidate_id=c['id'], procedure_id=pid, version=number, **{k: report[k] for k in ('warnings', 'supporting_tasks', 'attempt_families')}),
                guards={'read_set': expected, 'learning_admission':{'domain':domain,'title':c['content']['title'],'procedure_id':pid}})


def supported(root,domain,procedure):
    try:
        version=get(root,domain,'versions',procedure['id']+'.'+str(procedure['current_version']))
        return bool(version['evidence_ids']) and all(not get(root,domain,'evidence',ident).get('corrected_by') for ident in version['evidence_ids'])
    except (ValueError,OSError,KeyError):return False


def approved_notes(root, domain, notes, actionable=False):
    """Serve approved bytes even if an external editor changed or removed metadata."""
    folder = Path(root)/'domains'/domain/'learning'/'procedures'
    if not folder.exists(): return notes
    from .repository import parse_text
    try:
        manifests = {p['note_id']: p for p in rows(root, domain, 'procedures')}
        enabled = config(root, domain)['effective']
    except (ValueError,OSError,KeyError):
        # Broken learning state must not make unrelated canonical facts unusable.
        # Runbooks fail closed until doctor/restore repairs their authority data.
        return [n for n in notes if not n.get('learning_procedure') and (not actionable or n.get('type')!='runbook')]
    output = []
    for note in notes:
        p = manifests.get(note['id'])
        if not p:
            # Imported copies cannot impersonate an approved procedure.
            if note.get('learning_procedure'): continue
            output.append(note); continue
        evidence_ok=supported(root,domain,p)
        if actionable and (not enabled or p['state'] != 'active' or not evidence_ok):
            continue
        approved = parse_text(p['approved_note'], note['path'])
        approved.update(revision=note['revision'], learning_state=p['state'] if evidence_ok else 'needs_review', status=note.get('status','active'),
                        learning_pending_edit=serialize(note) != p['approved_note'])
        output.append(approved)
    return output


def stage_edit(root, domain, note, values):
    """Called by ordinary update; the published note stays intact."""
    pid = note.get('learning_procedure')
    if not pid: return None
    if set(values) - {'title', 'body'}:
        raise ValueError('Learned runbook edits accept title/body; lifecycle changes use learn procedure')
    p = get(root, domain, 'procedures', pid)
    v = get(root, domain, 'versions', pid+'.'+str(p['current_version']))
    content = dict(v['content'])
    if 'title' in values:content['title']=values['title']
    if 'body' in values:
        from .note_format import sections
        parts=sections(values['body'])
        content['body']=parts.get('summary',values['body'])
        details=parts.get('details','')
        if details:
            labels={label: text.strip() for label,text in re.findall(r'(?ms)^\* \*\*([^*]+):\*\*\s*(.*?)(?=^\* \*\*|\Z)',details)}
            required={'Use when','Do not use when','Procedure','Done when','Fallback'}
            if set(labels)!=required:raise ValueError('Edit learned procedure fields through Review → Learning; required field labels changed')
            content['use_when']=labels['Use when'].split('; ')
            exclusion=labels['Do not use when']
            content['do_not_use_when']=[] if exclusion=='No additional exclusions stated.' else exclusion.split('; ')
            content['steps']=[re.sub(r'^\d+\.\s*','',line).strip() for line in labels['Procedure'].splitlines() if line.strip()]
            content['done_when']=labels['Done when'].split('; ')
            content['fallback']=labels['Fallback']
    refs=[v['step_evidence'][i] if i<len(v['step_evidence']) else [] for i in range(len(content['steps']))]
    payload = dict(content=content, evidence_ids=v['evidence_ids'], step_evidence=refs,
                   procedure_id=pid, project=p.get('project'))
    candidate = _record(domain, _candidate(root, domain, payload))
    result = _commit(root, domain, 'candidates', candidate)
    result.update(id=note['id'], domain=domain, status='proposed', candidate_id=candidate['id'])
    return result


def problems(root, domain):
    found = []
    for kind in KINDS:
        try:
            for record in rows(root, domain, kind):
                try:
                    if kind == 'attempts': get(root, domain, 'tasks', record['task_id'])
                    if kind == 'evidence': get(root, domain, 'attempts', record['attempt_id'])
                    if kind == 'versions':
                        for ident in record['evidence_ids']: get(root, domain, 'evidence', ident)
                    if kind == 'procedures':
                        get(root, domain, 'versions', record['id']+'.'+str(record['current_version']))
                        path = confined(root, record['note_path'])
                        if not path.exists() or path.read_text(encoding='utf-8-sig') != record['approved_note']:
                            found.append(dict(path=record['note_path'], problem='approved projection differs; stage edit with learn procedure capture-edit'))
                except (ValueError, KeyError, OSError) as exc:
                    found.append(dict(path=rel(domain, kind, record['id']), problem=str(exc)))
        except (ValueError, KeyError, OSError) as exc:
            found.append(dict(path=f'domains/{domain}/learning/{kind}', problem=str(exc)))
    return found


def dispatch(root, domain, entity, action, ident=None, payload=None, all_domains=False):
    """Single contract shared by CLI, stdio bridge and Hivemind."""
    payload = payload or {}
    root = Path(root)
    if not isinstance(payload, dict): raise ValueError('Learning payload must be an object')
    with store_lock(root):
        if entity == 'config':
            if action == 'status': return dict(ok=True, config=config(root, domain))
            if action != 'set' or not isinstance(payload.get('enabled'), bool): raise ValueError('config set requires enabled boolean')
            c = config(root); c.pop('effective', None)
            if payload.get('global'): c['enabled'] = payload['enabled']
            else:
                if not domain: raise ValueError('Domain required')
                c['domains'][domain] = payload['enabled']
            result = transaction(root, {'learning.json': canonical_json(c)}, 'learning_config')
            return dict(result, config=config(root, domain))
        if action == 'list':
            kind = _kind(entity)
            domains = [domain] if not all_domains else [p.name for p in (root/'domains').iterdir() if p.is_dir() and DOMAIN.fullmatch(p.name)]
            values = [r for d in domains if d for r in rows(root, d, kind)]
            return dict(ok=True, records=values)
        if not domain or not DOMAIN.fullmatch(domain): raise ValueError('Domain required')
        from .common import load_bindings
        if domain not in load_bindings(root).get('domains', {}) and not (root/'domains'/domain/'notes').exists():
            raise ValueError('Unknown learning domain')
        kind = _kind(entity)
        if action == 'read': return dict(ok=True, record=get(root, domain, kind, ident))
        if entity == 'candidate' and action == 'evaluate': return evaluate(root, domain, get(root, domain, kind, ident))
        if not config(root, domain)['effective'] and action not in ('suspend', 'resume'):
            raise ValueError('Learning is disabled for this domain')
        _safe(payload)
        key = payload.get('idempotency_key')
        if key and action in ('create','start','add','report'):
            fingerprint=digest(canonical_json(payload).encode())
            for old in rows(root,domain,kind):
                if old.get('idempotency_key')==key:
                    if old.get('request_hash')!=fingerprint:raise ValueError('Idempotency conflict: payload differs')
                    return dict(ok=True,status='existing',record=old)
        if entity == 'task' and action == 'create':
            if payload.get('project'):
                from .notes import taxonomy
                if 'project/'+str(payload['project']) not in taxonomy(root):raise ValueError('Unknown project scope')
            record = _record(domain, dict(objective=_text(payload.get('objective'), 'objective'),
                      acceptance_criteria=_strings(payload.get('acceptance_criteria'), 'acceptance_criteria'),
                      family=_text(payload.get('family'), 'family', 200), project=payload.get('project')))
        elif entity == 'attempt' and action == 'start':
            if not isinstance(payload.get('environment',{}),dict):raise ValueError('Environment must be an object')
            t = get(root, domain, 'tasks', payload['task_id'])
            previous = get(root, domain, 'attempts', payload['retry_of']) if payload.get('retry_of') else None
            if previous and previous['task_id'] != t['id']: raise ValueError('Retry belongs to another task')
            origin=payload.get('source_origin_id')
            if origin:
                origin=_text(origin,'source_origin_id',200)
                existing=next((a for a in rows(root,domain,kind) if a.get('source_origin_id')==origin),None)
                if existing:
                    if existing['task_id']!=t['id']:raise ValueError('Origin conflict: imported attempt belongs to another task')
                    return dict(ok=True,status='existing',record=existing)
            record = _record(domain, dict(task_id=t['id'], attempt_family_id=previous['attempt_family_id'] if previous else uuid.uuid4().hex,
                      retry_of=payload.get('retry_of'), source_origin_id=origin, environment=payload.get('environment', {}),
                      session_refs=_strings(payload.get('session_refs',[]),'session_refs',empty=True), outcome='unknown', state='open'))
        elif entity == 'evidence' and action in ('add', 'correct'):
            a = get(root, domain, 'attempts', payload['attempt_id'])
            t = get(root, domain, 'tasks', a['task_id'])
            if payload.get('criterion') not in t['acceptance_criteria']: raise ValueError('Unknown acceptance criterion')
            if payload.get('result') not in ('pass', 'fail', 'unknown'): raise ValueError('Invalid evidence result')
            if payload.get('method') not in ('tool_check', 'user_acceptance', 'external_check', 'agent_assertion'): raise ValueError('Invalid verification method')
            excerpt = _text(payload.get('excerpt'), 'excerpt', 8192)
            if sum(len(e['excerpt'].encode()) for e in rows(root, domain, 'evidence') if e['attempt_id'] == a['id'])+len(excerpt.encode()) > 65536:
                raise ValueError('Attempt evidence exceeds 64 KiB')
            record = _record(domain, dict(attempt_id=a['id'], criterion=payload['criterion'], result=payload['result'],
                      method=payload['method'], observer=_text(payload.get('observer'), 'observer'), excerpt=excerpt,
                      source=payload.get('source'), limitations=_text(payload.get('limitations'), 'limitations'),
                      content_hash=digest(excerpt.encode())))
            if action == 'correct':
                old = get(root, domain, 'evidence', ident)
                if old['revision'] != payload.get('expected_revision'): raise ValueError('Stale revision: evidence changed')
                if old['attempt_id'] != a['id']: raise ValueError('Correction belongs to another attempt')
                record['corrects'] = ident
                changes = {rel(domain, kind, ident): canonical_json(dict(_clean(old), corrected_by=record['id'])),
                           rel(domain, kind, record['id']): canonical_json(record)}
                for p in rows(root, domain, 'procedures'):
                    v = get(root, domain, 'versions', p['id']+'.'+str(p['current_version']))
                    if ident in v['evidence_ids']: changes[rel(domain, 'procedures', p['id'])] = canonical_json(dict(_clean(p), state='suspended'))
                return transaction(root, changes, 'learning_evidence_correct', {rel(domain, kind, ident): old['revision']})
        elif entity == 'evidence' and action == 'delete-plan':
            from .governance import plan
            old=get(root,domain,kind,ident)
            if old['revision']!=payload.get('expected_revision'):raise ValueError('Stale revision: evidence changed')
            changes={rel(domain,kind,ident):None}
            for p in rows(root,domain,'procedures'):
                v=get(root,domain,'versions',p['id']+'.'+str(p['current_version']))
                if ident in v['evidence_ids']:changes[rel(domain,'procedures',p['id'])]=canonical_json(dict(_clean(p),state='suspended'))
            return plan(root,changes,'learning_evidence_delete',[domain],{'evidence_id':ident,'warning':'Dependent procedures are suspended; history retains unavailable evidence references.'},guards={'read_set':{rel(domain,kind,ident):old['revision']}})
        elif entity == 'candidate' and action == 'create':
            record = _record(domain, _candidate(root, domain, payload))
        elif entity == 'use' and action == 'report':
            a = get(root, domain, 'attempts', payload['attempt_id'])
            v = get(root, domain, 'versions', payload['procedure_id']+'.'+str(payload['version']))
            t = get(root, domain, 'tasks', a['task_id'])
            if t.get('project') != v.get('project'): raise ValueError('Use project differs from procedure scope')
            if payload.get('usage') not in USAGES: raise ValueError('Invalid procedure usage')
            record = _record(domain, dict(attempt_id=a['id'], procedure_id=v['procedure_id'], version=v['version'],
                      usage=payload['usage'], deviations=payload.get('deviations', ''), provenance='explicit_agent_report'))
        elif entity == 'feedback' and action == 'add':
            u = get(root, domain, 'uses', payload['use_id'])
            a = get(root, domain, 'attempts', u['attempt_id'])
            if a['state'] != 'closed': raise ValueError('Close the attempt before recording outcome feedback')
            if payload.get('corrects'):
                prior=get(root,domain,'feedback',payload['corrects'])
                if prior['use_id']!=u['id']:raise ValueError('Feedback correction belongs to another use')
            contribution = payload.get('contribution', 'unknown')
            if contribution not in ('helpful', 'harmful', 'neutral', 'unknown'): raise ValueError('Invalid contribution')
            evidence = payload.get('evidence_ids', [])
            for eid in evidence:
                if get(root, domain, 'evidence', eid)['attempt_id'] != a['id']: raise ValueError('Feedback evidence belongs to another attempt')
            if contribution != 'unknown' and (not evidence or not payload.get('reason')): raise ValueError('Contribution requires evidence and reason')
            record = _record(domain, dict(use_id=u['id'], outcome=a['outcome'], contribution=contribution,
                      evidence_ids=evidence, reason=payload.get('reason', ''), corrects=payload.get('corrects')))
        elif entity == 'candidate' and action == 'promote-plan':
            return promote_plan(root, domain, ident, payload.get('expected_revision'))
        elif entity == 'procedure' and action == 'capture-edit':
            p = get(root, domain, kind, ident)
            if p['revision'] != payload.get('expected_revision'):raise ValueError('Stale revision: procedure changed')
            path = confined(root, p['note_path'])
            from .repository import parse
            raw = parse(path)
            if not raw: raise ValueError('Cannot stage malformed note; restore approved version first')
            raw['learning_procedure'] = p['id']
            result = stage_edit(root, domain, raw, dict(title=raw['title'], body=raw['body']))
            transaction(root, {p['note_path']: p['approved_note']}, 'learning_restore_projection', {p['note_path']: raw['revision']})
            return result
        elif entity == 'procedure' and action == 'rollback-plan':
            p = get(root, domain, kind, ident)
            if p['revision'] != payload.get('expected_revision'): raise ValueError('Stale revision: procedure changed')
            v = get(root, domain, 'versions', ident+'.'+str(payload['version']))
            proposal = _record(domain, _candidate(root, domain, dict(content=v['content'], project=p.get('project'),
                          evidence_ids=v['evidence_ids'], step_evidence=v['step_evidence'], procedure_id=ident)))
            proposal['restored_from']=v['version']
            _commit(root, domain, 'candidates', proposal)
            return promote_plan(root, domain, proposal['id'], get(root, domain, 'candidates', proposal['id'])['revision'])
        else:
            old = get(root, domain, kind, ident)
            if old['revision'] != payload.get('expected_revision'): raise ValueError('Stale revision: learning record changed')
            if entity == 'attempt' and action == 'close':
                if old['state'] != 'open' or payload.get('outcome') not in OUTCOMES: raise ValueError('Invalid attempt closure')
                record = dict(_clean(old), outcome=payload['outcome'], state='closed', closed_at=now(), reason=payload.get('reason', ''))
            elif entity == 'candidate' and action in ('update', 'reject', 'defer'):
                if old['state'] not in ('draft', 'deferred'): raise ValueError('Candidate is already finalized')
                record = dict(_clean(old), **(_candidate(root, domain, payload) if action == 'update' else {'state': 'rejected' if action == 'reject' else 'deferred'}))
            elif entity == 'procedure' and action in ('suspend', 'resume'):
                if action == 'resume':
                    v = get(root, domain, 'versions', ident+'.'+str(old['current_version']))
                    c = dict(content=v['content'], evidence_ids=v['evidence_ids'], step_evidence=v['step_evidence'], project=v.get('project'), procedure_id=ident)
                    if not evaluate(root, domain, c)['eligible']: raise ValueError('Procedure evidence requires review')
                record = dict(_clean(old), state='suspended' if action == 'suspend' else 'active')
            else: raise ValueError('Unsupported learning operation')
            return _commit(root, domain, kind, record, old['revision'])
        return _commit(root, domain, kind, record, key=key, request=payload)


def _kind(entity):
    kinds = dict(task='tasks', attempt='attempts', evidence='evidence', candidate='candidates',
                 procedure='procedures', version='versions', use='uses', feedback='feedback')
    if entity not in kinds: raise ValueError('Unknown learning entity')
    return kinds[entity]


def export_records(root, domain, procedure_ids):
    """Full dependency closure for selected procedures; explicit export only."""
    selected={}
    def add(kind,ident):
        key=(kind,ident)
        if key in selected:return
        r=get(root,domain,kind,ident);selected[key]=_clean(r)
        if kind=='procedures':
            for v in rows(root,domain,'versions'):
                if v['procedure_id']==ident:add('versions',v['id'])
        elif kind=='versions':
            for eid in r['evidence_ids']:add('evidence',eid)
            if r.get('source_candidate'):add('candidates',r['source_candidate'])
        elif kind=='candidates':
            for eid in r['evidence_ids']:add('evidence',eid)
        elif kind=='evidence':
            add('attempts',r['attempt_id'])
            if r.get('corrects'):add('evidence',r['corrects'])
            if r.get('corrected_by'):add('evidence',r['corrected_by'])
        elif kind=='attempts':
            add('tasks',r['task_id'])
            if r.get('retry_of'):add('attempts',r['retry_of'])
        elif kind=='uses':add('attempts',r['attempt_id'])
        elif kind=='feedback':
            add('uses',r['use_id'])
            for eid in r['evidence_ids']:add('evidence',eid)
    for pid in procedure_ids:add('procedures',pid)
    for u in rows(root,domain,'uses'):
        if u['procedure_id'] in procedure_ids:add('uses',u['id'])
    for f in rows(root,domain,'feedback'):
        if ('uses',f['use_id']) in selected:add('feedback',f['id'])
    return {f'learning/{kind}/{ident}.json':canonical_json(value).encode() for (kind,ident),value in selected.items()}
