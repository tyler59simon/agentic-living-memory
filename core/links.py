"""The [[Title]] link graph between notes, shared by `search --links` and the wiki."""
from __future__ import annotations
from .common import load_bindings
from .repository import inventory

def note_links(root,domains,status):
    """The link graph of these domains: [[Title]] links (resolved like a title REF: the note's own domain, then its
    parent domains, then the global domains; case-insensitive) and supersedes, with links that match no note."""
    from .sync import WIKILINK
    from .runtime import parent_domains
    from .common import global_domains
    config=load_bindings(root);globals_=global_domains(root);notes={};titles={}
    def notes_of(d):
        if d not in notes:notes[d]=inventory(d,root,retired=status!='active') if (root/'domains'/d/'notes').is_dir() else []
        return notes[d]
    def by_title(d):
        if d not in titles:
            # An active note wins a title over a retired one that shares it.
            titles[d]={n['title'].strip().casefold():n for n in sorted(notes_of(d),key=lambda n:n.get('status','active')=='active')}
        return titles[d]
    nodes={};edges={};unresolved={}
    def node(d,n):
        ref=f"{d}:{n['id']}"
        nodes.setdefault(ref,{'ref':ref,'id':n['id'],'domain':d,'title':n.get('title'),'type':n.get('type'),'status':n.get('status','active'),'tags':n.get('tags') or []})
        return ref
    for d in domains:
        order=list(dict.fromkeys([d]+parent_domains(d,config)+globals_))
        ids={n['id']:n for n in notes_of(d)}
        for n in notes_of(d):
            source=node(d,n)
            for target in WIKILINK.findall(n.get('body','')):
                key=target.strip().casefold()
                hit=next(((o,by_title(o)[key]) for o in order if key in by_title(o)),None)
                if hit is None:unresolved.setdefault((source,target.strip()),None);continue
                found=node(*hit)
                if found!=source:edges.setdefault((source,found),'link')
            # Newer note -> the note it replaced, from either side of the pair; a link between the two wins.
            pairs=[(n,ids.get(str(s).split(':')[-1])) for s in n.get('supersedes') or []]
            pairs.append((ids.get(str(n.get('superseded_by') or '').split(':')[-1]),n))
            for newer,older in pairs:
                if newer and older and newer['id']!=older['id']:edges.setdefault((node(d,newer),node(d,older)),'supersedes')
    return {'ok':True,'domains':domains,'nodes':sorted(nodes.values(),key=lambda r:r['ref']),
            'edges':[{'from':a,'to':b,'kind':k} for (a,b),k in sorted(edges.items())],
            'unresolved':[{'from':a,'target':t} for a,t in sorted(unresolved)]}
