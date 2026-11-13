"""Digest notes, layer 2 of the project knowledge base: concise agent-written notes about one source file each, kept honest.

A digest is any note whose frontmatter `sources` lists the files it was written from, as {path, sha256}. A path is relative
to a folder bound to the domain (`billing/README.md`, like `files`), or `name:rel/path` for a file of a registered source
outside the bound folders. A digest is stale when a listed file's sha256 differs now, or the file is gone; an injected stale
digest says so (STALE: ...), `review` lists it, and `update` or `confirm` writes the file's current hash back (the author
checked it). `ingest-scan` ranks the source library's files into a worklist with the status of their digests, which
an agent (interactively, or scripts/ingest_run.py in batches) works through. LM itself never calls a model."""
from __future__ import annotations
import fnmatch,hashlib,os,re
from pathlib import Path
from .common import domain_prefixes,load_bindings
from .repository import inventory,read_json,canonical_json,now

NAMED=re.compile(r'([a-z0-9][a-z0-9._-]+):(.+)\Z')   # `wiki:Retry-Policy.md`; a single letter before the colon is a drive, so source-plan add refuses registry names under 2 characters
SHOWN=3
REF_CANDIDATES=300   # code files whose references are counted, largest first
MIN_MODULE=400

def file_sha256(path):
    """The sha256 (hex) of a file's bytes, None when it cannot be read."""
    h=hashlib.sha256()
    try:
        with open(path,'rb') as f:
            for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    except OSError:return None
    return h.hexdigest()

def _specs(root,domain,bindings):
    from . import sources
    return {s['name']:s for s in sources.resolve(root,domain,bindings)}

def _inside(path,base):
    try:return path.resolve().relative_to(Path(base).resolve()).as_posix()
    except (OSError,ValueError):return None

def stored_path(path,prefixes,specs):
    """The `sources` form of an absolute file path: relative to the bound folder holding it, else `name:rel` for the registered source holding it, else None."""
    for prefix in prefixes:
        rel=_inside(path,prefix)
        if rel is not None:return rel
    for name,spec in specs.items():
        rel=_inside(path,spec['path'])
        if rel is not None:return f'{name}:{rel}'
    return None

def locate(root,domain,path,bindings=None,specs=None):
    """The absolute file a stored source path names (None when it is missing), and whether this domain could say: False for a path
    relative to a bound folder in a domain that has none (a global note), which nothing can check."""
    bindings=bindings if bindings is not None else load_bindings(root)
    named=NAMED.match(path)
    if '..' in path.replace('\\','/').split('/'):return None,True
    if named:
        spec=(specs if specs is not None else _specs(root,domain,bindings)).get(named[1])
        base=Path(spec['path']) if spec else None;rel=named[2]
        found=(base/rel) if base is not None else None
        return (found if found is not None and found.is_file() and _inside(found,base) is not None else None),True
    prefixes=domain_prefixes(domain,bindings)
    for prefix in prefixes:
        if (prefix/path).is_file():return prefix/path,True
    return None,bool(prefixes)

def describe(root,domain,value,cwd=None,bindings=None):
    """{path, sha256} for a --source value: an absolute path, one relative to a bound folder (or to cwd), or `name:rel`. Refuses a
    file outside the domain's bound folders and registered sources, or one that is not a file."""
    bindings=bindings if bindings is not None else load_bindings(root);text=str(value).strip()
    if not text:raise ValueError('--source needs a file path')
    prefixes=domain_prefixes(domain,bindings);specs=_specs(root,domain,bindings);found=None
    if NAMED.match(text) and NAMED.match(text)[1] in specs:found=locate(root,domain,text,bindings,specs)[0]
    else:
        given=Path(os.path.expandvars(text)).expanduser()
        options=[given] if given.is_absolute() else [p/given for p in prefixes]+([Path(cwd)/given] if cwd else [])
        found=next((o for o in options if o.is_file()),None)
    if found is None:raise ValueError(f'--source {text}: no such file under the bound folders or registered sources')
    stored=stored_path(found,prefixes,specs)
    if stored is None:raise ValueError(f'--source {text}: outside the bound folders and registered sources of domain {domain}')
    sha=file_sha256(found)
    if sha is None:raise ValueError(f'--source {text}: cannot be read')
    return {'path':stored,'sha256':sha}

def rehash(sources,domain,root,bindings=None):
    """The sources with the current hash of each file that exists (a missing file keeps its old hash, so it stays stale)."""
    bindings=bindings if bindings is not None else load_bindings(root);specs=None;out=[]
    for s in sources:
        if NAMED.match(s['path']) and specs is None:specs=_specs(root,domain,bindings)
        found,_=locate(root,domain,s['path'],bindings,specs);sha=file_sha256(found) if found else None
        out.append({'path':s['path'],'sha256':sha or s['sha256']})
    return out

def changed(sources,domain,root,bindings=None):
    """The paths among a note's sources whose file differs from the hash recorded, or is gone."""
    bindings=bindings if bindings is not None else load_bindings(root);specs=None;found=[]
    for s in sources or []:
        if NAMED.match(s['path']) and specs is None:specs=_specs(root,domain,bindings)
        path,known=locate(root,domain,s['path'],bindings,specs)
        if known and (path is None or file_sha256(path)!=s['sha256']):found.append(s['path'])
    return found

def stale_text(paths):
    """`STALE: a.md and 2 more changed since this digest`, '' when nothing changed."""
    if not paths:return ''
    names=', '.join(paths[:SHOWN])+(f' and {len(paths)-SHOWN} more' if len(paths)>SHOWN else '')
    return f'STALE: {names} changed since this digest'

def stale_note(note,root,bindings=None):
    """stale_text for a note dict with `sources` and `domain` ('' when it has none or they are unchanged); never raises."""
    if not note.get('sources'):return ''
    try:return stale_text(changed(note['sources'],note['domain'],root,bindings))
    except Exception as exc:
        from .logs import swallowed
        swallowed('digests.stale_note',exc,root);return ''

def mark(hits,root,bindings=None):
    """Give each hit that is a digest with a changed source a `stale` text, and drop the `sources` list from all of them."""
    for hit in hits:
        sources=hit.pop('sources',None)
        if sources:
            text=stale_note(dict(hit,sources=sources),root,bindings)
            if text:hit['stale']=text

# ---- skip markers: files an agent judged not worth a digest ---------------------------------------------------------
SKIPS='.ingest-skips.json'   # domains/<D>/: {path: {sha256, reason, at}}; a non-canonical sidecar (not validated, not backed up), like .signals-seen.json
MAX_SKIPS=5000

def skips(root,domain):
    try:found=read_json(Path(root)/'domains'/domain/SKIPS,{})
    except (OSError,ValueError):return {}
    return found if isinstance(found,dict) else {}

def record_skips(root,domain,items,cwd=None,bindings=None):
    """Remember [{path, reason}] as skipped at the files' current hashes (a path that cannot be resolved is ignored); the newest MAX_SKIPS are kept. The count recorded."""
    from .common import atomic_write
    bindings=bindings if bindings is not None else load_bindings(root);found=skips(root,domain);count=0
    for item in items:
        try:sha=describe(root,domain,item['path'],cwd,bindings)
        except (ValueError,KeyError,TypeError):continue
        found[sha['path']]={'sha256':sha['sha256'],'reason':str(item.get('reason') or '')[:200],'at':now()};count+=1
    if count:
        kept=dict(sorted(found.items(),key=lambda kv:str((kv[1] or {}).get('at')))[-MAX_SKIPS:])
        folder=Path(root)/'domains'/domain
        if folder.is_dir():atomic_write(folder/SKIPS,canonical_json(kept))
    return count

# ---- ingest-scan: a ranked worklist ----------------------------------------------------------------------------------
ADR=re.compile(r'(?:^|/)(?:adrs?|decisions?|decision-records?)/|(?:^|/)adr[-_ ]?\d',re.I)
ENTRY=frozenset('main app cli server index __main__ manage wsgi asgi program startup application bootstrap'.split())
BUILD_NAMES=('pom.xml','build.gradle*','package.json','pyproject.toml','setup.py','setup.cfg','Cargo.toml','go.mod','*.csproj','Makefile','Dockerfile*','docker-compose*.yml','docker-compose*.yaml','CMakeLists.txt')

def classify(rel,source_kind='folder'):
    """(tier, kind) of a file of the source library: 0 decision records, 1 READMEs and top-level docs, 2 wiki and other docs, 3 entry points and
    build files, 4 modules (ranked by references and size), 5 the rest. Anything under a test or fixture path is a test, a Markdown ADR fixture included."""
    from . import sources as library
    low=rel.casefold();name=low.rsplit('/',1)[-1];stem,ext=os.path.splitext(name)
    if library.TEST_PATH.search(low):return 5,'test'
    if ext in library.DOC_EXT or ext in library.HTML_EXT:
        if ADR.search(low) and stem!='readme':return 0,'adr'   # an ADR folder's README only indexes the records
        if stem=='readme' or (stem in library.BARE_DOCS) or ('/' not in rel and source_kind not in library.REMOTE):return 1,'readme' if stem=='readme' else 'doc'
        return 2,'wiki' if ext in library.HTML_EXT or source_kind in ('html-export','git-wiki')+library.REMOTE else 'doc'
    if any(fnmatch.fnmatch(name,b.casefold()) for b in BUILD_NAMES):return 3,'build'
    if stem in ENTRY:return 3,'entry'
    return 4,'module'

def _references(db,name,file_id):
    """How many other files have a chunk whose text mentions this module's name (its file stem, camelCase and underscores split)."""
    from . import sources as library
    words=re.findall(r'[a-z0-9]+',library.split_camel(name).casefold())
    if len(''.join(words))<3:return 0
    try:
        return db.execute('SELECT count(DISTINCT c.file_id) FROM chunk_fts f JOIN chunk c ON c.id=f.rowid WHERE chunk_fts MATCH ? AND c.file_id<>?',
                          ('{symbol text names} : "'+' '.join(words)+'"',file_id)).fetchone()[0]
    except Exception:return 0

def scan(root,domain,source=None,limit=50,changed_only=False,bindings=None):
    """The ranked worklist of the domain's indexed files: {items, counts, total}. Each item has path (the form `--source` and `sources` use),
    source, kind, size, status (new | stale | current | skipped, against the active notes whose `sources` list the path) and, when one exists, digest
    (its ref; skipped: an agent judged the file not worth a digest, see record_skips); `file` is the path for `--file` when it lies in a bound folder; `abs` is where to read it. Reads the index and the files of existing digests only."""
    from . import sources as library
    bindings=bindings if bindings is not None else load_bindings(root);specs=_specs(root,domain,bindings)
    if source is not None and source not in specs:raise ValueError(f'unknown source {source}; `sources` lists them')
    db=library._readonly(root,domain)
    if db is None:raise ValueError('No knowledge-base index yet: run source-sync first')
    prefixes=domain_prefixes(domain,bindings);rows=[]
    try:
        files=db.execute('SELECT id,source,path,size FROM file WHERE skipped IS NULL AND chunks>0'+(' AND source=?' if source else ''),(source,) if source else ()).fetchall()
        bases={n:Path(s['path']).resolve() for n,s in specs.items()}
        leads={n:next(((f'{x}/' if x!='.' else '') for x in (_inside(b,p) for p in prefixes) if x is not None),f'{n}:') for n,b in bases.items()}   # stored path = lead + path within the source
        for file_id,name,rel,size in files:
            if name not in specs:continue
            stored=leads[name]+rel
            tier,kind=classify(rel,specs[name]['kind']);rows.append({'id':file_id,'path':stored,'rel':rel,'source':name,'kind':kind,'size':size,'tier':tier,'refs':0})
        modules=sorted((r for r in rows if r['tier']==4),key=lambda r:-r['size'])[:REF_CANDIDATES]
        for r in modules:
            if r['size']>=MIN_MODULE:r['refs']=_references(db,os.path.splitext(r['rel'].rsplit('/',1)[-1])[0],r['id'])
    finally:db.close()
    for r in rows:
        if r['tier']==4 and r['size']<MIN_MODULE:r['tier'],r['kind']=5,'other'
    depth=lambda r:r['path'].count('/')
    order={0:lambda r:(r['path'],),1:lambda r:(depth(r),r['path']),2:lambda r:(r['path'],),3:lambda r:(depth(r),r['path']),4:lambda r:(-r['refs'],-r['size'],r['path']),5:lambda r:(-r['size'],r['path'])}
    rows.sort(key=lambda r:(r['tier'],*order[r['tier']](r)))
    digests={}
    for n in inventory(domain,root):
        if n.get('status','active')=='active':
            for s in n.get('sources') or []:digests.setdefault(s['path'],[]).append((str(n.get('updated') or ''),n,s['sha256']))
    counts={'new':0,'stale':0,'current':0,'skipped':0};items=[];skipped=skips(root,domain)
    for r in rows:
        status,ref='new',None
        if r['path'] in digests:
            found=sorted(digests[r['path']],key=lambda d:d[0],reverse=True);live=file_sha256(bases[r['source']]/r['rel'])
            current=next((d for d in found if d[2]==live),None);pick=current or found[0]
            status='current' if current else 'stale';ref=f"{domain}:{pick[1]['id']}"
        elif isinstance(skipped.get(r['path']),dict) and skipped[r['path']].get('sha256')==file_sha256(bases[r['source']]/r['rel']):status='skipped'   # until the file changes
        counts[status]+=1
        if changed_only and status in ('current','skipped'):continue
        item={'path':r['path'],'source':r['source'],'kind':r['kind'],'size':r['size'],'status':status,'abs':(bases[r['source']]/r['rel']).as_posix()}
        if ref:item['digest']=ref
        if not NAMED.match(r['path']):item['file']=r['path']
        items.append(item)
    return {'ok':True,'domain':domain,'total':len(rows),'counts':counts,'shown':min(len(items),max(1,limit)),'items':items[:max(1,limit)]}
