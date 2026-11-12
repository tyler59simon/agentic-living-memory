"""System registry (systems.json): the systems an engineer works on, each with owners, repo folders, dependencies on
other systems, an Architecture Suite output folder, a passport note and a system/<slug> tag.

Systems are created and changed only through system-plan (hash-approved, journaled, undoable)."""
from __future__ import annotations
import uuid
from pathlib import Path
from .common import canonical_domain
from .projects import SLUG,_charter_domain,_repos,_text,infer_tags
from .repository import read_json,canonical_json,find_secrets,store_lock,now

STATUSES=('active','deprecated','retired')
SHARED={'slug','name','description','owners','capabilities','repos','depends_on','architecture','passport'}
FIELDS={'create':SHARED,'update':SHARED|{'status'},'retire':{'slug'}}

def tag(slug):return f'system/{slug}'
def _path(root):return Path(root)/'systems.json'

def load(root):
    """The registry as {'version':1,'systems':[...]}; a missing file is an empty registry."""
    data=read_json(_path(root),{'version':1,'systems':[]})
    if not isinstance(data,dict) or not isinstance(data.get('systems'),list) or any(not isinstance(s,dict) or not isinstance(s.get('slug'),str) for s in data['systems']):
        raise ValueError(f'Invalid or unreadable configuration preserved: {_path(root)}')
    return data

def infer(item,domain,root,touched=(),cwd=None):
    """(system tags to add, candidate slugs) for a capture, by repo folder; a broken registry infers nothing. See projects.infer_tags."""
    try:registry=load(root)['systems']
    except ValueError:return [],[]
    return infer_tags(item,domain,root,registry,'system',touched,cwd)

def _strings(value,field,most,high):
    if not isinstance(value,list) or len(value)>most:raise ValueError(f'{field} must be a list of at most {most} strings')
    return list(dict.fromkeys(_text(v,field,1,high) for v in value))

def _clean(values,base,others):
    """Validated registry fields from a payload's values, layered over base (an entry); others are the other systems' slugs."""
    entry=dict(base)
    if 'name' in values:entry['name']=_text(values['name'],'name',1,80)
    if 'description' in values:entry['description']=_text(values['description'] or '','description',0,500)
    if 'status' in values:
        if values['status'] not in STATUSES:raise ValueError('status must be one of '+', '.join(STATUSES))
        entry['status']=values['status']
    if 'owners' in values:entry['owners']=_strings(values['owners'] or [],'owners',10,80)
    if 'capabilities' in values:entry['capabilities']=_strings(values['capabilities'] or [],'capabilities',20,120)
    if 'repos' in values:entry['repos']=_repos(values['repos'] or [])
    if 'depends_on' in values:
        wanted=values['depends_on'] or []
        if not isinstance(wanted,list) or any(not isinstance(s,str) for s in wanted):raise ValueError('depends_on must be a list of system slugs')
        if entry['slug'] in wanted:raise ValueError('A system cannot depend on itself')
        unknown=sorted(set(wanted)-set(others))
        if unknown:raise ValueError('depends_on names unknown systems: '+', '.join(unknown)+'; create them with system-plan first')
        entry['depends_on']=list(dict.fromkeys(wanted))
    if 'architecture' in values:
        folder=values['architecture']
        if folder is not None and (not isinstance(folder,str) or not Path(folder).is_absolute()):raise ValueError('architecture must be an absolute folder path or null')
        entry['architecture']=_repos([folder])[0] if folder else None
    if 'passport' in values:
        if values['passport'] is not None and not isinstance(values['passport'],str):raise ValueError('passport must be a note reference or null')
        entry['passport']=values['passport']
    found=find_secrets('\n'.join([entry.get('name',''),entry.get('description','')]+list(entry.get('owners',[]))+list(entry.get('capabilities',[]))))
    if found:raise ValueError(f'Potential credential detected ({found[0][0]}) in the system fields; remove it')
    return entry

def _registry(entries):return canonical_json({'version':1,'systems':sorted(entries,key=lambda s:s['slug'])})

def system_plan(root,action,payload):
    """A reviewed plan creating, updating or retiring one system (kinds system_create, system_update, system_retire)."""
    from .governance import plan
    root=Path(root).resolve()
    if action not in FIELDS:raise ValueError('system-plan action must be create, update or retire')
    if not isinstance(payload,dict):raise ValueError('Payload must be a JSON object')
    extra=set(payload)-FIELDS[action]
    if extra:raise ValueError('Unknown payload fields: '+', '.join(sorted(extra)))
    slug=payload.get('slug')
    if not isinstance(slug,str) or not SLUG.fullmatch(slug):raise ValueError('slug must match [a-z0-9][a-z0-9-]{0,39}')
    with store_lock(root):
        entries=load(root)['systems'];current=next((s for s in entries if s['slug']==slug),None);others=[s for s in entries if s['slug']!=slug];stamp=now()
        slugs=[s['slug'] for s in others]
        if action=='create':
            if current:raise ValueError(f'System {slug} already exists')
            if 'name' not in payload:raise ValueError('create requires name')
            passport=payload.get('passport')
            if not isinstance(passport,dict) or not isinstance(passport.get('body'),str) or not passport['body'].strip():raise ValueError('create requires passport.body (starting with a summary)')
            if set(passport)-{'domain','title','body'}:raise ValueError('passport accepts domain, title and body')
            entry=_clean({k:v for k,v in payload.items() if k!='passport'},{'slug':slug,'status':'active','description':'','owners':[],'capabilities':[],'repos':[],'depends_on':[],'architecture':None,'created':stamp,'updated':stamp},slugs)
            from .notes import build_note
            from .tag_taxonomy import content
            known=read_json(root/'tag_taxonomy.json',{'tags':[]});known=known.get('tags',[]) if isinstance(known,dict) else known
            domain=_charter_domain(root,passport.get('domain'),entry['repos'],'passport.domain')
            note_id=str(uuid.uuid4());title=passport.get('title') or f"{entry['name']} passport"
            if not isinstance(title,str):raise ValueError('passport.title must be a string')
            changes={};added=tag(slug) not in known
            if added:changes['tag_taxonomy.json']=content([tag(slug)],root,allow_managed=True)[0]
            path,note,text=build_note(title,passport['body'],['subject/project','concern/architecture',tag(slug)],domain,root,'context',note_id=note_id,extra_tags=[tag(slug)])
            entry['passport']=f'{domain}:{note_id}'
            changes['systems.json']=_registry(others+[entry]);changes[path.relative_to(root).as_posix()]=text
            return plan(root,changes,'system_create',[domain],{'slug':slug,'tag':tag(slug),'added_tag':added,'passport':entry['passport'],'files':len(changes)})
        if not current:raise ValueError(f'Unknown system {slug}')
        values={'status':'retired'} if action=='retire' else {k:v for k,v in payload.items() if k!='slug'}
        entry=_clean(values,current,slugs);entry['updated']=stamp
        return plan(root,{'systems.json':_registry(others+[entry])},'system_'+action,[],{'slug':slug,'fields':sorted(values),'files':1})

def listing(root,status='any'):
    """Registry entries (optionally one status) with tag, tag_registered and dependents, and warnings for what no longer resolves."""
    from .notes import find_note,taxonomy
    root=Path(root);known=taxonomy(root);rows=[];warnings=[];entries=load(root)['systems'];slugs={s['slug'] for s in entries}
    for s in entries:
        if status!='any' and s.get('status')!=status:continue
        who=s['slug'];dependents=sorted(o['slug'] for o in entries if who in o.get('depends_on',[]))
        row=dict(s,tag=tag(who),tag_registered=tag(who) in known,dependents=dependents);rows.append(row)
        if not row['tag_registered']:warnings.append(f"{who}: tag {row['tag']} is not in the taxonomy")
        warnings+=[f'{who}: repo folder does not exist: {r}' for r in s.get('repos',[]) if not Path(r).is_dir()]
        if s.get('architecture') and not Path(s['architecture']).is_dir():warnings.append(f"{who}: architecture folder does not exist: {s['architecture']}")
        warnings+=[f'{who}: depends on unknown system {d}' for d in s.get('depends_on',[]) if d not in slugs]
        ref=s.get('passport')
        if ref:
            try:domain,note_id=ref.split(':',1);find_note(note_id,canonical_domain(domain,root),root)
            except (ValueError,AttributeError):warnings.append(f'{who}: passport {ref} does not resolve to a note')
    return {'ok':True,'systems':rows,'warnings':warnings}
