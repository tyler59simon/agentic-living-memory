"""Separate reviewed plans for configuration upgrades and note-type normalization."""
from pathlib import Path
from .common import memory_root
from .repository import read_json,canonical_json,inventory
from .governance import plan

DEFAULT_TAGS=['subject/project','subject/user','concern/architecture','concern/decision','concern/correction','concern/workflow','concern/preference','concern/knowledge','platform/codex','platform/claude','platform/copilot','platform/factory','technology/python','technology/sqlite']

def migration_plan(root=None):
    root=Path(root or memory_root()).resolve();changes={};warnings=[]
    config=read_json(root/'bindings.json',{'version':2,'domains':{},'search_scope':{'global_domains':['_global'],'foreign_domains':[]}})
    if not isinstance(config,dict) or not isinstance(config.get('domains',{}),dict):raise ValueError('Invalid bindings preserved')
    config['version']=2;scope=config.setdefault('search_scope',{});scope.pop('min_relevance_score',None)
    scope.setdefault('global_domains',['_global']);scope.setdefault('foreign_domains',[])
    if (root/'domains/global').exists() and not (root/'domains/_global').exists():
        config.setdefault('domain_aliases',{})['_global']='global';warnings.append('Legacy global directory retained as the _global alias')
    changes['bindings.json']=canonical_json(config)
    if not (root/'tag_taxonomy.json').exists():changes['tag_taxonomy.json']=canonical_json({'version':2,'facets':['subject','concern','platform','technology'],'tags':DEFAULT_TAGS})
    else:
        taxonomy=read_json(root/'tag_taxonomy.json');tags=taxonomy.get('tags',[]) if isinstance(taxonomy,dict) else taxonomy
        if any('/' not in tag for tag in tags):warnings.append('Legacy tags retained; use a reviewed retag-plan to migrate existing values')
    domains=sorted(p.name for p in (root/'domains').iterdir() if p.is_dir()) if (root/'domains').exists() else []
    count=sum(len(inventory(d,root,retired=True)) for d in domains)
    changes['schema-version.json']=canonical_json({'version':2,'canonical_notes_rewritten':False})
    return plan(root,changes,'schema_migration',domains,{'notes_preserved':count,'warnings':warnings,'files':len(changes)})


def rewrite_type_links(body, referring_path, targets, moved_from=None):
    """Update ordinary local Markdown links only when their file is being renamed. With moved_from, the
    referring note itself moves from that path: every relative link is resolved from its old folder and
    rewritten relative to its new one."""
    import os,re
    from urllib.parse import unquote,quote,urlsplit,urlunsplit
    def rewrite(raw):
        parsed=re.fullmatch(r'''(?P<url><[^>]*>|.*?)(?P<title>\s+(?:"[^"]*"|'[^']*'))?''',raw)
        if not parsed:return raw
        raw_url=parsed.group('url');title=parsed.group('title') or ''
        angle=raw_url.startswith('<') and raw_url.endswith('>')
        url=raw_url[1:-1] if angle else raw_url
        parts=urlsplit(url)
        if parts.scheme or parts.netloc or not parts.path:return raw
        resolved=((moved_from or referring_path).parent/unquote(parts.path)).resolve()
        target=targets.get(resolved,resolved if moved_from is not None else None)
        if target is None:return raw
        relative=Path(os.path.relpath(target,referring_path.parent)).as_posix()
        if '%' in parts.path:relative=quote(relative,safe='/')
        updated=urlunsplit(('', '', relative, parts.query, parts.fragment))
        if angle:updated='<'+updated+'>'
        return updated+title
    body=re.sub(r'(?P<start>!?\[[^\]\n]*\]\()(?P<url>[^)\n]*)\)',
                lambda m:m.group('start')+rewrite(m.group('url'))+')',body)
    return re.sub(r'(?m)^(?P<start> {0,3}\[[^\]\r\n]+\]:[^\S\r\n]*)(?P<url>[^\r\n]+)(?=\r?$)',
                  lambda m:m.group('start')+rewrite(m.group('url')),body)


def note_type_plan(root=None):
    """Rename existing notes through a digest-approved, revision-checked plan."""
    from .repository import store_lock,serialize,validate,now
    from .note_types import normalize
    from .notes import slug
    root=Path(root or memory_root()).resolve()
    with store_lock(root):
        notes=[];targets={};occupied=set()
        for directory in sorted((root/'domains').iterdir()) if (root/'domains').exists() else []:
            if directory.is_dir():notes.extend(inventory(directory.name,root,retired=True))
        for note in notes:
            normalized=normalize(note);path=Path(note['path'])
            target=path.with_name(f"{normalized['prefix']} - {slug(note['title'])}-{note['id'][:8]}.md")
            key=str(target).casefold()
            if key in occupied or (target!=path and target.exists()):raise ValueError('Portable filename collision: '+str(target))
            occupied.add(key)
            if path!=target:targets[path.resolve()]=target
        changes={};updates=[];domains=set()
        for original in notes:
            note=normalize(original);path=Path(original['path']);target=targets.get(path.resolve(),path)
            note['path']=str(target)
            note['body']=rewrite_type_links(note['body'],path,targets)
            validate(note,note['domain'],root)
            text=serialize(note)
            if target==path and text==path.read_bytes().decode('utf-8'):continue
            note['updated']=now();old=path.relative_to(root).as_posix();new=target.relative_to(root).as_posix()
            if old!=new:changes[old]=None
            changes[new]=serialize(note);domains.add(note['domain'])
            updates.append({'id':note['id'],'domain':note['domain'],'type':note['type'],'status':note['status'],'before':old,'after':new})
        return plan(root,changes,'note_type_migration',domains,
                    {'notes':len(updates),'renamed':sum(n['before']!=n['after'] for n in updates),
                     'ids_preserved':True,'statuses_preserved':True,'updates':updates})
