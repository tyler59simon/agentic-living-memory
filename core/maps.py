"""Codebase maps: one `map` note per repo, a synthesized orientation (layout, entry points, data flow,
build/test) that saves the next session from exploring the repo again.

A map is keyed by its repo: the first entry of its `files`, resolved against the domain's bound folder
('.' is that folder itself). Maps are found by repo rather than by search scope, so a map stored in a
child domain (a repo bound on its own under the session's folder) is still found from the parent."""
from __future__ import annotations
import json,os,re,sqlite3,subprocess
from pathlib import Path
from .common import domain_prefixes,git_root,resolve_domain,memory_root,repo_identity,repo_label,head_oid

MAP_BUDGET=2500
MAX_MAPS=2
# Repo-name words that say nothing alone ("add a plugin", "the memory of it"); two of them in a row still match.
COMMON=set('''agentic architecture plugin plugins memory living suite claude codex copilot factory app apps core lib libs
src test tests tool tools util utils service services server client web api cli docs doc demo example examples sample samples
project projects repo repos main master dev development config data model models script scripts module modules package packages
'''.split())
MIN_SINGLE=6
WORD=re.compile(r'[a-z0-9]+')

def _words(text):return WORD.findall(str(text).casefold())

def _resolve(raw,domain,bindings):
    from .search import normalize_file
    text=normalize_file(raw)
    if not text:return None
    path=Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        prefixes=domain_prefixes(domain,bindings)
        if not prefixes:return None
        path=prefixes[0] if text=='.' else prefixes[0]/text
    try:return path.resolve()
    except (OSError,ValueError):return None

def repo_of(note,domain,bindings):
    """The repo folder a map note is about (its first `files` entry, resolved), else None."""
    files=note.get('files')
    if isinstance(files,str):
        try:files=json.loads(files)
        except ValueError:files=[files]
    return _resolve(files[0],domain,bindings) if files else None

def map_domains(repo,bindings,root=None):
    """Where a repo's map can live: the domain the repo resolves to and that domain's parents, then global."""
    from .runtime import parent_domains
    from .common import global_domains
    found=resolve_domain(repo,bindings)
    near=[found]+parent_domains(found,bindings) if found else []
    return list(dict.fromkeys(near+global_domains(root)))

def repo_name(path):
    """The name maps and briefs use for a folder in titles and lines: the repo's name from its git remote (the same in every clone), else its folder name."""
    return repo_label(path)

def note_identity(files,domain,bindings):
    """The git identity (common.repo_identity) of the folder a keyed note's first `files` entry names, else None."""
    repo=_resolve(files[0],domain,bindings) if files else None
    return repo_identity(repo) if repo is not None and repo.is_dir() else None

def _identity_of(note,domain,bindings):
    """A keyed note's stored repo identity; for a note written before the `repo` property existed, that of the folder it is about."""
    if note.get('repo'):return note['repo']
    folder=repo_of(note,domain,bindings)
    return repo_identity(folder) if folder is not None else None

def _rows(domain,root,kind='map'):
    """Active rows of a note type (map or brief, or a tuple of types) from the domain's index (no rebuild: hooks must stay fast), else from the note files."""
    kinds=(kind,) if isinstance(kind,str) else tuple(kind)
    from .search import _unchanged,domain_dir
    try:folder=domain_dir(domain,root)
    except ValueError:return []
    if not (folder/'notes').is_dir():return []
    db_path=folder/'search.sqlite3'
    if db_path.exists():
        try:
            db=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)
            try:rows=db.execute(f"SELECT id,title,updated,files,body,path,revision,type,repo FROM notes WHERE status='active' AND type IN ({','.join('?'*len(kinds))})",kinds).fetchall()
            finally:db.close()
            return [{'id':i,'domain':domain,'title':t,'type':k,'updated':str(u or ''),'files':f,'body':b,'revision':r,'repo':e or ''}
                    for i,t,u,f,b,p,r,k,e in rows if _unchanged(p,r)]
        except sqlite3.Error:pass
    from .repository import inventory
    try:return [dict(n,domain=domain) for n in inventory(domain,root) if n.get('type') in kinds and n.get('status','active')=='active']
    except (OSError,ValueError):return []

def find_map(repo,root=None,bindings=None,notes=None,kind='map'):
    """The active note of a keyed type (map by default) for a folder, else None: the note of the same repo identity (a clone of the
    same origin elsewhere counts) first, then the one keyed to this very folder. notes, when given, is [(domain, note)] to look in instead."""
    from .common import load_bindings
    root=Path(root or memory_root()).resolve();bindings=bindings if bindings is not None else load_bindings(root)
    try:repo=Path(repo).resolve()
    except (OSError,ValueError):return None
    if notes is None:notes=[(d,n) for d in map_domains(repo,bindings,root) for n in _rows(d,root,kind)]
    identity=repo_identity(repo)
    if identity:
        found=next((dict(n,domain=d) for d,n in notes if _identity_of(n,d,bindings)==identity),None)
        if found:return found
    return next((dict(n,domain=d) for d,n in notes if repo_of(n,d,bindings)==repo),None)

def candidate_repos(cwd,bindings):
    """Repos a prompt may name: bound folders that are git repos, git repos directly under a bound folder,
    and the repo holding cwd. {resolved path: name}."""
    from .common import _entries
    found={}
    def add(path):
        try:found.setdefault(path.resolve(),path.resolve().name)
        except (OSError,ValueError):pass
    for _,_,prefixes in _entries(bindings):
        for raw in prefixes:
            try:prefix=Path(os.path.expandvars(str(raw))).expanduser().resolve()
            except (OSError,ValueError):continue
            if (prefix/'.git').exists():add(prefix)
            try:children=[c for c in prefix.iterdir() if c.is_dir() and (c/'.git').exists()]
            except OSError:continue
            for child in children:add(child)
    here=git_root(cwd)
    if here:add(here)
    return found

def named_repos(text,candidates,aliases=None):
    """Repos the text names, in order of first mention. A repo matches on its full name, two or more of its
    name words in a row ("architecture suite"), a distinctive single word ("hivemind"), or an alias."""
    from .search import STOP
    words=_words(text)
    if not words:return []
    joined=' '+' '.join(words)+' '
    names={repo:_words(name) for repo,name in candidates.items()}
    counts={}
    for parts in names.values():
        for w in set(parts):counts[w]=counts.get(w,0)+1
    hits=[]
    for repo,parts in names.items():
        at=None
        phrases=[' '+' '.join(parts[i:j])+' ' for i in range(len(parts)) for j in range(i+2,len(parts)+1)]
        if len(parts)==1:phrases=[]
        singles=[w for w in parts if len(w)>=MIN_SINGLE and w not in COMMON and w not in STOP and counts.get(w)==1]
        alias=[' '+' '.join(_words(a))+' ' for a in (aliases or {}).get(candidates[repo],[]) if _words(a)]
        for phrase in phrases+[' '+w+' ' for w in singles]+alias:
            index=joined.find(phrase)
            if index>=0:at=index if at is None else min(at,index)
        if at is not None:hits.append((at,repo))
    return [repo for _,repo in sorted(hits,key=lambda h:h[0])]

def commits_since(repo,updated,timeout=2):
    """Commits on HEAD since the map was updated, or None when git cannot tell quickly."""
    if not updated:return None
    try:
        done=subprocess.run(['git','-C',str(repo),'rev-list','--count',f'--since={updated}','HEAD'],capture_output=True,text=True,timeout=timeout)
        return int(done.stdout.strip()) if done.returncode==0 else None
    except (OSError,ValueError,subprocess.SubprocessError):return None

# A note is stale when a file it cites changed after it was last written: its own `files`, paths its body names, the repo's
# build files and its decision documents (triggers.decision_docs). A commit count says the repo moved, not that the note is wrong.
BUILD_FILES=('pom.xml','build.gradle*','package.json','pyproject.toml','setup.cfg','Cargo.toml','go.mod','*.csproj')
MAX_CHANGED=500
STALE_NAMES=3

def _git(repo,*args,timeout=2):
    done=subprocess.run(['git','-c','core.quotepath=off','-C',str(repo),*args],capture_output=True,timeout=timeout)
    return done.stdout.decode('utf-8','replace') if done.returncode==0 else None

def changed_since(repo,updated,timeout=2):
    """Paths (relative to repo, at most MAX_CHANGED) that differ between the last commit before `updated` and HEAD. A
    diff between the two, not `log --name-only`, which shows nothing for merge commits. [] when git cannot tell; never raises."""
    if not updated:return []
    try:
        base=(_git(repo,'rev-list','-1',f'--before={updated}','HEAD',timeout=timeout) or '').strip()
        if not base:return []
        out=_git(repo,'diff','--name-only','--no-renames','--relative',base,'HEAD',timeout=timeout)
        return [l.strip() for l in (out or '').splitlines() if l.strip()][:MAX_CHANGED]
    except (OSError,ValueError,subprocess.SubprocessError):return []

STALE_DOC_GLOBS=('**/adr/**','**/adrs/**','**/decisions/**')

def cited_paths(hit,changed,repo=None,bindings=None):
    """The paths among `changed` (repo-relative) that this map or brief cites."""
    import fnmatch
    from .search import normalize_file
    from .triggers import decision_docs
    listed=set()
    # Path-like tokens of the body (search.mentions_file's rule: a whole path token, either slash), compared as a set: a push can change hundreds of paths.
    named={t.strip('.').casefold() for t in re.findall(r'[\w./-]+',str(hit.get('body') or '').replace('\\','/')) if '/' in t or '.' in t.strip('.')}
    files=hit.get('files')
    if isinstance(files,str):
        try:files=json.loads(files)
        except ValueError:files=[files]
    for raw in (files or [])[1:]:
        found=_resolve(raw,hit['domain'],bindings or {}) if hit.get('domain') else None
        try:listed.add((found.relative_to(repo).as_posix() if found is not None and repo is not None else normalize_file(raw)).casefold())
        except ValueError:listed.add(normalize_file(raw).casefold())
    # Only decision records make a whole-repo summary stale by themselves; an ordinary docs or ticket edit does not
    # (those count only when the map names them), or every docs-heavy session would be asked to update its map.
    from .triggers import matches
    docs={p for p in decision_docs(changed,bindings) if matches(p,STALE_DOC_GLOBS)}
    def cites(path):
        parts=path.split('/')
        return (path.casefold() in listed or path in docs or path.casefold() in named
                or len(parts)<=2 and any(fnmatch.fnmatch(parts[-1].casefold(),b.casefold()) for b in BUILD_FILES))
    return [p for p in changed if cites(p)]

def currency(hit,repo,bindings=None,cache=None):
    """(entry, computed): entry is {'rev': note revision, 'head': HEAD oid, 'changed': cited paths changed since the note was
    written, 'count': commits since} for the note, reused from `cache` (an earlier entry) while the note and HEAD are the same, so
    no git runs. HEAD comes from the .git files; without one nothing is cached."""
    head=head_oid(repo);rev=str(hit.get('revision') or '')
    if cache and head and rev and cache.get('rev')==rev and cache.get('head')==head:return cache,False
    updated=hit.get('updated')
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(1) as pool:   # the commit count does not wait for the diff: process starts dominate on Windows
        count=pool.submit(commits_since,repo,updated);changed=changed_since(repo,updated)
        return {'rev':rev,'head':head,'changed':cited_paths(hit,changed,repo,bindings),'count':count.result()},True

def stale_clause(hit,entry,what='map'):
    """`STALE: a, b and N more changed since 2026-11-10 — check and update this map`, '' when nothing the note cites changed."""
    changed=(entry or {}).get('changed') or []
    if not changed:return ''
    names=', '.join(changed[:STALE_NAMES])+(f' and {len(changed)-STALE_NAMES} more' if len(changed)>STALE_NAMES else '')
    return f"STALE: {names} changed since {str(hit.get('updated',''))[:10]} — check and update this {what}"

def freshness(hit,repo,entry=None,bindings=None):
    entry=entry if entry is not None else currency(hit,repo,bindings)[0]
    count=entry.get('count');day=str(hit.get('updated',''))[:10]
    since=f'; {count} commit{"s" if count!=1 else ""} since' if count is not None else ''
    stale=stale_clause(hit,entry)
    if stale:return f"{stale}{since}. Verify the areas you touch against the code."
    return (f"updated {day}{since}. Verify the areas you touch against the code, and update the map "
            'when its layout, entry points or build steps have moved.')

def map_block(hit,repo,budget=MAP_BUDGET,entry=None):
    """The map's body for a prompt, with a freshness line; bounded."""
    from .notes import short_ref
    from .session_start import _cap
    head=f"Codebase map for {repo_name(repo)} — {short_ref(hit['domain'],hit['id'])} {hit['title']} ({freshness(hit,repo,entry)})"
    body=_cap(str(hit.get('body','')).strip(),max(400,budget-len(head)-1))
    return head+'\n'+body

def pointer_line(hit,repo,width=400,entry=None):
    """A one-line pointer to a map (SessionStart, tool hints)."""
    from .notes import short_ref
    from .note_format import sections,plain
    summary=' '.join(plain(sections(str(hit.get('body',''))).get('summary','')).split())
    if len(summary)>width:summary=summary[:summary.rfind(' ',0,width) if ' ' in summary[:width] else width]+' …'
    return f"Codebase map for {repo_name(repo)}: {short_ref(hit['domain'],hit['id'])} {hit['title']} — {summary} ({freshness(hit,repo,entry)})"

REPO_NOTE_TYPES=('decision','bug','runbook','policy')
REPO_NOTES_BUDGET=900
LINK=re.compile(r'\[\[([^\]|#]+)')

def repo_notes(hit,repo,root,bindings,shown,budget=REPO_NOTES_BUDGET,files=True,exclude=()):
    """(lines, refs): one-line summaries of the active decision, bug, runbook and policy notes in the map's (or brief's)
    domain about this repo, newest first within budget characters. A note belongs when its `files` fall inside the repo
    (files=False skips that test) or when hit links to it by [[Title]] in its Resource Links. Notes already shown this
    session (shown maps ref -> revision key) and refs in exclude are left out. '' and [] when none."""
    from .notes import short_ref,unconfirmed
    from .note_format import sections
    from .session_start import gist
    from .runtime import _unseen
    domain=hit['domain'];linked={t.strip().casefold() for t in LINK.findall(sections(str(hit.get('body',''))).get('links',''))}
    def inside(note):
        raw=note.get('files')
        if isinstance(raw,str):
            try:raw=json.loads(raw)
            except ValueError:raw=[raw]
        return any((p:=_resolve(f,domain,bindings)) is not None and p.is_relative_to(repo) for f in raw or [])
    found=[n for n in _rows(domain,root,REPO_NOTE_TYPES)
           if n['title'].strip().casefold() in linked or (files and inside(n))]
    lines=[];refs=[];used=0
    head='Notes about this repo (decisions, bugs, runbooks and policies; read one when it bears on the task):'
    for n in sorted(found,key=lambda n:n.get('updated',''),reverse=True):
        ref=f"{domain}:{n['id']}"
        if ref in exclude or not _unseen(dict(n,domain=domain),shown):continue
        summary=gist(n);line=f"- {n['type'].upper()} {short_ref(domain,n['id'])} {unconfirmed(n)}{n['title']}"+(f' — {summary}' if summary else '')
        if used+len(line)+1>budget-len(head)-1:break
        lines.append(line);refs.append(ref);used+=len(line)+1
    return ('\n'.join([head]+lines),refs) if lines else ('',[])

KEYED={'map':('codebase map','REPO','the repo folder they describe','repo'),'brief':('project brief','DIR','the project folder they describe','project')}

def capture_conflict(files,domain,root,bindings,replaced=(),kind='map'):
    """For a new map or brief: an error message when its folder already has an active note of that type, else None."""
    from .notes import short_ref
    from .repository import inventory
    label,arg,what,noun=KEYED[kind]
    repo=_resolve(files[0],domain,bindings) if files else None
    if repo is None:return f'{kind} notes need --file {arg} ({what})'
    if not repo.is_dir():return f'{kind} notes describe a {noun} folder; {repo.as_posix()} is not a folder'
    for other in dict.fromkeys([domain]+map_domains(repo,bindings,root)):
        try:notes=[n for n in inventory(other,root) if n.get('type')==kind and n.get('status','active')=='active' and n['id'] not in replaced]
        except (OSError,ValueError):continue
        hit=find_map(repo,root,bindings,[(other,n) for n in notes])
        if hit:
            ref=short_ref(other,hit['id']);identity=repo_identity(repo)
            same=f"{identity} (the same git remote as {repo.as_posix()})" if identity and _identity_of(hit,other,bindings)==identity and repo_of(hit,other,bindings)!=repo else repo.as_posix()
            return (f"{kind}_exists conflict: {ref} \"{hit['title']}\" is the {label} for {same}; "
                    f"run `update {ref}` (or capture with `--supersedes {ref}`)")
    return None
