"""Project registry (projects.json): named work contexts with repo folders, a charter note and a project/<slug> tag.

Projects are created and changed only through project-plan (hash-approved, journaled, undoable)."""
from __future__ import annotations
import os,re,uuid
from pathlib import Path
from .common import canonical_domain,domain_prefixes,global_domain,load_bindings,resolve_domain
from .repository import read_json,canonical_json,find_secrets,store_lock,now

STATUSES=('active','paused','archived')
SLUG=re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
FIELDS={'create':{'slug','name','repos','description','aliases','charter'},'update':{'slug','name','repos','aliases','description','status','charter'},'archive':{'slug'}}

def tag(slug):return f'project/{slug}'
def _path(root):return Path(root)/'projects.json'

def load(root):
    """The registry as {'version':1,'projects':[...]}; a missing file is an empty registry."""
    data=read_json(_path(root),{'version':1,'projects':[]})
    if not isinstance(data,dict) or not isinstance(data.get('projects'),list) or any(not isinstance(p,dict) or not isinstance(p.get('slug'),str) for p in data['projects']):
        raise ValueError(f'Invalid or unreadable configuration preserved: {_path(root)}')
    return data

def _norm(path):return Path(os.path.normcase(os.path.normpath(str(path).replace('\\','/'))))

def match_path(path,projects):
    """The active project whose repo folder contains path (longest repo prefix wins), else None. Pure: no filesystem access."""
    target=_norm(path);best=None;size=-1
    if not target.is_absolute():return None
    for project in projects:
        if project.get('status')!='active':continue
        for repo in project.get('repos',[]):
            prefix=_norm(repo)
            if target.is_relative_to(prefix) and len(prefix.parts)>size:best,size=project,len(prefix.parts)
    return best

MAX_AUTO=2

def resolve_file(value,domain,root):
    """Absolute candidates for a file a note names: itself when absolute, else joined to each prefix of the domain (notes store files relative to the bound folder)."""
    if Path(str(value).replace(chr(92),'/')).is_absolute():return [str(value)]
    return [str(prefix/value) for prefix in domain_prefixes(domain,load_bindings(root))]

def infer(item,domain,root,touched=(),cwd=None):
    """(project tags to add, candidate slugs) for a capture; a broken registry infers nothing. See infer_tags."""
    try:registry=load(root)['projects']
    except ValueError:return [],[]
    return infer_tags(item,domain,root,registry,'project',touched,cwd)

def infer_tags(item,domain,root,entries,facet,touched=(),cwd=None):
    """(facet tags to add, candidate slugs) for a capture, from registry entries (projects or systems). The first level that
    matches wins: the note's files, then this turn's touched files, then cwd (callers pass touched and cwd only for a session
    capture). More than MAX_AUTO matches add nothing and return the slugs as candidates. Only active entries with a registered tag count."""
    if any(str(t).startswith(facet+'/') for t in item.get('tags') or []):return [],[]
    from .notes import taxonomy
    known=taxonomy(root);eligible=[p for p in entries if p.get('status')=='active' and f"{facet}/{p['slug']}" in known]
    if not eligible:return [],[]
    for level in ([f for v in item.get('files') or [] for f in resolve_file(v,domain,root)],[f for v in touched for f in resolve_file(v,domain,root)],[str(cwd)] if cwd else []):
        found=sorted({m['slug'] for m in (match_path(f,eligible) for f in level) if m})
        if found:return ([f'{facet}/{s}' for s in found],[]) if len(found)<=MAX_AUTO else ([],found)
    return [],[]

def _text(value,field,low,high):
    if not isinstance(value,str) or not low<=len(value.strip())<=high:raise ValueError(f'{field} must be {low}..{high} characters')
    return value.strip()

def _repos(repos):
    """Validated repo folders: at most 10 absolute paths, forward slashes, no trailing slash, no duplicates."""
    if not isinstance(repos,list) or len(repos)>10 or any(not isinstance(r,str) or not r.strip() for r in repos):raise ValueError('repos must be a list of at most 10 absolute folder paths')
    if any(not Path(r).is_absolute() for r in repos):raise ValueError('repos must be absolute paths')
    fixed=[r.replace('\\','/') for r in repos];fixed=[r.rstrip('/') if len(r.rstrip('/'))>2 else r for r in fixed]
    return list(dict.fromkeys(fixed))

def _clean(values,base):
    """Validated registry fields from a payload's values, layered over base (an entry)."""
    entry=dict(base)
    if 'name' in values:entry['name']=_text(values['name'],'name',1,80)
    if 'description' in values:entry['description']=_text(values['description'] or '','description',0,500)
    if 'status' in values:
        if values['status'] not in STATUSES:raise ValueError('status must be one of '+', '.join(STATUSES))
        entry['status']=values['status']
    if 'repos' in values:entry['repos']=_repos(values['repos'])
    if 'aliases' in values:
        aliases=values['aliases'] or []
        if not isinstance(aliases,list) or len(aliases)>5:raise ValueError('aliases must be a list of at most 5 strings')
        entry['aliases']=list(dict.fromkeys(_text(a,'alias',1,40) for a in aliases))
    if 'charter' in values:
        if values['charter'] is not None and not isinstance(values['charter'],str):raise ValueError('charter must be a note reference or null')
        entry['charter']=values['charter']
    found=find_secrets('\n'.join([entry.get('name',''),entry.get('description','')]+list(entry.get('aliases',[]))))
    if found:raise ValueError(f'Potential credential detected ({found[0][0]}) in the project fields; remove it')
    return entry

def _registry(entries):return canonical_json({'version':1,'projects':sorted(entries,key=lambda p:p['slug'])})

def project_plan(root,action,payload):
    """A reviewed plan creating, updating or archiving one project (kinds project_create, project_update, project_archive)."""
    from .governance import plan
    root=Path(root).resolve()
    if action not in FIELDS:raise ValueError('project-plan action must be create, update or archive')
    if not isinstance(payload,dict):raise ValueError('Payload must be a JSON object')
    extra=set(payload)-FIELDS[action]
    if extra:raise ValueError('Unknown payload fields: '+', '.join(sorted(extra)))
    slug=payload.get('slug')
    if not isinstance(slug,str) or not SLUG.fullmatch(slug):raise ValueError('slug must match [a-z0-9][a-z0-9-]{0,39}')
    with store_lock(root):
        entries=load(root)['projects'];current=next((p for p in entries if p['slug']==slug),None);others=[p for p in entries if p['slug']!=slug];stamp=now()
        if action=='create':
            if current:raise ValueError(f'Project {slug} already exists')
            for key in ('name','repos'):
                if key not in payload:raise ValueError(f'create requires {key}')
            charter=payload.get('charter')
            if not isinstance(charter,dict) or not isinstance(charter.get('body'),str) or not charter['body'].strip():raise ValueError('create requires charter.body (starting with a summary)')
            if set(charter)-{'domain','title','body'}:raise ValueError('charter accepts domain, title and body')
            entry=_clean({k:v for k,v in payload.items() if k!='charter'},{'slug':slug,'status':'active','repos':[],'aliases':[],'description':'','created':stamp,'updated':stamp})
            from .notes import build_note
            from .tag_taxonomy import content
            known=read_json(root/'tag_taxonomy.json',{'tags':[]});known=known.get('tags',[]) if isinstance(known,dict) else known
            domain=_charter_domain(root,charter.get('domain'),entry['repos'])
            note_id=str(uuid.uuid4());title=charter.get('title') or f"{entry['name']} charter"
            if not isinstance(title,str):raise ValueError('charter.title must be a string')
            changes={};added=tag(slug) not in known
            if added:changes['tag_taxonomy.json']=content([tag(slug)],root,allow_managed=True)[0]
            path,note,text=build_note(title,charter['body'],['subject/project','concern/knowledge',tag(slug)],domain,root,'context',note_id=note_id,extra_tags=[tag(slug)])
            entry['charter']=f'{domain}:{note_id}'
            changes['projects.json']=_registry(others+[entry]);changes[path.relative_to(root).as_posix()]=text
            return plan(root,changes,'project_create',[domain],{'slug':slug,'tag':tag(slug),'added_tag':added,'charter':entry['charter'],'files':len(changes)})
        if not current:raise ValueError(f'Unknown project {slug}')
        values={'status':'archived'} if action=='archive' else {k:v for k,v in payload.items() if k!='slug'}
        entry=_clean(values,current);entry['updated']=stamp
        return plan(root,{'projects.json':_registry(others+[entry])},'project_'+action,[],{'slug':slug,'fields':sorted(values),'files':1})

def _charter_domain(root,named,repos,field='charter.domain'):
    """The charter's domain: the one named (must exist), else the repo's bound domain, else the global domain."""
    if named:
        name=canonical_domain(named,root)
        if name not in load_bindings(root).get('domains',{}) and not (Path(root)/'domains'/name).is_dir() and name!=global_domain(root):raise ValueError(f'{field} {named} is not a bound domain')
        return name
    found=resolve_domain(repos[0],load_bindings(root)) if repos else None
    return canonical_domain(found,root) if found else global_domain(root)

def listing(root,status='any'):
    """Registry entries (optionally one status) with tag, tag_registered, and warnings for what no longer resolves."""
    from .notes import find_note,taxonomy
    root=Path(root);known=taxonomy(root);rows=[];warnings=[]
    for p in load(root)['projects']:
        if status!='any' and p.get('status')!=status:continue
        row=dict(p,tag=tag(p['slug']),tag_registered=tag(p['slug']) in known);rows.append(row);who=p['slug']
        if not row['tag_registered']:warnings.append(f"{who}: tag {row['tag']} is not in the taxonomy")
        warnings+=[f'{who}: repo folder does not exist: {r}' for r in p.get('repos',[]) if not Path(r).is_dir()]
        ref=p.get('charter')
        if ref:
            try:domain,note_id=ref.split(':',1);find_note(note_id,canonical_domain(domain,root),root)
            except (ValueError,AttributeError):warnings.append(f'{who}: charter {ref} does not resolve to a note')
    return {'ok':True,'projects':rows,'warnings':warnings}
