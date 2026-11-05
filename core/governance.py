"""Hash-approved plans, receipts, history, and derived-state recovery."""
from __future__ import annotations
import hashlib,json,uuid
from pathlib import Path
from .common import atomic_write,memory_root
from .repository import canonical_json,confined,digest,read_json,transaction,recover,store_lock,reconcile,now

def plan(root, changes, kind, domains=(), summary=None):
    root=Path(root).resolve()
    with store_lock(root):
        expected={}
        for rel in changes:
            p=confined(root,rel)
            expected[rel]=digest(p.read_bytes()) if p.exists() else None
        value={'schema':2,'id':uuid.uuid4().hex,'kind':kind,'domains':sorted(set(domains)),
               'created':now(),'changes':changes,'expected':expected,'summary':summary or {'files':len(changes)}}
        value['digest']=hashlib.sha256(canonical_json(value).encode()).hexdigest()
        path=root/'.plans'/(value['id']+'.json');atomic_write(path,canonical_json(value))
        return {'ok':True,'plan_id':value['id'],'digest':value['digest'],'kind':kind,'summary':value['summary']}

def apply(root,plan_id,expected_digest,sync_overlay=True):
    root=Path(root).resolve()
    if not isinstance(plan_id,str) or not __import__('re').fullmatch(r'[a-f0-9]{32}',plan_id):raise ValueError('Invalid plan identity')
    with store_lock(root):
        value=read_json(root/'.plans'/(plan_id+'.json'))
        if not value:raise ValueError('Plan unavailable')
        unsigned={k:v for k,v in value.items() if k!='digest'}
        actual=hashlib.sha256(canonical_json(unsigned).encode()).hexdigest()
        if value['digest']!=actual or actual!=expected_digest:raise ValueError('Plan digest mismatch')
        receipt=transaction(root,value['changes'],value['kind'],value['expected'],value['id'])
        for domain in value['domains']:
            try:reconcile(domain,root,sync_overlay=sync_overlay)
            except Exception:receipt.setdefault('warnings',[]).append(f'{domain}: derived state requires repair')
        return receipt

def history(root,note_id=None,limit=20):
    root=Path(root).resolve();rows=[]
    for path in (root/'.operations').glob('*/journal.json'):
        value=read_json(path)
        if note_id:
            import base64
            # Sync bookkeeping mentions every note; only real note changes belong in a note's history.
            effects=[e for e in value['effects'] if not e['path'].endswith('sync-state.json') and '/staged/' not in e['path']]
            if not any(note_id.encode() in base64.b64decode(e[s]) for e in effects for s in ('before','after') if e[s]):continue
        rows.append({'operation_id':value['id'],'kind':value['kind'],'state':value['state'],'created':value['created'],'files':[e['path'] for e in value['effects']]})
    return sorted(rows,key=lambda r:r['created'],reverse=True)[:limit]

def inspect_plan(root,identity):
    import re,difflib
    if not re.fullmatch('[a-f0-9]{32}',identity):raise ValueError('Invalid plan identity')
    root=Path(root);value=read_json(root/'.plans'/(identity+'.json'))
    if not value:raise ValueError('Plan unavailable')
    effects=[]
    for rel,after in value['changes'].items():
        path=confined(root,rel);before=path.read_text(encoding='utf-8') if path.exists() else ''
        delta=''.join(difflib.unified_diff(before.splitlines(True),(after or '').splitlines(True),fromfile=rel,tofile=rel))
        effects.append({'path':rel,'expected_revision':value['expected'][rel],'action':'delete' if after is None else 'write','diff':delta[:16000],'truncated':len(delta)>16000})
    return {'ok':True,'plan_id':identity,'digest':value['digest'],'kind':value['kind'],'summary':value['summary'],'effects':effects}

def merge_plan(root,payload):
    from .notes import find_note
    from .repository import serialize,validate
    root=Path(root).resolve();domain,target_id=payload['target'].split(':',1);sources=payload['sources']
    if not isinstance(sources,list) or not 1<=len(sources)<=20 or len(set(sources))!=len(sources):raise ValueError('Merge requires 1..20 distinct source references')
    with store_lock(root):
        target_path,target=find_note(target_id,domain,root)
        if target.get('status')!='active':raise ValueError('Merge target must be active')
        target['body']=payload['body'].strip();target['updated']=now();changes={};merged=[]
        for ref in sources:
            source_domain,identity=ref.split(':',1)
            if source_domain!=domain or identity==target_id:raise ValueError('Merge sources must be distinct notes in the target domain')
            path,note=find_note(identity,domain,root)
            if note.get('status')!='active':raise ValueError('Merge sources must be active')
            note.update(status='superseded',superseded_by=target_id,updated=now());validate(note,domain,root)
            archived=root/'domains'/domain/'archive'/path.name
            if archived.exists():raise ValueError('Merge archive collision')
            changes[path.relative_to(root).as_posix()]=None;changes[archived.relative_to(root).as_posix()]=serialize(note);merged.append(identity)
        target['supersedes']=sorted(set(target.get('supersedes',[])+merged));validate(target,domain,root)
        changes[target_path.relative_to(root).as_posix()]=serialize(target)
        return plan(root,changes,'merge',[domain],{'target':payload['target'],'sources':sources,'files':len(changes)})

def undo(root,operation_id):
    if not __import__('re').fullmatch(r'[A-Za-z0-9_-]{1,100}',operation_id):raise ValueError('Invalid operation ID')
    root=Path(root).resolve();value=read_json(root/'.operations'/operation_id/'journal.json')
    if not value:raise ValueError('Operation unavailable')
    # A plan gives the user a chance to inspect current revisions before undo.
    import base64
    # The derived-state marker is rewritten by every reconcile; it is not part of what is being undone.
    value['effects']=[e for e in value['effects'] if Path(e['path']).name!='.derived-dirty']
    changes={e['path']:base64.b64decode(e['before']).decode() if e['before'] is not None else None for e in value['effects']}
    domains={Path(p).parts[1] for p in changes if Path(p).parts[0]=='domains'}
    for effect in value['effects']:
        p=confined(root,effect['path']);current=digest(p.read_bytes()) if p.exists() else None
        if current!=effect['after_hash']:raise ValueError('Undo conflicts with later edits')
    return plan(root,changes,'undo:'+operation_id,domains)

def remap(root,mappings):
    from .notes import find_note
    from .repository import serialize
    root=Path(root).resolve();changes={};sources=set();targets=set();domains=set()
    with store_lock(root):
        for mapping in mappings:
            before=mapping['before'];after=mapping['after'];a=(before['domain'],before['id']);b=(after['domain'],after['id'])
            if a==b or a in sources or b in targets:raise ValueError('Ambiguous remap or identity collision')
            sources.add(a);targets.add(b)
        if sources&targets:raise ValueError('Remap cycles are not allowed')
        for mapping in mappings:
            before,after=mapping['before'],mapping['after'];path,note=find_note(before['id'],before['domain'],root)
            if before['id']!=after['id']:raise ValueError('Stable IDs cannot change; remap changes domains only')
            try:find_note(after['id'],after['domain'],root)
            except ValueError:pass
            else:raise ValueError('Remap target already exists')
            if after['domain'] not in read_json(root/'bindings.json',{}).get('domains',{}):raise ValueError('Target domain is unregistered')
            target=confined(root,Path('domains')/after['domain']/('notes' if note['status']=='active' else 'archive')/path.name)
            if target.exists():raise ValueError('Remap filename collision')
            note['domain']=after['domain'];note['updated']=now()
            changes[path.relative_to(root).as_posix()]=None;changes[target.relative_to(root).as_posix()]=serialize(note)
            domains.update((before['domain'],after['domain']))
        return plan(root,changes,'reference_remap',domains,{'mappings':mappings,'files':len(changes)})

def retag_plan(root,mappings,domains=None):
    from .repository import inventory,serialize,FACETS
    root=Path(root).resolve();changes={};affected=[]
    if not isinstance(mappings,dict) or not mappings:raise ValueError('Provide old-tag to new-tag mappings')
    if any('/' not in tag or tag.split('/',1)[0] not in FACETS for tag in mappings.values()):raise ValueError('New tags require controlled facets')
    with store_lock(root):
        # The taxonomy is store-wide, so a retag must cover every domain directory
        # (including _global and domains no longer bound), or it strands other notes.
        every=sorted(p.name for p in (root/'domains').iterdir() if p.is_dir()) if (root/'domains').is_dir() else []
        if domains:
            outside=[d for d in every if d not in set(domains)]
            still=sorted({t for d in outside for n in inventory(d,root,retired=True) for t in n.get('tags',[]) if t in mappings})
            if still:raise ValueError('Tags are still used in other domains: '+', '.join(still)+'; run retag-plan without a domain restriction')
        for domain in sorted(domains or every):
            for note in inventory(domain,root,retired=True):
                tags=sorted({mappings.get(t,t) for t in note.get('tags',[])})
                if tags!=sorted(note.get('tags',[])):
                    note['tags']=tags;note['updated']=now();changes[Path(note['path']).relative_to(root).as_posix()]=serialize(note);affected.append(domain)
        taxonomy=read_json(root/'tag_taxonomy.json',{'tags':[]});known=taxonomy.get('tags',[]) if isinstance(taxonomy,dict) else taxonomy
        changes['tag_taxonomy.json']=canonical_json({'version':2,'facets':list(FACETS),'tags':sorted({mappings.get(t,t) for t in known}|set(mappings.values()))})
        return plan(root,changes,'bulk_retag',affected,{'files':len(changes),'mappings':mappings})
