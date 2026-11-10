"""Scoped canonical retrieval; SQLite is a disposable derived view."""
from __future__ import annotations
import hashlib,json,math,re,sqlite3,struct,tempfile,os
from collections import Counter
from pathlib import Path
from .common import memory_root,safe_domain,canonical_domain
from .repository import inventory,parse as parse_note,store_lock,digest

# Function words plus conversational filler, so "how is the webcam issue going" searches for "webcam".
STOP=set('''a an the please explain how our works what why when where who which is are was were be been being am do does did done
can could would should will shall may might must you we i me my your it its this that these those they them their there here
to of for and or but in on at by from with about into over under tell show give let lets
going go goes issue issues problem problems still need needs look looking make sure any ideas idea work working fix fixed check
run now just get got know think want like help some thing things also then has have had ok okay yes no thanks thank right
current currently state latest again more much very really'''.split())
MAX_TERMS=32
SNIPPET=450
# A word found in this share of a domain's notes says nothing about which note is meant ("project", "status").
COMMON_SHARE=0.4
COMMON_MIN_NOTES=8

def terms(text):
    return [t for t in re.findall(r'[^\W_]+',text.casefold(),re.UNICODE) if t not in STOP and len(t)>1]

def stem(token):
    """Light English suffix folding, close enough to FTS5's porter stemmer for overlap checks."""
    for suffix,keep in (('ies','y'),('sses','ss'),('ing',''),('ed',''),('es',''),('s','')):
        if token.endswith(suffix) and len(token)-len(suffix)>=3:
            return token[:-len(suffix)]+keep
    return token

def stems(text):return {stem(t) for t in terms(text)}

def query_terms(text):
    """Distinct search terms; very long prompts keep their start and end (the question is usually last)."""
    unique=list(dict.fromkeys(terms(text)))
    if len(unique)>MAX_TERMS:unique=unique[:MAX_TERMS//2]+unique[-MAX_TERMS//2:]
    return unique

def domain_dir(domain,root=None):return Path(root or memory_root())/'domains'/canonical_domain(domain,Path(root or memory_root()))
# Bump when the index schema changes, so an index built by older code counts as stale and is rebuilt.
INDEX_VERSION=2
# An installed semantic model is part of the fingerprint, so installing or removing it makes the index stale.
# Without one the fingerprint is unchanged, so lexical indexes built by earlier releases stay fresh.
def _fingerprint(notes,model=None):
    key=[INDEX_VERSION,[(n.get('id'),n['revision']) for n in notes]]
    if model:key.insert(1,model)
    return hashlib.sha256(json.dumps(key,sort_keys=True).encode()).hexdigest()
# Notes the model ranks this high join the lexical list in reciprocal rank fusion.
SEMANTIC_TOP=20
RRF_K=60

def normalize_file(value):
    """A path as the `files` property stores it: forward slashes, no ./ or trailing slash."""
    text=str(value).strip().replace('\\','/')
    while text.startswith('./'):text=text[2:]
    return text.rstrip('/') if len(text)>1 else text

def file_matches(files,wanted):
    """True when a note's files include this path, or anything under it when it names a folder."""
    wanted=normalize_file(wanted).casefold()
    return any(f.casefold()==wanted or f.casefold().startswith(wanted+'/') for f in map(normalize_file,files or []))

def mentions_file(body,path):
    """A body names this path as a whole path token (backslashes count as slashes)."""
    path=normalize_file(path)
    if '/' not in path and '.' not in path:return False   # a bare word is not a file reference
    pattern=r'(?<![\w./-])'+re.escape(path)+r'(?![\w/-])'
    return bool(re.search(pattern,body.replace('\\','/'),re.I))

def _unchanged(note_path,revision):
    """The canonical file still has the exact bytes the index row was built from."""
    try:return digest(Path(note_path).read_bytes())==revision
    except OSError:return False

def file_notes(path,domains,root=None,limit=2,ensure_fresh=True):
    """Active notes about a file: those listing it in `files` first, then those naming it in the body;
    newest first within each group. Hooks pass ensure_fresh=False: a missing index gives nothing, and a
    matched row whose canonical file changed since the index was built is skipped rather than trusted."""
    root=Path(root or memory_root()).resolve();ranked=[]
    for domain in dict.fromkeys(domains):
        try:folder=domain_dir(domain,root)
        except ValueError:continue
        if not (folder/'notes').is_dir():continue   # never create a domain folder just to look in it
        db_path=folder/'search.sqlite3'
        if ensure_fresh:
            with store_lock(root):
                if not _fresh(domain,root,inventory(domain,root,retired=True)):rebuild_index(domain,root)
        if not db_path.exists():continue
        try:
            db=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)
            try:rows=db.execute("SELECT id,title,type,updated,files,body,path,revision FROM notes WHERE status='active'").fetchall()
            finally:db.close()
        except sqlite3.Error:continue
        from .learning import rows as learning_rows,config as learning_config,supported as learning_supported
        learning_broken=False
        try:
            manifests={p['note_id']:p for p in learning_rows(root,domain,'procedures')}
            learning_on=learning_config(root,domain)['effective'] if manifests else True
        except (ValueError,OSError,KeyError):
            manifests={};learning_on=False;learning_broken=True
        for note_id,title,kind,updated,files,body,note_path,revision in rows:
            if learning_broken and kind=='runbook':continue
            if note_id in manifests and (not learning_on or manifests[note_id]['state']!='active' or not learning_supported(root,domain,manifests[note_id])):continue
            try:listed=json.loads(files or '[]')
            except ValueError:listed=[]
            group=0 if file_matches(listed,path) else 1 if mentions_file(body,path) else None
            if group is not None and not ensure_fresh and not _unchanged(note_path,revision):continue
            if group is not None:
                ranked.append((group,str(updated or ''),{'id':note_id,'domain':domain,'title':title,'type':kind or 'knowledge','updated':str(updated or '')[:10],
                               'match':'files' if group==0 else 'body','snippet':snippet(body,terms(path))}))
                if note_id in manifests:
                    ranked[-1][2].update(procedure_id=manifests[note_id]['id'],procedure_version=manifests[note_id]['current_version'])
    ranked.sort(key=lambda r:r[1],reverse=True);ranked.sort(key=lambda r:r[0])
    return [hit for _,_,hit in ranked[:max(1,limit)]]
def _embedding_text(note):
    """Title plus Executive Summary: what the note is about, without the detail that dilutes a mean vector."""
    from .note_format import sections,plain
    return note['title']+'\n'+plain(sections(note['body']).get('summary',''))

def _vector_identity(root):
    """What stored vectors depend on besides each note's own text: the model (its name and pinned revision) and
    how a note becomes embedding text (INDEX_VERSION). None without a model."""
    from . import semantic
    name=semantic.installed(root)
    return f"{INDEX_VERSION}:{name}@{semantic.MODELS.get(name,{}).get('revision','')}" if name else None

def _stored_vectors(path,identity):
    """{(note id, revision): vector blob} from the index about to be replaced, when the same model made them.
    Embedding is the slow part of a rebuild and a write changes one note, so the rest are carried over."""
    if not identity or not path.exists():return {}
    try:
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            if db.execute('SELECT vectors FROM metadata').fetchone()[0]!=identity:return {}
            return {(note_id,revision):blob for note_id,revision,blob in db.execute('SELECT n.id,n.revision,v.vec FROM vectors v JOIN notes n ON n.rowid=v.rowid')}
        finally:db.close()
    except (sqlite3.Error,TypeError,IndexError):return {}   # an index from before vectors were tracked, or a damaged one

def rebuild_index(domain,root=None):
    from . import semantic
    root=Path(root or memory_root()).resolve(); directory=domain_dir(domain,root);directory.mkdir(parents=True,exist_ok=True)
    with store_lock(root):
        notes=inventory(domain,root,retired=True);model=semantic.load(root)
        identity=_vector_identity(root) if model else None;kept=_stored_vectors(directory/'search.sqlite3',identity)
        fd,name=tempfile.mkstemp(prefix='.search-',suffix='.sqlite3',dir=directory);os.close(fd)
        db=sqlite3.connect(name)
        try:
            db.execute('CREATE TABLE notes(domain TEXT,id TEXT,prefix TEXT,title TEXT,type TEXT,status TEXT,created TEXT,updated TEXT,review_by TEXT,context TEXT,tags TEXT,body TEXT,path TEXT,sha256 TEXT,revision TEXT,schema_version INTEGER,files TEXT,PRIMARY KEY(domain,id))')
            db.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(title,tags,body,tokenize='porter unicode61')")
            db.execute('CREATE TABLE metadata(fingerprint TEXT,vectors TEXT)')
            if model:db.execute('CREATE TABLE vectors(rowid INTEGER PRIMARY KEY,vec BLOB NOT NULL)')
            for n in notes:
                values=(domain,n['id'],n.get('prefix','NOTE'),n['title'],n.get('type','knowledge'),n.get('status','active'),n.get('created'),n.get('updated'),n.get('review_by'),n.get('context',''),json.dumps(n.get('tags',[])),n['body'],n['path'],n['digest'],n['revision'],n.get('schema_version',1),json.dumps(n.get('files') or []))
                cursor=db.execute('INSERT INTO notes VALUES('+','.join('?'*17)+')',values)
                db.execute('INSERT INTO notes_fts(rowid,title,tags,body) VALUES(?,?,?,?)',(cursor.lastrowid,n['title'],' '.join(n.get('tags',[])),n['body']))
                if not model:continue
                blob=kept.get((n['id'],n['revision']))
                if blob is None:
                    vector=model.embed(_embedding_text(n));blob=semantic.to_blob(vector) if vector else None
                if blob:db.execute('INSERT INTO vectors VALUES(?,?)',(cursor.lastrowid,blob))
            db.execute('INSERT INTO metadata VALUES(?,?)',(_fingerprint(notes,semantic.installed(root)),identity));db.commit();db.close();os.replace(name,directory/'search.sqlite3')
        finally:
            db.close()
            if os.path.exists(name):os.unlink(name)
    return len(notes)
def _fresh(domain,root,notes):
    from .semantic import installed
    path=domain_dir(domain,root)/'search.sqlite3'
    if not path.exists():return False
    try:
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:return db.execute('SELECT fingerprint FROM metadata').fetchone()[0]==_fingerprint(notes,installed(root))
        finally:db.close()
    except (sqlite3.Error,IndexError,TypeError):return False

def _fallback(tokens,notes):
    if not tokens:return []
    wanted={stem(t) for t in tokens}
    counts=[Counter([stem(t) for t in terms(n['title'])]*5+[stem(t) for t in terms(' '.join(n.get('tags',[])))]*3+[stem(t) for t in terms(n['body'])]) for n in notes]
    average=sum(sum(c.values()) for c in counts)/max(1,len(counts));result=[]
    frequency_of={t:sum(bool(c[t]) for c in counts) for t in wanted}
    for note,count in zip(notes,counts):
        score=0.0
        for t in wanted:
            frequency=count[t]
            if not frequency:continue
            df=frequency_of[t];idf=math.log(1+(len(notes)-df+0.5)/(df+0.5))
            score+=idf*frequency*2.2/(frequency+1.2*(0.25+0.75*sum(count.values())/max(1,average)))
        if score:result.append((note,score))
    return sorted(result,key=lambda item:item[1],reverse=True)

def _coverage_fts(db,tokens):
    """(rowid -> informative query terms found in title or body, informative terms, common terms).

    Porter-stemmed like the index; tags are excluded because every note carries generic ones."""
    total=db.execute('SELECT count(*) FROM notes_fts').fetchone()[0]
    matches={t:{rowid for (rowid,) in db.execute('SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?',('{title body} : "'+t+'"',))} for t in tokens}
    common={t for t,rows in matches.items() if total>=COMMON_MIN_NOTES and len(rows)>COMMON_SHARE*total}
    found=Counter()
    for t,rows in matches.items():
        if t in common:continue
        for rowid in rows:found[rowid]+=1
    return found,[t for t in tokens if t not in common],common

def _common_fallback(tokens,notes):
    if len(notes)<COMMON_MIN_NOTES:return set()
    bags=[stems(n['title'])|stems(n['body']) for n in notes]
    return {t for t in tokens if sum(stem(t) in bag for bag in bags)>COMMON_SHARE*len(notes)}

def snippet(body,tokens,width=SNIPPET):
    """The densest window of whole-word matches, trimmed to word boundaries, whitespace collapsed."""
    from .note_format import plain
    # Section headings and list markup are layout, not content.
    text=' '.join(plain(body).split())
    if len(text)<=width:return text
    wanted={stem(t) for t in tokens}
    words=[(m.start(),m.end(),stem(m[0].casefold())) for m in re.finditer(r'[^\W_]+',text)]
    hits=[s for s,_,w in words if w in wanted]
    if not hits:start=0
    else:
        best=max(hits,key=lambda s:sum(1 for h in hits if s<=h<s+width))
        start=max(0,best-width//5)
    if start:
        space=text.rfind(' ',0,start)
        start=space+1 if space>=0 else start
    end=min(len(text),start+width)
    if end<len(text):
        space=text.rfind(' ',start,end)
        if space>start:end=space
    return ('… ' if start else '')+text[start:end]+(' …' if end<len(text) else '')

def search_domain(query,domain,limit=3,root=None,filters=None,rebuild=False):
    root=Path(root or memory_root()).resolve();tokens=query_terms(query)
    domain=canonical_domain(domain,root)
    if not tokens:return []
    with store_lock(root):
        notes=inventory(domain,root,retired=True);fresh=_fresh(domain,root,notes)
        if not fresh and rebuild:rebuild_index(domain,root);fresh=True
        if filters and not fresh:raise ValueError('stale_filtered_index: rebuild required before filtered search')
        from .learning import approved_notes
        notes=approved_notes(root,domain,notes,actionable=True)
        active=[n for n in notes if n.get('status','active')=='active']
        if filters:
            # Filters narrow the active notes; retired ones need an explicit status filter.
            active=[n for n in (notes if 'status' in filters else active) if all((v in n.get('tags',[]) if k=='tag' else file_matches(n.get('files'),v) if k=='file' else n.get(k)==v) for k,v in filters.items())]
        ranked=[];engine='canonical_bm25';coverage={};common=set();similarity=None
        if fresh:
            engine='fts5';db=sqlite3.connect((domain_dir(domain,root)/'search.sqlite3').as_uri()+'?mode=ro',uri=True)
            try:
                ids={n['id']:n for n in active};found=[];listed=set()
                # Notes with every term first; when they do not fill the limit, notes with some terms follow, so one
                # weak all-terms match cannot hide a note whose title carries most of the query.
                for joiner in (' AND ',' OR '):
                    match=joiner.join('"'+t+'"' for t in tokens)
                    batch=db.execute('SELECT n.id,bm25(notes_fts,5.0,3.0,1.0),n.rowid FROM notes_fts JOIN notes n ON n.rowid=notes_fts.rowid WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts,5.0,3.0,1.0)',(match,)).fetchall()
                    found+=[row for row in batch if row[0] in ids and row[0] not in listed];listed|={row[0] for row in batch}
                    if len(found)>=limit or len(tokens)==1:break
                rows=found;ranked=[(ids[note_id],-score) for note_id,score,_ in rows]
                per_row,informative,common=_coverage_fts(db,tokens)
                coverage={note_id:per_row.get(rowid,0)/max(1,len(informative)) for note_id,_,rowid in rows} if informative else {note_id:0.0 for note_id,_,_ in rows}
                similarity=_similarities(db,query,ids,root)
                if similarity is not None:
                    engine='fts5+semantic';ranked=_fuse(ranked,similarity,ids)
                    # Notes found by meaning alone still report how many query words they carry.
                    rowids=dict(db.execute('SELECT id,rowid FROM notes').fetchall())
                    for note,_ in ranked:
                        if note['id'] not in coverage:coverage[note['id']]=per_row.get(rowids[note['id']],0)/max(1,len(informative)) if informative else 0.0
            finally:db.close()
        else:
            ranked=_fallback(tokens,active);common=_common_fallback(tokens,notes)
        wanted={stem(t) for t in tokens if t not in common}
        output=[]
        for note,score in ranked[:max(1,min(100,limit))]:
            title=stems(note['title'])
            if note['id'] not in coverage:coverage[note['id']]=len(wanted&(title|stems(note['body'])))/len(wanted) if wanted else 0.0
            field='title' if wanted&title else 'tags' if wanted&stems(' '.join(note.get('tags',[]))) else 'body'
            output.append({'id':note['id'],'domain':domain,'path':note['path'],'title':note['title'],'type':note.get('type','knowledge'),'updated':str(note.get('updated') or '')[:10],
                           'tags':note.get('tags',[]),'status':note.get('status','active'),'snippet':snippet(note['body'],tokens),'score':score,'rank':len(output)+1,'engine':engine,
                           'revision':note['revision'],'sourceField':field,'sourceScope':'active','coverage':coverage[note['id']],
                           'titleOverlap':len(wanted&title),'titleTerms':len(title),'queryTerms':len(wanted),'relevance':round(coverage[note['id']],2)})
            if note.get('learning_procedure'):
                output[-1].update(procedure_id=note['learning_procedure'],procedure_version=note['learning_version'],evidence_status=note.get('learning_state'))
            if similarity is not None:output[-1]['similarity']=round(similarity.get(note['id'],0.0),4)
        return output

def _similarities(db,query,ids,root):
    """note id -> cosine with the query, for candidate notes; None when the index or model has no vectors."""
    from . import semantic
    model=semantic.load(root)
    if not model:return None
    try:rows=db.execute('SELECT n.id,v.vec FROM vectors v JOIN notes n ON n.rowid=v.rowid').fetchall()
    except sqlite3.Error:return None   # built before the model was installed, or without it
    try:wanted=model.embed(query)
    except (ValueError,struct.error):return None
    if wanted is None:return {}
    return {note_id:semantic.cosine(wanted,semantic.from_blob(blob)) for note_id,blob in rows if note_id in ids}

def _fuse(ranked,similarity,ids):
    """Reciprocal rank fusion of the lexical order and the semantic top: 1/(k+lexical rank) + 1/(k+semantic rank).
    Ties keep the lexical order: word matches are the better-tested signal."""
    fused={};notes={};lexical={}
    for rank,(note,_) in enumerate(ranked,1):fused[note['id']]=1/(RRF_K+rank);notes[note['id']]=note;lexical[note['id']]=rank
    top=sorted((i for i,s in similarity.items() if s>0),key=lambda i:(-similarity[i],i))[:SEMANTIC_TOP]
    for rank,note_id in enumerate(top,1):
        fused[note_id]=fused.get(note_id,0)+1/(RRF_K+rank);notes.setdefault(note_id,ids[note_id])
    order=sorted(fused,key=lambda i:(-round(fused[i],12),lexical.get(i,len(ranked)+1),i))
    return [(notes[i],fused[i]) for i in order]

def search_domains(query,domains,limit=3,root=None,filters=None,rebuild=False,weights=None,keep=None):
    """Rank-fused results across domains; the first domain counts double unless weights say otherwise.
    keep filters each domain's hits before the merge, so weak local hits cannot crowd out a strong
    match from another domain before relevance is judged."""
    hits=[]
    for index,domain in enumerate(dict.fromkeys(domains)):
        weight=(weights or {}).get(domain,2 if index==0 else 1)
        found=search_domain(query,domain,max(20,limit),root,filters,rebuild)
        for hit in keep(found) if keep else found:
            hit['score']=weight/(60+hit['rank']);hits.append(hit)
    from . import usage
    if usage.USE_STRENGTH and hits:
        # Reinforcement breaks near-ties: notes in use recently and repeatedly rise a little.
        power=usage.strength_map(Path(root or memory_root()),{h['domain'] for h in hits});top=max(power.values(),default=0) or 1
        for hit in hits:hit['score']*=1+usage.STRENGTH_NUDGE*power.get(f"{hit['domain']}:{hit['id']}",0)/top
    return sorted(hits,key=lambda h:(-h['score'],h['domain'],h['id']))[:limit]
def recent_notes(domain,limit=3,root=None):
    notes=inventory(domain,root)
    for note in notes:note.update(domain=domain,snippet=snippet(note['body'],[],SNIPPET))
    return sorted(notes,key=lambda n:str(n.get('updated',n.get('created',''))),reverse=True)[:max(1,min(20,limit))]
def readonly_query(domain,sql,root=None):
    import time
    path=domain_dir(domain,root)/'search.sqlite3';db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON');deadline=time.monotonic()+5
        db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
        db.set_authorizer(lambda action,*args:sqlite3.SQLITE_OK if action in allowed and not(action==sqlite3.SQLITE_FUNCTION and str(args[1]).lower() in {'load_extension','writefile','readfile'}) else sqlite3.SQLITE_DENY)
        cur=db.execute(sql);return {'columns':[c[0] for c in cur.description or []],'rows':cur.fetchmany(100)}
    finally:db.close()
