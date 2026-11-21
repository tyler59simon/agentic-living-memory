"""Register project bindings without replacing unreadable configuration."""
import argparse,json
from pathlib import Path
from .common import memory_root,safe_domain
from .repository import read_json,store_lock,canonical_json,transaction,reconcile,roll_forward_pending,digest

def _read_bindings(root,default):
    """bindings.json and the digest of the bytes read, after finishing any interrupted change to it. Writing with
    expected={'bindings.json':digest} then cannot overwrite a change recovered or made since (BIND-STALE)."""
    roll_forward_pending(root)
    path=root/'bindings.json'
    try:data=path.read_bytes()
    except FileNotFoundError:data=None
    return read_json(path,default),digest(data)


def init_domain(name,prefixes,root=None):
    root=Path(root or memory_root()).resolve();name=safe_domain(name)
    with store_lock(root):
        settings,read=_read_bindings(root,{'version':2,'domains':{},'search_scope':{'global_domains':['_global'],'foreign_domains':[]}})
        if not isinstance(settings,dict) or not isinstance(settings.get('domains',{}),dict):raise ValueError('Invalid bindings preserved')
        domains=settings.setdefault('domains',{});existing=domains.get(name,{})
        legacy=existing.get('paths',[]) if 'prefixes' not in existing else [];legacy=[legacy] if isinstance(legacy,str) else list(legacy or [])   # `prefixes` shadows a legacy `paths` list, so carry its folders over once; later it stays shadowed (STO-4.1)
        selected=sorted(set(existing.get('prefixes',[])+legacy+[str(Path(p).expanduser().resolve()) for p in prefixes]))
        for other,entry in domains.items():
            if other!=name and set(selected)&set(entry.get('prefixes',[])):raise ValueError('Prefix is already registered to another domain')
        # Binding a folder to a notebook-only domain makes it a memory domain (NS-2A): the flag goes in the same transaction, and the
        # folders below are created after it, so running bind again finishes a conversion that stopped half way.
        domains[name]={**{k:v for k,v in existing.items() if k!='notebook_only'},'prefixes':selected};settings['version']=2
        scope=settings.setdefault('search_scope',{});scope.setdefault('global_domains',['_global']);scope.setdefault('foreign_domains',[])
        transaction(root,{'bindings.json':canonical_json(settings)},'bind_domain',expected={'bindings.json':read})
        directory=root/'domains'/name;(directory/'notes').mkdir(parents=True,exist_ok=True);(directory/'archive').mkdir(exist_ok=True)
        reconcile(name,root);return directory

def unbind_domain(name,root=None):
    """Drop a domain's folder bindings (a journaled change to bindings.json). Refused for a global domain, an unknown name, and while notes or archived notes remain:
    move them first (remap), so no note is orphaned. The domain's folders stay on disk."""
    from .common import global_domains
    from .repository import _note_files
    root=Path(root or memory_root()).resolve();name=safe_domain(name)
    with store_lock(root):
        settings,read=_read_bindings(root,{})
        if not isinstance(settings,dict) or not isinstance(settings.get('domains',{}),dict):raise ValueError('Invalid bindings preserved')
        domains=settings.get('domains',{})
        if name in global_domains(root):raise ValueError(f'{name} is a global domain; global domains are not bound to folders')
        if name not in domains:raise ValueError(f'{name} is not a bound domain')
        held=sum(len(_note_files(root,d)) for d in (root/'domains'/name/'notes',root/'domains'/name/'archive') if d.is_dir())
        if held:raise ValueError(f'{name} still holds {held} note(s) (notes and archive): move them with remap first, then unbind')
        del domains[name]
        aliases=settings.get('domain_aliases')
        if isinstance(aliases,dict):settings['domain_aliases']={k:v for k,v in aliases.items() if v!=name}   # an alias must not name a domain that no longer resolves
        transaction(root,{'bindings.json':canonical_json(settings)},'unbind_domain',expected={'bindings.json':read})
        return name

def unbind_prefix(name,prefix,root=None):
    """Drop one folder from a domain's binding and keep the domain and its notes (BIND-RENAME: after a bound folder is renamed, `bind`
    adds the new folder and this removes the old one). Folders are matched as `resolve_domain` reads them: environment variables and ~
    expanded, case-insensitive on Windows, from `prefixes` (or legacy `paths`) and legacy `root` or `path`. Refused for an unknown
    domain, a folder the domain is not bound to, and the domain's last folder (use `unbind NAME` for that)."""
    import os
    from .common import _entries
    def norm(value):return os.path.normcase(str(Path(os.path.expandvars(str(value))).expanduser().resolve()))
    def folders(entry):return [norm(p) for _,_,prefixes in _entries({'domains':{name:entry}}) for p in prefixes]
    root=Path(root or memory_root()).resolve();name=safe_domain(name)
    with store_lock(root):
        settings,read=_read_bindings(root,{})
        if not isinstance(settings,dict) or not isinstance(settings.get('domains',{}),dict):raise ValueError('Invalid bindings preserved')
        entry=settings.get('domains',{}).get(name)
        if not isinstance(entry,dict):raise ValueError(f'{name} is not a bound domain')
        target=norm(prefix);shown=str(Path(os.path.expandvars(prefix)).expanduser().resolve());before=folders(entry)
        if target not in before:raise ValueError(f"{name} is not bound to {shown}; its folders are: {', '.join(before) or 'none'}")
        changed=dict(entry)
        for key in ('prefixes','paths'):
            if key in changed:
                values=[changed[key]] if isinstance(changed[key],str) else list(changed[key] or [])
                changed[key]=[v for v in values if norm(v)!=target]
        for key in ('root','path'):
            if changed.get(key) and norm(changed[key])==target:del changed[key]
        after=folders(changed)
        if not after:raise ValueError(f'{shown} is the last folder bound to {name}; bind the new folder first, or use unbind {name} to remove the whole binding')
        settings['domains'][name]=changed
        transaction(root,{'bindings.json':canonical_json(settings)},'unbind_prefix',expected={'bindings.json':read})
        return shown

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('name');p.add_argument('prefix',nargs='+');p.add_argument('--root',type=Path,default=memory_root());a=p.parse_args(argv);print(init_domain(a.name,a.prefix,a.root));return 0
if __name__=='__main__':raise SystemExit(main())
