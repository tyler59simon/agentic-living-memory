"""Controlled taxonomy changes are direct, journaled and reversible (undo OPERATION)."""
import difflib
from pathlib import Path
from .common import memory_root
from .repository import read_json,canonical_json,FACETS,store_lock
from .governance import commit

def load_tags(root):
    """Registered tags as a list (supports dict-with-tags, list, or dict-key forms)."""
    raw=read_json(Path(root)/'tag_taxonomy.json',{'tags':[]})
    tags=raw.get('tags',[]) if isinstance(raw,dict) else raw
    return list(tags) if isinstance(tags,(list,dict,set,tuple)) else []

def add_tags(tags,root=None,replace=False,allow_managed=False):
    root=Path(root or memory_root()).resolve()
    # Read and write under one lock, so the registry is written from the file it was read from.
    with store_lock(root):return _add_tags(tags,root,replace,allow_managed)

def content(tags,root,replace=False,allow_managed=False):
    """(taxonomy file text, vocabulary before, tags added) for adding tags; project/* and system/* only with allow_managed."""
    known=load_tags(root)
    invalid=[t for t in tags if '/' not in t or t.split('/',1)[0] not in FACETS or not t.split('/',1)[1] or any(c.isspace() for c in t)]
    if not allow_managed and any(t.startswith('project/') for t in tags):raise ValueError('Create projects with project create; project/* tags are not added by taxonomy add')
    if not allow_managed and any(t.startswith('system/') for t in tags):raise ValueError('Create systems with system create; system/* tags are not added by taxonomy add')
    if invalid:raise ValueError('Tags require a supported facet and nonempty value: '+', '.join(invalid))
    selected=sorted(set(tags if replace else list(known)+tags))
    if replace and set(known)-set(selected):raise ValueError('Vocabulary removal requires a retag migration first')
    return canonical_json({'version':2,'facets':list(FACETS),'tags':selected}),known,sorted(set(tags)-set(known))

def _add_tags(tags,root,replace,allow_managed=False):
    text,known,added=content(tags,root,replace,allow_managed)
    alternatives={t:difflib.get_close_matches(t,known,n=3,cutoff=0.6) for t in tags if t not in known}
    return commit(root,{'tag_taxonomy.json':text},'taxonomy_expansion',summary={'added':added,'alternatives':alternatives})
