"""Hash-approved plans, receipts, history, and derived-state recovery."""
from __future__ import annotations
import hashlib,json,uuid
from pathlib import Path
from .common import atomic_write,memory_root
from .repository import canonical_json,confined,digest,read_json,transaction,recover,store_lock,reconcile,now

def plan(root, changes, kind, domains=(), summary=None, guards=None):
    """guards: store-wide conditions that per-file hashes cannot express, re-checked at apply:
    absent_ids (no note in the plan's domains has these IDs) and unused_tags (no note outside the plan carries them)."""
    root=Path(root).resolve()
    with store_lock(root):
        expected={}
        for rel in changes:
            p=confined(root,rel)
            expected[rel]=digest(p.read_bytes()) if p.exists() else None
        value={'schema':2,'id':uuid.uuid4().hex,'kind':kind,'domains':sorted(set(domains)),
               'created':now(),'changes':changes,'expected':expected,'summary':summary or {'files':len(changes)}}
        if guards:value['guards']={k:(v if k in ('read_set','learning_admission','learning_unreferenced') else sorted(set(v))) for k,v in guards.items() if v}
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
        if not (root/'.operations'/plan_id/'journal.json').exists():_check_guards(root,value)
        receipt=transaction(root,value['changes'],value['kind'],value['expected'],value['id'])
        for domain in value['domains']:
            try:reconcile(domain,root,sync_overlay=sync_overlay)
            except Exception as exc:
                from .logs import swallowed
                swallowed('governance.reconcile',exc,root)
                receipt.setdefault('warnings',[]).append(f'{domain}: derived state requires repair')
        return receipt

def _check_guards(root,value):
    """Fail as stale when the store changed in a way the plan's file hashes cannot see."""
    from .repository import inventory
    guards=value.get('guards') or {}
    if not guards:return
    for rel,expected in guards.get('read_set',{}).items():
        path=confined(root,rel)
        if (digest(path.read_bytes()) if path.exists() else None)!=expected:
            raise ValueError('stale plan: a file this plan depends on changed: '+rel)
    admission=guards.get('learning_admission')
    if admission:
        for note in inventory(admission['domain'],root,retired=True):
            if note['title'].casefold()==admission['title'].casefold() and note.get('learning_procedure')!=admission['procedure_id']:
                raise ValueError('stale plan: another note now has this procedure title')
    for domain in guards.get('learning_enabled',[]):
        from .learning import _enabled
        if not _enabled(root,domain):raise ValueError('stale plan: learning is disabled for '+domain)
    unreferenced=guards.get('learning_unreferenced')
    if unreferenced:
        from .learning import referenced
        if set(unreferenced['attempts'])&referenced(root,unreferenced['domain'])[1]:
            raise ValueError('stale plan: a pruned learning record is referenced again')
    for item in guards.get('absent_learning_imports',[]):
        domain,key=item.split(':',1)
        from .learning import rows
        if any(r.get('import_key')==key for r in rows(root,domain,'candidates')):
            raise ValueError('stale plan: learning bundle already imported')
    absent=set(guards.get('absent_ids',[]));unused=set(guards.get('unused_tags',[]))
    folders=sorted(p.name for p in (root/'domains').iterdir() if p.is_dir()) if (root/'domains').is_dir() else []
    for domain in folders:
        if unused or domain in value['domains']:
            for note in inventory(domain,root,retired=True):
                rel=Path(note['path']).relative_to(root).as_posix()
                if absent and domain in value['domains'] and note['id'] in absent and rel not in value['changes']:
                    raise ValueError(f"stale plan: {domain}:{note['id']} was added since the plan was made; make a new plan")
                if unused and rel not in value['changes'] and unused&set(note.get('tags',[])):
                    raise ValueError(f"stale plan: {domain}:{note['id']} now uses {', '.join(sorted(unused&set(note['tags'])))}, which this plan removes; make a new plan")

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
    from .repository import serialize,validate,inventory
    from .migration import rewrite_type_links
    root=Path(root).resolve();domain,target_id=payload['target'].split(':',1);sources=payload['sources']
    if not isinstance(sources,list) or not 1<=len(sources)<=20 or len(set(sources))!=len(sources):raise ValueError('Merge requires 1..20 distinct source references')
    with store_lock(root):
        target_path,target=find_note(target_id,domain,root)
        if target.get('learning_procedure'):raise ValueError('Learned procedure changes require candidate review')
        if target.get('status')!='active':raise ValueError('Merge target must be active')
        target['body']=payload['body'].strip();target['updated']=now();changes={};merged=[];moves={}
        for ref in sources:
            source_domain,identity=ref.split(':',1)
            if source_domain!=domain:raise ValueError('Merge sources must be distinct notes in the target domain')
            path,note=find_note(identity,domain,root)
            if note.get('learning_procedure'):raise ValueError('Learned procedures cannot be merged without candidate review')
            # Compare resolved notes, not spellings: a full ID and its short prefix are the same note.
            if note['id']==target['id'] or note['id'] in merged:raise ValueError('Merge sources must be distinct notes in the target domain')
            if note.get('status')!='active':raise ValueError('Merge sources must be active')
            archived=root/'domains'/domain/'archive'/path.name
            if archived.exists():raise ValueError('Merge archive collision')
            merged.append(note['id']);moves[path.resolve()]=(path,archived,note)
        targets={key:archived for key,(_,archived,_) in moves.items()}
        for path,archived,note in moves.values():
            # Moving to archive/: the note's own relative links, and links between moved sources, still resolve.
            note['body']=rewrite_type_links(note['body'],archived,targets,moved_from=path)
            note.update(status='superseded',superseded_by=target['id'],updated=now());validate(note,domain,root)
            changes[path.relative_to(root).as_posix()]=None;changes[archived.relative_to(root).as_posix()]=serialize(note)
        target['body']=rewrite_type_links(target['body'],target_path,targets)
        target['supersedes']=sorted(set(target.get('supersedes',[])+merged));validate(target,domain,root)
        changes[target_path.relative_to(root).as_posix()]=serialize(target)
        for referring in inventory(domain,root,retired=True):
            # Links from other notes follow the sources into archive/.
            if referring['id']==target['id'] or Path(referring['path']).resolve() in moves:continue
            new=rewrite_type_links(referring['body'],Path(referring['path']),targets)
            if new!=referring['body']:
                referring['body']=new;referring['updated']=now()
                changes[Path(referring['path']).relative_to(root).as_posix()]=serialize(referring)
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
    # Check and record under one lock: an edit between the check and the plan's snapshot would be overwritten.
    with store_lock(root):
        for effect in value['effects']:
            p=confined(root,effect['path']);current=digest(p.read_bytes()) if p.exists() else None
            if current!=effect['after_hash']:raise ValueError('Undo conflicts with later edits')
        # Restoring an older taxonomy drops tags; notes tagged since (outside this undo) would then fail validation.
        guards=None
        if 'tag_taxonomy.json' in changes:
            tags_of=lambda t:set((t.get('tags',[]) if isinstance(t,dict) else t) or [])
            dropped=tags_of(read_json(root/'tag_taxonomy.json',{'tags':[]}))-tags_of(json.loads(changes['tag_taxonomy.json']) if changes['tag_taxonomy.json'] else {})
            guards={'unused_tags':dropped}
            try:_check_guards(root,{'guards':{'unused_tags':sorted(dropped)},'domains':sorted(domains),'changes':changes})
            except ValueError as exc:raise ValueError('Undo would remove tags that notes still use; retag those notes first ('+str(exc).split(': ',1)[-1]+')') from None
        return plan(root,changes,'undo:'+operation_id,domains,guards=guards)

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
            from .common import global_domains
            if after['domain'] not in read_json(root/'bindings.json',{}).get('domains',{}) and after['domain'] not in global_domains(root):
                raise ValueError('Target domain is unregistered')
            target=confined(root,Path('domains')/after['domain']/('notes' if note['status']=='active' else 'archive')/path.name)
            if target.exists():raise ValueError('Remap filename collision')
            note['domain']=after['domain'];note['updated']=now()
            changes[path.relative_to(root).as_posix()]=None;changes[target.relative_to(root).as_posix()]=serialize(note)
            domains.update((before['domain'],after['domain']))
        return plan(root,changes,'reference_remap',domains,{'mappings':mappings,'files':len(changes)})

PROMOTE_PAIR=0.8   # title score at which notes in two projects look like one piece of knowledge

def promote_suggest(root,domain=None):
    """Project notes that may belong in the global domain, with reasons. Writes nothing.
    Candidates: notes that appear in two unrelated projects, notes that repeat a global note, and notes whose
    tags or type suggest they hold beyond their project. The agent or user picks; promote-plan moves them."""
    from .common import global_domains
    from .notes import domain_folders,related,scope_hint,TitleScorer,short_ref
    from .repository import inventory
    root=Path(root).resolve();globals_=set(global_domains(root))
    projects=[d for d in domain_folders(root) if d not in globals_]
    active={d:[n for n in inventory(d,root) if n.get('status','active')=='active'] for d in domain_folders(root)}
    found={}
    def add(d,n,reason,**extra):
        row=found.setdefault(f"{d}:{n['id']}",{'ref':short_ref(d,n['id']),'title':n['title'],'type':n.get('type','knowledge'),'domain':d,'reasons':[]})
        row['reasons'].append(reason);row.update(extra)
    everything=[n for notes in active.values() for n in notes];scorer=TitleScorer(everything)
    for i,a in enumerate(projects):
        for b in projects[i+1:]:
            if related(a,b,root):continue
            for x in active[a]:
                for y in active[b]:
                    if x.get('type')==y.get('type') and scorer.score(x['title'],y['title'])>=PROMOTE_PAIR:
                        add(a,x,f"also in project {b}: {short_ref(b,y['id'])}",pair=short_ref(b,y['id']));add(b,y,f"also in project {a}: {short_ref(a,x['id'])}",pair=short_ref(a,x['id']))
    for d in projects:
        for n in active[d]:
            for g in globals_:
                twin=next((m for m in active.get(g,[]) if m.get('type')==n.get('type') and scorer.score(n['title'],m['title'])>=PROMOTE_PAIR),None)
                if twin:add(d,n,f"repeats global note {short_ref(g,twin['id'])}: update that one and archive this")
            reason=scope_hint(n)
            if reason:add(d,n,reason)
    if domain:found={k:v for k,v in found.items() if v['domain']==domain}
    rows=sorted(found.values(),key=lambda r:(r['domain'],r['title'].casefold()))
    return {'ok':True,'candidates':rows,'next':'Promote the ones that hold beyond their project: promote-plan REF [REF ...], then inspect-plan and apply.'}

def promote_plan(root,refs):
    """A reviewed plan moving project notes to the global domain. Stable IDs and filenames are kept and
    relative links follow. Two of the notes that are the same knowledge (same type, similar titles) become
    one: the newer supersedes the older inside the global domain."""
    from .common import global_domains
    from .notes import resolve_ref,find_note,_move_set,ensure_global,TitleScorer,domain_folders
    root=Path(root).resolve()
    if not isinstance(refs,list) or not 1<=len(refs)<=20:raise ValueError('promote-plan takes 1..20 note references')
    target=ensure_global(root);globals_=set(global_domains(root))
    with store_lock(root):
        notes=[]
        for ref in refs:
            d,note_id=resolve_ref(ref,None,root,[f for f in domain_folders(root) if f not in globals_])
            if d in globals_:raise ValueError(f'{ref} is already in the global domain')
            note=find_note(note_id,d,root)[1]
            if note.get('status','active')!='active':raise ValueError(f'{ref} is not active; only active notes are promoted')
            if any(n['id']==note['id'] for n in notes):raise ValueError(f'{ref} is listed twice')
            notes.append(note)
        scorer=TitleScorer(notes);moves={n['id']:{'note':n,'target':target} for n in notes};superseded=[]
        ordered=sorted(notes,key=lambda n:str(n.get('updated') or ''),reverse=True)
        for i,newer in enumerate(ordered):
            if moves[newer['id']].get('status'):continue
            for older in ordered[i+1:]:
                move=moves[older['id']]
                if move.get('status') or older.get('type')!=newer.get('type') or scorer.score(newer['title'],older['title'])<PROMOTE_PAIR:continue
                move.update(status='superseded',superseded_by=newer['id'])
                moves[newer['id']].setdefault('supersedes',[]).append(older['id'])
                superseded.append({'ref':f"{older['domain']}:{older['id']}",'by':f"{target}:{newer['id']}"})
        changes,_=_move_set(list(moves.values()),root)
        domains=sorted({n['domain'] for n in notes}|{target})
        summary={'target':target,'moves':[f"{n['domain']}:{n['id']}" for n in notes],'superseded':superseded,'files':len(changes)}
        return plan(root,changes,'promote',domains,summary,guards={'absent_ids':[n['id'] for n in notes]})

def retag_plan(root,mappings,domains=None):
    from .repository import inventory,serialize,FACETS
    root=Path(root).resolve();changes={};affected=[]
    if not isinstance(mappings,dict) or not mappings:raise ValueError('Provide old-tag to new-tag mappings')
    if any('/' not in tag or tag.split('/',1)[0] not in FACETS for tag in mappings.values()):raise ValueError('New tags require controlled facets')
    wanted=sorted({t.split('/',1)[1] for t in mappings.values() if t.startswith('project/')})
    if wanted:
        from .projects import load
        missing=[s for s in wanted if s not in {p['slug'] for p in load(root)['projects']}]
        if missing:raise ValueError('Unregistered project tags: '+', '.join('project/'+s for s in missing)+'; create projects with project-plan')
    wanted=sorted({t.split('/',1)[1] for t in mappings.values() if t.startswith('system/')})
    if wanted:
        from .systems import load
        missing=[s for s in wanted if s not in {x['slug'] for x in load(root)['systems']}]
        if missing:raise ValueError('Unregistered system tags: '+', '.join('system/'+s for s in missing)+'; create systems with system-plan')
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
        return plan(root,changes,'bulk_retag',affected,{'files':len(changes),'mappings':mappings},
                    guards={'unused_tags':set(mappings)-set(mappings.values())})
