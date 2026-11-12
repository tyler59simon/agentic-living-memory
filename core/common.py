"""Shared paths, domain resolution and small safety primitives."""
from __future__ import annotations
import json, os, re, sys, tempfile, time
from pathlib import Path
from contextlib import contextmanager

def memory_root() -> Path:
    from .storage import resolve_root
    return resolve_root()[0]

def load_bindings(root: Path|None=None) -> dict:
    path=(root or memory_root())/"bindings.json"
    try:
        data=json.loads(retry_sharing(lambda:path.read_text(encoding="utf-8-sig")))
        if not isinstance(data,dict) or ('domains' in data and not isinstance(data['domains'],dict)):raise ValueError(f'Invalid bindings preserved: {path}')
        return data
    except FileNotFoundError: return {}
    except (OSError,json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid bindings preserved: {path}") from exc

def _entries(bindings: dict):
    ds=bindings.get("domains",bindings)
    if isinstance(ds,dict):
        for name,value in ds.items():
            if name in {"version","defaults","global","decision_docs"} or not isinstance(value,dict): continue
            prefixes=value.get("prefixes",value.get("paths",[]))
            if isinstance(prefixes,str): prefixes=[prefixes]
            root=value.get("root") or value.get("path")
            if root: prefixes=list(prefixes)+[root]
            yield name,value,prefixes

def resolve_domain(cwd: str|Path, bindings: dict|None=None) -> str|None:
    bindings=bindings if bindings is not None else load_bindings()
    try: current=Path(cwd).expanduser().resolve()
    except (OSError,ValueError): return None
    found=[]
    for name,entry,prefixes in _entries(bindings):
        for raw in prefixes:
            try: prefix=Path(os.path.expandvars(str(raw))).expanduser().resolve()
            except (OSError,ValueError): continue
            try: current.relative_to(prefix)
            except ValueError: continue
            found.append((len(str(prefix)),name))
    return max(found)[1] if found else None

def domain_prefixes(domain: str|None, bindings: dict) -> list[Path]:
    """The folders bound to a domain, longest first."""
    paths=[]
    for name,_,prefixes in _entries(bindings):
        if name!=domain: continue
        for raw in prefixes:
            try: paths.append(Path(os.path.expandvars(str(raw))).expanduser().resolve())
            except (OSError,ValueError): continue
    return sorted(paths,key=lambda p:-len(str(p)))

def display_path(path: Path, prefixes: list[Path]) -> str|None:
    """A note's view of a file: relative to the bound project folder, else absolute; forward slashes."""
    for prefix in prefixes:
        if path.is_relative_to(prefix):
            rel=path.relative_to(prefix).as_posix()
            return None if rel=='.' else rel
    return path.as_posix()

def git_root(cwd) -> Path|None:
    """The enclosing git work tree (a .git folder, or a .git file for worktrees and submodules), else None."""
    try:here=Path(cwd).resolve()
    except (OSError,ValueError):return None
    return next((p for p in (here,*here.parents) if (p/'.git').exists()),None)

def git_dir(repo) -> Path|None:
    """The git directory of a work tree root: its .git folder, or the folder a .git file's `gitdir:` line names (worktrees, submodules)."""
    try:
        dot=Path(repo)/'.git'
        if dot.is_dir():return dot
        if dot.is_file():
            match=re.match(r'gitdir:\s*(.+)',dot.read_text(encoding='utf-8',errors='replace').strip())
            if match:
                target=Path(match[1].strip());target=target if target.is_absolute() else dot.parent/target
                return target.resolve() if target.is_dir() else None
    except (OSError,ValueError):pass
    return None

def _common_dir(gd: Path) -> Path:
    """Where a linked worktree's repository-wide files (config, packed-refs, refs) live: its `commondir`, else the git directory itself."""
    try:
        target=Path((gd/'commondir').read_text(encoding='utf-8',errors='replace').strip());return (target if target.is_absolute() else gd/target).resolve()
    except (OSError,ValueError):return gd

def _origin_url(config: str) -> str|None:
    inside=False
    for line in config.splitlines():
        line=line.strip()
        if line.startswith('['):inside=bool(re.match(r'\[\s*remote\s+"origin"\s*\]',line,re.I));continue
        if inside:
            match=re.match(r'url\s*=\s*(.*)',line,re.I)
            if match and match[1].strip().strip('"'):return match[1].strip().strip('"')
    return None

def normalize_remote(url,base=None) -> str|None:
    """A git remote url as the identity every clone of that repo shares: `host/owner/repo` (scheme, credentials, port,
    .git and trailing slashes dropped, host lowercase; https, ssh and scp-like forms agree), or `file:` plus the
    casefolded resolved path for a local remote (relative paths resolve against base)."""
    text=str(url or '').strip()
    if not text:return None
    def local(path):
        if re.match(r'/[A-Za-z]:[/\\]',path):path=path[1:]   # file:///C:/x
        try:
            found=Path(path).expanduser();found=found if found.is_absolute() or base is None else Path(base)/found
            return 'file:'+found.resolve().as_posix().casefold()
        except (OSError,ValueError):return None
    def remote(host,path):
        host=host.rpartition('@')[2].lower();path=re.sub(r'(?i)\.git$','',path.strip().strip('/')).strip('/')
        return f'{host}/{path.casefold()}' if path else host   # hosts such as GitHub treat Owner/Repo and owner/repo alike
    scheme=re.match(r'([a-z][a-z0-9+.-]*)://(.*)',text,re.I)
    if scheme:
        if scheme[1].lower()=='file':return local(scheme[2])
        host,_,path=scheme[2].partition('/');return remote(re.sub(r':\d*$','',host),path)
    scp=re.match(r'(?:[^@/\\]+@)?([^/\\:@]{2,}):(.+)',text)   # git@host:owner/repo (a single letter is a drive)
    return remote(scp[1],scp[2]) if scp else local(text)

def repo_identity(repo) -> str|None:
    """What every clone and worktree of one repository has in common: its normalized `origin` url, read from the common git
    config without running git. None for a folder that is not a work tree root or has no origin."""
    gd=git_dir(repo)
    if gd is None:return None
    try:url=_origin_url((_common_dir(gd)/'config').read_text(encoding='utf-8',errors='replace'))
    except OSError:return None
    return normalize_remote(url,repo) if url else None

def repo_label(repo) -> str:
    """The name a repo goes by in titles and lines: the last segment of its identity (the same in every clone), else its folder name."""
    return re.sub(r'(?i)\.git$','',(repo_identity(repo) or '').rsplit('/',1)[-1]) or Path(repo).name   # a local remote keeps its .git in the identity

def head_oid(repo) -> str|None:
    """The commit HEAD points to, read from the .git files (HEAD, loose refs, packed-refs) without running git."""
    gd=git_dir(repo)
    if gd is None:return None
    try:
        head=(gd/'HEAD').read_text(encoding='utf-8',errors='replace').strip()
        if not head.startswith('ref:'):return head or None
        ref=head[4:].strip();common=_common_dir(gd)
        for folder in (gd,common):
            try:return (folder/ref).read_text(encoding='utf-8',errors='replace').strip() or None
            except OSError:continue
        for line in (common/'packed-refs').read_text(encoding='utf-8',errors='replace').splitlines():
            if line.endswith(' '+ref):return line.split(' ',1)[0]
    except OSError:pass
    return None

def safe_domain(name: str) -> str:
    if not isinstance(name,str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*",name) or name in {".",".."}:
        raise ValueError("invalid domain")
    return name

def canonical_domain(name,root=None):
    aliases=load_bindings(root).get('domain_aliases',{});seen=set()
    while name in aliases:
        if name in seen:raise ValueError('Domain alias cycle')
        seen.add(name);name=aliases[name]
    return safe_domain(name)

# Notes that hold beyond any one project. Not bound to a folder: every session searches it.
GLOBAL='_global'

def global_domains(root=None) -> list[str]:
    """The configured global domains (search_scope.global_domains), aliases resolved; [_global] by default."""
    scope=load_bindings(root).get('search_scope',{})
    names=scope.get('global_domains',[GLOBAL]) if isinstance(scope,dict) else [GLOBAL]
    found=[]
    for name in names if isinstance(names,list) else []:
        try:found.append(canonical_domain(name,root))
        except ValueError:continue
    return list(dict.fromkeys(found)) or [GLOBAL]

def global_domain(root=None) -> str:
    """Where `capture --global` and promotion write: the first configured global domain."""
    return global_domains(root)[0]

@contextmanager
def file_lock(path: Path, timeout: float=10.0):
    """Cross-platform exclusive lock using OS file locking (no stale-lock guessing)."""
    path.parent.mkdir(parents=True,exist_ok=True); f=path.open("a+b"); deadline=time.monotonic()+timeout
    locked=False
    try:
        while True:
            try:
                if os.name=="nt":
                    import msvcrt
                    f.seek(0,os.SEEK_END)
                    if f.tell()==0:f.write(b"\0");f.flush()
                    f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                locked=True
                break
            except (OSError,BlockingIOError):
                if time.monotonic()>=deadline: raise TimeoutError(f"timed out locking {path}")
                time.sleep(.05)
        yield
    finally:
        try:
            if locked:
                if os.name=="nt":
                    import msvcrt
                    f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(),fcntl.LOCK_UN)
        finally: f.close()

RETRY_ATTEMPTS=10

def retry_sharing(operation):
    """Retry a filesystem call through transient Windows sharing violations.

    Antivirus, the search indexer, Obsidian and parallel hook processes briefly open
    files without FILE_SHARE_DELETE, which makes os.replace/unlink/open raise
    PermissionError for a few milliseconds."""
    for attempt in range(RETRY_ATTEMPTS):
        try:return operation()
        except PermissionError:
            if attempt==RETRY_ATTEMPTS-1:raise
            time.sleep(.02*(attempt+1))

def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix="."+path.name+".",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as out:
            out.write(text);out.flush();os.fsync(out.fileno())
        retry_sharing(lambda:os.replace(tmp,path))
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise
