"""Diff-aware staleness. A digest (`sources`) or a hashed note (`file_hashes`) goes STALE when any byte of a file it lists changes. Many
changes do not touch what the note says: an edit fifty lines away from every line and symbol it cites. This module tells the two apart
with no model and re-baselines the note when the change is irrelevant to it: the file's recorded hash becomes its current one, and the
note's `file:N` line citations are shifted to where those lines are now. `checked`, `updated` and `confirmed` stay as they were: the note
was not verified again, only shown to be unaffected.

Baselines: whenever a hash is recorded, the file's bytes are kept in the derived cache `.cache/baselines/<sha[:2]>/<sha>` (text files up to
1 MiB). For a hash recorded before that, the sweep looks for the version in the file's git history. Without a baseline nothing is decided
and the note stays STALE, exactly as before. Deleting the cache loses nothing canonical.

A change is irrelevant only when every one of these holds: the note cites the file (a `name.ext:N` or `:A-B` line citation, or a symbol
the file defines); no changed line is within MARGIN lines of a cited line; no cited symbol's definition changed or disappeared; no changed
line mentions an identifier the note names; and under CHANGE_MAX of the file's lines changed. Anything else is left for a person, an
agent or the maintenance reviewer."""
from __future__ import annotations
import difflib,hashlib,json,os,re,subprocess,tempfile,time
from pathlib import Path
from .common import retry_sharing,load_bindings,file_lock

BASELINE_MAX=1<<20
GIT_WALK=50   # commits of a file's history searched for the version a hash was recorded from
GIT_TIMEOUT=5
CHANGE_MAX=0.3   # a change to more of the file than this is a rewrite: someone has to read it
MARGIN=2   # a changed line this close to a cited line counts as touching it
SWEEP_EVERY=3600   # seconds between scheduled sweeps
SWEEP_SECONDS=20   # time budget of one sweep
PRUNE_DAYS=30   # an unreferenced baseline is kept this long

NUM=r'(\d{1,6})(?:\s*[-\u2013]\s*(\d{1,6}))?'
FILE_CITE=re.compile(r'(?<![\w/.\\-])((?:[\w.-]+[/\\])*[\w.-]+\.[A-Za-z]\w{0,9}):'+NUM+r'(?![\d])')   # `core/maps.py:165`, `store.ts:66-89`
BARE_CITE=re.compile(r'(?<=[\s(,;])(?<![\w.]):'+NUM+r'(?![\d])')   # `:44-52` after a file citation
LINE_CITE=re.compile(r'\blines?\s+(\d{1,6})(?:\s*(?:-|\u2013|to)\s*(\d{1,6}))?(?![\d])',re.I)   # `(line 10)`, `lines 3 to 9`
FILE_NAME=re.compile(r'(?<![\w/.\\-])((?:[\w.-]+[/\\])*[\w-][\w.-]*\.[A-Za-z]\w{0,9})(?![\w.])')   # `b.txt` named before `(line 30)`
NAMED_BEFORE=80   # how far before a `line N` its file's name is looked for
QUALIFIES=re.compile(r'\s*[(,]?\s*(?:at\s+|on\s+)?')   # what may stand between a file name and the `line N` it qualifies
IDENT=re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
TICKED=re.compile(r'`([^`\n]{1,200})`')
ONE_NAME=re.compile(r'[A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)*(?:\(\))?')   # a backticked span that is one name: `db`, `Job::end`, `main()`
PATHLIKE=re.compile(r'[\\/]|^[\w.-]+\.[A-Za-z]\w{0,9}$')
NOISE=re.compile(r'\[\[[^\]]*\]\]|https?://\S+|(?:[\w.-]+[/\\])+[\w.-]+|[\w.-]+\.[A-Za-z]\w{0,9}(?=[:\s),;]|$)')

# ---- baselines --------------------------------------------------------------------------------------------------------
def _folder(root):return Path(root)/'.cache'/'baselines'
def baseline_file(root,sha):return _folder(root)/sha[:2]/sha

def _text(data):return b'\0' not in data[:8192]

SCAN_LINE_MAX=2000   # a file with a longer line (generated, minified) is not cached: its credential scan could take seconds
SCAN_SECONDS=1.0   # a file whose scan takes longer (over about 1 MB) is not cached; diffs then come from git history

def _clean(data):
    """True when the bytes may be cached: no credential found by a line-by-line scan that finished within SCAN_SECONDS."""
    from .repository import find_secret
    stop=time.monotonic()+SCAN_SECONDS
    for line in data.decode('utf-8','replace').splitlines():
        if len(line)>SCAN_LINE_MAX or time.monotonic()>stop or find_secret(line):return False
    return True

def _put(root,sha,data):
    """Keep `data` as the baseline of `sha`. A file holding a credential is never copied into the cache (it would outlive its removal from
    the source), nor one whose scan cannot finish quickly; False then, and the comparison works from bytes in memory only."""
    target=baseline_file(root,sha)
    if target.exists():
        try:os.utime(target)   # still in use: prune goes by age
        except OSError:pass
        return True
    if not _clean(data):return False
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+sha[:8]+'.',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as out:out.write(data)
        retry_sharing(lambda:os.replace(tmp,target))
    except BaseException:
        try:os.unlink(tmp)
        except OSError:pass
        raise
    return True

def keep(root,path,sha=None):
    """Keep the file's bytes as the baseline of its hash (`sha` when given: nothing is kept if the file no longer matches it). A text file of
    at most BASELINE_MAX bytes only. Best effort: returns the hash kept or None, never raises."""
    try:
        p=Path(path)
        if not p.is_file() or p.stat().st_size>BASELINE_MAX:return None
        data=p.read_bytes()
        if not _text(data):return None
        actual=hashlib.sha256(data).hexdigest()
        if sha and actual!=sha:return None
        return actual if _put(root,actual,data) else None
    except Exception as exc:
        from .logs import swallowed
        swallowed('rebaseline.keep',exc,root);return None

def load(root,sha):
    try:return baseline_file(root,sha).read_bytes()
    except (OSError,ValueError):return None

def _repo(path):
    for parent in Path(path).resolve().parents:
        if (parent/'.git').exists():return parent
    return None

def _git(repo,*args,deadline=None):
    flags=0x08000000 if os.name=='nt' else 0   # CREATE_NO_WINDOW
    # Local history only: a partial clone would otherwise fetch a missing blob from its remote (the note then stays stale).
    env=dict(os.environ,GIT_NO_LAZY_FETCH='1',GIT_TERMINAL_PROMPT='0')
    timeout=GIT_TIMEOUT if deadline is None else min(GIT_TIMEOUT,deadline-time.monotonic())
    if timeout<=0:return None   # out of time: no new git process
    done=subprocess.run(['git','-c','core.quotepath=off','-C',str(repo),*args],capture_output=True,timeout=timeout,creationflags=flags,env=env)
    return done.stdout if done.returncode==0 else None

_PARTIAL={}
def _partial(repo):
    """True for a partial clone (a promisor remote): its history may be missing locally, and a git older than GIT_NO_LAZY_FETCH would fetch it."""
    key=str(repo)
    if key not in _PARTIAL:
        _PARTIAL[key]=bool(_git(repo,'config','--get','extensions.partialclone') or _git(repo,'config','--get-regexp',r'^remote\..*\.promisor$'))
    return _PARTIAL[key]

def backfill(root,path,sha,deadline=None,store=True):
    """The bytes of the version of `path` whose sha256 is `sha`, found in the last GIT_WALK commits that touched it (also as the CRLF checkout of
    an LF blob), and kept as its baseline unless `store` is False (a dry run). None when it is not there, the file is not in a git repository,
    or git fails."""
    try:
        repo=_repo(path)
        if repo is None or (deadline is not None and time.monotonic()>deadline) or _partial(repo):return None
        rel=Path(path).resolve().relative_to(repo.resolve()).as_posix()
        log=_git(repo,'log','--format=%H','-n',str(GIT_WALK),'--',rel,deadline=deadline)
        for commit in (log or b'').decode('ascii','replace').split():
            if deadline is not None and time.monotonic()>deadline:return None
            data=_git(repo,'show',f'{commit}:{rel}',deadline=deadline)
            if data is None:continue
            for candidate in (data,data.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')):
                if hashlib.sha256(candidate).hexdigest()==sha:
                    if store and len(candidate)<=BASELINE_MAX and _text(candidate):_put(root,sha,candidate)
                    return candidate
    except (OSError,ValueError,subprocess.SubprocessError):return None
    return None

def baseline(root,path,sha,deadline=None,store=True):
    return load(root,sha) or backfill(root,path,sha,deadline,store)

# ---- citations --------------------------------------------------------------------------------------------------------
def _plain(path):
    """A tracked path as compared with citations: the part after `name:` for a registered source, forward slashes, lower case."""
    from .digests import NAMED
    named=NAMED.match(path)
    return (named[2] if named else path).replace('\\','/').lower()

def _owners(cited,tracked):
    """The tracked paths a cited `dir/name.ext` names: equal, or one ends with the other at a folder boundary."""
    c=cited.replace('\\','/').lower()
    return {t for t in tracked if (p:=_plain(t))==c or p.endswith('/'+c) or c.endswith('/'+p)}

def citations(body,tracked):
    """[{files, start, end, spans}] for the line citations in a note body. `files` is the set of tracked paths a citation may refer to (empty for
    a file the note does not track); `spans` are the (start, end) offsets of its numbers in the body. A `line N` that a file name directly
    qualifies (`b.txt (line 30)`, `b.txt, line 30`) belongs to that file (one the note does not track: to none); with another file named
    between it and the last file citation (`Unlike b.txt, this file ... line 20`) its owner is unknown (ambiguous). Otherwise a bare `:A-B`
    or `line N` belongs to the file cited last before it, else to the note's only tracked file, else to every tracked file (ambiguous)."""
    found=[]
    for m in FILE_CITE.finditer(body):found.append((m.start(),'file',m))
    for m in BARE_CITE.finditer(body):found.append((m.start(),'bare',m))
    for m in LINE_CITE.finditer(body):found.append((m.start(),'line',m))
    found.sort(key=lambda f:f[0]);out=[];last=None;last_end=0;everything=set(tracked)
    for _,kind,m in found:
        named=None;unsure=False
        if kind=='file':
            last=_owners(m[1],tracked);last_end=m.end();first=2
        else:
            first=1
            if kind=='line':
                names=list(FILE_NAME.finditer(body,max(last_end,m.start()-NAMED_BEFORE),m.start()))
                if names and QUALIFIES.fullmatch(body,names[-1].end(),m.start()):named=_owners(names[-1][1],tracked)
                elif names:unsure=True
        files=named if named is not None else last if last is not None else (set(tracked) if len(tracked)==1 else everything)
        if unsure:files=everything
        a=int(m[first]);b=int(m[first+1]) if m[first+1] else a
        spans=[m.span(first)]+([m.span(first+1)] if m[first+1] else [])
        if a>b or a<1:continue
        out.append({'files':set(files),'start':a,'end':b,'spans':spans,'ambiguous':unsure or len(files)>1})
    return out

def identifiers(body,symbols=()):
    """Names the note mentions that code would contain: a name alone in backticks (any length, so `ID` and `db` count; of a dotted or
    `::` path, its last part), snake_case and CamelCase words, and any word equal to a symbol the file defines. The other words of a
    backticked command or call are prose (`git add -N` and `item_fields(text)` do not name `git` or `text`). File names and links are left out."""
    names=set();words=IDENT.findall(NOISE.sub(' ',TICKED.sub(' ',body)))
    for span in TICKED.findall(body):
        span=span.strip()
        if PATHLIKE.search(span):continue
        if ONE_NAME.fullmatch(span):names.add(IDENT.findall(span)[-1])   # named explicitly
        words+=IDENT.findall(span)
    defined={s.rsplit('.',1)[-1] for s in symbols if s}
    for w in words:
        if w in defined:names.add(w)
        elif len(w)>=3 and (('_' in w.strip('_')) or re.search(r'[a-z][A-Z]',w)):names.add(w)
    return names

# ---- the decision -----------------------------------------------------------------------------------------------------
def _decode(data):
    from .sources import _decode as decode
    return decode(data).replace('\r\n','\n').replace('\r','\n')

def _units(rel,text):
    from .sources import symbol_units
    try:return symbol_units(rel,text)
    except Exception:return []

def _shift(line,ops):
    """Where old line `line` (1-based) is in the new text, or None when it was changed or removed."""
    for tag,i1,i2,j1,j2 in ops:
        if tag=='equal' and i1<line<=i2:return line+(j1-i1)
    return None

def _opcodes(a,b):
    """SequenceMatcher opcodes of line lists `a` to `b`, with the common head and tail cut off first and the matcher's junk heuristic on, so
    a file of repeated lines costs about linear time. The opcodes are always a valid diff; one less than minimal only marks more lines changed."""
    head=0
    while head<len(a) and head<len(b) and a[head]==b[head]:head+=1
    tail=0
    while tail<len(a)-head and tail<len(b)-head and a[-1-tail]==b[-1-tail]:tail+=1
    ops=[('equal',0,head,0,head)] if head else []
    for tag,i1,i2,j1,j2 in difflib.SequenceMatcher(None,a[head:len(a)-tail],b[head:len(b)-tail]).get_opcodes():
        ops.append((tag,i1+head,i2+head,j1+head,j2+head))
    if tail:ops.append(('equal',len(a)-tail,len(a),len(b)-tail,len(b)))
    return ops

def _members(units):
    """{owner: [(symbol, text)]} of the member units under each dotted owner, built once per version."""
    out={}
    for s,_,_,t in units:
        if not s:continue
        parts=s.split('.')
        for n in range(1,len(parts)):out.setdefault('.'.join(parts[:n]),[]).append((s,t))
    return out

def _index(lines):
    where={}
    for i,line in enumerate(lines):where.setdefault(line,[]).append(i)
    return where

def _occurrences(window,lines,where):
    """How many times (0, 1 or 2 for more) the run of lines `window` occurs in `lines`, anchored on its rarest line."""
    if not window:return 1
    k=min(range(len(window)),key=lambda i:len(where.get(window[i],())))
    found=0
    for at in where.get(window[k],()):
        start=at-k
        if start>=0 and lines[start:start+len(window)]==window:
            found+=1
            if found>1:break
    return found

def assess(body,path,tracked,old,new):
    """{relevant, reason, shifts}: whether the change from text `old` to `new` of tracked file `path` may affect the note, and, when it cannot,
    [(body span, new number)] for its line citations of this file."""
    def relevant(reason):return {'relevant':True,'reason':reason,'shifts':[]}
    a=old.split('\n');b=new.split('\n')
    ops=_opcodes(a,b)
    changed=[op for op in ops if op[0]!='equal']
    if not changed:return {'relevant':False,'reason':'only line endings or encoding changed','shifts':[]}
    churn=sum(max(i2-i1,j2-j1) for _,i1,i2,j1,j2 in changed)
    if churn>CHANGE_MAX*max(len(a),1):return relevant(f'{churn} of {len(a)} lines changed')
    cites=[c for c in citations(body,tracked) if path in c['files']]
    if any(c['ambiguous'] for c in cites):return relevant('a line citation may refer to another file')
    units_old=_units(path,old);units_new=_units(path,new)
    names=identifiers(body,[u[0] for u in units_old])
    def cites_unit(u):return any(c['start']<=u[2] and c['end']>=u[1] for c in cites) or (u[0] and u[0].rsplit('.',1)[-1] in names)
    cited_units=[u for u in units_old if u[0] and cites_unit(u)]
    if not cites and not cited_units:return relevant('the note cites no line or symbol of this file')
    for _,i1,i2,j1,j2 in changed:
        lo=i1+1-MARGIN;hi=max(i2,i1+1)+MARGIN
        hit=next((c for c in cites if c['start']<=hi and c['end']>=lo),None)
        if hit:return relevant(f"lines {i1+1}-{max(i2,i1+1)} changed, near the cited line {hit['start']}")
        touched=set(IDENT.findall('\n'.join(a[i1:i2]+b[j1:j2])))&names
        if touched:return relevant(f"a changed line mentions {sorted(touched)[0]}")
    after={}
    for symbol,_,_,text in units_new:after.setdefault(symbol,[]).append(text)
    for symbol,_,_,text in cited_units:
        if text not in after.get(symbol,[]):return relevant(f'the cited definition {symbol} changed')
    # A cited class covers its members (`RetryPolicy` covers `RetryPolicy.max_tries`): any member changed, added or removed counts.
    old_members=_members(units_old);new_members=_members(units_new)
    for owner in {u[0] for u in cited_units}:
        if sorted(old_members.get(owner,[]))!=sorted(new_members.get(owner,[])):return relevant(f'a member of the cited definition {owner} changed')
    shifts=[];where=_index(b)
    for c in cites:
        moved=[_shift(c['start'],ops),_shift(c['end'],ops)]
        if None in moved:return relevant(f"the cited line {c['start']} moved in a way that cannot be followed")
        # The diff pins repeated lines to one copy; a citation follows its lines only when they occur once in the new file.
        for line in {c['start'],c['end']}:
            if _occurrences(a[max(0,line-1-MARGIN):line+MARGIN],b,where)!=1:return relevant(f'the lines around the cited line {line} occur more than once')
        for span,number in zip(c['spans'],moved if len(c['spans'])==2 else moved[:1]):shifts.append((span,number))
    return {'relevant':False,'reason':'no cited line, symbol or name changed','shifts':shifts}

def shifted(body,shifts):
    """The body with each (span, number) written in, from the end so earlier offsets stay valid."""
    for (s,e),number in sorted(shifts,key=lambda x:-x[0][0]):body=body[:s]+str(number)+body[e:]
    return body

# ---- the sweep --------------------------------------------------------------------------------------------------------
def note_plan(note,root,bindings,deadline=None,dry_run=False):
    """[{path, sha256 now, relevant, reason}] for each changed or missing file of an active note, and the body with the shifts of the
    irrelevant ones. A dry run keeps no baseline it finds in git history."""
    from .digests import states,locate,NAMED
    listed=(note.get('sources') or [])+(note.get('file_hashes') or [])
    recorded={s['path']:s['sha256'] for s in listed if s.get('sha256')}
    found=states(listed,note['domain'],root,bindings);files=[];shifts=[];specs=None
    tracked=list(recorded)
    for path,state in found.items():
        if state=='missing':   # still stale, as digests.changed counts it: only a reviewer can say what a deleted file means
            files.append({'path':path,'relevant':True,'reason':'the file is missing','sha256':None});continue
        if state!='changed':continue
        if NAMED.match(path) and specs is None:
            from .digests import _specs
            specs=_specs(root,note['domain'],bindings)
        location,_=locate(root,note['domain'],path,bindings,specs)
        row={'path':path,'relevant':True,'reason':'','sha256':None}
        try:
            data=location.read_bytes() if location else None
        except OSError:data=None
        if data is None or len(data)>BASELINE_MAX or not _text(data):
            row['reason']='the file cannot be compared';files.append(row);continue
        old=baseline(root,location,recorded[path],deadline,store=not dry_run)
        if old is None:row['reason']='no baseline of the version the note was checked against';files.append(row);continue
        verdict=assess(note.get('body') or '',path,tracked,_decode(old),_decode(data))
        row.update(relevant=verdict['relevant'],reason=verdict['reason'],sha256=hashlib.sha256(data).hexdigest(),abs=str(location))
        if not verdict['relevant']:shifts+=verdict['shifts']
        files.append(row)
    return files,shifted(note.get('body') or '',shifts)

def _write(root,note,files,body):
    """One journaled `rebaseline` change: the irrelevant files' hashes and the shifted body; `updated`, `checked`, `review_by` and `confirmed` kept."""
    from .notes import _change_set,parse_changed,find_note
    from .repository import serialize,store_lock
    from .governance import commit
    fresh={f['path']:f['sha256'] for f in files if not f['relevant']}
    domain=note['domain']
    with store_lock(root):
        path,current=find_note(note['id'],domain,root)
        if current['revision']!=note['revision']:return None
        sources=[dict(s,sha256=fresh.get(s['path'],s['sha256'])) for s in current.get('sources') or []]
        values={'body':body} if body!=(current.get('body') or '') else {}
        if current.get('sources'):values['sources']=sources
        if not values:values['files']=current.get('files') or []
        step,_,_=_change_set(note['id'],domain,'update',root,current['revision'],rehash=False,**values)
        rel=path.relative_to(root).as_posix()
        after=parse_changed(step[rel],current)
        if current.get('file_hashes'):after['file_hashes']=[dict(h,sha256=fresh.get(h['path'],h['sha256'])) for h in current['file_hashes']]
        for key in ('updated','checked','review_by','confirmed'):
            if current.get(key):after[key]=current[key]
            else:after.pop(key,None)
        return commit(root,{rel:serialize(after)},'rebaseline',[domain],{'notes':1,'ref':f"{domain}:{note['id']}",'files':sorted(fresh)})

def _domains(root):
    base=Path(root)/'domains'
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p/'notes').is_dir()) if base.is_dir() else []

def sweep(root,domains=None,dry_run=False,seconds=None):
    """Assess every active note with a changed file; re-baseline the irrelevant files unless dry_run. Stops at the time budget (seconds, None for
    no limit) and says so. Returns {ok, notes: [{ref, title, files, rebaselined, operation}], rebaselined, still_stale, complete}."""
    from .repository import inventory
    root=Path(root).resolve();bindings=load_bindings(root)   # find_note returns resolved paths; _write takes them relative to root
    deadline=time.monotonic()+seconds if seconds else None
    rows=[];done=0;stale=0;complete=True
    for domain in domains or _domains(root):
        for note in inventory(domain,root):
            if deadline is not None and time.monotonic()>deadline:complete=False;break
            if note.get('status','active')!='active' or not ((note.get('sources') or note.get('file_hashes'))):continue
            note=dict(note,domain=domain)
            try:files,body=note_plan(note,root,bindings,deadline,dry_run)
            except Exception as exc:
                from .logs import swallowed
                swallowed('rebaseline.note_plan',exc,root);continue
            if not files:continue
            irrelevant=[f for f in files if not f['relevant']]
            row={'ref':f"{domain}:{note['id']}",'title':note.get('title'),'files':[{k:f[k] for k in ('path','relevant','reason')} for f in files],'rebaselined':False}
            if irrelevant and not dry_run:
                try:
                    receipt=_write(root,note,files,body)
                    if receipt:
                        row.update(rebaselined=True,operation=receipt.get('operation_id'))
                        for f in irrelevant:keep(root,f['abs'],f['sha256'])
                except (OSError,ValueError,TimeoutError) as exc:
                    from .logs import swallowed
                    swallowed('rebaseline.write',exc,root);row['error']=str(exc)[:200]
            if irrelevant and (dry_run or row['rebaselined']):done+=1
            if any(f['relevant'] for f in files) or (irrelevant and not dry_run and not row['rebaselined']):stale+=1
            rows.append(row)
        else:continue
        break
    if not dry_run:
        try:prune(root)
        except Exception as exc:
            from .logs import swallowed
            swallowed('rebaseline.prune',exc,root)
    return {'ok':True,'dry_run':dry_run,'notes':rows,'rebaselined':done,'still_stale':stale,'complete':complete}

def prune(root,now=None):
    """Remove baselines no note in notes/ or archive/ records, once they are PRUNE_DAYS old."""
    from .repository import inventory
    folder=_folder(root)
    if not folder.is_dir():return 0
    used=set()
    for domain in _domains(root):
        for note in inventory(domain,root,retired=True):
            for s in (note.get('sources') or [])+(note.get('file_hashes') or []):
                if s.get('sha256'):used.add(s['sha256'])
    cutoff=(now or time.time())-PRUNE_DAYS*86400;removed=0
    for p in folder.glob('*/*'):
        try:
            if p.name not in used and p.stat().st_mtime<cutoff:p.unlink();removed+=1
        except OSError:continue
    return removed

# ---- scheduling -------------------------------------------------------------------------------------------------------
def schedule(root,bindings,*,subagent=False,plan=False,compact=False):
    """The SessionStart check: start a detached `rebaseline --scheduled` at most once per SWEEP_EVERY seconds. Off with
    `"auto_rebaseline": false` at the top level of bindings.json. Returns 'spawned' or why not; never raises, never waits."""
    try:
        from .runtime import headless
        if bindings.get('auto_rebaseline',True) is False:return 'disabled'
        if subagent:return 'subagent'
        if headless():return 'headless'
        if plan:return 'plan_mode'   # the user asked for no changes
        if compact:return 'compact'
        if os.environ.get('LIVING_MEMORY_NO_BACKGROUND'):return 'background_disabled'
        if os.environ.get('LIVING_MEMORY_MAINTENANCE_CHILD'):return 'maintenance_child'
        marker=Path(root)/'hooks'/'last-rebaseline.json'
        try:
            if time.time()-float(json.loads(marker.read_text(encoding='utf-8'))['at'])<SWEEP_EVERY:return 'cooldown'
        except (OSError,ValueError,KeyError,TypeError):pass
        from .common import atomic_write
        atomic_write(marker,json.dumps({'at':time.time()}))
        _spawn(root)
        return 'spawned'
    except Exception as exc:
        from .logs import swallowed
        swallowed('rebaseline.schedule',exc,root);return 'error'

def _spawn(root):
    script=Path(__file__).resolve().parents[1]/'living_memory.py'
    import sys
    argv=[sys.executable,str(script),'--root',str(root),'rebaseline','--scheduled']
    options={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL,'close_fds':True,'cwd':tempfile.gettempdir()}
    if os.name=='nt':
        flags=0x00000008|0x00000200
        try:subprocess.Popen(argv,creationflags=flags|0x01000000,**options)
        except OSError:subprocess.Popen(argv,creationflags=flags,**options)
    else:subprocess.Popen(argv,start_new_session=True,**options)

def scheduled(root):
    """The detached sweep: one at a time (try-once lock), within SWEEP_SECONDS; a sweep that finds the lock held does nothing."""
    try:
        with file_lock(Path(root)/'hooks'/'rebaseline.lock',timeout=0):
            return sweep(root,seconds=SWEEP_SECONDS)
    except (TimeoutError,OSError):
        return {'ok':True,'skipped':'already_running','notes':[],'rebaselined':0,'still_stale':0,'complete':False}

def before_review(root,cands,seconds):
    """Re-baseline the notes a maintenance run picked, before it reserves anything, so it never pays to review a note whose change this
    module can clear. `cands` are maintenance candidates ({note, domain}); at most `seconds`. Returns how many notes were written (the run
    then picks again). Off with auto_rebaseline false; never raises."""
    try:
        root=Path(root).resolve();bindings=load_bindings(root)
        if bindings.get('auto_rebaseline',True) is False:return 0
        deadline=time.monotonic()+max(0.0,seconds);written=0
        for cand in cands:
            note=cand.get('note') or {}
            if time.monotonic()>deadline:break
            if not (note.get('sources') or note.get('file_hashes')):continue
            note=dict(note,domain=cand['domain'])
            files,body=note_plan(note,root,bindings,deadline)
            if any(not f['relevant'] for f in files) and _write(root,note,files,body):
                for f in files:
                    if not f['relevant']:keep(root,f['abs'],f['sha256'])
                written+=1
        return written
    except Exception as exc:
        from .logs import swallowed
        swallowed('rebaseline.before_review',exc,root);return 0
