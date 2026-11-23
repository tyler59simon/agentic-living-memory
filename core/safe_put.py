"""Writing a file outside the store without ever losing a save made meanwhile (the Obsidian mirror and folder notebook pages).

Nothing here follows a link or junction, and a file is replaced only while it still holds the bytes the decision was made on."""
from __future__ import annotations
import hashlib,os,time,tempfile
from pathlib import Path
from .common import retry_sharing
from .repository import is_link_or_junction as _is_link


def _sha(data):return hashlib.sha256(data).hexdigest() if data is not None else None


def _bytes(path):
    """A file's bytes; None when it is not there. Other errors (a folder in its place, a locked file) raise OSError."""
    try:return path.read_bytes()
    except (FileNotFoundError,NotADirectoryError):return None


def _safe(base,rel):
    """base/rel when no part below base is a link or junction (the last one included), else None."""
    cursor=Path(base)
    for part in rel.split('/'):
        cursor=cursor/part
        try:
            if _is_link(cursor):return None
        except OSError:return None
    return cursor


def _place(src,dst):
    """Move src to dst unless something is at dst (a file saved meanwhile is never replaced): FileExistsError then. Windows
    rename never replaces; elsewhere a hard link is made and src removed, or, on a filesystem without hard links, dst is
    checked just before an ordinary rename."""
    if os.name=='nt':retry_sharing(lambda:os.rename(src,dst));return
    try:os.link(src,dst)
    except FileExistsError:raise
    except OSError:
        if os.path.lexists(dst):raise FileExistsError(dst)
        retry_sharing(lambda:os.rename(src,dst));return
    retry_sharing(lambda:os.unlink(src))


def _put_back(aside,path):
    """Return a file moved aside to its place, or, when a new save is there, beside it under a name of its own."""
    try:_place(aside,path);return
    except FileExistsError:pass
    stamp=time.strftime('%Y%m%d-%H%M%S')
    for n in range(1,1000):
        kept=path.with_name(f'{path.stem} (kept {stamp}{"" if n==1 else f" {n}"}){path.suffix}')
        try:_place(aside,kept);return
        except FileExistsError:continue
    raise FileExistsError(path)


def _put(base,rel,data,expect):
    """Write base/rel (remove it, with data None) while it holds the bytes whose hash is `expect` (None: absent). False when it
    changed meanwhile or a link or junction is in the way. A save made at any moment is never lost: the file is first moved
    aside by a rename and judged there (a save after the rename makes a new file, which the final no-overwrite rename then
    refuses), and a file found changed is moved back."""
    path=_safe(base,rel)
    if path is None or _sha(_bytes(path))!=expect:return False
    cursor=Path(base)
    for part in rel.split('/')[:-1]:
        cursor=cursor/part
        if not cursor.is_dir():cursor.mkdir()
        if _is_link(cursor):return False
    aside=None
    if expect is not None:
        aside=path.with_name(f'.{path.name}.{os.getpid()}-{time.monotonic_ns()}.aside')
        try:retry_sharing(lambda:os.rename(path,aside))   # the bytes now at the path are judged where nobody else writes
        except FileNotFoundError:return False
        try:changed=_sha(_bytes(aside))!=expect
        except OSError:changed=True   # cannot be judged: put back
        if changed:   # changed just before the rename: put back, or beside a save made since
            _put_back(aside,path);return False
    try:
        if data is not None:
            fd,temp=tempfile.mkstemp(prefix='.',suffix='.mirror',dir=path.parent)
            try:
                with os.fdopen(fd,'wb') as f:f.write(data)
                _place(temp,path)   # never over a file saved meanwhile
            except FileExistsError:
                if aside is not None:
                    try:retry_sharing(aside.unlink)   # the expected bytes are known; the new save stays
                    except OSError:pass
                return False
            finally:
                if os.path.exists(temp):
                    try:retry_sharing(lambda:os.unlink(temp))
                    except OSError:pass
    except BaseException:
        if aside is not None and aside.exists():_put_back(aside,path)
        raise
    if aside is not None:
        try:retry_sharing(aside.unlink)
        except OSError:pass
    return True
