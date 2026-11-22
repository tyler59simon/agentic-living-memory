"""Notebook pages, the derived index (NS-2B): domains/<D>/docs.sqlite3.

Pages are chunked and searched like sources (core/sources.py: same tables, chunker, ranking and vectors), one `source` row
per notebook, plus the task lines and frontmatter properties of each page. The index is never canonical and never backed up;
`doc search --rebuild` recreates it. Freshness does not hang on any write path: `doc search`, `doc tasks`, `doc query`,
`search` and `ask` first stat-walk the domain's notebooks for at most WALK_SECONDS, resuming from a cursor, then reindex
changed pages for at most INDEX_SECONDS, and report `pages_stale` when work is left. The prompt hook never reindexes.
Each call walks at least one notebook and indexes at least one page, even past those budgets, so it always makes progress;
of several domains, only the one that had that allowance longest ago gets it. A page that cannot be read is retried after the
others and keeps `pages_stale` true; a folder that cannot be listed keeps the rows of the pages under it until it can be.
Every notebook is indexed, hidden and private ones too; who may see a page is decided when results are served, from the
notebook.json files as they are now. The registered folder notebooks of a domain (core/folder_policy.py) are listed beside the
hosted ones, named nb- plus ten hex digits as in sources._notebook_sources, so their task lines reach `open`. Their search is served
from the sources index, so here they get only file, task and prop rows: no chunks, search text, vocabulary or vectors. Only `open`
serves them (visible with a policy), judged for each page by the strictest policy over its folder. A folder that is hidden or
private now is not listed and its rows are dropped; a dot folder, a link and a page over sources.MAX_BYTES are not walked."""
from __future__ import annotations
import hashlib,json,os,re,sqlite3,tempfile,time
from collections import Counter
from pathlib import Path
from . import sources
from .common import file_lock
from .folder_policy import SERVED

VERSION=2   # 2: the task table has owner and priority; an index of another VERSION is replaced by _open_writable
WALK_SECONDS=0.2
INDEX_SECONDS=0.5
WRITE_INDEX_SECONDS=1.0
TEMP_PREFIX='.docs-'
SCHEMA=sources.SCHEMA+('CREATE TABLE task(file_id INTEGER NOT NULL,line INTEGER,done INTEGER,text TEXT,due TEXT,owner TEXT,priority TEXT)','CREATE INDEX task_file ON task(file_id)',
                       'CREATE TABLE prop(file_id INTEGER NOT NULL,key TEXT,value TEXT)','CREATE INDEX prop_file ON prop(file_id)')
TASK=re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+\[([ xX])\]\s+(.*\S)\s*$')
FRONT=re.compile(r'\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)',re.S)
TABLES={'meta','source','file','chunk','chunk_fts','vocab','task','prop'}
MAX_PROPS=50
MAX_TASKS=500
OPS=('=','!=','<','<=','>','>=','contains','exists','missing')


def db_path(root,domain):return Path(root)/'domains'/domain/'docs.sqlite3'


def _notebooks(root,domain,folders=False):
    """{notebook: folder} of the domain's notebooks with a notebook.json, never through a link (notebook.json included); with
    folders, its registered folder notebooks too (_folder_notebooks; a hosted name wins). None when the notebooks folder is
    there but cannot be listed now: that is not the same as having none."""
    found=_hosted_notebooks(root,domain)
    return {**_folder_notebooks(root,domain),**found} if folders and found is not None else found


def _folder_notebooks(root,domain,policy=None):
    """{name: real folder} of the registered folder notebooks of a domain that agents may reach now (searchable or injectable,
    none inside a hidden or private folder), named nb- plus 10 hex of the folder as sources._notebook_sources names them.
    A broken registry has none. policy: a folder_policy.Policy the caller reads for the whole call."""
    from .folder_policy import Policy,key
    try:
        policy=policy or Policy(root)
        if policy.broken:return {}
        out={}
        for path,_ in policy.registered_sources(domain):
            folder=policy.real(path)
            if os.path.isdir(folder):out['nb-'+hashlib.sha256(key(folder).encode()).hexdigest()[:10]]=Path(folder)
        return out
    except (OSError,ValueError):return {}


def _hosted_notebooks(root,domain):
    from .docs import SLUG,_is_link
    base=Path(root)/'domains'/domain/'notebooks'
    if _is_link(Path(root)/'domains') or _is_link(Path(root)/'domains'/domain) or not base.is_dir() or _is_link(base):return {}
    try:entries=sorted(os.scandir(base),key=lambda e:e.name)
    except OSError:return None
    return {e.name:Path(e.path) for e in entries if SLUG.fullmatch(e.name) and e.is_dir(follow_symlinks=False) and not _is_link(Path(e.path))
            and os.path.isfile(os.path.join(e.path,'notebook.json')) and not _is_link(Path(e.path)/'notebook.json')}


def _walk_pages(folder,deadline=None,unread=None,inner=None):
    """{page path: (size, mtime_ns)} of a notebook's pages (no attachments, proposals or links), or None past the deadline.
    Raises OSError when the notebook folder itself cannot be listed. A subfolder that cannot be listed is added to `unread`
    (its pages are unknown, not gone); without that list it raises too. inner: for a folder notebook, the compare keys of the
    other folder notebooks, whose folders are not entered; it also means no dot folders, no page over sources.MAX_BYTES and no
    reserved names (those are a hosted notebook's)."""
    from .docs import RESERVED,_is_link
    from .folder_policy import key
    found={};stack=['']
    while stack:
        if deadline is not None and time.monotonic()>deadline:return None
        rel=stack.pop()
        try:entries=list(os.scandir(folder/rel if rel else folder))
        except OSError:
            if not rel or unread is None:raise
            unread.append(rel);continue
        for e in entries:
            path=f'{rel}/{e.name}' if rel else e.name
            try:
                if (inner is None and not rel and e.name in RESERVED) or _is_link(Path(e.path)):continue
                if e.is_dir(follow_symlinks=False):
                    if inner is None or not (e.name.startswith('.') or key(e.path) in inner):stack.append(path)
                elif e.is_file(follow_symlinks=False) and e.name.casefold().endswith('.md'):
                    info=e.stat(follow_symlinks=False)
                    if inner is None or info.st_size<=sources.MAX_BYTES:found[path]=(info.st_size,info.st_mtime_ns)
            except OSError:continue
    return found


def _lock(root,domain,timeout):
    return file_lock(Path(root)/'hooks'/f'docs-{domain}.lock',timeout=timeout)


def _create(path):
    db=sqlite3.connect(path,timeout=30)
    for statement in SCHEMA:db.execute(statement)
    db.execute('INSERT INTO meta VALUES(?,?)',('version',str(VERSION)));db.commit();return db


def _sound(db):
    """Whether an open index has this VERSION and every table it needs."""
    return sources._meta(db,'version')==str(VERSION) and TABLES<={n for (n,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _open_writable(root,domain,rebuild=False):
    """The index open for writing; a missing one, one of another VERSION or schema, or rebuild makes a new file first."""
    target=db_path(root,domain)
    if not rebuild and target.is_file():
        db=None
        try:
            db=sqlite3.connect(target,timeout=30)
            if _sound(db):return db
        except sqlite3.Error:pass
        if db is not None:db.close()   # a damaged or older file is replaced below, which Windows refuses while it is open
    fd,temp=tempfile.mkstemp(prefix=TEMP_PREFIX,suffix='.sqlite3',dir=target.parent);os.close(fd);os.unlink(temp)
    try:
        _create(temp).close();os.replace(temp,target)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return sqlite3.connect(target,timeout=30)


def _readonly(root,domain):
    db=None
    try:
        path=db_path(root,domain)
        if not path.is_file():return None
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1.0)
        if _sound(db):return db
    except (ValueError,sqlite3.Error):pass
    if db is not None:db.close()
    return None


# ---- what a page holds ----------------------------------------------------------------------------------------------
def _decode(data):
    try:return data.decode('utf-8-sig')
    except UnicodeDecodeError:return data.decode('cp1252',errors='replace')


def _scalar(value):
    value=value.strip()
    if len(value)>=2 and value[0]==value[-1] and value[0] in '"\'':value=value[1:-1]
    return value


def properties(text):
    """[(key, value)] of a page's YAML frontmatter, read as flat keys: a scalar, an inline [a, b] list or a block list of
    `- item` lines gives one pair per value. Nested maps and anything else are skipped, never guessed."""
    m=FRONT.match(text)
    if not m:return []
    out=[];key=None
    for line in m[1].splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        item=re.match(r'^\s+-\s+(.*)$',line) or re.match(r'^-\s+(.*)$',line)
        if item and key:out.append((key,_scalar(item[1])));continue
        pair=re.match(r'^([A-Za-z0-9_][\w .-]{0,63}?)\s*:(?:\s+(.*))?$',line)
        if not pair:key=None;continue
        key=pair[1].strip();value=(pair[2] or '').strip()
        if not value:continue   # a block list (or a nested map) follows
        if value.startswith('[') and value.endswith(']'):out+=[(key,_scalar(v)) for v in value[1:-1].split(',') if v.strip()]
        else:out.append((key,_scalar(value)))
        key=None if value else key
    return out[:MAX_PROPS]


def tasks(text):
    """[(line, done, text, due, owner, priority)] of a page's task lines (`- [ ]`, `- [x]`), outside fenced code and frontmatter.
    text is what follows the box, cut at 500 characters; the fields are read from that text by note_format.item_fields."""
    from .note_format import item_fields
    out=[];fence=None;lines=text.replace('\r\n','\n').replace('\r','\n').split('\n')
    start=FRONT.match(text);skip=start[0].count('\n') if start else 0
    for number,line in enumerate(lines,1):
        if number<=skip:continue
        f=sources.FENCE.match(line)
        if f:fence=None if fence==f[1] else (fence or f[1]);continue
        if fence:continue
        m=TASK.match(line)
        if m:
            cut=m[2][:500];fields=item_fields(cut)[1];out.append((number,m[1]!=' ',cut,fields['due'],fields['owner'],fields['priority']))
            if len(out)>=MAX_TASKS:break
    return out


def title_of(text,rel):
    """A page's title: frontmatter `title`, else its first level-one heading, else its file name."""
    for key,value in properties(text):
        if key=='title' and value:return value[:200]
    for line in text.split('\n')[:200]:
        m=sources.ATX.match(line.rstrip('\r'))
        if m and len(m[1])==1 and m[2]:return m[2][:200]
    return os.path.splitext(rel.rsplit('/',1)[-1])[0]


def under(rel,folders):
    """Whether a page path lies in one of these folders (paths within a notebook)."""
    return any(rel.startswith(f+'/') for f in folders)


# ---- refresh --------------------------------------------------------------------------------------------------------
def _drop(db,file_id,delta):
    sources._drop_file(db,file_id,delta)
    db.execute('DELETE FROM task WHERE file_id=?',(file_id,));db.execute('DELETE FROM prop WHERE file_id=?',(file_id,))
    db.execute('DELETE FROM file WHERE id=?',(file_id,))


def _index_page(db,nb,folder,rel,size,mtime,old,delta,chunked=True):
    """Bring one page's rows up to date; False when it could not be read now. chunked=False (a folder notebook, whose search the
    sources index serves): the file, task and prop rows only, with no chunks, search text, vocabulary or vectors."""
    try:data=(folder/rel).read_bytes()
    except OSError:return False
    sha=hashlib.sha256(data).hexdigest()
    if old and old[3]==sha:
        db.execute('UPDATE file SET mtime_ns=?,size=? WHERE id=?',(mtime,size,old[0]));return True
    if old:_drop(db,old[0],delta)
    text=_decode(data);clean,redacted=sources.redact_text(text);front=FRONT.match(clean)
    chunks=sources.chunk_file(rel,'\n'*front[0].count('\n')+clean[front.end():] if front else clean) if chunked else []   # properties are not prose: blank lines keep the numbering
    file_id=db.execute('INSERT INTO file(source,path,mtime_ns,size,sha256,chunks,redacted,title) VALUES(?,?,?,?,?,?,?,?)',
                       (nb,rel,mtime,size,sha,len(chunks),redacted,title_of(clean,rel))).lastrowid
    for c in chunks:
        cid=db.execute('INSERT INTO chunk(file_id,source,path,kind,symbol,line_start,line_end,sha256) VALUES(?,?,?,?,?,?,?,?)',
                       (file_id,nb,rel,c['kind'],c['symbol'],c['line_start'],c['line_end'],hashlib.sha256(c['text'].encode()).hexdigest())).lastrowid
        db.execute('INSERT INTO chunk_fts(rowid,symbol,path,names,text) VALUES(?,?,?,?,?)',(cid,c['symbol'],rel,sources._names(c['symbol'],c['text']),c['text']))
        for s in sources._stems(c['symbol'],rel,c['text']):delta[s]+=1
    db.executemany('INSERT INTO task VALUES(?,?,?,?,?,?,?)',[(file_id,n,int(d),t,due,owner,priority) for n,d,t,due,owner,priority in tasks(clean)])
    db.executemany('INSERT INTO prop VALUES(?,?,?)',[(file_id,k,v) for k,v in properties(clean)])
    return True


def refresh(root,domain,walk_until=None,index_until=None,notebooks=None,rebuild=False,lock_timeout=0,embed=True,progress=True):
    """Bring the index up to date within the deadlines (time.monotonic() values; None: no limit). notebooks limits the walk
    to those (a write's own notebook). progress: walk one notebook and index one page even past the deadlines. Returns
    {stale, indexed, removed}, with `error` when the index could not be opened or replaced (another process held it, or
    Windows refused to replace a file a reader has open); stale is True then, and when work was left."""
    root=Path(root);result={'stale':False,'indexed':0,'removed':0}
    if not (Path(root)/'domains'/domain/'notebooks').is_dir() and not db_path(root,domain).is_file() and not _folder_notebooks(root,domain):return result
    try:
        with _lock(root,domain,lock_timeout):
            present=_notebooks(root,domain)   # under the lock: a notebook a writer just indexed is not mistaken for a deleted one
            if present is None:result.update(stale=True,error='The notebooks folder cannot be listed now');return result
            folders={n:p for n,p in _folder_notebooks(root,domain).items() if n not in present};present={**folders,**present}
            db=_open_writable(root,domain,rebuild)
            try:return _refresh(db,root,domain,present,walk_until,index_until,notebooks,embed,result,progress,folders)
            finally:db.close()
    except (TimeoutError,OSError,sqlite3.Error) as exc:
        result.update(stale=True,error=str(exc) or type(exc).__name__);return result


def _refresh(db,root,domain,present,walk_until,index_until,only,embed,result,progress=True,folders=None):
    """folders: the folder notebooks among present, {name: real folder}."""
    from .folder_policy import key
    delta=Counter();folders=folders or {}
    for (gone,) in db.execute('SELECT name FROM source').fetchall():
        if gone not in present:
            for (file_id,) in db.execute('SELECT id FROM file WHERE source=?',(gone,)).fetchall():_drop(db,file_id,delta);result['removed']+=1
            db.execute('DELETE FROM source WHERE name=?',(gone,))
    names=sorted(n for n in present if only is None or n in only)
    cursor=sources._meta(db,'cursor') or ''
    if only is None and cursor in names:names=names[names.index(cursor):]+names[:names.index(cursor)]
    todo=[];walked=0;cut=None;unread=0;listed=set()
    for nb in names:
        if walk_until is not None and (walked or not progress) and time.monotonic()>walk_until:cut=nb;break
        skipped=[]
        inner={key(p) for n,p in folders.items() if n!=nb} if nb in folders else None
        try:listing=_walk_pages(present[nb],walk_until if (walked or not progress) else None,skipped,inner)   # the first notebook is finished, so a call makes progress
        except OSError:unread+=1;continue   # this notebook cannot be listed now: its rows stay as they are
        unread+=bool(skipped)
        if listing is None:cut=nb;break
        walked+=1;listed.add(nb)
        db.execute('INSERT INTO source(name,kind,root,implicit,config) VALUES(?,?,?,0,?) ON CONFLICT(name) DO UPDATE SET root=excluded.root',(nb,'notebook',present[nb].as_posix(),''))
        have={p:(i,m,s,h) for i,p,m,s,h in db.execute('SELECT id,path,mtime_ns,size,sha256 FROM file WHERE source=?',(nb,))}
        for rel,(size,mtime) in sorted(listing.items()):
            old=have.get(rel)
            if not old or (old[1],old[2])!=(mtime,size):todo.append((nb,rel,size,mtime,old))
        for rel,(file_id,*_) in have.items():
            if rel not in listing and not under(rel,skipped):_drop(db,file_id,delta);result['removed']+=1   # a page in a folder that could not be listed stays
    if only is None:sources._set_meta(db,'cursor',cut or '')
    try:failed=set(json.loads(sources._meta(db,'failed') or '[]'))
    except ValueError:failed=set()
    todo.sort(key=lambda t:f'{t[0]}/{t[1]}' in failed)   # pages that could not be read last time go last, so they never hold up the others
    left=0;tried=0;now_failed=[]
    for nb,rel,size,mtime,old in todo:
        if index_until is not None and (tried or not progress) and time.monotonic()>index_until:left+=1;continue   # one page always, so each call makes progress
        tried+=1
        if _index_page(db,nb,present[nb],rel,size,mtime,old,delta,nb not in folders):result['indexed']+=1
        else:now_failed.append(f'{nb}/{rel}')
    waiting={f'{t[0]}/{t[1]}' for t in todo[tried:]}   # past the deadline: not tried this time
    held={f for f in failed if f.split('/',1)[0] not in listed or f in waiting}   # a failure is forgotten once its page is read or gone
    sources._set_meta(db,'failed',json.dumps(sorted(held|set(now_failed))[:500]))
    sources._apply_vocab(db,delta)
    for (nb,) in db.execute('SELECT name FROM source').fetchall():
        files,chunks=db.execute('SELECT count(*),coalesce(sum(chunks),0) FROM file WHERE source=?',(nb,)).fetchone()
        db.execute('UPDATE source SET files=?,chunks=? WHERE name=?',(files,chunks,nb))
    if result['indexed'] or sources._meta(db,'vectors_checked')!='1':   # cleared below only when the embedding caught up; an index from before this rule is checked once
        sources._set_meta(db,'vectors_pending','1');sources._set_meta(db,'vectors_checked','1')
    db.commit()
    if embed and (result['indexed'] or sources._meta(db,'vectors_pending')=='1' or (sources._meta(db,'vectors') or None)!=_identity(root)):
        remaining=(index_until-time.monotonic()) if index_until is not None else sources.EMBED_SECONDS
        if remaining>0.05:
            vectors=sources._embed(db,root,time.monotonic()+remaining)
            sources._set_meta(db,'vectors_pending','1' if vectors and vectors['pending'] else '0');db.commit()
    result['stale']=bool(cut or left or unread or now_failed)
    return result


def _identity(root):
    """The identity of the installed semantic model's vectors (search._vector_identity), or None without a model."""
    from .search import _vector_identity
    return _vector_identity(root)


def page_level(policy,folder,rel):
    """The level a folder notebook page is served at now: the strictest policy over its folder and every folder above it
    (None outside every notebook, which is not served either)."""
    return policy.file(os.path.join(folder,*rel.split('/')))[0]


def after_write(root,domain,nb):
    """Reindex the notebook a doc write just changed, outside the journal; a busy or failing index is left for the next walk."""
    try:refresh(root,domain,None,time.monotonic()+WRITE_INDEX_SECONDS,[nb],embed=False)
    except Exception as exc:
        from .logs import swallowed
        swallowed('docs_index.after_write',exc,root)


# ---- who may see what -----------------------------------------------------------------------------------------------
def visible(root,domain,include_private=False,named=None,include_hidden=False,policy=None):
    """{notebook: (title, visibility)} of the domain's notebooks the caller may see now: never a private one without
    include_private, never a hidden one unless include_hidden or it is the one named. A damaged notebook.json hides its
    notebook. policy (a folder_policy.Policy) adds the registered folder notebooks that are searchable or injectable by it now;
    a hidden or private folder notebook is never listed, whoever asks. Those pages are judged one by one with page_level."""
    from .docs import _read_meta
    out={}
    for nb,folder in _folder_notebooks(root,domain,policy).items() if policy is not None else ():
        if named and nb!=named:continue
        level,_,title=policy.folder(folder)
        if level in SERVED:out[nb]=(title or nb,level)
    for nb in _notebooks(root,domain) or {}:
        if named and nb!=named:continue
        try:meta=_read_meta(root,domain,nb)[0]   # the doc commands' reader: a link or junction on the way, or damage, hides the notebook
        except (OSError,ValueError):continue
        if not isinstance(meta,dict) or (meta.get('private') and not include_private):continue
        level=meta.get('visibility','searchable')
        if level not in ('hidden','searchable','injectable'):continue
        if level=='hidden' and not include_hidden and nb!=named:continue
        out[nb]=(str(meta.get('title') or nb),level)
    return out


ALLOWANCE='hooks/docs-allowance.json'   # {domain: when it last had the progress allowance}; runtime state, outside every index


def _allowances(root):
    from .repository import read_json
    try:found=read_json(Path(root)/ALLOWANCE,{})
    except (OSError,ValueError):return {}
    return {k:v for k,v in found.items() if isinstance(v,(int,float))} if isinstance(found,dict) else {}


def refresh_all(root,domains):
    """Refresh each domain within one shared budget (WALK_SECONDS, then INDEX_SECONDS), the one that had the progress allowance
    longest ago first; only that one may pass the budget to make progress. True when any of them was left unfinished."""
    start=time.monotonic();walk_until=start+WALK_SECONDS;index_until=walk_until+INDEX_SECONDS;stale=False
    wanted=[d for d in domains if (Path(root)/'domains'/d/'notebooks').is_dir() or db_path(root,d).is_file() or _folder_notebooks(root,d)]
    from .common import atomic_write
    had=_allowances(root);order=sorted(wanted,key=lambda d:had.get(d,0.0))
    if order:   # recorded before the refresh, so a domain whose refresh fails still passes the allowance on
        try:atomic_write(Path(root)/ALLOWANCE,json.dumps(dict(had,**{order[0]:time.time()}),sort_keys=True))
        except OSError:pass
    for n,domain in enumerate(order):
        stale|=refresh(root,domain,walk_until,index_until,progress=n==0)['stale']
    return stale


# ---- search, tasks, query -------------------------------------------------------------------------------------------
def _page(domain,nb,title,hit,info):
    revision,page_title=info.get((hit['source'],hit['path']),(None,None))
    return {'origin':'lm','domain':domain,'notebook':nb,'notebook_title':title,'title':page_title or hit['path'],'path':hit['path'],
            'heading':hit['symbol'],'snippet':hit['snippet'],'line_start':hit['line_start'],'line_end':hit['line_end'],'revision':revision,
            'score':hit['score'],'relevance':hit['relevance'],'label':'notebook page'}


def search(root,domains,query,limit=5,include_private=False,notebook=None,refresh_first=True,injectable_only=False,width=sources.SNIPPET):
    """(pages, stale): page hits from each domain's index, best first within a domain, the domains in the order given,
    cut to limit. Hidden notebooks only when `notebook` names one; private ones only with include_private."""
    stale=refresh_all(root,domains) if refresh_first else False
    pages=[]
    for domain in domains:
        allowed=visible(root,domain,include_private,notebook)
        if injectable_only:allowed={n:v for n,v in allowed.items() if v[1]=='injectable'}
        if not allowed:continue
        db=_readonly(root,domain)
        if db is None:continue
        try:
            hits=sources.search_db(db,root,query,limit,width=width,keep=lambda _root,source,_path:source in allowed)
            wanted={(h['source'],h['path']) for h in hits}
            info={(s,p):(h,t) for s,p,h,t in db.execute('SELECT source,path,sha256,title FROM file') if (s,p) in wanted} if wanted else {}
        finally:db.close()
        pages+=[_page(domain,h['source'],allowed[h['source']][0],h,info) for h in hits]
    return pages[:limit],stale


def vocabulary(root,domains,wanted):
    """(chunks, {stem: df}) for these words over the page indexes of these domains; (0, {}) without any."""
    total=0;df={}
    for domain in domains:
        db=_readonly(root,domain)
        if db is None:continue
        try:
            n,found=sources._vocab(db,wanted);total+=n
            for k,v in found.items():df[k]=df.get(k,0)+v
        except sqlite3.Error:pass
        finally:db.close()
    return total,df


def prompt_hits(root,domain,query,limit,gate,least,width,near=None,dropped=None,allowed=None):
    """Excerpts of injectable, non-private notebooks for the prompt hook, judged by its gate (sources.search_db with gate, least
    and near), from the index as it is: the hook never reindexes. Each hit gains domain, notebook and notebook_title.
    allowed: the injectable notebooks ({notebook: (title, visibility)}), when the caller read them already."""
    if allowed is None:allowed={n:v for n,v in visible(root,domain).items() if v[1]=='injectable'}
    db=_readonly(root,domain) if allowed else None
    if db is None:return []
    try:hits=sources.search_db(db,root,query,limit,None,True,gate,least,width,near,dropped,keep=lambda _root,source,_path:source in allowed)
    finally:db.close()
    return [dict(h,domain=domain,notebook=h['source'],notebook_title=allowed[h['source']][0]) for h in hits]


def task_rows(root,domains,notebook=None,open_only=False,include_private=False,limit=500,refresh=True,levels=None,folders=False):
    """(tasks, stale): task lines of the pages the caller may see: page, line, text (as written), task (as shown), done, due,
    owner, priority. refresh=False reads the index as it is, which is what SessionStart does (stale is False then, and an
    index that is missing or of another VERSION gives no rows). levels: only pages served at one of these levels (default
    all); folders: the registered folder notebooks too, each page judged by page_level."""
    from .note_format import item_fields
    stale=refresh_all(root,domains) if refresh else False;out=[]
    policy=None
    if folders:
        from .folder_policy import Policy
        policy=Policy(root)
    for domain in domains:
        allowed=visible(root,domain,include_private,notebook,policy=policy)
        db=_readonly(root,domain) if allowed else None
        if db is None:continue
        where=_folder_notebooks(root,domain,policy) if policy is not None else {}
        try:
            for nb,path,title,line,done,text,due,owner,priority in db.execute('SELECT f.source,f.path,f.title,t.line,t.done,t.text,t.due,t.owner,t.priority FROM task t JOIN file f ON f.id=t.file_id ORDER BY f.source,f.path,t.line'):
                if nb not in allowed or (open_only and done):continue
                level=page_level(policy,where[nb],path) if nb in where else allowed[nb][1]
                if (nb in where and level not in SERVED) or (levels is not None and level not in levels):continue
                out.append({'domain':domain,'notebook':nb,'path':path,'title':title,'line':line,'done':bool(done),'text':text,'task':item_fields(text)[0],'due':due,'owner':owner,'priority':priority})
        finally:db.close()
    out.sort(key=lambda t:(t['done'],t['due'] is None,t['due'] or '',t['domain'],t['notebook'],t['path'],t['line']))
    return out[:limit],stale


def _number(value):
    try:return float(value)
    except (TypeError,ValueError):return None


def _matches(values,op,want):
    if op=='exists':return bool(values)
    if op=='missing':return not values
    if op=='contains':return any(str(want).casefold() in v.casefold() for v in values)
    for v in values:
        a,b=_number(v),_number(want)
        left,right=(a,b) if a is not None and b is not None else (v.casefold(),str(want).casefold())
        if (op=='=' and left==right) or (op=='!=' and left!=right) or (op=='<' and left<right) or (op=='<=' and left<=right) or (op=='>' and left>right) or (op=='>=' and left>=right):return True
    return op=='!=' and not values


def check_query(payload):
    """The validated {where, sort, limit, notebook} of a doc query payload."""
    if not isinstance(payload,dict):raise ValueError('The query payload is a JSON object: {"where": [...], "sort": [...], "limit": N}')
    extra=set(payload)-{'where','sort','limit','notebook'}
    if extra:raise ValueError('Unknown query fields: '+', '.join(sorted(extra)))
    where=payload.get('where',[]);sort=payload.get('sort',[])
    if not isinstance(where,list) or len(where)>20 or not isinstance(sort,list) or len(sort)>5:raise ValueError('where is a list of at most 20 conditions and sort of at most 5 keys')
    for c in where:
        if not isinstance(c,dict) or not isinstance(c.get('key'),str) or c.get('op','=') not in OPS or set(c)-{'key','op','value'}:
            raise ValueError('A condition is {"key": K, "op": one of '+', '.join(OPS)+', "value": V}')
        if c.get('op','=') not in ('exists','missing') and not isinstance(c.get('value'),(str,int,float)):raise ValueError(f"Condition on {c['key']} needs a value")
    for s in sort:
        if not isinstance(s,dict) or not isinstance(s.get('key'),str) or s.get('order','asc') not in ('asc','desc') or set(s)-{'key','order'}:
            raise ValueError('A sort key is {"key": K, "order": "asc" or "desc"}')
    limit=payload.get('limit',100)
    if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=1000:raise ValueError('limit is a whole number from 1 to 1000')
    notebook=payload.get('notebook')
    if notebook is not None and not isinstance(notebook,str):raise ValueError('notebook is a notebook id')
    return {'where':where,'sort':sort,'limit':limit,'notebook':notebook}


def query(root,domains,payload,include_private=False):
    """(pages, stale): pages whose frontmatter properties meet every condition (pseudo keys: title, path, notebook), sorted
    by the sort keys (a page without the key sorts last), at most limit."""
    spec=check_query(payload);stale=refresh_all(root,domains);rows=[]
    for domain in domains:
        allowed=visible(root,domain,include_private,spec['notebook'])
        db=_readonly(root,domain) if allowed else None
        if db is None:continue
        try:
            props={}
            for file_id,key,value in db.execute('SELECT file_id,key,value FROM prop'):props.setdefault(file_id,{}).setdefault(key,[]).append(value)
            for file_id,nb,path,title,sha in db.execute('SELECT id,source,path,title,sha256 FROM file ORDER BY source,path'):
                if nb not in allowed:continue
                own=props.get(file_id,{});seen=dict(own,title=[title or ''],path=[path],notebook=[nb])
                if all(_matches(seen.get(c['key'],[]),c.get('op','='),c.get('value')) for c in spec['where']):
                    rows.append({'domain':domain,'notebook':nb,'path':path,'title':title,'revision':sha,'properties':{k:(v[0] if len(v)==1 else v) for k,v in own.items()},'_seen':seen})
        finally:db.close()
    for s in reversed(spec['sort']):
        key=s['key'];desc=s.get('order','asc')=='desc'
        present=[r for r in rows if r['_seen'].get(key)];absent=[r for r in rows if not r['_seen'].get(key)]
        def value(r,key=key):
            v=r['_seen'][key][0];n=_number(v);return (0,n,'') if n is not None else (1,0.0,v.casefold())
        rows=sorted(present,key=value,reverse=desc)+absent
    return [{k:v for k,v in r.items() if k!='_seen'} for r in rows[:spec['limit']]],stale
