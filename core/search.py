"""Scoped canonical retrieval; SQLite is a disposable derived view."""
from __future__ import annotations
import hashlib,json,math,re,sqlite3,struct,tempfile,os
from collections import Counter
from functools import lru_cache
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

@lru_cache(maxsize=1<<16)
def stem(token):
    """Light English suffix folding, close enough to FTS5's porter stemmer for overlap checks."""
    for suffix,keep in (('ies','y'),('sses','ss'),('ing',''),('ed',''),('es',''),('s','')):
        if token.endswith(suffix) and len(token)-len(suffix)>=3:
            return token[:-len(suffix)]+keep
    return token

def stems(text):return {stem(t) for t in set(re.findall(r'[^\W_]+',text.casefold(),re.UNICODE)) if t not in STOP and len(t)>1}   # terms() without its order, stemming each word once

def df_of(vocab,token):
    """How many notes carry this word. The light stemmer and FTS5's porter differ on a final e ("rotates"/"rotate"), so both spellings count."""
    s=stem(token);return max(vocab.get(s,0),vocab.get(s[:-1] if s.endswith('e') else s+'e',0))

def _weighted(text,vocab):
    """[(log(N/df), position, term)] for the distinct terms some note carries, leaving out words most notes carry."""
    total,df=vocab;ranked=[]
    for position,t in enumerate(dict.fromkeys(terms(text))):
        n=df_of(df,t)
        if n and not (total>=COMMON_MIN_NOTES and n>COMMON_SHARE*total):ranked.append((math.log(total/n),position,t))
    return ranked

def idf_terms(text,vocab,limit=MAX_TERMS//2):
    """The prompt's most informative terms, in prompt order: words no note carries (df=0) cannot match and words most
    notes carry say nothing, so both go; the rest rank by log(N/df), ties to later terms (the task usually comes last).
    vocab is (N, {stem: df}) from `vocabulary`."""
    best=sorted(_weighted(text,vocab),reverse=True)[:limit]
    return [t for _,_,t in sorted(best,key=lambda r:r[1])]

def idf_sum(text,vocab):
    """How much the text says about this domain's notes: the summed log(N/df) of its terms."""
    return sum(w for w,_,_ in _weighted(text,vocab))

def query_terms(text,vocab=None):
    """Distinct search terms. A very long prompt keeps its most informative terms when the notes' vocabulary is known
    (see idf_terms), else its start and end (the question is usually last)."""
    unique=list(dict.fromkeys(terms(text)))
    if len(unique)>MAX_TERMS:
        if vocab and vocab[0]:unique=idf_terms(text,vocab)
        else:unique=unique[:MAX_TERMS//2]+unique[-MAX_TERMS//2:]
    return unique

def domain_dir(domain,root=None):return Path(root or memory_root())/'domains'/canonical_domain(domain,Path(root or memory_root()))
# Bump when the index schema changes, so an index built by older code counts as stale and is rebuilt.
INDEX_VERSION=4
# What stored vectors were made from: the model and how a note becomes embedding text. Separate from INDEX_VERSION,
# so a schema change rebuilds the index without re-embedding every note.
EMBED_VERSION=2
# An installed model's vector identity (EMBED_VERSION, name and pinned revision) is part of the fingerprint, so
# installing, removing or re-pinning it, or changing how notes become embedding text, makes the index stale.
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

def active_notes(refs,root=None):
    """{'domain:id': (title, type)} for the active notes among these refs, from the derived index (a hook never
    parses the whole store for a title). A missing index or a retired note is simply absent."""
    root=Path(root or memory_root()).resolve();found={}
    for domain in dict.fromkeys(r.split(':',1)[0] for r in refs):
        ids=[r.split(':',1)[1] for r in refs if r.startswith(domain+':')]
        try:
            db_path=domain_dir(domain,root)/'search.sqlite3'
            if not db_path.exists():continue
            db=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)
            try:rows=db.execute(f"SELECT id,title,type FROM notes WHERE status='active' AND id IN ({','.join('?'*len(ids))})",ids).fetchall()
            finally:db.close()
        except (ValueError,sqlite3.Error):continue
        found.update({f'{domain}:{i}':(title,kind or 'knowledge') for i,title,kind in rows})
    return found

def file_notes(path,domains,root=None,limit=2,ensure_fresh=True):
    """Active notes about a file: those listing it in `files` first, then those naming it in the body;
    newest first within each group. Hooks pass ensure_fresh=False: a missing index gives nothing, and a
    matched row whose canonical file changed since the index was built is skipped rather than trusted."""
    return _file_hits([path],domains,root,limit,ensure_fresh)[path]

def files_notes(paths,domains,root=None,per_file=2):
    """{path: file_notes(path)} for many files in one scan of each domain's index rows (a hook, so never rebuilt): Stop looks up
    every file a shell command or a long edit list touched, which one file_notes call per path would read the index for each time."""
    return _file_hits(list(dict.fromkeys(paths)),domains,root,per_file,False)

def _file_hits(paths,domains,root,limit,ensure_fresh):
    from .notes import is_interim
    root=Path(root or memory_root()).resolve();ranked={p:[] for p in paths}
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
            try:
                try:rows=db.execute("SELECT id,title,type,updated,files,body,path,revision,sources,file_hashes FROM notes WHERE status='active'").fetchall()
                except sqlite3.OperationalError:
                    try:rows=[r+('',) for r in db.execute("SELECT id,title,type,updated,files,body,path,revision,sources FROM notes WHERE status='active'")]   # an index from before file hashes
                    except sqlite3.OperationalError:rows=[r+('','') for r in db.execute("SELECT id,title,type,updated,files,body,path,revision FROM notes WHERE status='active'")]   # an index from before digests
            finally:db.close()
        except sqlite3.Error:continue
        from .learning import published
        try:manifests,servable=published(root,domain)
        except (ValueError,OSError):manifests,servable={},set()
        for note_id,title,kind,updated,files,body,note_path,revision,digested,hashed in rows:
            if note_id in manifests and note_id not in servable:continue
            try:listed=json.loads(files or '[]');digest_sources=json.loads(digested or '[]');file_hashes=json.loads(hashed or '[]')
            except ValueError:listed=[];digest_sources=[];file_hashes=[]
            entries=listed+[s['path'] for s in digest_sources+file_hashes];plain=None;intact=None
            for path in paths:
                if file_matches(entries,path):group=0
                else:
                    plain=body.replace('\\','/').casefold() if plain is None else plain
                    group=1 if normalize_file(path).casefold() in plain and mentions_file(body,path) else None
                if group is None:continue
                if not ensure_fresh:
                    intact=_unchanged(note_path,revision) if intact is None else intact
                    if not intact:break
                hit={'id':note_id,'domain':domain,'title':title,'type':kind or 'knowledge','updated':str(updated or '')[:10],
                     'match':'files' if group==0 else 'body','snippet':snippet(body,terms(path))}
                if digest_sources:hit['sources']=digest_sources
                if file_hashes:hit['file_hashes']=file_hashes
                if is_interim({'type':kind,'title':title,'body':body}):hit['interim']=True
                if note_id in manifests:hit.update(procedure_id=manifests[note_id]['id'],procedure_version=manifests[note_id]['current_version'])
                ranked[path].append((group,str(updated or ''),hit))
    out={}
    for path,found in ranked.items():
        found.sort(key=lambda r:r[1],reverse=True);found.sort(key=lambda r:r[0])
        out[path]=[hit for _,_,hit in found[:max(1,limit)]]
    return out
def _embedding_text(note):
    """Title plus summary: what the note is about, without the detail that dilutes a mean vector."""
    from .note_format import sections,plain
    return note['title']+'\n'+plain(sections(note['body']).get('summary',''))

def _vector_identity(root):
    """What stored vectors depend on besides each note's own text: the model (its name and pinned revision) and
    how a note becomes embedding text (EMBED_VERSION). None without a model."""
    from . import semantic
    name=semantic.installed(root)
    return f"{EMBED_VERSION}:{name}@{semantic.MODELS.get(name,{}).get('revision','')}" if name else None

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

def note_commands(note):
    """The shell commands a note records, for the `commands` column: its `commands` property plus those found in its text (core/commands.py)."""
    explicit=[c for c in (note.get('commands') or []) if isinstance(c,str)]
    try:
        from .commands import extract
        return extract(note['title'],note['body'],explicit)
    except Exception as exc:   # a derived hint must never stop an index rebuild
        from .logs import swallowed
        swallowed('search.note_commands',exc,None);return explicit

def rebuild_index(domain,root=None):
    from . import semantic
    root=Path(root or memory_root()).resolve(); directory=domain_dir(domain,root);directory.mkdir(parents=True,exist_ok=True)
    with store_lock(root):
        notes=inventory(domain,root,retired=True);model=semantic.load(root)
        identity=_vector_identity(root) if model else None;kept=_stored_vectors(directory/'search.sqlite3',identity)
        from . import vectors
        reuse=vectors.reuse(domain,root,identity);fresh={}
        fd,name=tempfile.mkstemp(prefix='.search-',suffix='.sqlite3',dir=directory);os.close(fd)
        db=sqlite3.connect(name)
        try:
            db.execute('CREATE TABLE notes(domain TEXT,id TEXT,prefix TEXT,title TEXT,type TEXT,status TEXT,created TEXT,updated TEXT,review_by TEXT,context TEXT,tags TEXT,body TEXT,path TEXT,sha256 TEXT,revision TEXT,schema_version INTEGER,files TEXT,commands TEXT,repo TEXT,sources TEXT,file_hashes TEXT,checked TEXT,PRIMARY KEY(domain,id))')
            db.execute('CREATE TABLE vocab(stem TEXT PRIMARY KEY,df INTEGER NOT NULL)')
            db.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(title,tags,body,tokenize='porter unicode61')")
            db.execute('CREATE TABLE metadata(fingerprint TEXT,vectors TEXT)')
            if model:db.execute('CREATE TABLE vectors(rowid INTEGER PRIMARY KEY,vec BLOB NOT NULL)')
            df=Counter()
            for n in notes:
                values=(domain,n['id'],n.get('prefix','NOTE'),n['title'],n.get('type','knowledge'),n.get('status','active'),n.get('created'),n.get('updated'),n.get('review_by'),n.get('context',''),json.dumps(n.get('tags',[])),n['body'],n['path'],n['digest'],n['revision'],n.get('schema_version',1),json.dumps(n.get('files') or []),json.dumps(note_commands(n)),n.get('repo') or '',json.dumps(n.get('sources') or []),json.dumps(n.get('file_hashes') or []),n.get('checked') or '')
                cursor=db.execute('INSERT INTO notes VALUES('+','.join('?'*22)+')',values)
                if n.get('status','active')=='active':df.update(stems(n['title']+' '+n['body']))
                db.execute('INSERT INTO notes_fts(rowid,title,tags,body) VALUES(?,?,?,?)',(cursor.lastrowid,n['title'],' '.join(n.get('tags',[])),n['body']))
                if not model:continue
                blob=kept.get((n['id'],n['revision']))
                if blob is None:
                    text=_embedding_text(n);vector=reuse(text)   # same text, same model: the overview unit's embedding
                    if vector is None:
                        vector=model.embed(text)
                        if vector:fresh[hashlib.sha256(text.encode('utf-8')).hexdigest()]=vector   # the unit index reuses it below
                    blob=semantic.to_blob(vector) if vector else None
                if blob:db.execute('INSERT INTO vectors VALUES(?,?)',(cursor.lastrowid,blob))
            db.executemany('INSERT INTO vocab VALUES(?,?)',df.items())
            fingerprint=_fingerprint(notes,_vector_identity(root))
            db.execute('INSERT INTO metadata VALUES(?,?)',(fingerprint,identity));db.commit();db.close();os.replace(name,directory/'search.sqlite3')
        finally:
            db.close();reuse.close()   # before the unit index update, which may replace that file
            if os.path.exists(name):os.unlink(name)
        # The unit index follows the note index; a failure costs section-level matches, never the write.
        try:
            from . import vectors
            vectors.update(domain,root,notes,model,identity,fingerprint,fresh)
        except Exception as exc:
            from .logs import swallowed
            swallowed('search.unit_index',exc,root)
    return len(notes)
def _fresh(domain,root,notes):
    path=domain_dir(domain,root)/'search.sqlite3'
    if not path.exists():return False
    try:
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:return db.execute('SELECT fingerprint FROM metadata').fetchone()[0]==_fingerprint(notes,_vector_identity(root))
        finally:db.close()
    except (sqlite3.Error,IndexError,TypeError):return False

# SessionStart rebuilds a stale index of up to this many notes; a larger one waits for the next write or `reconcile`.
REFRESH_MAX=1000
def refresh_stale(domain,root=None):
    """Rebuild this domain's index when it is stale (built by older code, or the notes changed outside the scripts).
    Hooks read indexes without checking them, so a stale one means no file hints, no vocabulary and canonical-scan
    search. Stored vectors are reused, so only changed notes are embedded. True when rebuilt."""
    root=Path(root or memory_root()).resolve()
    if not (domain_dir(domain,root)/'notes').is_dir():return False
    with store_lock(root):
        notes=inventory(domain,root,retired=True)
        if len(notes)>REFRESH_MAX or _fresh(domain,root,notes):return False
        rebuild_index(domain,root);return True

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

def vocabulary(domains,root=None,wanted=None):
    """(N, {stem: df}) over the active notes of these domains, from their indexes (read-only: a hook never rebuilds).
    An index built before the vocabulary table existed adds nothing. wanted, when given, limits the stems read."""
    root=Path(root or memory_root()).resolve();total=0;df=Counter()
    ask=sorted({v for w in (wanted or ()) for v in (stem(w),stem(w)[:-1] if stem(w).endswith('e') else stem(w)+'e')})
    for domain in dict.fromkeys(domains):
        try:
            db_path=domain_dir(domain,root)/'search.sqlite3'
            if not db_path.exists():continue
            db=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)
            try:
                count=db.execute("SELECT count(*) FROM notes WHERE status='active'").fetchone()[0]
                if wanted is None:rows=db.execute('SELECT stem,df FROM vocab').fetchall()
                else:rows=[r for i in range(0,len(ask),500) for r in db.execute(f"SELECT stem,df FROM vocab WHERE stem IN ({','.join('?'*len(ask[i:i+500]))})",ask[i:i+500])]
            finally:db.close()
        except (ValueError,sqlite3.Error):continue
        total+=count;df.update(dict(rows))
    return total,dict(df)

def search_domain(query,domain,limit=3,root=None,filters=None,rebuild=False):
    from .notes import is_interim
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
        ranked=[];engine='canonical_bm25';coverage={};common=set();similarity=None;units={};semantic_info={'backend':'lexical'}
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
                similarity,units,semantic_info=_semantic(db,query,ids,root,domain,[note_id for note_id,_,_ in rows])
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
            if note.get('sources'):output[-1]['sources']=note['sources']
            if note.get('file_hashes'):output[-1]['file_hashes']=note['file_hashes']
            if is_interim(note):output[-1]['interim']=True
            if note.get('learning_procedure'):
                output[-1].update(procedure_id=note['learning_procedure'],procedure_version=note['learning_version'],evidence_status=note.get('learning_state'))
            if similarity is not None:output[-1]['similarity']=round(similarity.get(note['id'],0.0),4)
            output[-1]['backend']=semantic_info['backend']
            if semantic_info.get('reason'):output[-1]['backendReason']=semantic_info['reason']
            # Scored units, best first: the prompt hook picks its evidence from them; the CLI shows their headings.
            if note['id'] in units:output[-1]['units']=units[note['id']]
        LAST_SEMANTIC[domain]=semantic_info
        return output

_queries={}
def _query_vector(model,query):
    """The query's embedding, computed once per process for a query searched in several domains."""
    key=(id(model),query)
    if key not in _queries:
        if len(_queries)>64:_queries.clear()
        _queries[key]=model.embed(query)
    return _queries[key]

def _similarities(db,query,ids,root):
    """note id -> cosine with the query over the note-level vectors in search.sqlite3; None when the index or model has
    no vectors. The fallback when the unit index (core/vectors.py) is missing or stale."""
    from . import semantic
    model=semantic.load(root)
    if not model:return None
    try:rows=db.execute('SELECT n.id,v.vec FROM vectors v JOIN notes n ON n.rowid=v.rowid').fetchall()
    except sqlite3.Error:return None   # built before the model was installed, or without it
    try:wanted=_query_vector(model,query)
    except (ValueError,struct.error):return None
    if wanted is None:return {}
    return {note_id:semantic.cosine(wanted,semantic.from_blob(blob)) for note_id,blob in rows if note_id in ids}

# How the semantic channel is served; the eval reads it. units: the unit index (core/vectors.py) when fresh;
# notes: only the note-level vectors in search.sqlite3 (the behaviour before units).
SEMANTIC_SOURCE='units'
# domain -> the last search's semantic info (backend, reason, candidate counts, ms), for diagnostics in this process.
LAST_SEMANTIC={}

def _semantic(db,query,ids,root,domain,lexical):
    """(note id -> similarity, or None without a model; note id -> scored units; info) for one domain.

    The unit index finds candidates by meaning on its own, so a note with no query word can rank, each scored by its
    best unit. Lexical candidates are scored too, so the relevance gate sees every hit's similarity. info: backend
    (native, python, notes or lexical), reason when not native, candidate counts and milliseconds. A missing or stale
    unit index falls back to the note-level vectors; nothing here raises for a missing model or extension."""
    import time
    from . import semantic,vectors
    started=time.perf_counter();info={'backend':'lexical'}
    model=semantic.load(root)
    identity=_vector_identity(root) if model else None
    try:fingerprint=db.execute('SELECT fingerprint FROM metadata').fetchone()[0]
    except (sqlite3.Error,TypeError):fingerprint=None
    index,reason=vectors.open_index(domain,root,fingerprint,identity,model.dim if model else 0) if SEMANTIC_SOURCE=='units' else (None,'units_off')
    wanted_ids=[i for i in lexical if i in ids]
    try:
        wanted=None
        if model:
            try:wanted=_query_vector(model,query)
            except (ValueError,struct.error):wanted=None
        if index is not None and model and index.vectors:
            similarity={}
            if wanted is not None:similarity=dict(index.top_notes(wanted,ids,SEMANTIC_TOP))
            info.update(backend=index.backend,vectors=index.vectors,semantic_candidates=len(similarity))   # top_notes may fall back to python
            units=index.units(wanted,list(dict.fromkeys(wanted_ids+list(similarity))))
            for note_id,rows in units.items():
                scored=[u['similarity'] for u in rows if u['similarity'] is not None]
                if scored and note_id not in similarity:similarity[note_id]=max(scored)
            if index.reason:info['reason']=index.reason
        else:
            similarity=_similarities(db,query,ids,root) if model else None
            units=index.units(None,wanted_ids) if index is not None else {}
            if similarity is not None:info.update(backend='notes',reason=reason or 'no_unit_vectors')
            elif reason:info['reason']=reason
        info.update(lexical_candidates=len(lexical),ms=round((time.perf_counter()-started)*1000,1))
        return similarity,units,info
    finally:
        if index is not None:index.close()

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
def relevance_gate(query,root,trace=None,body=True,least=0):
    """The prompt hook's relevance test as a search_domains `keep` filter: enough of the query's informative
    words in a note's title or body, or (with a semantic model) a close match in meaning. trace, when a list,
    receives one row per candidate: ref, rank, engine, engine score, relevance, similarity and the verdict.
    body=False ignores body coverage: for a query spread over a whole long prompt, words found in a note's body
    match by chance ("each", "one", "change"), so only its title (or its meaning) can vouch for the note. least is
    the number of query words body coverage must find: a short piece of a long prompt covers itself too easily."""
    from .semantic import threshold
    count=len({stem(t) for t in query_terms(query or '')});near=threshold(root)
    def relevant(hits):
        kept=[]
        for hit in hits:
            overlap=hit.get('titleOverlap',0);title=hit.get('titleTerms',0);informative=hit.get('queryTerms',count)
            if not informative:gate='dropped:no_informative'   # only words that appear in most notes
            # One-word prompts ("continue", "yes") inject only on a title hit.
            elif informative==1:gate='kept:title' if overlap else 'dropped:one_word_no_title'
            elif body and hit.get('coverage',0)>=0.5 and (informative<4 or hit.get('coverage',0)>0.5) and round(hit.get('coverage',0)*informative)>=least:gate='kept:coverage'
            elif overlap>=2 or (title and overlap/title>=0.5):gate='kept:title'
            # Same meaning in other words ("laid out" for "layout"): only a close semantic match counts.
            elif near is not None and hit.get('similarity',0)>=near:gate='kept:semantic'
            else:gate='dropped:weak'
            if gate.startswith('kept'):kept.append(hit)
            if isinstance(trace,list):
                # keep runs before search_domains replaces score with the fused rank score
                trace.append({'ref':f"{hit['domain']}:{hit['id']}",'rank':hit.get('rank'),'engine':hit.get('engine'),
                              'engine_score':round(hit['score'],4) if isinstance(hit.get('score'),(int,float)) else None,
                              'relevance':hit.get('relevance'),'similarity':hit.get('similarity'),'gate':gate})
        return kept
    return relevant
def note_hits(refs,root=None,query=None):
    """Search-shaped hits for these 'domain:id' refs, active notes only, in the order given, with their units scored
    against query when a model is installed. Read-only: from the derived indexes, never a rebuild. For a follow-up
    prompt ("why?") whose topic is the notes the previous prompt found."""
    from . import semantic,vectors
    from .notes import is_interim
    root=Path(root or memory_root()).resolve();found={}
    model=semantic.load(root);identity=_vector_identity(root) if model else None
    wanted=_query_vector(model,query) if model and query else None
    for domain in dict.fromkeys(r.split(':',1)[0] for r in refs):
        ids=[r.split(':',1)[1] for r in refs if r.startswith(domain+':')]
        try:
            path=domain_dir(domain,root)/'search.sqlite3'
            if not path.exists():continue
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
            try:
                fingerprint=db.execute('SELECT fingerprint FROM metadata').fetchone()[0]
                marks=','.join('?'*len(ids))
                try:rows=db.execute(f"SELECT id,title,type,updated,body,revision,tags,sources,file_hashes FROM notes WHERE status='active' AND id IN ({marks})",ids).fetchall()
                except sqlite3.OperationalError:rows=[r+('','') for r in db.execute(f"SELECT id,title,type,updated,body,revision,tags FROM notes WHERE status='active' AND id IN ({marks})",ids)]   # an index from before digests or file hashes
            finally:db.close()
        except (ValueError,sqlite3.Error,TypeError):continue
        # The same publication rule as search: a learned runbook that is suspended, retired or no longer supported is not served.
        from .learning import published
        try:manifests,servable=published(root,domain)
        except (ValueError,OSError):manifests,servable={},set()
        rows=[r for r in rows if r[0] not in manifests or r[0] in servable]
        index,_=vectors.open_index(domain,root,fingerprint,identity,model.dim if model else 0)
        try:units=index.units(wanted,[r[0] for r in rows]) if index is not None else {}
        finally:
            if index is not None:index.close()
        for note_id,title,kind,updated,body,revision,tags,digested,hashed in rows:
            hit={'id':note_id,'domain':domain,'title':title,'type':kind or 'knowledge','updated':str(updated or '')[:10],'status':'active',
                 'revision':revision,'snippet':snippet(body,[]),'engine':'topic','units':units.get(note_id,[])}
            try:digest_sources=json.loads(digested or '[]');file_hashes=json.loads(hashed or '[]')
            except ValueError:digest_sources=[];file_hashes=[]
            if digest_sources:hit['sources']=digest_sources
            if file_hashes:hit['file_hashes']=file_hashes
            if note_id in manifests:hit.update(procedure_id=manifests[note_id]['id'],procedure_version=manifests[note_id]['current_version'])
            if is_interim({'type':kind,'title':title,'body':body}):hit['interim']=True
            found[f'{domain}:{note_id}']=hit
    return [found[r] for r in refs if r in found]

def recent_notes(domain,limit=3,root=None):
    notes=sorted(inventory(domain,root),key=lambda n:str(n.get('updated',n.get('created',''))),reverse=True)[:max(1,min(20,limit))]
    for note in notes:note.update(domain=domain,snippet=snippet(note['body'],[],SNIPPET))   # only the few that are shown
    return notes
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
