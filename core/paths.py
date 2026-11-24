"""Path comparison that matches how the disk names files (MACOS-1).

Windows (NTFS) and macOS (APFS, HFS+) ignore letter case by default, and macOS also treats the NFC and NFD Unicode spellings
of a name as one file. os.path.normcase folds case only on Windows, and Path.is_relative_to never folds, so guards and
lookups that compare paths must go through fold, path_key or is_within.

Limit: macOS folding assumes the default case-insensitive volume. On a case-sensitive APFS volume, two folders whose names
differ only in case get one key, so a lookup can find the other folder's entry. Standard library only: the fast read hook
imports this module."""
from __future__ import annotations
import os,sys,unicodedata

JUNK_NAMES=frozenset({'.DS_Store','.localized'})


def _caseless(text):
    """Unicode canonical caseless form (NFD, casefold, NFD): equal for the NFC and NFD spellings of one name in any case."""
    return unicodedata.normalize('NFD',unicodedata.normalize('NFD',text).casefold())


def fold(text):
    """Text in the form the disk compares names in: case-folded on Windows (normcase, then lower() as pathlib compares)
    and on macOS (canonical caseless form); unchanged elsewhere."""
    text=os.path.normcase(str(text))
    if sys.platform=='darwin':return _caseless(text)
    if os.name=='nt':return text.lower()
    return text


def same_name(a,b):
    """True when two file names differ only in case or Unicode spelling, so some disk would hold them as one file."""
    a,b=str(a),str(b)
    return a.casefold()==b.casefold() or _caseless(a)==_caseless(b)


def name_within(child,parent):
    """True when the /-separated name child is parent or lies under it, ignoring case and Unicode spelling."""
    child,parent=str(child)+'/',str(parent)+'/'
    return child.casefold().startswith(parent.casefold()) or _caseless(child).startswith(_caseless(parent))


def _real(path):
    try:return os.path.realpath(str(path))
    except (OSError,ValueError):return os.path.abspath(str(path))


def path_key(path,resolve=True):
    """The form two paths are compared in: absolute, normalised, links resolved (unless resolve is False) and folded."""
    return fold(os.path.normpath(_real(path) if resolve else os.path.abspath(str(path))))


def is_within(child,parent,resolve=True):
    """True when child is parent or lies inside it, compared as path_key does."""
    c,p=path_key(child,resolve),path_key(parent,resolve)
    return c==p or c.startswith(p if p.endswith(os.sep) else p+os.sep)


def same_path(a,b,resolve=True):return path_key(a,resolve)==path_key(b,resolve)


APPLEDOUBLE=b'\x00\x05\x16\x07'
DS_STORE=b'\x00\x00\x00\x01Bud1'


def is_os_junk(path):
    """A file macOS drops into folders (Finder's .DS_Store and .localized, ._ AppleDouble files on non-APFS volumes):
    never a note, page or content, and never a reason to call a folder non-empty. Recognised by name and content, so a
    page someone named `._draft.md` is kept."""
    name=os.path.basename(str(path))
    if name not in JUNK_NAMES and not name.startswith('._'):return False
    try:
        with open(path,'rb') as f:head=f.read(len(DS_STORE))
    except OSError:return False
    if name=='.localized':return head==b''
    if name=='.DS_Store':return head==DS_STORE
    return head.startswith(APPLEDOUBLE)


def _is_link(path):
    return os.path.islink(path) or bool(getattr(os.path,'isjunction',lambda p:False)(path))


def remove_empty_dir(path):
    """Remove a folder that holds nothing, or only macOS junk files (deleted first). False when it holds anything else,
    is a link or junction, or cannot be removed."""
    try:
        if _is_link(path):return False
        names=os.listdir(path)
        if not all(is_os_junk(os.path.join(path,n)) for n in names):return False
        for n in names:os.remove(os.path.join(path,n))
        os.rmdir(path);return True
    except OSError:return False
