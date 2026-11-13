"""The derived unit index: domains/D/vectors.sqlite3, beside search.sqlite3 and as disposable.

It holds each note's memory units (core/units.py) with their line spans and excerpts, one embedding per distinct unit
text, and, when the optional sqlite-vec extension loads, a vec0 table over those embeddings for exact native KNN.
search.sqlite3 is untouched, so code without the extension still opens and rebuilds it, and deleting this file only
costs section-level matches until the next write rebuilds it.

Backends, chosen per query and reported as `backend` with a `reason` when it is not the first:
  native  sqlite-vec loaded and its vec0 table written in the same generation as the units
  python  the same embeddings scored with a Python dot product (no extension, extension disabled, or vec0 stale)
The caller (search.py) falls back to note-level vectors in search.sqlite3 when this index is missing or stale, and
to FTS5 alone without a model. Hooks only read this file; it is written under the store lock by rebuild_index.
"""
from __future__ import annotations
import array,heapq,importlib.util,json,os,sqlite3,sys,tempfile,time
from pathlib import Path

VERSION=1
FILE='vectors.sqlite3'
SETTINGS='native.json'
# The eval's switches: units False indexes only the overview unit (the note-level text), native False forces Python.
USE_UNITS=True
NATIVE=None
ENV='LIVING_MEMORY_NATIVE'
SUPPORTED=((0,1,6),(0,2,0))   # sqlite-vec versions whose vec0 syntax and cosine metric this code uses: >= first, < second
K_MAX=4096                     # vec0's own ceiling on k
K_START=200
EVIDENCE=2

def _blob(vector):
    if sys.byteorder!='little':vector=array.array('f',vector);vector.byteswap()
    return vector.tobytes()

def _vector(blob):
    vector=array.array('f');vector.frombytes(blob)
    if sys.byteorder!='little':vector.byteswap()
    return vector

def _dot(a,b):
    import operator
    return sum(map(operator.mul,a,b))

# --- the optional extension -------------------------------------------------------------------------------------

_library={}
def _library_path():
    """The vec0 library shipped in the installed sqlite-vec package, found without importing the package (its
    __init__ imports numpy when present). Only that fixed location is ever loaded, never a path from data."""
    if 'path' not in _library:
        try:
            spec=importlib.util.find_spec('sqlite_vec')
            folder=Path(spec.origin).parent if spec and spec.origin else None
        except (ImportError,ValueError,AttributeError):folder=None
        found=None
        if folder:
            for suffix in ('.dll','.so','.dylib'):
                if (folder/('vec0'+suffix)).is_file():found=str(folder/'vec0');break
        _library['path']=found
    return _library['path']

def enabled(root):
    """False when native search is switched off: `semantic native off`, LIVING_MEMORY_NATIVE=0, or the eval."""
    if NATIVE is False or os.environ.get(ENV,'').strip().lower() in ('0','off','false','no'):return False
    try:return json.loads((Path(root)/'.models'/SETTINGS).read_text(encoding='utf-8')).get('native',True) is not False
    except (OSError,ValueError,AttributeError):return True

def set_enabled(root,on):
    from .common import atomic_write,canonical_json
    folder=Path(root)/'.models';folder.mkdir(parents=True,exist_ok=True)
    atomic_write(folder/SETTINGS,canonical_json({'native':bool(on)}))

def _parse(version):
    try:return tuple(int(p) for p in str(version).lstrip('v').split('-')[0].split('.')[:3])
    except ValueError:return None

def load(db):
    """Load sqlite-vec into this connection: (True, version) or (False, reason). Extension loading is switched on
    only around the one trusted load and off again whatever happens."""
    path=_library_path()
    if not path:return False,'not_installed'
    if not hasattr(db,'enable_load_extension'):return False,'load_unsupported'   # Python built without extension loading
    try:
        db.enable_load_extension(True)
        try:db.load_extension(path)
        finally:db.enable_load_extension(False)
        version=db.execute('SELECT vec_version()').fetchone()[0]
    except (sqlite3.Error,AttributeError,OSError):return False,'load_blocked'
    parsed=_parse(version)
    if not parsed or not SUPPORTED[0]<=parsed<SUPPORTED[1]:return False,f'unsupported_version:{version}'
    return True,version

def probe():
    """{'installed', 'loadable', 'version' or 'reason'} for status: sqlite-vec loaded into a throwaway connection."""
    db=sqlite3.connect(':memory:')
    try:ok,info=load(db)
    finally:db.close()
    return {'installed':bool(_library_path()),'loadable':ok,**({'version':info} if ok else {'reason':info})}

# --- writing ----------------------------------------------------------------------------------------------------

SCHEMA='''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS unit(rowid INTEGER PRIMARY KEY,unit_id TEXT UNIQUE NOT NULL,note_id TEXT NOT NULL,note_revision TEXT NOT NULL,
  role TEXT NOT NULL,heading TEXT NOT NULL,line_start INTEGER,line_end INTEGER,sha TEXT NOT NULL,excerpt TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS unit_note ON unit(note_id);
CREATE INDEX IF NOT EXISTS unit_sha ON unit(sha);
CREATE TABLE IF NOT EXISTS blob(sha TEXT PRIMARY KEY,vec BLOB NOT NULL);'''

def _meta(db):return dict(db.execute('SELECT key,value FROM meta'))
def _put(db,**values):db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',[(k,str(v)) for k,v in values.items()])
def _has_knn(db):return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='unit_knn'").fetchone())
def _mode():return 'units' if USE_UNITS else 'overview'

def path_of(domain,root):
    from .search import domain_dir
    return domain_dir(domain,root)/FILE

def update(domain,root,notes,model,identity,fingerprint,known=None):
    """Bring the unit index in line with these notes (all statuses, as search.sqlite3 indexes them). Unchanged notes
    keep their rows; a changed note's units are re-extracted and only texts never embedded before are embedded. The
    whole change is one transaction that bumps `generation`, so a reader sees the old index or the new one. A schema,
    model or mode change, or an unreadable file, rebuilds into a temporary file that then replaces this one.
    Callers hold the store lock. known: text sha256 -> embedding the caller already computed (a new note's overview
    text, embedded once for search.sqlite3). Returns a summary dict."""
    from .common import retry_sharing
    path=path_of(domain,root);dim=model.dim if model else 0
    want={'version':str(VERSION),'identity':identity or '','dim':str(dim),'mode':_mode()}
    rebuild=not path.exists()
    if not rebuild:
        try:
            db=sqlite3.connect(path,timeout=5)
            try:
                meta=_meta(db);rebuild=any(meta.get(k)!=v for k,v in want.items())
                if not rebuild:return _sync(db,notes,model,dim,root,fingerprint,known=known)
            finally:db.close()
        except sqlite3.DatabaseError:rebuild=True   # damaged or not a database: rebuilt below, never trusted
    fd,name=tempfile.mkstemp(prefix='.vectors-',suffix='.sqlite3',dir=path.parent);os.close(fd)
    try:
        db=sqlite3.connect(name)
        try:
            db.executescript(SCHEMA);_put(db,**want,generation=0,native_generation=-1)
            summary=_sync(db,notes,model,dim,root,fingerprint,fresh=True,known=known)
        finally:db.close()
        retry_sharing(lambda:os.replace(name,path))
    finally:
        if os.path.exists(name):os.unlink(name)
    return {**summary,'rebuilt':True}

def _sync(db,notes,model,dim,root,fingerprint,fresh=False,known=None):
    from .units import extract
    db.executescript(SCHEMA)
    meta=_meta(db);generation=int(meta.get('generation') or 0)
    native,_=load(db) if model and enabled(root) else (False,None)
    if native and not _has_knn(db):db.execute(f'CREATE VIRTUAL TABLE unit_knn USING vec0(embedding float[{int(dim)}] distance_metric=cosine)')
    # vec0 rows are kept in step only while they were written with the units; otherwise they are rewritten in full.
    resync=native and str(meta.get('native_generation'))!=str(generation)
    existing=dict(db.execute('SELECT note_id,max(note_revision) FROM unit GROUP BY note_id'))
    current={n['id']:n for n in notes if n.get('id')}
    touched=set();removed=set();embedded=0;extracted=0
    with db:
        for note_id in set(existing)-set(current):
            removed.update(r for (r,) in db.execute('SELECT rowid FROM unit WHERE note_id=?',(note_id,)))
            db.execute('DELETE FROM unit WHERE note_id=?',(note_id,))
        for note_id,note in current.items():
            if existing.get(note_id)==note['revision']:continue
            units=extract(note);extracted+=1
            if not USE_UNITS:units=units[:1]
            old={u:(r,s) for r,u,s in db.execute('SELECT rowid,unit_id,sha FROM unit WHERE note_id=?',(note_id,))}
            for unit in units:
                if model and not db.execute('SELECT 1 FROM blob WHERE sha=?',(unit['sha'],)).fetchone():
                    vector=(known or {}).get(unit['sha'])
                    if vector is None:vector=model.embed(unit['text']);embedded+=bool(vector)
                    if vector:db.execute('INSERT INTO blob VALUES(?,?)',(unit['sha'],_blob(vector)))
                lines=unit['lines'] or [None,None]
                values=(note_id,note['revision'],unit['role'],unit['heading'],lines[0],lines[1],unit['sha'],unit['excerpt'])
                if unit['unit_id'] in old:
                    rowid,sha=old.pop(unit['unit_id'])
                    db.execute('UPDATE unit SET note_id=?,note_revision=?,role=?,heading=?,line_start=?,line_end=?,sha=?,excerpt=? WHERE rowid=?',values+(rowid,))
                    if sha!=unit['sha']:touched.add(rowid)
                else:
                    rowid=db.execute('INSERT INTO unit(note_id,note_revision,role,heading,line_start,line_end,sha,excerpt,unit_id) VALUES(?,?,?,?,?,?,?,?,?)',values+(unit['unit_id'],)).lastrowid
                    touched.add(rowid)
            for rowid,_ in old.values():db.execute('DELETE FROM unit WHERE rowid=?',(rowid,));removed.add(rowid)
        db.execute('DELETE FROM blob WHERE sha NOT IN (SELECT sha FROM unit)')
        if native:
            if resync:
                db.execute('DELETE FROM unit_knn')
                rows=db.execute('SELECT u.rowid,b.vec FROM unit u JOIN blob b ON b.sha=u.sha').fetchall()
            else:
                for rowid in removed|touched:db.execute('DELETE FROM unit_knn WHERE rowid=?',(rowid,))
                rows=[r for r in (db.execute('SELECT u.rowid,b.vec FROM unit u JOIN blob b ON b.sha=u.sha WHERE u.rowid=?',(rowid,)).fetchone() for rowid in sorted(touched)) if r]
            db.executemany('INSERT INTO unit_knn(rowid,embedding) VALUES(?,?)',rows)
        generation+=1
        vectors=db.execute('SELECT count(*) FROM unit u JOIN blob b ON b.sha=u.sha').fetchone()[0]
        _put(db,generation=generation,notes_fingerprint=fingerprint,vectors=vectors,
             **({'native_generation':generation} if native else {}),updated=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
    return {'extracted':extracted,'embedded':embedded,'removed':len(removed),'generation':generation,'native':bool(native)}

def reuse(domain,root,identity):
    """A lookup, text -> stored embedding (array) or None, over this domain's unit index when the same model made it.
    rebuild_index uses it for note-level vectors: a note's overview unit embeds exactly its note-level text, so an
    edit that leaves the title and summary alone (a moved section, a property) costs no embedding."""
    import hashlib
    path=path_of(domain,root);opened={}
    def lookup(text):
        if not identity or not path.exists():return None
        try:
            if 'db' not in opened:
                opened['db']=db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
                opened['ok']=_meta(db).get('identity')==identity
            if not opened['ok']:return None
            row=opened['db'].execute('SELECT vec FROM blob WHERE sha=?',(hashlib.sha256(text.encode('utf-8')).hexdigest(),)).fetchone()
            return _vector(row[0]) if row else None
        except sqlite3.DatabaseError:opened['ok']=False;return None
    def close():
        if opened.get('db') is not None:opened['db'].close()
    lookup.close=close
    return lookup

# --- reading ----------------------------------------------------------------------------------------------------

class Index:
    """A read-only view of one domain's unit index for one query. backend is 'native' or 'python'; reason says why
    native was not used (None when it was)."""
    def __init__(self,db,meta,backend,reason):
        self.db=db;self.meta=meta;self.backend=backend;self.reason=reason
        self.vectors=int(meta.get('vectors') or 0)

    def close(self):self.db.close()

    def top_notes(self,query,eligible,top):
        """[(note id, best unit similarity)] for the `top` eligible notes nearest the query, best first, ties by id.
        A note's score is its best unit's, never a sum, so a long note gains nothing from its length."""
        if self.backend=='native':
            try:
                owner=dict(self.db.execute('SELECT rowid,note_id FROM unit'))
                k=min(self.vectors,K_START,K_MAX);probe=_blob(query)
                while k:
                    # vec0 takes exactly its own `ORDER BY distance`; ties are broken by rowid here.
                    rows=sorted(self.db.execute('SELECT rowid,distance FROM unit_knn WHERE embedding MATCH ? AND k=? ORDER BY distance',(probe,k)),key=lambda r:(r[1],r[0]))
                    best={}
                    for rowid,distance in rows:
                        note_id=owner.get(rowid)
                        if note_id in eligible and note_id not in best:best[note_id]=1.0-distance
                    # Retired or filtered-out notes may fill the first k: widen until enough eligible notes or nothing is left.
                    if len(best)>=top or k>=self.vectors:return sorted(best.items(),key=lambda r:(-r[1],r[0]))[:top]
                    if k>=K_MAX:self.backend,self.reason='python','knn_k_cap';break
                    k=min(self.vectors,k*4,K_MAX)
                else:return []
            except sqlite3.Error:self.backend,self.reason='python','knn_error'   # served below from the same embeddings
        best={}
        for note_id,blob in self.db.execute('SELECT u.note_id,b.vec FROM unit u JOIN blob b ON b.sha=u.sha'):
            if note_id not in eligible:continue
            score=_dot(query,_vector(blob))
            if score>best.get(note_id,-2.0):best[note_id]=score
        return heapq.nsmallest(top,best.items(),key=lambda r:(-r[1],r[0]))

    def units(self,query,note_ids):
        """note id -> its units scored against the query, best first: {unit, role, heading, lines, similarity, excerpt}.
        Scored in Python whatever the backend: it is a handful of notes. query None gives similarity None."""
        found={}
        if not note_ids:return found
        marks=','.join('?'*len(note_ids))
        for note_id,unit_id,role,heading,start,end,excerpt,blob in self.db.execute(
                f'SELECT u.note_id,u.unit_id,u.role,u.heading,u.line_start,u.line_end,u.excerpt,b.vec FROM unit u LEFT JOIN blob b ON b.sha=u.sha WHERE u.note_id IN ({marks}) ORDER BY u.rowid',list(note_ids)):
            score=round(_dot(query,_vector(blob)),4) if (query is not None and blob) else None
            found.setdefault(note_id,[]).append({'unit':unit_id,'role':role,'heading':heading,'lines':[start,end] if start else None,'similarity':score,'excerpt':excerpt})
        for rows in found.values():rows.sort(key=lambda u:(-(u['similarity'] if u['similarity'] is not None else -2.0),u['unit']))
        return found

def open_index(domain,root,fingerprint,identity,dim):
    """(Index, None) when this domain's unit index matches the note index and the model, else (None, reason).
    Read-only; never builds. A stale native table is never queried: the Python path serves the same embeddings."""
    path=path_of(domain,root)
    if not path.exists():return None,'no_unit_index'
    db=None
    try:
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)
        meta=_meta(db)
    except sqlite3.DatabaseError:
        if db is not None:db.close()   # an open handle would block the rebuild's replace on Windows
        return None,'unit_index_unreadable'
    if meta.get('version')!=str(VERSION) or meta.get('mode')!=_mode():db.close();return None,'unit_index_version'
    if meta.get('identity')!=(identity or '') or meta.get('dim')!=str(dim):db.close();return None,'unit_index_model'
    if meta.get('notes_fingerprint')!=fingerprint:db.close();return None,'unit_index_stale'
    backend,reason='python',None
    if not enabled(root):reason='disabled'
    elif meta.get('native_generation')!=meta.get('generation'):reason='native_stale' if _has_knn(db) else 'native_absent'
    else:
        ok,info=load(db)
        if ok:backend='native'
        else:reason=info
    return Index(db,meta,backend,reason),None

def status(domain,root):
    """Counts and freshness for `semantic status`: units, notes, vectors, generation, native table state."""
    path=path_of(domain,root)
    if not path.exists():return {'units':'absent'}
    try:
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            meta=_meta(db);units,notes=db.execute('SELECT count(*),count(DISTINCT note_id) FROM unit').fetchone()
            knn=_has_knn(db);fresh=knn and meta.get('native_generation')==meta.get('generation')
            if fresh:
                ok,_=load(db)
                if ok and db.execute('SELECT count(*) FROM unit_knn').fetchone()[0]!=int(meta.get('vectors') or 0):fresh=False
        finally:db.close()
    except sqlite3.DatabaseError:return {'units':'unreadable'}
    return {'units':units,'notes':notes,'vectors':int(meta.get('vectors') or 0),'generation':int(meta.get('generation') or 0),'mode':meta.get('mode'),
            'native_table':'fresh' if fresh else 'stale' if knn else 'absent','size_bytes':path.stat().st_size,'updated':meta.get('updated')}
