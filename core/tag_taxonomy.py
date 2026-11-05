"""Controlled taxonomy changes are hash-approved, reversible plans."""
import argparse,difflib,json
from pathlib import Path
from .common import memory_root
from .repository import read_json,canonical_json,FACETS
from .governance import plan

def plan_update(tags,root=None,replace=False):
    root=Path(root or memory_root()).resolve();old=read_json(root/'tag_taxonomy.json',{'version':2,'tags':[]})
    known=old.get('tags',[]) if isinstance(old,dict) else old
    if isinstance(known,dict):known=list(known)
    invalid=[t for t in tags if '/' not in t or t.split('/',1)[0] not in FACETS or not t.split('/',1)[1] or any(c.isspace() for c in t)]
    if invalid:raise ValueError('Tags require a supported facet and nonempty value: '+', '.join(invalid))
    selected=sorted(set(tags if replace else list(known)+tags))
    if replace and set(known)-set(selected):raise ValueError('Vocabulary removal requires a retag migration first')
    alternatives={t:difflib.get_close_matches(t,known,n=3,cutoff=0.6) for t in tags if t not in known}
    return plan(root,{'tag_taxonomy.json':canonical_json({'version':2,'facets':list(FACETS),'tags':selected})},'taxonomy_expansion',summary={'added':sorted(set(tags)-set(known)),'alternatives':alternatives})
def update(tags,root=None,replace=False):return plan_update(tags,root,replace)
def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('tags',nargs='+');p.add_argument('--root',type=Path,default=memory_root());a=p.parse_args(argv);print(json.dumps(plan_update(a.tags,a.root)));return 0
if __name__=='__main__':raise SystemExit(main())
