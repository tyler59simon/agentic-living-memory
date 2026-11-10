"""Controlled taxonomy changes are hash-approved, reversible plans."""
import argparse,difflib,json
from pathlib import Path
from .common import memory_root
from .repository import read_json,canonical_json,FACETS,store_lock
from .governance import plan

def plan_update(tags,root=None,replace=False,allow_managed=False):
    root=Path(root or memory_root()).resolve()
    # Read and record under one lock, so the plan's recorded hash is of the file its contents came from.
    with store_lock(root):return _plan_update(tags,root,replace,allow_managed)

def content(tags,root,replace=False,allow_managed=False):
    """(taxonomy file text, vocabulary before, tags added) for adding tags; project/* and system/* only with allow_managed."""
    root=Path(root)
    old=read_json(root/'tag_taxonomy.json',{'version':2,'tags':[]})
    known=old.get('tags',[]) if isinstance(old,dict) else old
    if isinstance(known,dict):known=list(known)
    invalid=[t for t in tags if '/' not in t or t.split('/',1)[0] not in FACETS or not t.split('/',1)[1] or any(c.isspace() for c in t)]
    if not allow_managed and any(t.startswith('project/') for t in tags):raise ValueError('Create projects with project-plan; project/* tags are not added by taxonomy-plan')
    if not allow_managed and any(t.startswith('system/') for t in tags):raise ValueError('Create systems with system-plan; system/* tags are not added by taxonomy-plan')
    if invalid:raise ValueError('Tags require a supported facet and nonempty value: '+', '.join(invalid))
    selected=sorted(set(tags if replace else list(known)+tags))
    if replace and set(known)-set(selected):raise ValueError('Vocabulary removal requires a retag migration first')
    return canonical_json({'version':2,'facets':list(FACETS),'tags':selected}),known,sorted(set(tags)-set(known))

def _plan_update(tags,root,replace,allow_managed=False):
    text,known,added=content(tags,root,replace,allow_managed)
    alternatives={t:difflib.get_close_matches(t,known,n=3,cutoff=0.6) for t in tags if t not in known}
    return plan(root,{'tag_taxonomy.json':text},'taxonomy_expansion',summary={'added':added,'alternatives':alternatives})
def update(tags,root=None,replace=False):return plan_update(tags,root,replace)
def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('tags',nargs='+');p.add_argument('--root',type=Path,default=memory_root());a=p.parse_args(argv);print(json.dumps(plan_update(a.tags,a.root)));return 0
if __name__=='__main__':raise SystemExit(main())
