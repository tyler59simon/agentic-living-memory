"""Codebase maps: one `map` note per repo, a synthesized orientation (layout, entry points, data flow,
build/test) that saves the next session from exploring the repo again.

A map is keyed by its repo: the first entry of its `files`, resolved against the domain's bound folder
('.' is that folder itself). Maps are found by repo rather than by search scope, so a map stored in a
child domain (a repo bound on its own under the session's folder) is still found from the parent."""
from __future__ import annotations
import json,os,re,sqlite3,subprocess
from pathlib import Path
from .common import domain_prefixes,git_root,resolve_domain,memory_root

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

def _rows(domain,root):
    """Active map rows from the domain's index (no rebuild: hooks must stay fast), else from the note files."""
    from .search import _unchanged,domain_dir
    try:folder=domain_dir(domain,root)
    except ValueError:return []
    if not (folder/'notes').is_dir():return []
    db_path=folder/'search.sqlite3'
    if db_path.exists():
        try:
            db=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)
            try:rows=db.execute("SELECT id,title,updated,files,body,path,revision FROM notes WHERE status='active' AND type='map'").fetchall()
            finally:db.close()
            return [{'id':i,'domain':domain,'title':t,'type':'map','updated':str(u or ''),'files':f,'body':b,'revision':r}
                    for i,t,u,f,b,p,r in rows if _unchanged(p,r)]
        except sqlite3.Error:pass
    from .repository import inventory
    try:return [dict(n,domain=domain) for n in inventory(domain,root) if n.get('type')=='map' and n.get('status','active')=='active']
    except (OSError,ValueError):return []

def find_map(repo,root=None,bindings=None,notes=None):
    """The active map note for a repo folder, else None. notes, when given, is [(domain, note)] to look in instead."""
    from .common import load_bindings
    root=Path(root or memory_root()).resolve();bindings=bindings if bindings is not None else load_bindings(root)
    try:repo=Path(repo).resolve()
    except (OSError,ValueError):return None
    if notes is None:notes=[(d,n) for d in map_domains(repo,bindings,root) for n in _rows(d,root)]
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

def freshness(hit,repo):
    count=commits_since(repo,hit.get('updated'))
    since=f'; {count} commit{"s" if count!=1 else ""} since' if count is not None else ''
    return (f"updated {str(hit.get('updated',''))[:10]}{since}. Verify the areas you touch against the code, and update the map "
            'when its layout, entry points or build steps have moved.')

def map_block(hit,repo,budget=MAP_BUDGET):
    """The map's body for a prompt, with a freshness line; bounded."""
    from .notes import short_ref
    from .session_start import _cap
    head=f"Codebase map for {Path(repo).name} — {short_ref(hit['domain'],hit['id'])} {hit['title']} ({freshness(hit,repo)})"
    body=_cap(str(hit.get('body','')).strip(),max(400,budget-len(head)-1))
    return head+'\n'+body

def pointer_line(hit,repo,width=400):
    """A one-line pointer to a map (SessionStart, tool hints)."""
    from .notes import short_ref
    from .note_format import sections,plain
    summary=' '.join(plain(sections(str(hit.get('body',''))).get('summary','')).split())
    if len(summary)>width:summary=summary[:summary.rfind(' ',0,width) if ' ' in summary[:width] else width]+' …'
    return f"Codebase map for {Path(repo).name}: {short_ref(hit['domain'],hit['id'])} {hit['title']} — {summary} ({freshness(hit,repo)})"

def capture_conflict(files,domain,root,bindings,replaced=()):
    """For a new map: an error message when its repo already has an active map, else None."""
    from .notes import short_ref
    from .repository import inventory
    repo=_resolve(files[0],domain,bindings) if files else None
    if repo is None:return 'map notes need --file REPO (the repo folder they describe)'
    if not repo.is_dir():return f'map notes describe a repo folder; {repo.as_posix()} is not a folder'
    for other in dict.fromkeys([domain]+map_domains(repo,bindings,root)):
        try:notes=[n for n in inventory(other,root) if n.get('type')=='map' and n.get('status','active')=='active' and n['id'] not in replaced]
        except (OSError,ValueError):continue
        hit=find_map(repo,root,bindings,[(other,n) for n in notes])
        if hit:
            ref=short_ref(other,hit['id'])
            return (f"map_exists conflict: {ref} \"{hit['title']}\" is the codebase map for {repo.as_posix()}; "
                    f"run `update {ref}` (or capture with `--supersedes {ref}`)")
    return None
