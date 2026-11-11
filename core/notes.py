"""Governed canonical note operations; all persistence goes through transactions."""
from __future__ import annotations
import argparse,json,re,uuid
from datetime import date
from pathlib import Path
from .common import memory_root,safe_domain,canonical_domain
from .repository import inventory,parse,serialize,validate,read_json,transaction,reconcile,store_lock,now,digest,clean_files
from .note_types import prefix_for,normalize,effective_type,default_review_by
from .note_format import format_body,without_actions,ACTION_TYPES

# Fields an agent may change directly (journaled and undoable, no plan) with `update`.
UPDATABLE=('title','body','tags','type','context','review_by','files')

def slug(value):return re.sub(r'[^a-z0-9]+','-',value.casefold()).strip('-')[:64] or 'note'
def taxonomy(root):
    data=read_json(Path(root)/'tag_taxonomy.json',{'tags':[]});tags=data.get('tags',[]) if isinstance(data,dict) else data
    return set(tags)
# Injected lines show the first 8 characters of an ID (filenames already end in them); commands accept any unique prefix that long.
SHORT_ID=8
ID_PREFIX=re.compile(r'[0-9a-f][0-9a-f-]{%d,}'%(SHORT_ID-1),re.I)

def short_ref(domain,note_id):return f'{domain}:{str(note_id)[:SHORT_ID]}'

def _by_id(note_id,notes):
    exact=[n for n in notes if n.get('id')==note_id]
    if exact or not ID_PREFIX.fullmatch(note_id):return exact
    return [n for n in notes if str(n.get('id','')).lower().startswith(note_id.lower())]

def _ambiguous(domain,matches):
    return ValueError('ambiguous_ref: '+', '.join(f"{domain}:{n['id']} \"{n['title']}\"" for n in matches[:10]))

def find_note(note_id,domain,root=None):
    """The note with this ID, or with this unique ID prefix (8+ characters)."""
    matches=_by_id(note_id,inventory(domain,root,retired=True))
    if len(matches)>1:raise _ambiguous(domain,matches)
    if len(matches)!=1:raise ValueError(f'Expected one note ID, found {len(matches)}')
    return Path(matches[0]['path']),matches[0]

def resolve_ref(value,domain=None,root=None,search_domains=()):
    """(domain, full ID) for DOMAIN:ID, DOMAIN:PREFIX, a bare ID or prefix in the active domain,
    or an active note's exact title (case-insensitive) in the active or search domains.
    A title may itself contain a colon, so an unmatched DOMAIN:ID falls through to titles."""
    value=str(value).strip();head,sep,tail=value.partition(':')
    lookup=None
    if sep and head and not any(c.isspace() for c in head):
        try:explicit=canonical_domain(head,root)
        except ValueError:explicit=None
        if explicit and (Path(root or memory_root())/'domains'/explicit).is_dir():lookup=(explicit,tail.strip())
    elif domain:lookup=(domain,value)
    if lookup:
        found=_by_id(lookup[1],inventory(lookup[0],root,retired=True))
        if len(found)>1:raise _ambiguous(lookup[0],found)
        if found:return lookup[0],found[0]['id']
    if sep and ID_PREFIX.fullmatch(tail.strip()):
        # IDs are unique across the store, and a promoted or remapped note keeps its ID while its domain
        # changes, so an older DOMAIN:ID still finds it wherever it now lives.
        elsewhere=[(d,n) for d in domain_folders(root or memory_root()) if not lookup or d!=lookup[0]
                   for n in _by_id(tail.strip(),inventory(d,root,retired=True))]
        if len(elsewhere)>1:raise ValueError('ambiguous_ref: '+', '.join(f"{d}:{n['id']} \"{n['title']}\"" for d,n in elsewhere[:10]))
        if elsewhere:return elsewhere[0][0],elsewhere[0][1]['id']
    domains=list(dict.fromkeys([d for d in (domain,*search_domains) if d]))
    if not domains:raise ValueError('Pass domain:id, --domain, or --cwd for a bound project')
    wanted=value.casefold();titled=[]
    for d in domains:
        try:titled+=[(d,n) for n in inventory(d,root) if n.get('status','active')=='active' and n['title'].strip().casefold()==wanted]
        except (OSError,ValueError):continue
    if len(titled)>1:raise ValueError('ambiguous_ref: several notes have this title: '+', '.join(f'{d}:{n["id"]}' for d,n in titled[:10]))
    if not titled:raise ValueError(f'No note with ID, ID prefix or exact title {value!r}')
    return titled[0][0],titled[0][1]['id']
def _commit(domain,changes,kind,root,expected=None):
    return _commit_many([domain],changes,kind,root,expected)

def _commit_many(domains,changes,kind,root,expected=None):
    """One journaled transaction across domains (a promotion moves a note between two), then each
    domain's derived state."""
    domains=list(dict.fromkeys(domains));changes=dict(changes)
    for domain in domains:changes[f'domains/{domain}/.derived-dirty']='dirty\n'
    receipt=transaction(root,changes,kind,expected)
    for domain in domains:
        try:reconcile(domain,root)
        except Exception as exc:
            from .logs import swallowed
            swallowed('notes.reconcile',exc,root)
            receipt['warning']='canonical_committed_index_dirty';receipt['detail']=str(exc)
    return receipt

def ensure_global(root):
    """The global domain's folders, created on first use; returns its name."""
    from .common import global_domain
    root=Path(root);name=global_domain(root);folder=root/'domains'/name
    if not (folder/'notes').is_dir():
        (folder/'notes').mkdir(parents=True,exist_ok=True);(folder/'archive').mkdir(exist_ok=True)
        reconcile(name,root)
    return name

def related(a,b,root):
    """Two domains whose notes already reach each other's sessions: the same domain, or parent and child."""
    if a==b:return True
    from .runtime import parent_domains
    bindings=read_json(Path(root)/'bindings.json',{})
    return a in parent_domains(b,bindings) or b in parent_domains(a,bindings)

def domain_folders(root):
    folder=Path(root)/'domains'
    return sorted(p.name for p in folder.iterdir() if (p/'notes').is_dir()) if folder.is_dir() else []

def _shingles(text,size=3):
    from .note_format import plain
    words=re.findall(r'[^\W_]+',plain(text or '').casefold())
    return {tuple(words[i:i+size]) for i in range(max(1,len(words)-size+1))} if words else set()

NEAR_TITLE=0.9
NEAR_BODY=0.7

def near_identical(title,body,note_type,other,scorer=None):
    """The same note written twice: same type, near-equal title and mostly the same body (3-word shingles).
    Strong enough to merge on without asking; mere title similarity is not."""
    if effective_type(other)!=note_type:return False
    if (scorer or TitleScorer([other])).score(title,other['title'])<NEAR_TITLE:return False
    a,b=_shingles(body),_shingles(other.get('body',''))
    return bool(a and b) and len(a&b)/len(a|b)>=NEAR_BODY

def scope_hint(note):
    """Why a project note may hold beyond its project, or None. Advisory: the agent decides."""
    tags=set(note.get('tags',[]))
    if 'subject/user' in tags:return 'tagged subject/user'
    if 'subject/tooling' in tags:return 'tagged subject/tooling'
    if note.get('type')=='preference' and not note.get('files'):return 'a preference that names no project files'
    return None

def _move_set(moves,root):
    """(changes, expected) moving notes to other domains in one step. Each move is {'note', 'target',
    and optionally 'status', 'superseded_by', 'supersedes'}. IDs and filenames are kept; relative Markdown
    links in the moved notes and in the notes left behind follow the move."""
    from .migration import rewrite_type_links
    root=Path(root);targets={};steps=[];changes={};expected={}
    for move in moves:
        note=move['note'];source=Path(note['path']);status=move.get('status') or note.get('status','active')
        if note.get('learning_procedure'):raise ValueError(f"Learned procedures move domains through review: learn procedure promote-plan {note['learning_procedure'][:8]} --target DOMAIN")
        destination=root/'domains'/move['target']/('notes' if status=='active' else 'archive')/source.name
        if destination.exists() or destination in targets.values():
            raise ValueError('Promotion filename collision: '+destination.relative_to(root).as_posix())
        targets[source.resolve()]=destination;steps.append((move,source,destination,status))
    moved={move['note']['id'] for move in moves}
    for move,source,destination,status in steps:
        note={**move['note'],'domain':move['target'],'status':status,'updated':now(),'path':str(destination)}
        if move.get('superseded_by'):note['superseded_by']=move['superseded_by']
        if move.get('supersedes'):note['supersedes']=sorted(set(note.get('supersedes',[]))|set(move['supersedes']))
        note['body']=rewrite_type_links(note['body'],destination,targets,moved_from=source)
        validate(note,move['target'],root)
        rel=source.relative_to(root).as_posix();changes[rel]=None;expected[rel]=move['note']['revision']
        changes[destination.relative_to(root).as_posix()]=serialize(note)
    for domain in sorted({move['note']['domain'] for move in moves}):
        for referring in inventory(domain,root,retired=True):
            if referring['id'] in moved:continue
            new=rewrite_type_links(referring['body'],Path(referring['path']),targets)
            if new!=referring['body']:
                rel=Path(referring['path']).relative_to(root).as_posix()
                changes[rel]=serialize({**referring,'body':new,'updated':now()});expected[rel]=referring['revision']
    return changes,expected

def _review_date(value):
    if value in (None,''):return None
    text=str(value).strip()[:10]
    try:date.fromisoformat(text)
    except ValueError as exc:raise ValueError('review_by must be a date: YYYY-MM-DD') from exc
    return text

# Only function words: interrogatives and verbs distinguish titles ("How we deploy" vs "Why we deploy").
TITLE_STOP=set('a an the of for and or to in on at by with from is are was were be this that these those our my your its'.split())

def _title_words(text):
    from .search import stem
    return {stem(t) for t in re.findall(r'[^\W_]+',text.casefold()) if t not in TITLE_STOP}

def title_similarity(a,b):
    """Jaccard overlap, or containment when the shorter title has at least three words."""
    x,y=_title_words(a),_title_words(b)
    if not x or not y:return 0.0
    shared=len(x&y);score=shared/len(x|y)
    if min(len(x),len(y))>=3:score=max(score,shared/min(len(x),len(y)))
    return score

# Signs a note records a moment rather than lasting knowledge. Advisory: the note is still saved.
QUALITY_CHECKS=(
    (re.compile(r'(?i)scratchpad|[\\/]temp[\\/]|/tmp/|appdata[\\/]local[\\/]temp'),
     'mentions a temporary or scratchpad path that will not exist in a later session'),
    (re.compile(r'(?i)\b(?:pending|awaiting|not yet (?:run|done|implemented)|nothing implemented yet|next steps?:|todo)\b'),
     'describes an unfinished state; update this note when the outcome is known instead of capturing another'),
    (re.compile(r'(?i)\b\d+\s+(?:tests?|passed)\b|\b(?:tests?|suite)\s+(?:pass(?:es|ed)?|green)\b'),
     'test counts go stale; keep the outcome or the gotcha, not the count'),
    (re.compile(r'(?i)\bcommits?\s+[0-9a-f]{7,40}\b|\bplan\s+[0-9a-f]{32}\b'),
     'commit and plan identifiers are already recorded by git and the journal'),
)

def quality_warnings(note):
    title=note.get('title','');body=note.get('body','')
    found=[]
    for pattern,message in QUALITY_CHECKS:
        unfinished=message.startswith('describes an unfinished')
        if unfinished and note.get('type')=='plan':continue
        # Open steps belong in a bug or risk note's Action Items; elsewhere they are a passing state.
        text=title+'\n'+(without_actions(body) if unfinished and note.get('type') in ACTION_TYPES else body)
        if pattern.search(text):found.append(message)
    return found

# Below this many active notes, document frequencies say little; the overlap rule above is used instead.
IDF_MIN_NOTES=8

def _identifier(token):
    """Version numbers, single letters and short codes name a different thing: "plugin B", "v2", "Phase 3"."""
    return len(token)<=2 or any(c.isdigit() for c in token)

class TitleScorer:
    """Near-duplicate title scores over one domain's active titles: IDF-weighted cosine of the title words,
    with a rare-token veto. A word that appears in few titles and only on one side ("rdlp-api" vs
    "rdlp-plugin", "Phase 1" vs "Phase 2") marks the pair distinct whatever the overlap."""
    def __init__(self,notes):
        from collections import Counter
        self.n=len(notes);self.df=Counter(t for n in notes for t in _title_words(n['title']))
    def idf(self,token):
        import math
        return math.log((self.n+1)/(self.df.get(token,0)+1))+1
    def rare(self,token):return self.df.get(token,0)<=max(2,0.1*self.n)
    def vetoed(self,x,y):
        left=[t for t in x-y if self.rare(t)];right=[t for t in y-x if self.rare(t)]
        # Substituted rare words on both sides, or an added identifier on either side.
        return bool(left and right) or any(_identifier(t) for t in left+right)
    @staticmethod
    def _base(word):
        """The word without one inflectional ending: use, uses, used, using -> us. Different words stay
        different (path, patch)."""
        for suffix in ('ing','ed','es','s','e'):
            if word.endswith(suffix) and len(word)-len(suffix)>=2:return word[:-len(suffix)]
        return word
    @classmethod
    def _fold(cls,x,y):
        """y with inflections of x's words replaced by them ("using" and "use" are one word; the light
        stemmer keeps short stems apart)."""
        y=set(y)
        for t in x-y:
            for u in sorted(y-x):
                if not _identifier(t) and not _identifier(u) and cls._base(t)==cls._base(u):
                    y.discard(u);y.add(t);break
        return y
    def score(self,a,b):
        x,y=_title_words(a),_title_words(b)
        if not x or not y:return 0.0
        y=self._fold(x,y)
        if x==y:return 1.0
        if self.n<IDF_MIN_NOTES:return title_similarity(a,b)
        shared=x&y
        if len(shared)<2 or self.vetoed(x,y):return 0.0
        import math
        weight=lambda words:sum(self.idf(t)**2 for t in words)
        return weight(shared)/math.sqrt(weight(x)*weight(y))

def similar_title(title,notes,threshold=0.8):
    """Near-duplicate guard: the active note whose title scores highest, if it reaches the threshold."""
    scorer=TitleScorer(notes);best=None;top=0.0
    for note in notes:
        score=scorer.score(title,note['title'])
        if score>top:best,top=note,score
    return best if top>=threshold else None

def near_titles(title,notes,low=0.6,high=0.8):
    scorer=TitleScorer(notes)
    scored=sorted(((scorer.score(title,n['title']),n) for n in notes),key=lambda s:-s[0])
    return [n for s,n in scored if low<=s<high][:3]

def build_note(title,body,tags,domain,root,note_type='knowledge',context='',key=None,review_by=None,supersedes=(),files=None,origin=None,note_id=None,extra_tags=()):
    """(path, note, file text) for a new active note, validated like every capture. Writes nothing; capture and
    ingest-plan both create notes through it."""
    stamp=now()
    note={'id':note_id or str(uuid.uuid4()),'title':title.strip(),'body':format_body(body).strip(),'domain':domain,'tags':sorted(set(tags)),'status':'active','created':stamp,'updated':stamp,'type':note_type,'prefix':prefix_for(note_type),'schema_version':3,'context':context or '','capture_key':key}
    review=_review_date(review_by) or default_review_by(note_type)
    if review:note['review_by']=review
    if supersedes:note['supersedes']=sorted(set(supersedes))
    files=clean_files(files)
    if files:note['files']=files
    if origin:note['origin']=str(origin)
    validate(note,domain,root,new=True,extra_tags=extra_tags)
    path=Path(root)/'domains'/domain/'notes'/f"{note['prefix']} - {slug(title)}-{note['id'][:8]}.md"
    return path,note,serialize(note)

def _cross_domain(title,body,domain,root):
    """An exact copy of a new note outside its own domain, as (action, domain, note), or None.

    Global and parent/child domains are already visible from here, so a copy there is a duplicate. A copy
    in an unrelated project means the knowledge holds beyond one project, so it is promoted ('promote').
    _cross_similar handles title-similar notes: a near-identical one in an unrelated project is promoted, a
    similar one seen while capturing to global is superseded by the new global note ('supersede'), and
    anything else is refused with the way to update or promote it."""
    from .common import global_domains
    globals_=set(global_domains(root))
    others=[d for d in domain_folders(root) if d!=domain]
    order=lambda d:(0 if d in globals_ else 1 if related(d,domain,root) else 2,d)
    wanted=title.strip().casefold()
    for other in sorted(others,key=order):
        notes=[n for n in inventory(other,root) if n.get('status','active')=='active']
        if not notes:continue
        visible=other in globals_ or related(other,domain,root)
        exact=next((n for n in notes if n['title'].strip().casefold()==wanted and n['body'].strip()==body.strip()),None)
        if exact:return ('duplicate' if visible else 'promote'),other,exact
    return None

def _cross_similar(title,body,note_type,domain,root):
    """Like _cross_domain, for title-similar notes (skipped with allow_similar)."""
    from .common import global_domains
    globals_=set(global_domains(root));to_global=domain in globals_
    order=lambda d:(0 if d in globals_ else 1 if related(d,domain,root) else 2,d)
    for other in sorted((d for d in domain_folders(root) if d!=domain),key=order):
        notes=[n for n in inventory(other,root) if n.get('status','active')=='active' and not (note_type=='map' and effective_type(n)=='map')]
        similar=similar_title(title,notes) if notes else None
        if not similar:continue
        if other in globals_ or related(other,domain,root):return 'visible',other,similar
        if to_global:return ('supersede' if effective_type(similar)==note_type else 'promotable'),other,similar
        if near_identical(title,body,note_type,similar,TitleScorer(notes)):return 'promote',other,similar
        return 'promotable',other,similar
    return None

def _promote_existing(note,source,root,origin_ref=None):
    """Move one active project note into the global domain as it is (journaled, undoable)."""
    target=ensure_global(root)
    changes,expected=_move_set([{'note':note,'target':target}],root)
    receipt=_commit_many([source,target],changes,'promote',root,expected)
    ref=f"{target}:{note['id']}"
    receipt.update(id=note['id'],domain=target,ref=ref,status='promoted',promoted_from=f"{source}:{note['id']}",
                   hint=f'The same note was already in project {source}, so it now lives in the global domain, '
                        f'where every project sees it. If your version adds detail, run `update {ref}`.')
    return receipt

def capture(title,body,tags,domain,root=None,**metadata):
    root=Path(root or memory_root()).resolve();domain=canonical_domain(domain,root)
    with store_lock(root):
        selected_type=metadata.pop('type','knowledge');derived=prefix_for(selected_type)
        supplied_prefix=metadata.pop('prefix',None)
        if supplied_prefix is not None and supplied_prefix!=derived:
            raise ValueError('Prefix is derived from type; use --type '+selected_type+' and omit --prefix')
        from .common import global_domains
        registered=read_json(root/'bindings.json',{}).get('domains',{})
        if domain==global_domains(root)[0]:ensure_global(root)
        elif domain not in registered and domain not in {'global','_global'}:raise ValueError('Domain is not registered; run bind first')
        body=format_body(body)
        key=metadata.pop('idempotency_key',None);allow_similar=bool(metadata.pop('allow_similar',False))
        supersedes=metadata.pop('supersedes',None) or []
        if isinstance(supersedes,str):supersedes=[supersedes]
        existing=inventory(domain,root,retired=True)
        resolved=[]
        for ref in supersedes:
            ref_id=ref.split(':',1)[1] if ':' in ref else ref
            found=_by_id(ref_id,existing)
            if len(found)>1:raise _ambiguous(domain,found)
            # An unknown ref fails in _change_set below with the usual message.
            resolved.append(found[0]['id'] if found else ref_id)
        supersedes=resolved
        for note in existing:
            if key and note.get('capture_key')==key:
                if note['body'].strip()!=body.strip() or note['title']!=title.strip():raise ValueError('Idempotency key has different content')
                if effective_type(note)!=selected_type:raise ValueError('Existing note has a different type; update that note instead')
                return {'ok':True,'id':note['id'],'status':'existing','path':note['path']}
            if note['title'].casefold()==title.strip().casefold() and note['body'].strip()==body.strip():
                if effective_type(note)!=selected_type:raise ValueError('Existing note has a different type; update that note instead')
                return {'ok':True,'id':note['id'],'status':'duplicate','path':note['path']}
        # A map is one per repo, enforced by its repo folder (maps.capture_conflict below). Every map is titled
        # "<repo> codebase map", so comparing map titles would refuse the second repo's map as a duplicate.
        other_map=lambda n:selected_type=='map' and effective_type(n)=='map'
        if not allow_similar:
            replaced=set(supersedes)
            similar=similar_title(title,[n for n in existing if n.get('status','active')=='active' and n['id'] not in replaced and not other_map(n)])
            if similar:
                ref=f"{domain}:{similar['id']}"
                raise ValueError(f"possible_duplicate conflict: similar to {ref} \"{similar['title']}\". If it is the same subject, "
                                 f"run `update {ref}` (the fact moved on) or capture with `--supersedes {ref}` (it was replaced); "
                                 "pass allow_similar only for a genuinely distinct note")
        # Other domains: global and parent/child notes are already visible here; an unrelated project's copy
        # of the same knowledge belongs in the global domain.
        absorbed=None
        found=_cross_domain(title,body,domain,root) or (None if allow_similar else _cross_similar(title,body,selected_type,domain,root))
        if found:
            action,other,match=found;ref=f"{other}:{match['id']}"
            if action in {'duplicate','promote'} and effective_type(match)!=selected_type:
                raise ValueError(f'Existing note {ref} has a different type; update that note instead')
            if action=='duplicate':return {'ok':True,'id':match['id'],'domain':other,'ref':ref,'status':'duplicate','path':match['path']}
            if action=='promote':return _promote_existing(match,other,root)
            if action=='visible':
                raise ValueError(f"possible_duplicate conflict: similar to {ref} \"{match['title']}\", which this domain already sees. "
                                 f"If it is the same subject, run `update {ref}`; pass allow_similar only for a genuinely distinct note")
            if action=='promotable':
                raise ValueError(f"possible_duplicate conflict: similar to {ref} \"{match['title']}\" in another project. If it holds for both "
                                 f"projects, run `promote-plan {ref}` and then update it; pass allow_similar only for a genuinely distinct note")
            absorbed=(other,match)   # 'supersede': the new global note replaces the project one
        review_by=metadata.pop('review_by',None);context=metadata.pop('context','');files=metadata.pop('files',None);origin=metadata.pop('origin',None)
        if metadata:raise ValueError('Unsupported capture metadata: '+', '.join(metadata))
        if selected_type=='map':
            # One map per repo: a second capture would split what the next session reads.
            from .maps import capture_conflict
            conflict=capture_conflict(clean_files(files),domain,root,read_json(root/'bindings.json',{}),set(supersedes))
            if conflict:raise ValueError(conflict)
        replaces=supersedes+([absorbed[1]['id']] if absorbed else [])
        path,note,text=build_note(title,body,tags,domain,root,selected_type,context=context,key=key,review_by=review_by,supersedes=replaces,files=files,origin=origin)
        changes={path.relative_to(root).as_posix():text};expected={}
        for old in supersedes:
            # The new note and the retirement of what it replaces commit together.
            old_changes,old_expected,_=_change_set(old,domain,'supersede',root,successor=note,pending=changes)
            changes.update(old_changes)
            for rel,revision in old_expected.items():expected.setdefault(rel,revision)
        if absorbed:
            # The project note moves into the global archive as superseded, so its history stays with its successor.
            moved,moved_expected=_move_set([{'note':absorbed[1],'target':domain,'status':'superseded','superseded_by':note['id']}],root)
            changes.update(moved);expected.update(moved_expected)
            result=_commit_many([domain,absorbed[0]],changes,'capture_promote',root,expected)
            result['promoted']=[f"{absorbed[0]}:{absorbed[1]['id']}"]
        else:
            result=_commit(domain,changes,'capture_supersede' if supersedes else 'capture',root,expected or None)
        result.update(id=note['id'],domain=domain,status='created',path=str(path))
        hint=None if domain in global_domains(root) else scope_hint(note)
        if hint:result['scope_hint']={'reason':hint,'next':f'If this holds beyond this project, run `promote-plan {domain}:{note["id"]}`.'}
        near=near_titles(title,[n for n in existing if n.get('status','active')=='active' and n['id'] not in set(supersedes) and not other_map(n)])
        if near:result['similar']=[{'ref':f"{domain}:{n['id']}",'title':n['title']} for n in near]
        warnings=quality_warnings(note)
        if warnings:result['quality_warnings']=warnings
        if supersedes:result['superseded']=[f'{domain}:{old}' for old in supersedes]
        return result

def _change_set(note_id,domain,action,root,expected_revision=None,successor=None,pending=None,**values):
    """Files to write for one note action, with the revisions they must still have. `pending` holds the
    changes of earlier steps in the same transaction (rel path -> text, or None when removed); files are
    read through it, so steps build on each other instead of overwriting one another."""
    pending=pending or {}
    path,note=find_note(note_id,domain,root)
    if note.get('learning_procedure'):
        raise ValueError('Learned procedures change through candidate review or learn procedure controls')
    if expected_revision and note['revision']!=expected_revision:raise ValueError('stale_revision')
    expected={path.relative_to(root).as_posix():note['revision']}
    if pending.get(path.relative_to(root).as_posix()):note=parse_changed(pending[path.relative_to(root).as_posix()],note)
    note=normalize(note)
    if action=='update' and 'type' in values:note=normalize(note,values['type'])
    if 'prefix' in values and values['prefix']!=note['prefix']:raise ValueError('Prefix is derived from type; update the type instead')
    if action=='rename':action,values='update',{'title':values['title']}
    if action=='retag':action,values='update',{'tags':values['tags']}
    folder=path.parent
    if action=='update':
        unknown=set(values)-set(UPDATABLE)
        if unknown:raise ValueError('Cannot update: '+', '.join(sorted(unknown)))
        if not values:raise ValueError('Update requires at least one of: '+', '.join(UPDATABLE))
        if 'title' in values:
            note['title']=str(values['title']).strip()
            if not note['title']:raise ValueError('Invalid title')
        if 'body' in values:note['body']=format_body(str(values['body']))
        if 'tags' in values:note['tags']=sorted(set(values['tags']))
        if 'context' in values:note['context']=str(values['context'] or '')
        if 'files' in values:
            files=clean_files(values['files'])
            if files:note['files']=files
            else:note.pop('files',None)
        if 'review_by' in values:
            review=_review_date(values['review_by'])
            if review:note['review_by']=review
            else:note.pop('review_by',None)
        elif 'body' in values and note.get('status')=='active':
            # Updating a time-sensitive note makes it current again.
            review=default_review_by(note['type'])
            if review:note['review_by']=review
        if note['type']=='map' and note.get('status','active')=='active' and ('type' in values or 'files' in values):
            # One map per repo holds for updates too: retyping a note, or pointing a map at another repo, must not make a second one.
            from .maps import capture_conflict
            conflict=capture_conflict(clean_files(note.get('files')),domain,root,read_json(root/'bindings.json',{}),{note['id']})
            if conflict:raise ValueError(conflict)
    elif action=='confirm':
        # "Still true": a dated confirmation and a fresh review date, without touching content or `updated`.
        if note.get('status','active')!='active':raise ValueError('Only active notes can be confirmed')
        today=date.today().isoformat();confirmed=[d for d in note.get('confirmed') or [] if isinstance(d,str) and d!=today]
        note['confirmed']=(confirmed+[today])[-CONFIRM_KEEP:]
        review=default_review_by(note['type'])
        if review:note['review_by']=review
        elif note.get('review_by'):
            # An explicit date on a type without an interval would keep the note due; move it out like the age rule.
            from .note_types import STALE_DAYS
            from datetime import timedelta
            note['review_by']=(date.today()+timedelta(days=STALE_DAYS)).isoformat()
    elif action in {'resolve','reopen'}:
        # Notes that track work (plan, bug, risk) are resolved when the work is finished; other types are archived by plan.
        if action=='resolve' and (note['type'] not in ACTION_TYPES or note.get('status')!='active'):raise ValueError('Only active plan, bug and risk notes may be resolved')
        if action=='reopen' and (note['type'] not in ACTION_TYPES or note.get('status')!='resolved'):raise ValueError('Only resolved plan, bug and risk notes may be reopened')
        note['status']='resolved' if action=='resolve' else 'active'
        folder=root/'domains'/domain/('archive' if action=='resolve' else 'notes')
    elif action in {'archive','supersede'}:
        note['status']='superseded' if action=='supersede' else 'archived'
        if action=='supersede':
            successor_id=successor['id'] if successor else values['superseded_by']
            if ':' in successor_id:successor_id=successor_id.split(':',1)[1]
            if successor_id==note_id:raise ValueError('Cannot supersede a note with itself')
            note['superseded_by']=successor_id
        folder=root/'domains'/domain/'archive'
    elif action!='delete':raise ValueError('Unknown note action')
    target=path if action=='delete' else folder/f"{note['prefix']} - {slug(note['title'])}-{note_id[:8]}.md"
    # `updated` means the content changed; it drives recency and review age. A tag-, snooze- or files-only edit
    # (triage, backfill) must not reorder recency lists or reset the review clock; confirm never bumps it either.
    if action!='confirm' and not (action=='update' and set(values)<={'tags','review_by','files'}):note['updated']=now()
    note['path']=str(target)
    if target.parent!=path.parent and action!='delete':
        # Moving between notes/ and archive/: the note's own relative links must still reach their files.
        from .migration import rewrite_type_links
        note['body']=rewrite_type_links(note['body'],target,{path.resolve():target},moved_from=path)  # self-links follow the note
    if action!='delete':validate(note,domain,root)
    if target!=path and target.exists():raise ValueError('Portable filename collision')
    changes={path.relative_to(root).as_posix():None}
    if action!='delete':changes[target.relative_to(root).as_posix()]=serialize(note)
    if action=='supersede' and successor is None:
        successor_path,successor_note=find_note(note['superseded_by'],domain,root)
        successor_note['supersedes']=sorted(set(successor_note.get('supersedes',[])+[note_id]))
        successor_note['updated']=now()
        changes[successor_path.relative_to(root).as_posix()]=serialize(successor_note)
        expected[successor_path.relative_to(root).as_posix()]=successor_note['revision']
    if target!=path:
        from .migration import rewrite_type_links
        moved={}
        if any(text is None for text in pending.values()):
            from .repository import parse_text
            for rel,text in pending.items():
                if text is not None:
                    parsed=parse_text(text,root/rel)
                    if parsed and parsed.get('id'):moved.setdefault(parsed['id'],rel)
        for referring in inventory(domain,root,retired=True):
            if referring['id']==note_id:continue
            rel=Path(referring['path']).relative_to(root).as_posix();original=rel
            if rel in pending and pending[rel] is None:
                # An earlier step moved this note (e.g. retired it too): rewrite its new copy, not the old file.
                if referring['id'] not in moved:continue
                rel=moved[referring['id']];referring={**referring,'path':str(root/rel)}
            current=parse_changed(changes.get(rel,pending.get(rel)),referring)
            old=current['body'];new=rewrite_type_links(old,Path(referring['path']),{path.resolve():target})
            if new!=old:
                current['body']=new;current['updated']=now()
                changes[rel]=serialize(current)
                if rel==original:expected[rel]=referring['revision']
    return changes,expected,target

def parse_changed(pending_text,note):
    """A note as this change set will leave it (an earlier step may already have edited it)."""
    if pending_text is None:return note
    from .repository import parse_text
    return {**parse_text(pending_text,note['path']),'path':note['path']}

def change(note_id,domain,action,root=None,expected_revision=None,plan_only=False,**values):
    root=Path(root or memory_root()).resolve()
    domain=canonical_domain(domain,root)
    with store_lock(root):
        _,current=find_note(note_id,domain,root)
        if current.get('learning_procedure'):
            if action!='update':
                raise ValueError(f"Learned procedures change through learn procedure suspend|resume|retire-plan|promote-plan {current['learning_procedure'][:8]}; "
                                 'an update of the title or body is staged for review')
            if expected_revision and current['revision']!=expected_revision:raise ValueError('stale_revision')
            from .learning import stage_edit
            return stage_edit(root,domain,current,values,preview=plan_only)
        changes,expected,target=_change_set(note_id,domain,action,root,expected_revision,**values)
        if plan_only:
            from .governance import plan
            return plan(root,changes,action,[domain],{'id':note_id,'action':action,'files':len(changes)})
        result=_commit(domain,changes,action,root,expected);result.update(id=note_id,path=str(target),status=action)
        if action=='update' and ('body' in values or 'title' in values):
            from .repository import parse
            warnings=quality_warnings(parse(target) or {})
            if warnings:result['quality_warnings']=warnings
        return result

def open_work(domains,root=None,tags=()):
    """[(domain, note, [(1-based index, text) of unchecked Action Items])] for active plan, bug and risk
    notes with open items, newest first. tags: only notes carrying all of them."""
    from .note_format import action_items
    found=[]
    for domain in dict.fromkeys(domains):
        try:notes=inventory(domain,root)
        except (OSError,ValueError):continue
        for note in notes:
            if note.get('type') not in ACTION_TYPES or any(t not in (note.get('tags') or []) for t in tags):continue
            items=[(i+1,text) for i,(done,text) in enumerate(action_items(note.get('body',''))) if not done and text]
            if items:found.append((domain,note,items))
    return sorted(found,key=lambda f:str(f[1].get('updated') or ''),reverse=True)

CONFIRM_KEEP=8

def confirm(note_id,domain,root=None):
    """Record that an active note is still true today: a journaled, undoable change that appends the date to
    `confirmed` (the last CONFIRM_KEEP kept) and moves a time-sensitive note's review date out."""
    root=Path(root or memory_root()).resolve()
    with store_lock(root):
        _,note=find_note(note_id,domain,root)
        if date.today().isoformat() in (note.get('confirmed') or []):
            return {'ok':True,'id':note['id'],'status':'unchanged','confirmed':note['confirmed'],'review_by':note.get('review_by')}
        result=change(note['id'],domain,'confirm',root,note['revision'])
        after=find_note(note['id'],domain,root)[1]
        result.update(confirmed=after.get('confirmed',[]),review_by=after.get('review_by'))
        return result

def check_action(note_id,domain,root=None,item=None,index=None,done=True):
    """Tick (or with done=False untick) one Action Item, chosen by its 1-based index or its text:
    an exact match (case-insensitive) first, else a unique substring. A journaled, undoable update."""
    from .note_format import action_items,set_action
    root=Path(root or memory_root()).resolve()
    with store_lock(root):
        _,note=find_note(note_id,domain,root)
        if note.get('type') not in ACTION_TYPES:raise ValueError('Only plan, bug and risk notes have Action Items')
        items=action_items(note['body'])
        if not items:raise ValueError('This note has no Action Items')
        if index is not None:
            if not 1<=index<=len(items):raise ValueError(f'No Action Item {index}; the note has {len(items)}')
            chosen=index-1
        else:
            wanted=str(item or '').strip().casefold()
            if not wanted:raise ValueError('Name the item by its text or pass --index N')
            matches=[i for i,(_,text) in enumerate(items) if text.casefold()==wanted] or [i for i,(_,text) in enumerate(items) if wanted in text.casefold()]
            if len(matches)>1:raise ValueError('ambiguous_item: '+'; '.join(f'{i+1}. {items[i][1]}' for i in matches[:10])+'. Pass --index N')
            if not matches:raise ValueError('No Action Item matches '+repr(item)+'. Items: '+'; '.join(f'{i+1}. {t}' for i,(_,t) in enumerate(items[:10])))
            chosen=matches[0]
        if items[chosen][0]==done:
            return {'ok':True,'id':note['id'],'status':'unchanged','item':items[chosen][1],'done':done}
        result=change(note['id'],domain,'update',root,note['revision'],body=set_action(note['body'],chosen,done))
        result.update(item=items[chosen][1],index=chosen+1,done=done,open=sum(1 for d,_ in action_items(find_note(note['id'],domain,root)[1]['body']) if not d))
        if done and not result['open']:
            ref=short_ref(domain,note['id'])
            result['hint']=f'Every Action Item is done. If the work is finished, run `resolve {ref}` so the note leaves the active set; if not, add the next step with `update {ref} --add-action`.'
        return result

def main(argv=None):
    from .cli import main as command
    return command(argv)
if __name__=='__main__':raise SystemExit(main())
