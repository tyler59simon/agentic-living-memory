"""Folder notebook pages, the derived index: domains/<D>/docs.sqlite3.

It holds the task lines of the pages of each registered folder notebook (core/folder_policy.py), named nb- plus ten hex digits as in
sources._notebook_sources, so `open` and SessionStart can list them. Search of those pages is served from the sources index, so there are
no chunks, search text, vocabulary or vectors here: one `source` row per notebook, a `file` row per page and a `task` row per task line.
The index is never canonical and never backed up. Freshness does not hang on any write path: `open` first stat-walks the domain's folder
notebooks for at most WALK_SECONDS, resuming from a cursor, then reindexes changed pages for at most INDEX_SECONDS, and reports
`pages_stale` when work is left. SessionStart reads the index as it is. Each call walks at least one notebook and indexes at least one
page, even past those budgets, so it always makes progress; of several domains, only the one that had that allowance longest ago gets it.
A page that cannot be read is retried after the others and keeps `pages_stale` true; a folder that cannot be listed keeps the rows of the
pages under it until it can be. Only notebooks that are searchable or injectable now are listed, judged for each page by the strictest
policy over its folder; a folder that is hidden or private now is not listed and its rows are dropped. A dot folder, a link and a page over
sources.MAX_BYTES are not walked."""
from __future__ import annotations
import hashlib,json,os,re,sqlite3,tempfile,time
from pathlib import Path
from . import sources
from .common import file_lock
from .paths import is_os_junk
from .folder_policy import SERVED

VERSION=3   # 2: the task table has owner and priority; 3: no chunk, search or property tables (hosted notebooks retired); an index of another VERSION is replaced by _open_writable
WALK_SECONDS=0.2
INDEX_SECONDS=0.5
TEMP_PREFIX='.docs-'
SCHEMA=('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)',
        'CREATE TABLE source(name TEXT PRIMARY KEY,root TEXT,files INTEGER DEFAULT 0)',
        'CREATE TABLE file(id INTEGER PRIMARY KEY,source TEXT NOT NULL,path TEXT NOT NULL,mtime_ns INTEGER,size INTEGER,sha256 TEXT,title TEXT,UNIQUE(source,path))',
        'CREATE TABLE task(file_id INTEGER NOT NULL,line INTEGER,done INTEGER,text TEXT,due TEXT,owner TEXT,priority TEXT)','CREATE INDEX task_file ON task(file_id)')
TASK=re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+\[([ xX])\]\s+(.*\S)\s*$')
FRONT=re.compile(r'\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)',re.S)
TABLES={'meta','source','file','task'}
MAX_PROPS=50
MAX_TASKS=500


def db_path(root,domain):return Path(root)/'domains'/domain/'docs.sqlite3'


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


def _walk_pages(folder,deadline=None,unread=None,inner=()):
    """{page path: (size, mtime_ns)} of a folder notebook's Markdown pages (no dot folders, links, pages over sources.MAX_BYTES or the folders
    of the other notebooks, `inner`: their compare keys), or None past the deadline. Raises OSError when the notebook folder itself cannot be
    listed. A subfolder that cannot be listed is added to `unread` (its pages are unknown, not gone); without that list it raises too."""
    from .docs import _is_link
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
                if _is_link(Path(e.path)):continue
                if e.is_dir(follow_symlinks=False):
                    if not (e.name.startswith('.') or key(e.path) in inner):stack.append(path)
                elif e.is_file(follow_symlinks=False) and not is_os_junk(e.path) and e.name.casefold().endswith(('.md','.markdown')):
                    info=e.stat(follow_symlinks=False)
                    if info.st_size<=sources.MAX_BYTES:found[path]=(info.st_size,info.st_mtime_ns)
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
def _drop(db,file_id):
    db.execute('DELETE FROM task WHERE file_id=?',(file_id,));db.execute('DELETE FROM file WHERE id=?',(file_id,))


def _index_page(db,nb,folder,rel,size,mtime,old):
    """Bring one page's file and task rows up to date; False when it could not be read now."""
    try:data=(folder/rel).read_bytes()
    except OSError:return False
    sha=hashlib.sha256(data).hexdigest()
    if old and old[3]==sha:
        db.execute('UPDATE file SET mtime_ns=?,size=? WHERE id=?',(mtime,size,old[0]));return True
    if old:_drop(db,old[0])
    clean=sources.redact_text(_decode(data))[0]
    file_id=db.execute('INSERT INTO file(source,path,mtime_ns,size,sha256,title) VALUES(?,?,?,?,?,?)',(nb,rel,mtime,size,sha,title_of(clean,rel))).lastrowid
    db.executemany('INSERT INTO task VALUES(?,?,?,?,?,?,?)',[(file_id,n,int(d),t,due,owner,priority) for n,d,t,due,owner,priority in tasks(clean)])
    return True


def refresh(root,domain,walk_until=None,index_until=None,rebuild=False,lock_timeout=0,progress=True):
    """Bring the index up to date within the deadlines (time.monotonic() values; None: no limit). progress: walk one notebook and index
    one page even past the deadlines. Returns {stale, indexed, removed}, with `error` when the index could not be opened or replaced
    (another process held it, or Windows refused to replace a file a reader has open); stale is True then, and when work was left."""
    root=Path(root);result={'stale':False,'indexed':0,'removed':0}
    if not db_path(root,domain).is_file() and not _folder_notebooks(root,domain):return result
    try:
        with _lock(root,domain,lock_timeout):
            present=_folder_notebooks(root,domain)   # under the lock: a notebook another process just indexed is not mistaken for a gone one
            db=_open_writable(root,domain,rebuild)
            try:return _refresh(db,root,domain,present,walk_until,index_until,result,progress)
            finally:db.close()
    except (TimeoutError,OSError,sqlite3.Error) as exc:
        result.update(stale=True,error=str(exc) or type(exc).__name__);return result


def _refresh(db,root,domain,present,walk_until,index_until,result,progress=True):
    """present: the folder notebooks of the domain, {name: real folder}."""
    from .folder_policy import Policy,key
    all_registered={k for k,(_,e) in Policy(root).entries.items() if not e.get('removed')} if present else set()
    for (gone,) in db.execute('SELECT name FROM source').fetchall():
        if gone not in present:
            for (file_id,) in db.execute('SELECT id FROM file WHERE source=?',(gone,)).fetchall():_drop(db,file_id);result['removed']+=1
            db.execute('DELETE FROM source WHERE name=?',(gone,))
    names=sorted(present)
    cursor=sources._meta(db,'cursor') or ''
    if cursor in names:names=names[names.index(cursor):]+names[:names.index(cursor)]
    todo=[];walked=0;cut=None;unread=0;listed=set()
    for nb in names:
        if walk_until is not None and (walked or not progress) and time.monotonic()>walk_until:cut=nb;break
        skipped=[]
        try:listing=_walk_pages(present[nb],walk_until if (walked or not progress) else None,skipped,all_registered-{key(present[nb])})   # the first notebook is finished, so a call makes progress
        except OSError:unread+=1;continue   # this notebook cannot be listed now: its rows stay as they are
        unread+=bool(skipped)
        if listing is None:cut=nb;break
        walked+=1;listed.add(nb)
        db.execute('INSERT INTO source(name,root) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET root=excluded.root',(nb,present[nb].as_posix()))
        have={p:(i,m,s,h) for i,p,m,s,h in db.execute('SELECT id,path,mtime_ns,size,sha256 FROM file WHERE source=?',(nb,))}
        for rel,(size,mtime) in sorted(listing.items()):
            old=have.get(rel)
            if not old or (old[1],old[2])!=(mtime,size):todo.append((nb,rel,size,mtime,old))
        for rel,(file_id,*_) in have.items():
            if rel not in listing and not under(rel,skipped):_drop(db,file_id);result['removed']+=1   # a page in a folder that could not be listed stays
    sources._set_meta(db,'cursor',cut or '')
    try:failed=set(json.loads(sources._meta(db,'failed') or '[]'))
    except ValueError:failed=set()
    todo.sort(key=lambda t:f'{t[0]}/{t[1]}' in failed)   # pages that could not be read last time go last, so they never hold up the others
    left=0;tried=0;now_failed=[]
    for nb,rel,size,mtime,old in todo:
        if index_until is not None and (tried or not progress) and time.monotonic()>index_until:left+=1;continue   # one page always, so each call makes progress
        tried+=1
        if _index_page(db,nb,present[nb],rel,size,mtime,old):result['indexed']+=1
        else:now_failed.append(f'{nb}/{rel}')
    waiting={f'{t[0]}/{t[1]}' for t in todo[tried:]}   # past the deadline: not tried this time
    held={f for f in failed if f.split('/',1)[0] not in listed or f in waiting}   # a failure is forgotten once its page is read or gone
    sources._set_meta(db,'failed',json.dumps(sorted(held|set(now_failed))[:500]))
    for (nb,) in db.execute('SELECT name FROM source').fetchall():
        db.execute('UPDATE source SET files=? WHERE name=?',(db.execute('SELECT count(*) FROM file WHERE source=?',(nb,)).fetchone()[0],nb))
    db.commit()
    result['stale']=bool(cut or left or unread or now_failed)
    return result


# ---- who may see what -----------------------------------------------------------------------------------------------
def visible(root,domain,policy):
    """{notebook: (title, visibility)} of the registered folder notebooks of the domain that agents may reach now: searchable or injectable
    by `policy` (a folder_policy.Policy). A hidden or private folder notebook is never listed, whoever asks. Pages are judged one by one."""
    out={}
    for nb,folder in _folder_notebooks(root,domain,policy).items():
        level,_,title=policy.folder(folder)
        if level in SERVED:out[nb]=(title or nb,level)
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
    wanted=[d for d in domains if db_path(root,d).is_file() or _folder_notebooks(root,d)]
    from .common import atomic_write
    had=_allowances(root);order=sorted(wanted,key=lambda d:had.get(d,0.0))
    if order:   # recorded before the refresh, so a domain whose refresh fails still passes the allowance on
        try:atomic_write(Path(root)/ALLOWANCE,json.dumps(dict(had,**{order[0]:time.time()}),sort_keys=True))
        except OSError:pass
    for n,domain in enumerate(order):
        stale|=refresh(root,domain,walk_until,index_until,progress=n==0)['stale']
    return stale


# ---- tasks ----------------------------------------------------------------------------------------------------------
def task_rows(root,domains,open_only=False,limit=500,refresh=True,levels=None):
    """(tasks, stale): task lines of the folder notebook pages agents may see: notebook, path, line, text (as written), task (as shown),
    done, due, owner, priority. refresh=False reads the index as it is, which is what SessionStart does (stale is False then, and an
    index that is missing or of another VERSION gives no rows). levels: only pages served at one of these levels (default all). Each
    page is judged by the strictest policy over its folder and the folders above it."""
    from .folder_policy import Policy,key
    from .note_format import item_fields
    stale=refresh_all(root,domains) if refresh else False;out=[]
    policy=Policy(root)
    for domain in domains:
        allowed=visible(root,domain,policy)
        db=_readonly(root,domain) if allowed else None
        if db is None:continue
        where=_folder_notebooks(root,domain,policy)
        try:
            for nb,path,title,line,done,text,due,owner,priority in db.execute('SELECT f.source,f.path,f.title,t.line,t.done,t.text,t.due,t.owner,t.priority FROM task t JOIN file f ON f.id=t.file_id ORDER BY f.source,f.path,t.line'):
                if nb not in allowed or nb not in where or (open_only and done):continue
                level,owner_folder,_=policy.file(os.path.join(where[nb],*path.split('/')))
                if level not in SERVED:continue
                if owner_folder is not None and key(owner_folder)!=key(where[nb]):
                    reg=policy.entries.get(key(owner_folder))
                    if reg is not None and not reg[1].get('removed'):continue
                if levels is not None and level not in levels:continue
                out.append({'domain':domain,'notebook':nb,'path':path,'title':title,'line':line,'done':bool(done),'text':text,'task':item_fields(text)[0],'due':due,'owner':owner,'priority':priority})
        finally:db.close()
    out.sort(key=lambda t:(t['done'],t['due'] is None,t['due'] or '',t['domain'],t['notebook'],t['path'],t['line']))
    return (out if limit is None else out[:limit]),stale
