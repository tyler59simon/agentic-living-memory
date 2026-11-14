"""Governed canonical note operations; all persistence goes through transactions."""
from __future__ import annotations
import argparse,json,re,uuid
from datetime import date
from pathlib import Path
from .common import memory_root,safe_domain,canonical_domain
from .repository import inventory,parse,serialize,validate,read_json,transaction,reconcile,store_lock,now,digest,clean_files,clean_commands,clean_sources
from .note_types import prefix_for,normalize,effective_type,default_review_by,keep_baseline,KEYED_TYPES
from .note_format import format_body,without_actions,summary_text,ACTION_TYPES
from .digests import stale_note

# Fields an agent may change directly (journaled and undoable, no plan) with `update`.
UPDATABLE=('title','body','tags','type','context','review_by','files','commands','sources')

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
        note={**keep_baseline(dict(move['note'])),'domain':move['target'],'status':status,'updated':now(),'path':str(destination)}
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
                changes[rel]=serialize({**keep_baseline(dict(referring)),'body':new,'updated':now()});expected[rel]=referring['revision']
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

# A belief nobody has confirmed yet (bug, knowledge and decision notes), or labelled **Hypothesis:**. STRONG phrases say so wherever
# they are; WEAK words count only in the title or summary, or one "probably" deep in a long note would mark the whole note.
_STRONG=(r'unconfirmed|not (?:yet )?(?:confirmed|verified|approved|signed)|until (?:it is )?confirmed|pending (?:confirmation|approval|sign-?off)|'
         r'root cause (?:unconfirmed|unknown|unclear)')
_WEAK=r'probably|suspect(?:ed)?|likely cause|hypothes[ie]s'
STRONG=re.compile(r'(?i)\b(?:'+_STRONG+r')\b')
WEAK=re.compile(r'(?i)\b(?:'+_WEAK+r')\b')
INTERIM=re.compile(r'(?i)\b(?:'+_STRONG+'|'+_WEAK+r'|not reproduced)\b')   # both: the capture-time advisory, and a resolution that is itself hedged
INTERIM_TYPES=('bug','knowledge','decision')
# A hypothesis said to be one: a **Hypothesis:** label, a line or bullet that starts "Hypothesis:", or a Hypothesis section.
HYPOTHESIS=re.compile(r'(?im)\*\*hypothesis|^[ \t]*(?:[-*+][ \t]+)?hypothesis:|^##[ \t]+hypothesis\b')
# A labelled cause, fix or outcome, or a section with that heading: when its text is not itself hedged, the note stopped being interim.
RESOLUTION=re.compile(r'(?im)^(?:\s*[-*+]?\s*\*\*(?:root cause|fix|resolution|outcome|confirmed):?\*\*:?[ \t]*|##[ \t]+(?:root cause|fix|resolution|outcome)[ \t]*\n+[ \t]*)(\S[^\n]*)')

def is_interim(note):
    """True for an active note that still states an unconfirmed belief: labelled **Hypothesis:**, or (bug, knowledge,
    decision) worded as one (STRONG anywhere, WEAK in the title or summary) with no labelled root cause, fix or outcome that settles it."""
    if note.get('status','active')!='active':return False
    body=str(note.get('body') or '')
    if HYPOTHESIS.search(body):return True
    if note.get('type') not in INTERIM_TYPES:return False
    title=str(note.get('title') or '');summary=summary_text(body)
    if not (STRONG.search(title+'\n'+body) or WEAK.search(title+'\n'+(summary or body[:400]))):return False
    return not any(not INTERIM.search(m[1]) for m in RESOLUTION.finditer(body))

def unconfirmed(note):
    """The prefix an injected line gives a note that is still an unconfirmed belief, dated by its last update; '' otherwise."""
    return f"UNCONFIRMED (as of {str(note.get('updated') or note.get('created') or '')[:10]}): " if is_interim(note) else ''

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
    (INTERIM,'this reads as an interim hypothesis: say so in the note (e.g. **Hypothesis:** ...) and update REF once it is confirmed'),
)

def quality_checks(note):
    """The content checks of QUALITY_CHECKS that a note trips (no style checks); the review worklist recomputes these."""
    title=note.get('title','');body=note.get('body','')
    found=[]
    for pattern,message in QUALITY_CHECKS:
        unfinished=message.startswith('describes an unfinished')
        if unfinished and note.get('type')=='plan':continue
        # A hypothesis already labelled as one (**Hypothesis:** ...) is what the check asks for.
        if message.startswith('this reads as an interim') and (note.get('type') not in INTERIM_TYPES or HYPOTHESIS.search(body)):continue
        # Open steps belong in a bug or risk note's action items; elsewhere they are a passing state.
        text=title+'\n'+(without_actions(body) if unfinished and note.get('type') in ACTION_TYPES else body)
        if pattern.search(text):found.append(message)
    return found

def quality_warnings(note):
    # Writing habits and type shape (WRITING.md); advisory like the checks above.
    from .style import style_warnings
    return quality_checks(note)+style_warnings(note)

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
    def score(self,a,b,lenient=False):
        """lenient: words that differ only as identifiers ("Sprint 3" / "Sprint 4") count as the same, for numbered status notes."""
        x,y=_title_words(a),_title_words(b)
        if lenient:
            drop={t for t in x^y if _identifier(t)};x,y=x-drop,y-drop
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

LENIENT_CAP=0.79   # numbered status notes are advisory `similar`, never a refusal

def pair_score(scorer,a,b,a_type,b_type):
    """Title score of two notes; two status notes score leniently, capped below the refusal threshold."""
    if a_type==b_type=='status':return min(scorer.score(a,b,lenient=True),LENIENT_CAP)
    return scorer.score(a,b)

def near_titles(title,notes,low=0.6,high=0.8,note_type=None):
    scorer=TitleScorer(notes)
    scored=sorted(((pair_score(scorer,title,n['title'],note_type,effective_type(n)),n) for n in notes),key=lambda s:-s[0])
    return [n for s,n in scored if low<=s<high][:3]

def _rehashed(note,domain,root):
    from .digests import rehash
    return rehash(note['sources'],domain,root,read_json(Path(root)/'bindings.json',{}))

FILE_HASH_TYPES=frozenset({'knowledge','runbook','policy','bug','risk','research'})   # a changed file means these may now be wrong; decisions, plans, statuses and preferences are about a moment
HASHED_FILES_MAX=5

def _file_hashes(note,domain,root,keep=None,fresh=False):
    """[{path, sha256}] for the first HASHED_FILES_MAX regular files a note of FILE_HASH_TYPES lists in `files`: the file's hash now, or the one in
    `keep` ({path: sha256}) for a file already hashed. With fresh=True a kept file that can be read is hashed again (the author vouches for it
    as it is now) and one that cannot be found keeps its old hash, so a deleted file stays visible. Folders, globs, files already in `sources`
    and files that were never hashed and are missing get none."""
    if note.get('type') not in FILE_HASH_TYPES:return []
    from .digests import locate,file_sha256
    bindings=read_json(Path(root)/'bindings.json',{});digested={s['path'] for s in note.get('sources') or []};out=[]
    for path in clean_files(note.get('files')):
        if len(out)>=HASHED_FILES_MAX:break
        if path in digested or len(path)>300 or chr(10) in path or any(c in path for c in '*?['):continue
        if keep and path in keep and not fresh:out.append({'path':path,'sha256':keep[path]});continue
        found,_=locate(root,domain,path,bindings);sha=file_sha256(found) if found else None
        sha=sha or (keep or {}).get(path)
        if sha:out.append({'path':path,'sha256':sha})
    return out

def _set_file_hashes(note,domain,root,keep=None,fresh=False):
    hashes=_file_hashes(note,domain,root,keep,fresh)
    if hashes:note['file_hashes']=hashes
    else:note.pop('file_hashes',None)

def _set_identity(note,files,domain,root,moved=True):
    """A map or brief carries the git identity of its folder (`repo`, system-managed), so a clone of the same origin finds it.
    An update that leaves `files` alone (moved=False) keeps the identity when the folder is gone: the note was shared from another clone."""
    from .maps import note_identity,_resolve
    bindings=read_json(Path(root)/'bindings.json',{})
    identity=note_identity(files,domain,bindings);folder=_resolve(files[0],domain,bindings) if files else None
    if identity:note['repo']=identity
    elif moved or (folder is not None and folder.is_dir()):note.pop('repo',None)

def build_note(title,body,tags,domain,root,note_type='knowledge',context='',key=None,review_by=None,supersedes=(),files=None,origin=None,note_id=None,extra_tags=(),commands=None,sources=None):
    """(path, note, file text) for a new active note, validated like every capture. Writes nothing; capture and
    ingest-plan both create notes through it."""
    stamp=now()
    note={'id':note_id or str(uuid.uuid4()),'title':title.strip(),'body':format_body(body).strip(),'domain':domain,'tags':sorted(set(tags)),'status':'active','created':stamp,'updated':stamp,'type':note_type,'prefix':prefix_for(note_type),'schema_version':3,'context':context or '','capture_key':key}
    review=_review_date(review_by) or default_review_by(note_type)
    if review:note['review_by']=review
    if supersedes:note['supersedes']=sorted(set(supersedes))
    files=clean_files(files)
    if files:note['files']=files
    commands=clean_commands(commands)
    if commands:note['commands']=commands
    sources=clean_sources(sources)
    if sources:note['sources']=sources
    _set_file_hashes(note,domain,root)
    if origin:note['origin']=str(origin)
    if note_type in KEYED_TYPES:_set_identity(note,files,domain,root)
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
        notes=[n for n in inventory(other,root) if n.get('status','active')=='active' and not (note_type in KEYED_TYPES and effective_type(n)==note_type)]
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

DIGEST_KEY='digest:'   # capture_key of a digest note: digest:<path>:<sha256 of that file's content>

def _redigested(existing,sources,skip):
    """Ids of the active notes that digest exactly these source paths with other content hashes: digesting a changed file replaces its old digest."""
    new={s['path']:s['sha256'] for s in clean_sources(sources)}
    return [n['id'] for n in existing if n.get('status','active')=='active' and n['id'] not in skip and n.get('sources')
            and (old:={s['path']:s['sha256'] for s in n['sources']})!=new and set(old)==set(new)]

def _carry_pointers(body,files,old_notes,dest):
    """(body, files, carried links, carried files) with the replaced notes' Links bullets and files added after
    the new note's own (duplicates dropped), so a successor keeps pointing at the documents its predecessor did. Relative
    links are rebased from each replaced note's path to `dest`, the successor's (a file in its folder)."""
    from .note_format import sections,amend
    from .migration import rewrite_type_links
    key=lambda text:re.sub(r'^(?:[-*+]|\d+\.)\s+','',text.strip()).casefold()
    have={key(l) for l in sections(body).get('links','').splitlines() if l.strip()}
    links=[]
    for old in old_notes:
        for line in sections(old.get('body') or '').get('links','').splitlines():
            line=rewrite_type_links(line.strip(),dest,{},moved_from=Path(old['path']))
            if line and key(line) not in have:have.add(key(line));links.append(line)
    if links:body=amend(body,links=links)
    mine=clean_files(files);more=[f for old in old_notes for f in clean_files(old.get('files'))]
    merged=clean_files(mine+more)
    return body,(merged or files),[re.sub(r'^(?:[-*+]|\d+\.)\s+','',l) for l in links],[f for f in merged if f not in mine]

RELATED_GROUPS={'decision':'decision','policy':'decision','preference':'decision'}   # one subject often moves between these

def _related(note,domain,root,skip):
    """Up to 3 active notes of the same type group the new note may replace: one search over its title and summary through the
    relevance gate (semantic matching joins through RRF when installed). Advisory: never raises, and digests and keyed types get none."""
    kind=note.get('type');group=RELATED_GROUPS.get(kind,kind)
    if kind in KEYED_TYPES or note.get('sources'):return []
    try:
        from .search import search_domains,relevance_gate
        query=f"{note['title']} {summary_text(note.get('body') or '') or ''}"
        hits=search_domains(query,[domain],6,root,keep=relevance_gate(query,root))
        found=[h for h in hits if h['id']!=note['id'] and h['id'] not in skip and not h.get('sources') and h.get('type') not in KEYED_TYPES
               and RELATED_GROUPS.get(h.get('type'),h.get('type'))==group]
        return [{'ref':f"{domain}:{h['id']}",'title':h['title'],'type':h['type']} for h in found[:3]]
    except Exception as exc:
        from .logs import swallowed
        swallowed('notes.related',exc,root);return []

USER_BRIEF_MIN_PREFS=2

def _user_brief_hint(note,domain,root):
    """For a preference, a note in a global domain or one tagged subject/user: how the user brief should hear about it. The brief's update
    command when one exists, else (with at least USER_BRIEF_MIN_PREFS global preferences) the capture command that creates it. Advisory: never raises."""
    from .common import global_domains
    try:
        globals_=global_domains(root)
        if note.get('type')=='brief' or not (note.get('type')=='preference' or domain in globals_ or 'subject/user' in (note.get('tags') or [])):return None
        from .briefs import find_user_brief,USER_BRIEF_LABELS
        brief=find_user_brief(root)
        if brief:
            ref=short_ref(brief['domain'],brief['id'])
            return f"user brief {ref}: update it if this changes how you work with the user (update {ref} --set-detail '**Label:** ...')"
        prefs=sum(1 for d in globals_ for n in inventory(d,root) if n.get('type')=='preference' and n.get('status','active')=='active')
        if prefs<USER_BRIEF_MIN_PREFS:return None
        details=' '.join(f"--detail '**{label.strip()}:** ...'" for label in USER_BRIEF_LABELS.split(','))
        return f"no user brief yet ({prefs} global preferences): capture --global --type brief --title 'User brief' --summary '...' {details}"
    except Exception as exc:
        from .logs import swallowed
        swallowed('notes.user_brief_hint',exc,root);return None

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
        supersedes=metadata.pop('supersedes',None) or [];drop_links=bool(metadata.pop('drop_links',False))
        if isinstance(supersedes,str):supersedes=[supersedes]
        existing=inventory(domain,root,retired=True)
        resolved=[]
        for ref in supersedes:
            try:other,ref_id=resolve_ref(ref,domain,root)
            except ValueError:
                # Titles resolve to active notes only, but a retried capture already retired its predecessor.
                retired=[n for n in existing if n.get('status','active')!='active' and n['title'].casefold()==str(ref).strip().casefold()]
                if len(retired)!=1:raise
                other,ref_id=domain,retired[0]['id']
            if other!=domain:raise ValueError(f'--supersedes must name a note in {domain}: {ref} is in {other}')
            resolved.append(ref_id)
        supersedes=resolved
        digesting=bool(metadata.get('sources'))
        if digesting:supersedes+=_redigested(existing,metadata['sources'],set(supersedes))
        for note in existing:
            if key and note.get('capture_key')==key and not (key.startswith(DIGEST_KEY) and note.get('status','active')!='active'):
                # A digest key names a file and its hash: the same file content is already digested, whatever the new wording.
                # A superseding capture stores the submitted body plus the links it inherited; compare against that.
                sent={body.strip()}
                if supersedes and not drop_links:sent.add(_carry_pointers(body,metadata.get('files'),[n for n in existing if n['id'] in supersedes],root/'domains'/domain/'notes'/'x.md')[0].strip())
                if (note['body'].strip() not in sent or note['title']!=title.strip()) and not key.startswith(DIGEST_KEY):raise ValueError('Idempotency key has different content')
                if effective_type(note)!=selected_type:raise ValueError('Existing note has a different type; update that note instead')
                return {'ok':True,'id':note['id'],'status':'existing','path':note['path']}
            if note['title'].casefold()==title.strip().casefold() and note['body'].strip()==body.strip():
                # A digest is about a file at a hash: a note being replaced for that file (its hash moved on) or one already retired is no duplicate, and is never answered with.
                if digesting and (note['id'] in supersedes or note.get('status','active')!='active'):continue
                if effective_type(note)!=selected_type:raise ValueError('Existing note has a different type; update that note instead')
                return {'ok':True,'id':note['id'],'status':'duplicate','path':note['path']}
        # A map is one per repo and a brief one per project folder, enforced by that folder (maps.capture_conflict below).
        # Every map is titled "<repo> codebase map", so comparing titles would refuse the second repo's map as a duplicate.
        # Likewise digests (notes with `sources`): each is about its own file, so similar titles are not duplicates.
        other_map=lambda n:(selected_type in KEYED_TYPES and effective_type(n)==selected_type) or (digesting and bool(n.get('sources')))
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
        found=_cross_domain(title,body,domain,root) or (None if allow_similar or digesting else _cross_similar(title,body,selected_type,domain,root))
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
        review_by=metadata.pop('review_by',None);context=metadata.pop('context','');files=metadata.pop('files',None);origin=metadata.pop('origin',None);commands=metadata.pop('commands',None);sources=metadata.pop('sources',None)
        if metadata:raise ValueError('Unsupported capture metadata: '+', '.join(metadata))
        carried=carried_files=[]
        if supersedes and not drop_links:
            body,files,carried,carried_files=_carry_pointers(body,files,[n for n in existing if n['id'] in supersedes],root/'domains'/domain/'notes'/'x.md')
        if selected_type in KEYED_TYPES:
            # One map per repo, one brief per project folder: a second capture would split what the next session reads.
            from .maps import capture_conflict
            conflict=capture_conflict(clean_files(files),domain,root,read_json(root/'bindings.json',{}),set(supersedes),selected_type)
            if conflict:raise ValueError(conflict)
        replaces=supersedes+([absorbed[1]['id']] if absorbed else [])
        path,note,text=build_note(title,body,tags,domain,root,selected_type,context=context,key=key,review_by=review_by,supersedes=replaces,files=files,origin=origin,commands=commands,sources=sources)
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
        near=near_titles(title,[n for n in existing if n.get('status','active')=='active' and n['id'] not in set(supersedes) and not other_map(n)],note_type=selected_type)
        if near:result['similar']=[{'ref':f"{domain}:{n['id']}",'title':n['title']} for n in near]
        related=_related(note,domain,root,{n['id'] for n in near}|set(supersedes))
        if related:
            result['related']=related
            result['related_hint']=f"if this replaces one of them: supersede REF --by {domain}:{note['id']}; a different subject: nothing to do"
        warnings=quality_warnings(note)
        if warnings:result['quality_warnings']=warnings
        if supersedes:result['superseded']=[f'{domain}:{old}' for old in supersedes];result['carried_links']=carried;result['carried_files']=carried_files
        user=_user_brief_hint(note,domain,root)
        if user:result['user_brief']=user
        return result

def _change_set(note_id,domain,action,root,expected_revision=None,successor=None,pending=None,carried=None,rehash=True,**values):
    """Files to write for one note action, with the revisions they must still have. `pending` holds the
    changes of earlier steps in the same transaction (rel path -> text, or None when removed); files are
    read through it, so steps build on each other instead of overwriting one another. A standalone supersede carries the replaced
    note's links and files into its successor (unless values has drop_links); `carried`, when a dict, receives what moved.
    `rehash=False` (check, rewrite-plan) leaves the recorded file hashes alone: those edits do not re-read the files."""
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
        if 'commands' in values:
            commands=clean_commands(values['commands'])
            if commands:note['commands']=commands
            else:note.pop('commands',None)
        if 'sources' in values:
            sources=clean_sources(values['sources'])
            if sources:note['sources']=sources
            else:note.pop('sources',None)
        elif 'body' in values and note.get('sources') and rehash:note['sources']=_rehashed(note,domain,root)   # rewritten from the file as it is now
        if rehash and set(values)&{'files','sources','body','type'}:
            # Only a body rewrite vouches for the files as they are now (a deleted file keeps its last hash). A files or type change keeps the hash
            # of every file still listed and starts one only for a file without a hash; a title edit does not come here at all.
            _set_file_hashes(note,domain,root,{h['path']:h['sha256'] for h in note.get('file_hashes') or []},fresh='body' in values)
        if 'review_by' in values:
            review=_review_date(values['review_by'])
            if review:note['review_by']=review
            else:note.pop('review_by',None)
        elif 'body' in values and note.get('status')=='active':
            # Updating a time-sensitive note makes it current again.
            review=default_review_by(note['type'])
            if review:note['review_by']=review
        if note['type'] in KEYED_TYPES:_set_identity(note,clean_files(note.get('files')),domain,root,'files' in values or 'type' in values)
        else:note.pop('repo',None)
        if note['type'] in KEYED_TYPES and note.get('status','active')=='active' and ('type' in values or 'files' in values):
            # One map per repo (brief per project folder) holds for updates too: retyping a note, or pointing one at another folder, must not make a second.
            from .maps import capture_conflict
            conflict=capture_conflict(clean_files(note.get('files')),domain,root,read_json(root/'bindings.json',{}),{note['id']},note['type'])
            if conflict:raise ValueError(conflict)
    elif action=='confirm':
        # "Still true": a dated confirmation and a fresh review date, without touching content or `updated`.
        if note.get('status','active')!='active':raise ValueError('Only active notes can be confirmed')
        today=date.today().isoformat();confirmed=[d for d in note.get('confirmed') or [] if isinstance(d,str) and d!=today]
        note['confirmed']=(confirmed+[today])[-CONFIRM_KEEP:]
        if note.get('sources'):note['sources']=_rehashed(note,domain,root)   # still true of the file as it is now
        _set_file_hashes(note,domain,root,{h['path']:h['sha256'] for h in note.get('file_hashes') or []},fresh=True);note['checked']=now()   # a map or brief reads current from here until the next commit
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
    if action!='confirm' and not (action=='update' and set(values)<={'tags','review_by','files','commands','sources'}):
        if action!='delete' and not (action=='update' and 'body' in values):keep_baseline(note)   # `updated` moves; what was checked does not
        note['updated']=now()
        if action=='update' and 'body' in values and note.get('checked'):note['checked']=note['updated']   # a body rewrite is the author's word for the facts as of now
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
        if not values.get('drop_links'):
            body,files,links,pointed=_carry_pointers(successor_note.get('body') or '',successor_note.get('files'),[note],Path(successor_note['path']))
            successor_note['body']=body
            if files:successor_note['files']=files
        else:links,pointed=[],[]
        if isinstance(carried,dict):carried.update(carried_links=links,carried_files=pointed)
        keep_baseline(successor_note);successor_note['updated']=now()
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
                keep_baseline(current);current['body']=new;current['updated']=now()
                changes[rel]=serialize(current)
                if rel==original:expected[rel]=referring['revision']
    return changes,expected,target

def parse_changed(pending_text,note):
    """A note as this change set will leave it (an earlier step may already have edited it)."""
    if pending_text is None:return note
    from .repository import parse_text
    return {**parse_text(pending_text,note['path']),'path':note['path']}

def change(note_id,domain,action,root=None,expected_revision=None,plan_only=False,rehash=True,**values):
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
        carried={}
        changes,expected,target=_change_set(note_id,domain,action,root,expected_revision,carried=carried,rehash=rehash,**values)
        if plan_only:
            from .governance import plan
            return plan(root,changes,action,[domain],{'id':note_id,'action':action,'files':len(changes)})
        result=_commit(domain,changes,action,root,expected);result.update(id=note_id,path=str(target),status=action,**carried)
        if action=='update' and ('body' in values or 'title' in values):
            from .repository import parse
            warnings=quality_warnings(parse(target) or {})
            if warnings:result['quality_warnings']=warnings
        if action=='update':
            from .repository import parse
            user=_user_brief_hint(parse(target) or {},domain,root)
            if user:result['user_brief']=user
        return result

REWRITE_MAX=50
# Maps and briefs keep fixed labelled bullets (`--set-detail` targets them), so a prose rewrite leaves them alone.
REWRITE_FIXED_TYPES=('map','brief')

def rewrite_plan(items,domain=None,root=None):
    """One reviewed plan that replaces the bodies of several notes, for a rewrite of their prose. Each item is
    {"ref", "revision", "body"}; `revision` is the one the new body was written from, so a note changed since is
    refused. Titles never change here: [[links]] and filenames depend on them. The rewrite keeps each note's
    `updated` and `review_by`, because its facts did not change."""
    root=Path(root or memory_root()).resolve()
    if not isinstance(items,list) or not items:raise ValueError('rewrite-plan needs a JSON list of {"ref", "revision", "body"}')
    if len(items)>REWRITE_MAX:raise ValueError(f'rewrite-plan takes at most {REWRITE_MAX} notes; split the batch')
    with store_lock(root):
        changes={};domains=set();rewritten=[];unchanged=[];seen=set()
        for item in items:
            if not isinstance(item,dict):raise ValueError('Each item is an object with ref, revision and body')
            if 'title' in item:raise ValueError('rewrite-plan never changes titles: [[links]] and filenames depend on them')
            if set(item)-{'ref','revision','body'} or not all(isinstance(item.get(k),str) and item[k].strip() for k in ('ref','revision','body')):
                raise ValueError('Each item needs ref, revision and body as non-empty strings, and nothing else')
            d,note_id=resolve_ref(item['ref'],domain,root)
            ref=f'{d}:{note_id}'
            if note_id in seen:raise ValueError(f'{ref} appears twice')
            seen.add(note_id)
            path,note=find_note(note_id,d,root)
            if note.get('status','active')!='active':raise ValueError(f'{ref} is not active')
            if note.get('type') in REWRITE_FIXED_TYPES:
                raise ValueError(f"{ref} is a {note['type']} note; it keeps its labelled layout, so update it with --set-detail")
            step,_,_=_change_set(note_id,d,'update',root,item['revision'],pending=changes,rehash=False,body=item['body'])
            rel=path.relative_to(root).as_posix()
            after=parse_changed(step[rel],note)
            after['updated']=note.get('updated')
            if note.get('review_by'):after['review_by']=note['review_by']
            else:after.pop('review_by',None)
            text=serialize(after)
            if text==path.read_bytes().decode('utf-8'):unchanged.append(ref);continue
            changes[rel]=text;domains.add(d)
            entry={'ref':ref,'title':note['title'],'type':note['type']}
            warnings=quality_warnings(after)
            if warnings:entry['quality_warnings']=warnings
            rewritten.append(entry)
        if not changes:return {'ok':True,'notes':[],'unchanged':unchanged,'hint':'Nothing to rewrite: every body is already as given'}
        from .governance import plan
        result=plan(root,changes,'rewrite',sorted(domains),{'notes':len(rewritten),'titles_unchanged':True})
        result.update(notes=rewritten,unchanged=unchanged)
        return result

def open_work(domains,root=None,tags=()):
    """[(domain, note, [(1-based index, text) of unchecked action items])] for active plan, bug and risk
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

CAUSE_LABEL=re.compile(r'(?im)^\s*[-*+]?\s*\*\*(?:root cause|fix):?\*\*:?\s*\S')

def unresolved_bugs(domains,root=None):
    """[(domain, note)] for active bug notes with no `**Root cause:**` or `**Fix:**` detail, newest first: a bug whose cause nobody has recorded."""
    found=[]
    for domain in dict.fromkeys(domains):
        try:notes=inventory(domain,root)
        except (OSError,ValueError):continue
        found+=[(domain,n) for n in notes if n.get('type')=='bug' and not CAUSE_LABEL.search(n.get('body',''))]
    return sorted(found,key=lambda f:str(f[1].get('updated') or ''),reverse=True)

CONFIRM_KEEP=8

def confirm(note_id,domain,root=None):
    """Record that an active note is still true today: a journaled, undoable change that appends the date to
    `confirmed` (the last CONFIRM_KEEP kept) and moves a time-sensitive note's review date out."""
    root=Path(root or memory_root()).resolve()
    with store_lock(root):
        _,note=find_note(note_id,domain,root)
        if date.today().isoformat() in (note.get('confirmed') or []) and note.get('type') not in KEYED_TYPES and not stale_note(note,root) and not (note.get('checked') and note['checked']<=str(note.get('updated') or '')):   # a map or brief, a note whose file changed since, or one edited since its last confirmation, is confirmed again
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
        if note.get('type') not in ACTION_TYPES:raise ValueError('Only plan, bug and risk notes have action items')
        items=action_items(note['body'])
        if not items:raise ValueError('This note has no action items')
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
        result=change(note['id'],domain,'update',root,note['revision'],rehash=False,body=set_action(note['body'],chosen,done))
        result.update(item=items[chosen][1],index=chosen+1,done=done,open=sum(1 for d,_ in action_items(find_note(note['id'],domain,root)[1]['body']) if not d))
        if done and not result['open']:
            ref=short_ref(domain,note['id'])
            result['hint']=f'Every Action Item is done. If the work is finished, run `resolve {ref}` so the note leaves the active set; if not, add the next step with `update {ref} --add-action`.'
        return result

def main(argv=None):
    from .cli import main as command
    return command(argv)
if __name__=='__main__':raise SystemExit(main())
