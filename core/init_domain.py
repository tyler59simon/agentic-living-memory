"""Register project bindings without replacing unreadable configuration."""
import argparse,json
from pathlib import Path
from .common import memory_root,safe_domain
from .repository import read_json,store_lock,canonical_json,transaction,reconcile

def init_domain(name,prefixes,root=None):
    root=Path(root or memory_root()).resolve();name=safe_domain(name)
    with store_lock(root):
        settings=read_json(root/'bindings.json',{'version':2,'domains':{},'search_scope':{'global_domains':['_global'],'foreign_domains':[]}})
        if not isinstance(settings,dict) or not isinstance(settings.get('domains',{}),dict):raise ValueError('Invalid bindings preserved')
        domains=settings.setdefault('domains',{});existing=domains.get(name,{})
        selected=sorted(set(existing.get('prefixes',[])+[str(Path(p).expanduser().resolve()) for p in prefixes]))
        for other,entry in domains.items():
            if other!=name and set(selected)&set(entry.get('prefixes',[])):raise ValueError('Prefix is already registered to another domain')
        domains[name]={**existing,'prefixes':selected};settings['version']=2
        scope=settings.setdefault('search_scope',{});scope.setdefault('global_domains',['_global']);scope.setdefault('foreign_domains',[])
        transaction(root,{'bindings.json':canonical_json(settings)},'bind_domain')
        directory=root/'domains'/name;(directory/'notes').mkdir(parents=True,exist_ok=True);(directory/'archive').mkdir(exist_ok=True)
        reconcile(name,root);return directory

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('name');p.add_argument('prefix',nargs='+');p.add_argument('--root',type=Path,default=memory_root());a=p.parse_args(argv);print(init_domain(a.name,a.prefix,a.root));return 0
if __name__=='__main__':raise SystemExit(main())
