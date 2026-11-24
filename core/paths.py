"""Path comparison that matches how the disk names files (MACOS-1).

Windows (NTFS) and macOS (APFS, HFS+) ignore letter case by default, and macOS also treats the NFC and NFD Unicode spellings
of a name as one file. os.path.normcase folds case only on Windows, and Path.is_relative_to never folds, so guards and
lookups that compare paths must go through fold, path_key or is_within. Folding on a case-sensitive APFS volume only
over-matches, which errs toward refusing in a guard. Standard library only: the fast read hook imports this module."""
from __future__ import annotations
import os,sys,unicodedata

JUNK_NAMES=frozenset({'.DS_Store','.localized'})


def fold(text):
    """Text in the form the disk compares names in: case-folded on Windows (normcase) and macOS (NFC, then casefold)."""
    text=os.path.normcase(str(text))
    if sys.platform=='darwin':text=unicodedata.normalize('NFC',text).casefold()
    return text


def name_key(name):
    """A file name with case and Unicode spelling folded on every system: for refusing names that would be one file on
    some disk, and as a cheap test before os.path.samefile."""
    return unicodedata.normalize('NFC',str(name)).casefold()


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


def is_os_junk(name):
    """Files macOS drops into folders (Finder's .DS_Store and .localized, ._ AppleDouble files on non-APFS volumes):
    never notes, pages or content, and never a reason to call a folder non-empty."""
    name=os.path.basename(str(name))
    return name in JUNK_NAMES or name.startswith('._')
