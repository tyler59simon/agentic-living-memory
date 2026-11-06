"""Governed canonical note operations; all persistence goes through transactions."""
from __future__ import annotations
import argparse,json,re,uuid
from datetime import date
from pathlib import Path
from .common import memory_root,safe_domain,canonical_domain
from .repository import inventory,parse,serialize,validate,read_json,transaction,reconcile,store_lock,now,digest
from .note_types import prefix_for,normalize,effective_type,default_review_by
from .note_format import format_body,without_actions,ACTION_TYPES

# Fields an agent may change directly (journaled and undoable, no plan) with `update`.
UPDATABLE=('title','body','tags','type','context','review_by')

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
    changes=dict(changes);changes[f'domains/{domain}/.derived-dirty']='dirty\n'
    receipt=transaction(root,changes,kind,expected)
    try:reconcile(domain,root)
    except Exception as exc:
        receipt['warning']='canonical_committed_index_dirty';receipt['detail']=str(exc)
    return receipt

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

def similar_title(title,notes,threshold=0.8):
    """Near-duplicate guard: high title-term overlap with an active note."""
    # Keeps digits and single characters: "Parallel 0" != "Parallel 1".
    for note in notes:
        if title_similarity(title,note['title'])>=threshold:return note
    return None

def near_titles(title,notes,low=0.6,high=0.8):
    return [n for n in notes if low<=title_similarity(title,n['title'])<high][:3]

def capture(title,body,tags,domain,root=None,**metadata):
    root=Path(root or memory_root()).resolve();domain=canonical_domain(domain,root)
    with store_lock(root):
        selected_type=metadata.pop('type','knowledge');derived=prefix_for(selected_type)
        supplied_prefix=metadata.pop('prefix',None)
        if supplied_prefix is not None and supplied_prefix!=derived:
            raise ValueError('Prefix is derived from type; use --type '+selected_type+' and omit --prefix')
        registered=read_json(root/'bindings.json',{}).get('domains',{})
        if domain not in registered and domain not in {'global','_global'}:raise ValueError('Domain is not registered; run bind first')
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
        if not allow_similar:
            replaced=set(supersedes)
            similar=similar_title(title,[n for n in existing if n.get('status','active')=='active' and n['id'] not in replaced])
            if similar:
                ref=f"{domain}:{similar['id']}"
                raise ValueError(f"possible_duplicate conflict: similar to {ref} \"{similar['title']}\". If it is the same subject, "
                                 f"run `update {ref}` (the fact moved on) or capture with `--supersedes {ref}` (it was replaced); "
                                 "pass allow_similar only for a genuinely distinct note")
        stamp=now()
        review_by=_review_date(metadata.pop('review_by',None)) or default_review_by(selected_type)
        note={'id':str(uuid.uuid4()),'title':title.strip(),'body':body.strip(),'domain':domain,'tags':sorted(set(tags)),'status':'active','created':stamp,'updated':stamp,'type':selected_type,'prefix':derived,'schema_version':3,'context':metadata.pop('context',''),'capture_key':key}
        if review_by:note['review_by']=review_by
        if supersedes:note['supersedes']=sorted(set(supersedes))
        if metadata:raise ValueError('Unsupported capture metadata: '+', '.join(metadata))
        validate(note,domain,root,new=True)
        path=root/'domains'/domain/'notes'/f"{note['prefix']} - {slug(title)}-{note['id'][:8]}.md"
        changes={path.relative_to(root).as_posix():serialize(note)};expected={}
        for old in supersedes:
            # The new note and the retirement of what it replaces commit together.
            old_changes,old_expected,_=_change_set(old,domain,'supersede',root,successor=note)
            changes.update(old_changes);expected.update(old_expected)
        result=_commit(domain,changes,'capture_supersede' if supersedes else 'capture',root,expected or None)
        result.update(id=note['id'],status='created',path=str(path))
        near=near_titles(title,[n for n in existing if n.get('status','active')=='active' and n['id'] not in set(supersedes)])
        if near:result['similar']=[{'ref':f"{domain}:{n['id']}",'title':n['title']} for n in near]
        warnings=quality_warnings(note)
        if warnings:result['quality_warnings']=warnings
        if supersedes:result['superseded']=[f'{domain}:{old}' for old in supersedes]
        return result

def _change_set(note_id,domain,action,root,expected_revision=None,successor=None,**values):
    """Files to write for one note action, with the revisions they must still have."""
    path,note=find_note(note_id,domain,root)
    if expected_revision and note['revision']!=expected_revision:raise ValueError('stale_revision')
    expected={path.relative_to(root).as_posix():note['revision']}
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
        if 'review_by' in values:
            review=_review_date(values['review_by'])
            if review:note['review_by']=review
            else:note.pop('review_by',None)
        elif 'body' in values and note.get('status')=='active':
            # Updating a time-sensitive note makes it current again.
            review=default_review_by(note['type'])
            if review:note['review_by']=review
    elif action in {'resolve','reopen'}:
        if action=='resolve' and (note['type']!='bug' or note.get('status')!='active'):raise ValueError('Only active bug notes may be resolved')
        if action=='reopen' and (note['type']!='bug' or note.get('status')!='resolved'):raise ValueError('Only resolved bug notes may be reopened')
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
    note['updated']=now()
    note['path']=str(target)
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
        for referring in inventory(domain,root,retired=True):
            if referring['id']==note_id:continue
            rel=Path(referring['path']).relative_to(root).as_posix()
            current=parse_changed(changes.get(rel),referring)
            old=current['body'];new=rewrite_type_links(old,Path(referring['path']),{path.resolve():target})
            if new!=old:
                current['body']=new;current['updated']=now()
                changes[rel]=serialize(current);expected[rel]=referring['revision']
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

def main(argv=None):
    from .cli import main as command
    return command(argv)
if __name__=='__main__':raise SystemExit(main())
