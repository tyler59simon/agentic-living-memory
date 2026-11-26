"""Direct journaled writes, receipts, history, undo and derived-state recovery. There are no plans to approve: every write is
journaled with its before-bytes, so `undo OPERATION` reverses it (the user removed plans on 2026-11-26)."""
from __future__ import annotations
import json,uuid
from pathlib import Path
from .common import memory_root
from .note_types import keep_baseline
from .repository import roll_forward_pending,canonical_json,confined,digest,read_json,transaction,recover,store_lock,reconcile,now


def reconciled_domains(paths):
    """The domains whose notes a change to `paths` can affect: every `domains/D/...` path except a folder notebook proposal's."""
    out=set()
    for p in paths:
        parts=Path(p).parts
        if len(parts)>2 and parts[0]=='domains' and parts[2]!='folder-proposals':out.add(parts[1])
    return out


def commit(root,changes,kind,domains=(),summary=None,guards=None,sync_overlay=True,operation_id=None,meta=None):
    """A direct write under one lock. The journal is the receipt: its operation ID (prefix `d-`; operations from before
    2026-11-26 may carry a plan ID) is what `undo` takes. guards: store-wide conditions that per-file hashes cannot express,
    checked under the lock: read_set ({path: sha256} of files the change was built from), absent_ids (no note in the
    domains has these IDs) and unused_tags (no note outside the change carries them)."""
    root=Path(root).resolve()
    with store_lock(root):   # re-entrant: a builder that already holds the lock reads and writes under it
        expected={}
        for rel in changes:
            p=confined(root,rel)
            expected[rel]=digest(p.read_bytes()) if p.exists() else None
        domains=sorted(set(domains))
        if guards:
            roll_forward_pending(root)
            _check_guards(root,{'guards':{k:(v if k=='read_set' else sorted(set(v))) for k,v in guards.items() if v},'domains':domains,'changes':changes})
        operation_id=operation_id or 'd-'+uuid.uuid4().hex
        receipt=transaction(root,changes,kind,expected,operation_id,meta=meta)
        from .notes import note_revisions
        revisions=note_revisions(changes)
        if revisions:receipt['revisions']=revisions
        for domain in domains:
            try:reconcile(domain,root,sync_overlay=sync_overlay)
            except Exception as exc:
                from .logs import swallowed
                swallowed('governance.commit',exc,root)
                receipt.setdefault('warnings',[]).append(f'{domain}: derived state requires repair')
        receipt.update(kind=kind,summary=summary or {'files':len(changes)},undo=f'undo {operation_id}')
        return receipt

def operation_root(root,operation_id):
    """The folder whose journal holds operation_id: the store, or for an install (`i-`) or harness setup (`s-HARNESS-`) the install
    folder or the harness home that operation wrote to."""
    root=Path(root).resolve()
    if not __import__('re').fullmatch(r'[A-Za-z0-9_-]{1,100}',operation_id) or (root/'.operations'/operation_id/'journal.json').exists():return root
    homes=[]
    if operation_id.startswith('i-'):
        try:
            import install   # install.py beside living_memory.py: the source checkout or the installed copy
            homes=[install.INSTALL_HOME,install.CODEX_HOME]
        except ImportError:pass
    elif operation_id.startswith('s-'):
        from .instructions import harness_home
        harness=operation_id[2:].split('-',1)[0]
        if harness in ('claude','codex','copilot','factory','antigravity'):homes=[harness_home(harness)]
    for home in homes:
        if (Path(home)/'.operations'/operation_id/'journal.json').exists():return Path(home).resolve()
    return root

def _check_guards(root,value):
    """Fail as stale when the store changed in a way the change's file hashes cannot see."""
    from .repository import inventory
    guards=value.get('guards') or {}
    if not guards:return
    for rel,expected in guards.get('read_set',{}).items():
        path=confined(root,rel)
        if (digest(path.read_bytes()) if path.exists() else None)!=expected:
            raise ValueError('stale_write: a file this change was built from changed: '+rel)
    absent=set(guards.get('absent_ids',[]));unused=set(guards.get('unused_tags',[]))
    if not (absent or unused):return   # file guards only (setup in a harness home): no notes to scan
    folders=sorted(p.name for p in (root/'domains').iterdir() if p.is_dir()) if (root/'domains').is_dir() else []
    for domain in folders:
        if unused or domain in value['domains']:
            for note in inventory(domain,root,retired=True):
                rel=Path(note['path']).relative_to(root).as_posix()
                if absent and domain in value['domains'] and note['id'] in absent and rel not in value['changes']:
                    raise ValueError(f"stale_write: {domain}:{note['id']} was added meanwhile; run the command again")
                if unused and rel not in value['changes'] and unused&set(note.get('tags',[])):
                    raise ValueError(f"stale_write: {domain}:{note['id']} now uses {', '.join(sorted(unused&set(note['tags'])))}, which this change removes; run the command again")

def history(root,note_id=None,limit=20,include_private=False):
    root=Path(root).resolve();rows=[]
    if note_id:
        from .activity import note_history   # only operations that changed the note itself, from the activity cache
        rows=note_history(root,note_id,limit)
        if not include_private:
            from .docs import is_private_path
            for row in rows:
                kept=[f for f in row.get('files') or [] if not is_private_path(f)]
                if len(kept)!=len(row.get('files') or []):row.update(files=kept,redacted=len(row['files'])-len(kept))
        return rows
    from .activity import _entries,operation_label
    from .docs import journal_private,is_private_path
    entries=_entries(root)
    for path in (root/'.operations').glob('*/journal.json'):
        try:
            value=read_json(path)
            summary,actor=operation_label(entries.get(value['id']),value['id'],str(value['kind']))
            row={'operation_id':value['id'],'kind':value['kind'],'state':value['state'],'created':value['created'],'files':[e['path'] for e in value['effects']],'summary':summary,'actor':actor,**({'user':value['os_user']} if isinstance(value.get('os_user'),str) and value['os_user'] else {})}
            # Operations on folder notebook bookkeeping show no paths and no summary unless the caller passes --include-private.
            if not include_private and journal_private(root,value):
                kept=[f for f in row['files'] if not is_private_path(f)]
                row.update(files=kept,redacted=len(row['files'])-len(kept),summary=None)
        except (ValueError,KeyError,TypeError):continue  # doctor reports unreadable journals by ID
        rows.append(row)
    return sorted(rows,key=lambda r:r['created'],reverse=True)[:limit]

def merge(root,payload):
    """A direct, journaled merge: the target takes the combined body, each source is archived as superseded by it, and links
    follow the sources into archive/. The target and each source name the revision the caller read (domain:id@REVISION)."""
    from .notes import find_note,retirement_record,split_revision,check_revision
    from .repository import serialize,validate,inventory
    from .migration import rewrite_type_links
    root=Path(root).resolve()
    target_ref,target_revision=split_revision(payload['target']);domain,target_id=target_ref.split(':',1);sources=payload['sources']
    if not isinstance(sources,list) or not 1<=len(sources)<=20 or len(set(sources))!=len(sources):raise ValueError('Merge requires 1..20 distinct source references')
    with store_lock(root):
        target_path,target=find_note(target_id,domain,root)
        if target_revision:check_revision(target['revision'],target_revision,target_ref)
        if target.get('status')!='active':raise ValueError('Merge target must be active')
        keep_baseline(target);target['body']=payload['body'].strip();target['updated']=now();changes={};merged=[];moves={};named=[]
        if target.get('checked'):target['checked']=target['updated']   # the merged body is the author's word for the facts as of now
        for given in sources:
            ref,revision=split_revision(given);source_domain,identity=ref.split(':',1)
            if source_domain!=domain:raise ValueError('Merge sources must be distinct notes in the target domain')
            path,note=find_note(identity,domain,root)
            if revision:check_revision(note['revision'],revision,ref)
            # Compare resolved notes, not spellings: a full ID and its short prefix are the same note.
            if note['id']==target['id'] or note['id'] in merged:raise ValueError('Merge sources must be distinct notes in the target domain')
            if note.get('status')!='active':raise ValueError('Merge sources must be active')
            archived=root/'domains'/domain/'archive'/path.name
            if archived.exists():raise ValueError('Merge archive collision')
            merged.append(note['id']);named.append(ref);moves[path.resolve()]=(path,archived,note)
        targets={key:archived for key,(_,archived,_) in moves.items()}
        for path,archived,note in moves.values():
            # Moving to archive/: the note's own relative links, and links between moved sources, still resolve.
            note['body']=rewrite_type_links(note['body'],archived,targets,moved_from=path)
            note.update(status='superseded',superseded_by=target['id'],updated=now(),last_retirement=retirement_record('supersede','agent',f"{domain}:{target['id']}"));validate(note,domain,root)
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
        return commit(root,changes,'merge',[domain],{'target':target_ref,'sources':named,'files':len(changes)})

def undo(root,operation_id,include_private=False,revert_files=True):
    """revert_files: for the accept of a folder notebook proposal, put the page outside the store back first (folder_docs.revert_accept)."""
    if not __import__('re').fullmatch(r'[A-Za-z0-9_-]{1,100}',operation_id):raise ValueError('Invalid operation ID')
    root=Path(root).resolve();journal=root/'.operations'/operation_id/'journal.json'
    try:value=read_json(journal)
    except ValueError:
        if journal.exists():raise ValueError(f'corrupt_journal {operation_id}: this operation cannot be undone; see RECOVERY.md') from None
        raise
    if not value:raise ValueError('Operation unavailable')
    import base64
    # The derived-state marker is rewritten by every reconcile; it is not part of what is being undone.
    value['effects']=[e for e in value['effects'] if Path(e['path']).name!='.derived-dirty']
    def before(e):
        # An operation of the retired hosted notebooks (CON-18) may have moved an attachment's bytes aside or left a page in another encoding.
        if e.get('blob'):raise ValueError(f"{operation_id} moved the notebook attachment {e['path']}; hosted notebooks were retired (CON-18), so it cannot be undone")
        if e['before'] is None:return None
        try:return base64.b64decode(e['before']).decode()
        except UnicodeDecodeError:raise ValueError(f"{operation_id} changed {e['path']}, which was not UTF-8 text; hosted notebooks were retired (CON-18), so it cannot be undone") from None
    from .docs import is_notebook_journal,DOC_OP_PREFIX,record_index,journal_private
    changes={e['path']:before(e) for e in value['effects']}
    domains=reconciled_domains(changes)
    notebook_op=is_notebook_journal(value['effects'])
    # Check and write under one lock: an edit between the check and the write would be overwritten.
    with store_lock(root):
        from .folder_policy import REGISTRY,loosens
        if not include_private and (journal_private(root,value,authorize=True) or (REGISTRY in changes and loosens(root,changes[REGISTRY]))):
            raise ValueError(f'not_found: operation {operation_id} changed a private notebook; only Hivemind undoes it (--include-private)')
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
        kind=str(value['kind'])
        while kind.startswith('undo:'):   # an undo of X: the folder notebook checks below look at X's kind
            try:kind=str(read_json(root/'.operations'/kind[5:]/'journal.json')['kind'])
            except (ValueError,KeyError,TypeError):break
        if notebook_op:
            folder=(value.get('meta') or {}).get('folder_notebook')
            if folder and kind=='doc_accept' and str(value['kind']).startswith('undo:'):raise ValueError('The page of a folder notebook proposal is not put back by undoing an undo; accept the proposal again')
            from .folder_docs import revert_accept
            again=revert_accept(root,value,include_private) if folder and revert_files and str(value['kind'])=='doc_accept' else None
            # A `doc-` id and the journal's meta: notebook retention covers the undo, and doc undo still knows it after its parent is pruned.
            new_id=DOC_OP_PREFIX+uuid.uuid4().hex
            try:receipt=commit(root,changes,'undo:'+operation_id,domains,guards=guards,operation_id=new_id,meta=value.get('meta'))
            except BaseException:
                if again:again()
                raise
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
            if mapping.get('revision'):
                from .notes import check_revision
                check_revision(note['revision'],mapping['revision'],f"{before['domain']}:{before['id']}")
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
    from .notes import resolve_ref,find_note,_move_set,ensure_global,TitleScorer,domain_folders,promotion_blocker,split_revision,check_revision
    root=Path(root).resolve()
    if not isinstance(refs,list) or not 1<=len(refs)<=20:raise ValueError('promote takes 1..20 note references')
    target=ensure_global(root);globals_=set(global_domains(root))
    with store_lock(root):
        notes=[]
        for given in refs:
            ref,revision=split_revision(given)   # REF@REVISION: the revision the caller read
            d,note_id=resolve_ref(ref,None,root,[f for f in domain_folders(root) if f not in globals_])
            if d in globals_:raise ValueError(f'{ref} is already in the global domain')
            note=find_note(note_id,d,root)[1]
            if revision:check_revision(note['revision'],revision,f'{d}:{note_id}')
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

def retag(root,mappings,domains=None):
    """A direct, journaled rename of tags across every note that carries them, and in the taxonomy."""
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
            if still:raise ValueError('Tags are still used in other domains: '+', '.join(still)+'; run retag without a domain restriction')
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
        return commit(root,changes,'bulk_retag',affected,{'files':len(changes),'mappings':mappings},
                      guards={'unused_tags':set(mappings)-set(mappings.values())})
