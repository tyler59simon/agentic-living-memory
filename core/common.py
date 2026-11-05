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
        data=json.loads(retry_sharing(lambda:path.read_text(encoding="utf-8")))
        if not isinstance(data,dict) or ('domains' in data and not isinstance(data['domains'],dict)):raise ValueError(f'Invalid bindings preserved: {path}')
        return data
    except FileNotFoundError: return {}
    except (OSError,json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid bindings preserved: {path}") from exc

def _entries(bindings: dict):
    ds=bindings.get("domains",bindings)
    if isinstance(ds,dict):
        for name,value in ds.items():
            if name in {"version","defaults","global"} or not isinstance(value,dict): continue
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
