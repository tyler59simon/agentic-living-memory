"""Folder notebooks (NS-2B, design sections 8.2 and 12b): which folders outside the store are notebooks, and how far agents reach
into them.

The registry notebook-folders.json (store root; `doc folder set|unset`, journaled and undoable) lists the folder notebooks
Hivemind knows: canonical path, domain, visibility (hidden, searchable or injectable) and private. A folder's own sidecar,
`.hivemind/notebook.json`, is a second opinion. The effective policy is the stricter of the two (private, then hidden, then
searchable, then injectable); a sidecar that cannot be parsed counts as hidden; a folder inside a notebook inherits the strictest
policy above it. sources.walk applies it when indexing and sources.search again for every hit it serves, so a notebook made
hidden or private disappears before the next sync. A damaged registry serves nothing from any source until it is repaired."""
from __future__ import annotations
import hashlib,json,os
from pathlib import Path

REGISTRY='notebook-folders.json'
SIDECAR_DIR='.hivemind'
LEVELS=('hidden','searchable','injectable')
STRICT={None:-1,'injectable':0,'searchable':1,'hidden':2,'private':3}
SERVED=('searchable','injectable')
MIRROR='Notebooks'   # notebook_sync.MIRROR: <vault>/Notebooks
_VAULTS={}   # store root -> (sync-state stamps, vault folders)
_SIDECARS={}   # sidecar path -> ((mtime_ns, size), (level, title)): re-read only when the file changes


def key(path):
    """The form two paths are compared in: absolute, normalised and, on Windows, case-folded."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def stricter(a,b):return a if STRICT[a]>=STRICT[b] else b


def level_of(visibility,private):
    return 'private' if private else visibility if visibility in LEVELS else 'hidden'


def load(root):
    """The registry as {'version': 1, 'folders': [...]}; a missing file is empty, a damaged one raises ValueError."""
    path=Path(root)/REGISTRY
    try:
        with open(path,'rb') as f:data=json.loads(f.read().decode('utf-8-sig'))
    except FileNotFoundError:return {'version':1,'folders':[]}
    except (OSError,UnicodeDecodeError,ValueError):raise ValueError(f'{REGISTRY} is damaged; run doctor') from None
    if not isinstance(data,dict) or not isinstance(data.get('folders'),list) or not all(isinstance(e,dict) and isinstance(e.get('path'),str) for e in data['folders']):
        raise ValueError(f'{REGISTRY} is damaged; run doctor')
    return data


def managed_vaults(root):
    """Folders of the Obsidian vaults the store manages: a domain's sync-state.json names one that has .living-memory/vault.json.
    Cached until a sync-state.json changes."""
    states=sorted(Path(root).glob('domains/*/sync-state.json'))
    stamps=[]
    for p in states:
        try:stamps.append((str(p),os.stat(p).st_mtime_ns))
        except OSError:pass
    held=_VAULTS.get(str(root))
    if held and held[0]==stamps:return held[1]
    found=set()
    for p,_ in stamps:
        try:
            with open(p,'rb') as f:vault=json.loads(f.read().decode('utf-8-sig')).get('vault')
        except (OSError,UnicodeDecodeError,ValueError,AttributeError):continue
        if isinstance(vault,str) and os.path.isfile(os.path.join(vault,'.living-memory','vault.json')):found.add(os.path.normpath(vault))
    _VAULTS[str(root)]=(stamps,sorted(found))
    return sorted(found)


def sidecar(folder):
    """(level, title) from a folder's .hivemind/notebook.json; None when it has none. One that cannot be parsed is hidden."""
    path=os.path.join(str(folder),SIDECAR_DIR,'notebook.json')
    try:info=os.stat(path)
    except OSError:return None
    stamp=(info.st_mtime_ns,info.st_size);held=_SIDECARS.get(path)
    if held and held[0]==stamp:return held[1]
    try:
        with open(path,'rb') as f:data=json.loads(f.read(256*1024).decode('utf-8-sig'))
    except (OSError,UnicodeDecodeError,ValueError):data=None
    if not isinstance(data,dict):found=('hidden',None)
    else:
        title=data.get('title') if isinstance(data.get('title'),str) and data['title'].strip() else None
        found=(level_of(data.get('visibility','searchable'),bool(data.get('private'))),title[:200] if title else None)
    _SIDECARS[path]=(stamp,found)
    return found


class Policy:
    """The policy of folders and files as the registry and the sidecars say now. One instance per search or sync: it caches
    each folder it has judged."""
    def __init__(self,root):
        self.broken=False;self.entries={}
        try:data=load(root)
        except ValueError:self.broken=True;data={'folders':[]}
        for e in data['folders']:
            self.entries[key(e['path'])]=(level_of(e.get('visibility'),bool(e.get('private'))),e)
        for vault in managed_vaults(root):   # the notebook mirror: its pages are served from the store, never as a second copy
            self.entries.setdefault(key(os.path.join(vault,MIRROR)),('hidden',{'path':os.path.join(vault,MIRROR),'mirror':True}))
        self._folders={}

    def registered(self,path):
        """The registry's level for exactly this folder, or None."""
        held=self.entries.get(key(path));return held[0] if held else None

    def own(self,path,has_sidecar=None):
        """(level, title) of this folder as a notebook in its own right, or None. has_sidecar=False skips the file check
        (the caller's directory listing showed no .hivemind)."""
        found=sidecar(path) if has_sidecar is not False else None
        registered=self.registered(path)
        if found is None and registered is None:return None
        return stricter(found[0] if found else None,registered),(found[1] if found else None) or os.path.basename(os.path.normpath(str(path)))

    def folder(self,path):
        """(level, notebook folder, title) for a folder: the strictest level over it and every folder above it; the notebook is
        the nearest notebook folder at or above it. (None, None, None) outside every notebook."""
        k=key(path)
        if k in self._folders:return self._folders[k]
        parent=os.path.dirname(os.path.normpath(os.path.abspath(str(path))))
        above=self.folder(parent) if key(parent)!=k else (None,None,None)
        own=self.own(path)
        result=above if own is None else (stricter(above[0],own[0]),os.path.normpath(os.path.abspath(str(path))),own[1])
        self._folders[k]=result
        return result

    def file(self,path):return self.folder(os.path.dirname(os.path.normpath(os.path.abspath(str(path)))))

    def revision(self,top):
        """A hash of the registry entries at, under or above a source folder: a source's index is stale when it changes."""
        k=key(top);rows=sorted((p,lvl) for p,(lvl,_) in self.entries.items() if p==k or p.startswith(k+os.sep) or k.startswith(p+os.sep))
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]

    def registered_sources(self,domain):
        """Registry entries of a domain that agents may reach: (canonical path, level), not removed."""
        return sorted((e['path'],lvl) for _,(lvl,e) in self.entries.items() if e.get('domain')==domain and not e.get('removed') and lvl in SERVED)


# ---- the registry writes (`doc folder set|unset`) -------------------------------------------------------------------
def _text(entries):
    return json.dumps({'version':1,'folders':sorted(entries,key=lambda e:key(e['path']))},ensure_ascii=False,sort_keys=True,indent=2)+'\n'


def _canonical_folder(root,path):
    folder=Path(path).expanduser()
    if not folder.is_absolute():raise ValueError('The folder must be an absolute path')
    folder=folder.resolve()
    if not folder.is_dir():raise ValueError(f'not a folder: {folder}')
    store=Path(root).resolve()
    if folder==store or store in folder.parents:raise ValueError('A folder notebook cannot be inside the memory store; Living Memory notebooks live there')
    return folder.as_posix()


def set_folder(root,path,domain,visibility,private=None):
    """Add or change a folder notebook's entry. Unchanged: no write. private None keeps the entry's current value."""
    from .governance import commit
    from .repository import now,store_lock
    from .sources import _bound
    root=Path(root).resolve()
    if visibility not in LEVELS:raise ValueError('Visibility is hidden, searchable or injectable')
    with store_lock(root):
        canonical=_canonical_folder(root,path);named=_bound(root,domain)
        entries=load(root)['folders'];k=key(canonical)
        old=next((e for e in entries if key(e['path'])==k),None)
        entry={'path':canonical,'domain':named,'visibility':visibility,'private':bool(old and not old.get('removed') and old.get('private')) if private is None else bool(private)}
        if old and not old.get('removed') and {f:old.get(f) for f in ('path','domain','visibility','private')}==entry:
            return {'ok':True,'status':'unchanged','folder':entry}
        entry['updated']=now()
        receipt=commit(root,{REGISTRY:_text([e for e in entries if key(e['path'])!=k]+[entry])},'doc_folder',[],
                       {'folder':canonical,'domain':named,'visibility':visibility,'private':entry['private'],'files':1})
    _spawn(root,named)
    return dict(receipt,status='set',folder=entry)


def unset_folder(root,path,forget=False):
    """Remove a folder notebook's entry. A hidden or private one stays as a tombstone (removed: true), so the folder stays out
    of reach even where its sidecar could not be written, unless forget."""
    from .governance import commit
    from .repository import now,store_lock
    root=Path(root).resolve()
    with store_lock(root):
        if not Path(path).expanduser().is_absolute():raise ValueError('The folder must be an absolute path')
        entries=load(root)['folders'];k=key(Path(path).expanduser().resolve())
        old=next((e for e in entries if key(e['path'])==k),None)
        if old is None:raise ValueError(f'not_found: no folder notebook {path}')
        level=level_of(old.get('visibility'),bool(old.get('private')))
        keep=level in ('hidden','private') and not forget
        if keep and old.get('removed'):return {'ok':True,'status':'unchanged','folder':old}
        rest=[e for e in entries if key(e['path'])!=k]+([dict(old,removed=True,updated=now())] if keep else [])
        receipt=commit(root,{REGISTRY:_text(rest)},'doc_folder',[],{'folder':old['path'],'removed':True,'tombstone':keep,'files':1})
    _spawn(root,old.get('domain'))
    return dict(receipt,status='tombstone' if keep else 'removed',folder=old['path'])


def _spawn(root,domain):
    """A background source-sync, so the index follows the new policy soon (serving follows it at once)."""
    if not domain:return
    try:
        from .sources import spawn
        spawn(root,domain)
    except Exception:pass
