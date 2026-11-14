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

def unbind_domain(name,root=None):
    """Drop a domain's folder bindings (a journaled change to bindings.json). Refused for a global domain, an unknown name, and while notes or archived notes remain:
    move them first (remap-plan), so no note is orphaned. The domain's folders stay on disk."""
    from .common import global_domains
    from .repository import _note_files
    root=Path(root or memory_root()).resolve();name=safe_domain(name)
    with store_lock(root):
        settings=read_json(root/'bindings.json',{})
        if not isinstance(settings,dict) or not isinstance(settings.get('domains',{}),dict):raise ValueError('Invalid bindings preserved')
        domains=settings.get('domains',{})
        if name in global_domains(root):raise ValueError(f'{name} is a global domain; global domains are not bound to folders')
        if name not in domains:raise ValueError(f'{name} is not a bound domain')
        held=sum(len(_note_files(root,d)) for d in (root/'domains'/name/'notes',root/'domains'/name/'archive') if d.is_dir())
        if held:raise ValueError(f'{name} still holds {held} note(s) (notes and archive): move them with remap-plan first, then unbind')
        del domains[name]
        aliases=settings.get('domain_aliases')
        if isinstance(aliases,dict):settings['domain_aliases']={k:v for k,v in aliases.items() if v!=name}   # an alias must not name a domain that no longer resolves
        transaction(root,{'bindings.json':canonical_json(settings)},'unbind_domain')
        return name

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('name');p.add_argument('prefix',nargs='+');p.add_argument('--root',type=Path,default=memory_root());a=p.parse_args(argv);print(init_domain(a.name,a.prefix,a.root));return 0
if __name__=='__main__':raise SystemExit(main())
