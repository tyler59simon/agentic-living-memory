"""The `alm` launcher: a console .exe (Windows, pip's distlib stub plus a zip) or an sh script that runs living_memory.py."""
from __future__ import annotations
import functools,os,sys
from pathlib import Path
NAME='alm'
MARK='# agentic-living-memory launcher'
FILE=NAME+'.exe' if os.name=='nt' else NAME

def tail(python,script):
    """What the launcher ends with: the whole sh script, or the shebang line plus a zip holding __main__.py (fixed timestamp, stored: the bytes depend only on the two paths)."""
    python,script=str(python),str(script)
    if os.name!='nt':
        import shlex
        return f'#!/bin/sh\n{MARK}\nexec {shlex.quote(python)} {shlex.quote(script)} "$@"\n'.encode()
    import io,zipfile
    main=f'{MARK}\nimport runpy,sys\nsys.path.insert(0,{str(Path(script).parent)!r})\nsys.argv[0]={script!r}\nrunpy.run_path({script!r},run_name="__main__")\n'
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_STORED) as z:z.writestr(zipfile.ZipInfo('__main__.py',(1980,1,1,0,0,0)),main.encode())
    return b'#!'+(f'"{python}"' if ' ' in python else python).encode()+b'\n'+buf.getvalue()

def _stub():
    """pip's console stub for this machine (found without importing pip), or None."""
    import importlib.util,platform
    try:spec=importlib.util.find_spec('pip')
    except (ImportError,ValueError):return None
    for base in (spec.submodule_search_locations or []) if spec else []:
        name='t64-arm.exe' if platform.machine().lower() in {'arm64','aarch64'} else 't64.exe' if sys.maxsize>2**32 else 't32.exe'
        path=Path(base)/'_vendor'/'distlib'/name
        if path.is_file():return path.read_bytes()
    return None

def build(python,script):
    """The launcher's bytes, or None when Windows has no stub to build it from."""
    if os.name!='nt':return tail(python,script)
    stub=_stub()
    return stub+tail(python,script) if stub else None

def ours(path):
    """True for a regular file (not a symlink) that carries our marker."""
    try:return not os.path.islink(path) and os.path.isfile(path) and MARK.encode() in Path(path).read_bytes()
    except OSError:return False

def _names():
    if os.name!='nt':return [NAME]
    return [NAME]+[NAME+e.lower() for e in dict.fromkeys(os.environ.get('PATHEXT','.COM;.EXE;.BAT;.CMD').lower().split(';')+['.ps1','.cmd','.bat','.exe']) if e]

@functools.lru_cache(maxsize=8)
def _active(path_env,python,script):
    for folder in path_env.split(os.pathsep):
        if not folder:continue
        hits=[n for n in _names() if os.path.lexists(os.path.join(folder,n))]
        if not hits:continue
        # The first folder with any `alm*` decides, and only our own file may be in it: a .cmd or .ps1 beside or ahead of it would run instead.
        if [h.lower() for h in hits]!=[FILE.lower()]:return False
        file=Path(folder)/FILE
        try:return not os.path.islink(file) and file.read_bytes().endswith(tail(python,script))
        except OSError:return False
    return False

def active(script):
    """True when a bare `alm` in the hook's shell runs this install's script: the first `alm*` on PATH is our launcher and ends with the tail for this interpreter and script."""
    return _active(os.environ.get('PATH',''),str(sys.executable),str(script))
