"""Hash-approved plans, receipts, history, and derived-state recovery."""
from __future__ import annotations
import hashlib,json,uuid
from pathlib import Path
from .common import atomic_write,memory_root
from .note_types import keep_baseline
from .repository import canonical_json,confined,digest,read_json,transaction,recover,store_lock,reconcile,now,_stage_blob


def reconciled_domains(paths):
    """The domains whose notes a change to `paths` can affect: every `domains/D/...` path except a notebook's (NS-2A). A notebook-only
    domain has no notes, and reconcile rejects a domain without notes/."""
    out=set()
    for p in paths:
        parts=Path(p).parts
        if len(parts)>2 and parts[0]=='domains' and parts[2]!='notebooks':out.add(parts[1])
    return out


def plan(root, changes, kind, domains=(), summary=None, guards=None):
    """guards: store-wide conditions that per-file hashes cannot express, re-checked at apply:
    absent_ids (no note in the plan's domains has these IDs) and unused_tags (no note outside the plan carries them).
    A change given as bytes (a notebook attachment) is staged under .plans/<id>/blobs/<sha256> and recorded as {"blob": sha256},
    since the plan itself is JSON text."""
    root=Path(root).resolve()
    with store_lock(root):
        expected={}
        for rel in changes:
            p=confined(root,rel)
            expected[rel]=digest(p.read_bytes()) if p.exists() else None
        plan_id=uuid.uuid4().hex
        staged={rel:v for rel,v in changes.items() if isinstance(v,(bytes,bytearray))}
        for data in staged.values():_stage_blob(root/'.plans'/plan_id/'blobs',bytes(data))
        changes={rel:({'blob':digest(bytes(v))} if rel in staged else v) for rel,v in changes.items()}
        value={'schema':2,'id':plan_id,'kind':kind,'domains':sorted(set(domains)),
               'created':now(),'changes':changes,'expected':expected,'summary':summary or {'files':len(changes)}}
        if guards:value['guards']={k:(v if k in ('read_set','inventory') else sorted(set(v))) for k,v in guards.items() if v}
        value['digest']=hashlib.sha256(canonical_json(value).encode()).hexdigest()
        path=root/'.plans'/(value['id']+'.json');atomic_write(path,canonical_json(value))
        return {'ok':True,'plan_id':value['id'],'digest':value['digest'],'kind':kind,'summary':value['summary']}


def _plan_changes(root,value):
    """A plan's changes with staged attachments read back as bytes (checked against their hash)."""
    out={}
    for rel,v in value['changes'].items():
        if isinstance(v,dict) and isinstance(v.get('blob'),str):
            path=root/'.plans'/value['id']/'blobs'/v['blob']
            data=path.read_bytes() if path.exists() else None
            if data is None or digest(data)!=v['blob']:raise ValueError(f'stale plan: the attachment bytes staged for {rel} are missing; make a new plan')
            out[rel]=data
        else:out[rel]=v
    return out

def commit(root,changes,kind,domains=(),summary=None,guards=None,sync_overlay=True,operation_id=None,meta=None):
    """A direct write: what plan() then apply() do, in one call under one lock, with no plan to approve. The journal is the receipt:
    its operation ID (prefix `d-`, which tells a direct operation from a plan-applied one) is what `undo` takes."""
    root=Path(root).resolve()
    with store_lock(root):   # re-entrant: a builder that already holds the lock reads and writes under it
        expected={}
        for rel in changes:
            p=confined(root,rel)
            expected[rel]=digest(p.read_bytes()) if p.exists() else None
        domains=sorted(set(domains))
        if guards:_check_guards(root,{'guards':{k:(v if k in ('read_set','inventory') else sorted(set(v))) for k,v in guards.items() if v},'domains':domains,'changes':changes})
        operation_id=operation_id or 'd-'+uuid.uuid4().hex
        receipt=transaction(root,changes,kind,expected,operation_id,meta=meta)
        for domain in domains:
            try:reconcile(domain,root,sync_overlay=sync_overlay)
            except Exception as exc:
                from .logs import swallowed
                swallowed('governance.commit',exc,root)
                receipt.setdefault('warnings',[]).append(f'{domain}: derived state requires repair')
        receipt.update(kind=kind,summary=summary or {'files':len(changes)},undo=f'undo {operation_id}')
        return receipt

def apply(root,plan_id,expected_digest,sync_overlay=True):
    root=Path(root).resolve()
    if not isinstance(plan_id,str) or not __import__('re').fullmatch(r'[a-f0-9]{32}',plan_id):raise ValueError('Invalid plan identity')
    with store_lock(root):
        value=read_json(root/'.plans'/(plan_id+'.json'))
        if not value:raise ValueError('Plan unavailable')
        unsigned={k:v for k,v in value.items() if k!='digest'}
        actual=hashlib.sha256(canonical_json(unsigned).encode()).hexdigest()
        if value['digest']!=actual or actual!=expected_digest:raise ValueError('Plan digest mismatch')
        if not (root/'.operations'/plan_id/'journal.json').exists():
            _check_guards(root,value)
            # Notes or the type registry may have changed since the plan was made: an undo or restore plan must not leave a
            # note whose custom type is unregistered or takes no action items.
            from .type_registry import check_undo
            try:check_undo(root,value['changes'])
            except ValueError as exc:raise ValueError('stale plan: it would break notes that use a custom type: '+str(exc)+'; make a new plan') from None
        receipt=transaction(root,_plan_changes(root,value),value['kind'],value['expected'],value['id'])
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
    # inventory: a folder must still hold exactly these files (deleting a notebook must not strand a page added since; NS-2A).
    for rel,expected in guards.get('inventory',{}).items():
        from .docs import _files
        folder=confined(root,rel)
        if (_files(folder) if folder.is_dir() else [])!=sorted(expected):
            raise ValueError('stale plan: files were added to or removed from '+rel+' since the plan was made; make a new plan')
    absent=set(guards.get('absent_ids',[]));unused=set(guards.get('unused_tags',[]))
    if not (absent or unused):return   # file guards only (setup plans in a harness home): no notes to scan
    folders=sorted(p.name for p in (root/'domains').iterdir() if p.is_dir()) if (root/'domains').is_dir() else []
    for domain in folders:
        if unused or domain in value['domains']:
            for note in inventory(domain,root,retired=True):
                rel=Path(note['path']).relative_to(root).as_posix()
                if absent and domain in value['domains'] and note['id'] in absent and rel not in value['changes']:
                    raise ValueError(f"stale plan: {domain}:{note['id']} was added since the plan was made; make a new plan")
                if unused and rel not in value['changes'] and unused&set(note.get('tags',[])):
                    raise ValueError(f"stale plan: {domain}:{note['id']} now uses {', '.join(sorted(unused&set(note['tags'])))}, which this plan removes; make a new plan")

def history(root,note_id=None,limit=20,include_private=False):
    root=Path(root).resolve();rows=[]
    if note_id:
        from .activity import note_history   # only operations that changed the note itself, from the activity cache
        rows=note_history(root,note_id,limit)
        if not include_private:
            from .docs import private_scope,is_private_path
            scope=private_scope(root)
            for row in rows:
                kept=[f for f in row.get('files') or [] if not is_private_path(f,scope)]
                if len(kept)!=len(row.get('files') or []):row.update(files=kept,redacted=len(row['files'])-len(kept))
        return rows
    from .activity import _entries,operation_label
    from .docs import private_scope,private_journal,is_private_path
    entries=_entries(root);scope=None if include_private else private_scope(root)
    for path in (root/'.operations').glob('*/journal.json'):
        try:
            value=read_json(path)
            summary,actor=operation_label(entries.get(value['id']),value['id'],str(value['kind']))
            row={'operation_id':value['id'],'kind':value['kind'],'state':value['state'],'created':value['created'],'files':[e['path'] for e in value['effects']],'summary':summary,'actor':actor}
            # Operations on a private notebook show no paths and no summary unless the caller passes --include-private (NS-2A).
            if scope and private_journal(value,scope):
                kept=[f for f in row['files'] if not is_private_path(f,scope) and not (value.get('meta') or {}).get('uid') in scope[1]]
                row.update(files=kept,redacted=len(row['files'])-len(kept),summary=None)
        except (ValueError,KeyError,TypeError):continue  # doctor reports unreadable journals by ID
        rows.append(row)
    return sorted(rows,key=lambda r:r['created'],reverse=True)[:limit]

def inspect_plan(root,identity,full=False,include_private=False):
    """Each change a plan would make, as a unified diff (cut at 16000 characters unless `full`). An attachment shows as its hash and size,
    and a change inside a private notebook is left out unless `include_private` (NS-2A)."""
    import re,difflib
    from .docs import private_scope,is_private_path
    from .repository import is_blob
    cut=None if full else 16000
    if not re.fullmatch('[a-f0-9]{32}',identity):raise ValueError('Invalid plan identity')
    root=Path(root);value=read_json(root/'.plans'/(identity+'.json'))
    if not value:raise ValueError('Plan unavailable')
    effects=[];scope=None if include_private else private_scope(root,value['changes']);redacted=0
    for rel,after in value['changes'].items():
        if scope and is_private_path(rel,scope):redacted+=1;continue
        path=confined(root,rel)
        if isinstance(after,dict) or is_blob(rel):
            blob=(after or {}).get('blob') if isinstance(after,dict) else None
            effects.append({'path':rel,'expected_revision':value['expected'][rel],'action':'delete' if after is None else 'write','binary':True,'sha256':blob,'diff':''})
            continue
        before=path.read_bytes().decode('utf-8',errors='replace') if path.exists() else ''   # a damaged file being repaired still shows
        delta=''.join(difflib.unified_diff(before.splitlines(True),(after or '').splitlines(True),fromfile=rel,tofile=rel))
        effects.append({'path':rel,'expected_revision':value['expected'][rel],'action':'delete' if after is None else 'write','diff':delta[:cut],'truncated':cut is not None and len(delta)>cut})
    summary=value['summary']
    if redacted:
        from .docs import redact_summary
        summary=redact_summary(summary,scope)
    result={'ok':True,'plan_id':identity,'digest':value['digest'],'kind':value['kind'],'summary':summary,'effects':effects}
    if redacted:result['redacted']=redacted
    return result

def merge_plan(root,payload):
    from .notes import find_note,retirement_record
    from .repository import serialize,validate,inventory
    from .migration import rewrite_type_links
    root=Path(root).resolve();domain,target_id=payload['target'].split(':',1);sources=payload['sources']
    if not isinstance(sources,list) or not 1<=len(sources)<=20 or len(set(sources))!=len(sources):raise ValueError('Merge requires 1..20 distinct source references')
    with store_lock(root):
        target_path,target=find_note(target_id,domain,root)
        if target.get('status')!='active':raise ValueError('Merge target must be active')
        keep_baseline(target);target['body']=payload['body'].strip();target['updated']=now();changes={};merged=[];moves={}
        if target.get('checked'):target['checked']=target['updated']   # the approved merged body is the author's word for the facts as of now
        for ref in sources:
            source_domain,identity=ref.split(':',1)
            if source_domain!=domain:raise ValueError('Merge sources must be distinct notes in the target domain')
            path,note=find_note(identity,domain,root)
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
            note.update(status='superseded',superseded_by=target['id'],updated=now(),last_retirement=retirement_record('supersede','plan',f"{domain}:{target['id']}"));validate(note,domain,root)
            changes[path.relative_to(root).as_posix()]=None;changes[archived.relative_to(root).as_posix()]=serialize(note)
        target['body']=rewrite_type_links(target['body'],target_path,targets)
        target['supersedes']=sorted(set(target.get('supersedes',[])+merged));validate(target,domain,root)
        changes[target_path.relative_to(root).as_posix()]=serialize(target)
        for referring in inventory(domain,root,retired=True):
            # Links from other notes follow the sources into archive/.
            if referring['id']==target['id'] or Path(referring['path']).resolve() in moves:continue
            new=rewrite_type_links(referring['body'],Path(referring['path']),targets)
            if new!=referring['body']:
                keep_baseline(referring);referring['body']=new;referring['updated']=now()
                changes[Path(referring['path']).relative_to(root).as_posix()]=serialize(referring)
        return plan(root,changes,'merge',[domain],{'target':payload['target'],'sources':sources,'files':len(changes)})

# Operations undone directly: the direct note commands and the direct writes (note edits, archive, rewrite, remap, promote, taxonomy and
# registry writes). Every other kind (merge, delete, bulk retag, migrations, import, ingest, maintenance, bindings, Obsidian, and install or
# setup operations outside the store) comes back as a plan to approve, so a kind added later fails safe.
DIRECT_UNDO_KINDS=frozenset({'capture','capture_supersede','capture_promote','update','supersede','resolve','reopen','confirm','archive','rewrite','reference_remap','promote','taxonomy_expansion','config_set','rebaseline'})
DIRECT_UNDO_PREFIXES=('source_','project_','system_','change_','views_','type_','doc_')

def _plan_kind(kind,effects=()):
    """True when undoing an operation of this kind needs an approved plan (an undo of X is judged by X's kind, found through the chain).
    `restore` is two things with one journal kind: a note restore (direct; notes._commit_many marks the domain `.derived-dirty` in the
    same journal) and a backup restore (applied from a plan, which writes no such marker)."""
    if kind=='restore':return not any(Path(e['path']).name=='.derived-dirty' for e in effects)
    return not (kind in DIRECT_UNDO_KINDS or kind.startswith(DIRECT_UNDO_PREFIXES))

def undo(root,operation_id):
    if not __import__('re').fullmatch(r'[A-Za-z0-9_-]{1,100}',operation_id):raise ValueError('Invalid operation ID')
    root=Path(root).resolve();journal=root/'.operations'/operation_id/'journal.json'
    try:value=read_json(journal)
    except ValueError:
        if journal.exists():raise ValueError(f'corrupt_journal {operation_id}: this operation cannot be undone; see RECOVERY.md') from None
        raise
    if not value:raise ValueError('Operation unavailable')
    # A kept plan kind (merge, delete, bulk retag, ...) comes back as a plan, so the user can inspect current revisions first; every other kind is undone directly.
    import base64
    # The derived-state marker is rewritten by every reconcile; it is not part of what is being undone.
    raw_effects=value['effects'];value['effects']=[e for e in value['effects'] if Path(e['path']).name!='.derived-dirty']
    def before(e):
        if not e.get('blob'):return base64.b64decode(e['before']).decode() if e['before'] is not None else None
        # An attachment's bytes were moved aside into the operation's blobs folder when it removed them (NS-2A).
        if e['before_hash'] is None:return None
        kept=root/'.operations'/operation_id/'blobs'/e['before_hash']
        if not kept.exists() or digest(kept.read_bytes())!=e['before_hash']:raise ValueError(f"The attachment {e['path']} this operation removed is no longer kept, so it cannot be undone")
        return kept.read_bytes()
    changes={e['path']:before(e) for e in value['effects']}
    domains=reconciled_domains(changes)
    from .docs import is_notebook_journal,PRIVACY,merge_privacy,DOC_OP_PREFIX,record_index
    notebook_op=is_notebook_journal(value['effects'])
    if PRIVACY in changes:
        # Every notebook that was ever private stays listed, so undoing a privacy change never exposes what was written meanwhile.
        current=(root/PRIVACY).read_text(encoding='utf-8') if (root/PRIVACY).exists() else None
        merged=merge_privacy(root,changes[PRIVACY] or '')
        value['effects']=[e for e in value['effects'] if e['path']!=PRIVACY]   # the registry may have grown since: no precondition on it
        if merged==current:changes.pop(PRIVACY)
        else:changes[PRIVACY]=merged
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
        # Restoring an older type registry can drop a custom type, or its action items, that notes written since depend on; restoring an
        # older note can bring back action items its custom type no longer takes. Judged on the store as the undo leaves it.
        from .type_registry import check_undo
        try:check_undo(root,changes)
        except ValueError as exc:raise ValueError('Undo would break notes that use a custom type: '+str(exc)+'; retype those notes first') from None
        kind,judged_effects=str(value['kind']),raw_effects
        while kind.startswith('undo:'):   # an undo of X: judged by X's kind
            try:judged=read_json(root/'.operations'/kind[5:]/'journal.json');kind=str(judged['kind']);judged_effects=judged.get('effects') or []
            except (ValueError,KeyError,TypeError):break
        # A `doc-` undo was itself undone directly (plans apply under `d-` ids), so its chain stays direct after retention prunes the parent.
        if kind.startswith('undo:') and notebook_op and operation_id.startswith(DOC_OP_PREFIX):kind='doc_undo'
        if _plan_kind(kind,judged_effects):return plan(root,changes,'undo:'+operation_id,domains,guards=guards)
        if notebook_op:
            # A `doc-` id and the journal's meta: notebook retention covers the undo, and doc undo still knows it after its parent is pruned.
            new_id=DOC_OP_PREFIX+uuid.uuid4().hex
            receipt=commit(root,changes,'undo:'+operation_id,domains,guards=guards,operation_id=new_id,meta=value.get('meta'))
            record_index(root,new_id,changes)
            return receipt
        return commit(root,changes,'undo:'+operation_id,domains,guards=guards)

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
            keep_baseline(note);note['domain']=after['domain'];note['updated']=now()   # a move vouches for nothing
            changes[path.relative_to(root).as_posix()]=None;changes[target.relative_to(root).as_posix()]=serialize(note)
            domains.update((before['domain'],after['domain']))
        return commit(root,changes,'reference_remap',domains,{'mappings':mappings,'files':len(changes)})

PROMOTE_PAIR=0.8   # title score at which notes in two projects look like one piece of knowledge

def promote_suggest(root,domain=None):
    """Project notes that may belong in the global domain, with reasons. Writes nothing.
    Candidates: notes that appear in two unrelated projects, notes that repeat a global note, and notes whose
    tags or type suggest they hold beyond their project. The agent or user picks; promote moves them."""
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
    return {'ok':True,'candidates':rows,'next':'Promote the ones that hold beyond their project: promote REF [REF ...] (undo OPERATION reverses it).'}

def promote(root,refs):
    """A direct, journaled move of project notes to the global domain. Stable IDs and filenames are kept and
    relative links follow. Two of the notes that are the same knowledge (same type, similar titles) become
    one: the newer supersedes the older inside the global domain."""
    from .common import global_domains
    from .notes import resolve_ref,find_note,_move_set,ensure_global,TitleScorer,domain_folders,promotion_blocker
    root=Path(root).resolve()
    if not isinstance(refs,list) or not 1<=len(refs)<=20:raise ValueError('promote takes 1..20 note references')
    target=ensure_global(root);globals_=set(global_domains(root))
    with store_lock(root):
        notes=[]
        for ref in refs:
            d,note_id=resolve_ref(ref,None,root,[f for f in domain_folders(root) if f not in globals_])
            if d in globals_:raise ValueError(f'{ref} is already in the global domain')
            note=find_note(note_id,d,root)[1]
            if note.get('status','active')!='active':raise ValueError(f'{ref} is not active; only active notes are promoted')
            if any(n['id']==note['id'] for n in notes):raise ValueError(f'{ref} is listed twice')
            blocker=promotion_blocker(note,root)
            if blocker:raise ValueError(f'{ref} stays in its project: {blocker}')
            notes.append(note)
        scorer=TitleScorer(notes);moves={n['id']:{'note':n,'target':target,'clean':True} for n in notes};superseded=[]
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
        return commit(root,changes,'promote',domains,summary,guards={'absent_ids':[n['id'] for n in notes]})

def retag_plan(root,mappings,domains=None):
    from .repository import inventory,serialize,FACETS
    root=Path(root).resolve();changes={};affected=[]
    if not isinstance(mappings,dict) or not mappings:raise ValueError('Provide old-tag to new-tag mappings')
    if any('/' not in tag or tag.split('/',1)[0] not in FACETS for tag in mappings.values()):raise ValueError('New tags require controlled facets')
    wanted=sorted({t.split('/',1)[1] for t in mappings.values() if t.startswith('project/')})
    if wanted:
        from .projects import load
        missing=[s for s in wanted if s not in {p['slug'] for p in load(root)['projects']}]
        if missing:raise ValueError('Unregistered project tags: '+', '.join('project/'+s for s in missing)+'; create projects with project create')
    wanted=sorted({t.split('/',1)[1] for t in mappings.values() if t.startswith('system/')})
    if wanted:
        from .systems import load
        missing=[s for s in wanted if s not in {x['slug'] for x in load(root)['systems']}]
        if missing:raise ValueError('Unregistered system tags: '+', '.join('system/'+s for s in missing)+'; create systems with system create')
    with store_lock(root):
        # The taxonomy is store-wide, so a retag must cover every domain directory
        # (including _global and domains no longer bound), or it strands other notes.
        every=sorted(p.name for p in (root/'domains').iterdir() if p.is_dir()) if (root/'domains').is_dir() else []
        if domains:
            outside=[d for d in every if d not in set(domains)]
            still=sorted({t for d in outside for n in inventory(d,root,retired=True) for t in n.get('tags',[]) if t in mappings})
            if still:raise ValueError('Tags are still used in other domains: '+', '.join(still)+'; run retag-plan without a domain restriction')
        from .notes import taxonomy;registered=taxonomy(root)
        used={t for d in sorted(domains or every) for n in inventory(d,root,retired=True) for t in n.get('tags',[])}
        unknown=sorted(t for t in mappings if t not in registered and t not in used)
        if unknown:raise ValueError('Unknown source tags: '+', '.join(unknown)+'; check the spelling with taxonomy')
        for domain in sorted(domains or every):
            for note in inventory(domain,root,retired=True):
                tags=sorted({mappings.get(t,t) for t in note.get('tags',[])})
                if tags!=sorted(note.get('tags',[])):
                    note['tags']=tags;note['updated']=now();changes[Path(note['path']).relative_to(root).as_posix()]=serialize(note);affected.append(domain)
        taxonomy=read_json(root/'tag_taxonomy.json',{'tags':[]});known=taxonomy.get('tags',[]) if isinstance(taxonomy,dict) else taxonomy
        changes['tag_taxonomy.json']=canonical_json({'version':2,'facets':list(FACETS),'tags':sorted({mappings.get(t,t) for t in known}|set(mappings.values()))})
        return plan(root,changes,'bulk_retag',affected,{'files':len(changes),'mappings':mappings},
                    guards={'unused_tags':set(mappings)-set(mappings.values())})
