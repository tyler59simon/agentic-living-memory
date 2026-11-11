"""Change registry (changes.json): numbered units of work (CHG-014), each in one project, at one stage, with a brief
(a plan note) and optionally a repo folder, a branch, a system and an architecture element.

Changes are created and changed only through change-plan (hash-approved, journaled, undoable)."""
from __future__ import annotations
import re,uuid
from pathlib import Path
from . import projects,systems
from .projects import _charter_domain,_repos,_text
from .repository import read_json,canonical_json,digest,find_secrets,store_lock,now

STAGES=('explore','design','implement','validate')
STATUSES=('active','done','cancelled')
SHARED={'title','stage','repo','branch','system','element'}
FIELDS={'create':SHARED|{'project','brief'},'update':SHARED|{'id','status'},'close':{'id','status'}}
NUMBER=re.compile(r'CHG-0*([1-9]\d*)',re.I)
# The title becomes the brief's title, and other notes link to it as [[CHG-014 Title]]: these characters end a link target.
UNLINKABLE=re.compile(r'[\[\]|#]')

def display(number):return 'CHG-%03d'%number
def _path(root):return Path(root)/'changes.json'

def load(root):
    """The registry as {'version':1,'changes':[...]}; a missing file is an empty registry."""
    data=read_json(_path(root),{'version':1,'changes':[]});rows=data.get('changes') if isinstance(data,dict) else None
    if not isinstance(rows,list) or any(not isinstance(c,dict) or type(c.get('id')) is not int or c['id']<1 for c in rows) or len({c['id'] for c in rows})!=len(rows):
        raise ValueError(f'Invalid or unreadable configuration preserved: {_path(root)}')
    return data

def _number(value):
    """A change number from 14 or 'CHG-014'."""
    if isinstance(value,str):
        found=NUMBER.fullmatch(value.strip());value=int(found[1]) if found else None
    if type(value) is not int or value<1:raise ValueError('id must be a change number such as 14 or CHG-014')
    return value

def _brief(ref,root):
    """(domain, id, note) for a brief ref wherever the note lives now (a promoted or remapped note keeps its ID), else None."""
    from .notes import resolve_ref,find_note
    if not isinstance(ref,str) or ':' not in ref:return None
    try:domain,note_id=resolve_ref(ref,None,root);return domain,note_id,find_note(note_id,domain,root)[1]
    except (ValueError,OSError):return None

def _clean(values,base,root):
    """Validated registry fields from a payload's values, layered over base (an entry)."""
    entry=dict(base)
    if 'title' in values:
        entry['title']=_text(values['title'],'title',1,120)
        if UNLINKABLE.search(entry['title']):raise ValueError('title cannot contain [ ] | or #')
    if 'stage' in values:
        if values['stage'] not in STAGES:raise ValueError('stage must be one of '+', '.join(STAGES))
        entry['stage']=values['stage']
    if 'status' in values:
        if values['status'] not in STATUSES:raise ValueError('status must be one of '+', '.join(STATUSES))
        entry['status']=values['status']
    if 'repo' in values:
        repo=values['repo']
        if repo is not None and (not isinstance(repo,str) or not repo.strip() or not Path(repo).is_absolute()):raise ValueError('repo must be an absolute folder path or null')
        entry['repo']=_repos([repo])[0] if repo else None
    for field,high in (('branch',200),('element',120)):
        if field in values:entry[field]=None if values[field] is None else _text(values[field],field,1,high)
    if 'system' in values:
        slug=values['system']
        if slug is not None and not any(s['slug']==slug for s in systems.load(root)['systems']):raise ValueError(f'system names an unknown system: {slug}; create it with system-plan first')
        entry['system']=slug
    if entry.get('element') and not entry.get('system'):raise ValueError('element needs a system')
    found=find_secrets('\n'.join(str(entry.get(k) or '') for k in ('title','branch','element')))
    if found:raise ValueError(f'Potential credential detected ({found[0][0]}) in the change fields; remove it')
    return entry

def _registry(entries):return canonical_json({'version':1,'changes':sorted(entries,key=lambda c:c['id'])})

def change_plan(root,action,payload):
    """A reviewed plan creating, updating or closing one change (kinds change_create, change_update, change_close)."""
    from .governance import plan
    from .notes import build_note,_change_set
    root=Path(root).resolve()
    if action not in FIELDS:raise ValueError('change-plan action must be create, update or close')
    if not isinstance(payload,dict):raise ValueError('Payload must be a JSON object')
    extra=set(payload)-FIELDS[action]
    if extra:raise ValueError('Unknown payload fields: '+', '.join(sorted(extra)))
    with store_lock(root):
        entries=load(root)['changes'];stamp=now();files={};guards=None
        if action=='create':
            for key in ('project','title','brief'):
                if key not in payload:raise ValueError(f'create requires {key}')
            slug=payload['project'];project=next((p for p in projects.load(root)['projects'] if p['slug']==slug),None)
            if not project:raise ValueError(f'project names an unknown project: {slug}; create it with project-plan first')
            # Provisional until apply: a second plan made from the same registry gets the same number and goes stale.
            number=max((c['id'] for c in entries),default=0)+1
            entry=_clean({k:v for k,v in payload.items() if k not in ('project','brief')},{'id':number,'project':slug,'stage':'explore','status':'active','repo':None,'branch':None,'system':None,'element':None,'created':stamp,'updated':stamp},root)
            tags=[projects.tag(slug)]+([systems.tag(entry['system'])] if entry['system'] else []);brief=payload['brief']
            if isinstance(brief,str):
                # Adopt an existing plan note: it keeps its title and gains the tags a new brief would carry.
                found=_brief(brief,root)
                if not found:raise ValueError(f'brief {brief} does not resolve to a note')
                domain,note_id,note=found
                if note.get('type')!='plan' or note.get('status')!='active':raise ValueError('brief must be an active plan note')
                taken=next((c for c in entries if str(c.get('brief')).endswith(':'+note_id)),None)
                if taken:raise ValueError(f"That note is already the brief of {display(taken['id'])}")
                missing=set(tags)-set(note.get('tags') or [])
                if missing:files.update(_change_set(note_id,domain,'retag',root,tags=sorted(set(note.get('tags') or [])|missing))[0])
                else:
                    # Nothing to write to the note, so guard it: a note resolved, retyped or deleted before apply makes the plan stale.
                    from .notes import find_note
                    rel=find_note(note_id,domain,root)[0];guards={'read_set':{Path(rel).resolve().relative_to(root).as_posix():digest(Path(rel).read_bytes())}}
            else:
                if not isinstance(brief,dict) or not isinstance(brief.get('body'),str) or not brief['body'].strip():raise ValueError('create requires brief.body (with an Executive Summary), or brief as the reference of an existing plan note')
                if set(brief)-{'domain','title','body'}:raise ValueError('brief accepts domain, title and body')
                if not isinstance(brief.get('title') or '',str):raise ValueError('brief.title must be a string')
                if brief.get('title'):
                    _text(brief['title'],'brief.title',1,120)
                    if UNLINKABLE.search(brief['title']):raise ValueError('brief.title cannot contain [ ] | or #')
                domain=_charter_domain(root,brief.get('domain'),[entry['repo']] if entry['repo'] else project.get('repos') or [],'brief.domain')
                note_id=str(uuid.uuid4())
                path,note,text=build_note(f"{display(number)} {brief.get('title') or entry['title']}",brief['body'],['subject/project','concern/workflow']+tags,domain,root,'plan',note_id=note_id)
                files[path.relative_to(root).as_posix()]=text
            entry['brief']=f'{domain}:{note_id}';files['changes.json']=_registry(entries+[entry])
            return plan(root,files,'change_create',[domain],guards=guards,summary={'id':number,'display':display(number),'title':entry['title'],'fields':sorted(payload),'brief':entry['brief'],'adopted':isinstance(brief,str),'files':len(files)})
        number=_number(payload.get('id'));current=next((c for c in entries if c['id']==number),None)
        if not current:raise ValueError(f'Unknown change {display(number)}')
        if action=='close':
            values={'status':payload.get('status','done')}
            if values['status'] not in ('done','cancelled'):raise ValueError('close status must be done or cancelled')
            if current.get('status')!='active':raise ValueError(f"{display(number)} is already {current.get('status')}")
        else:
            values={k:v for k,v in payload.items() if k!='id'}
            if not values:raise ValueError('update requires at least one field to change')
        entry=_clean(values,current,root);entry['updated']=stamp;domains=[];moved=None
        live=entry['status']=='active'
        # Closing resolves the brief and reopening reopens it, in this same plan; a brief already there is left alone.
        found=_brief(entry.get('brief'),root) if live!=(current.get('status')=='active') else None
        if found and found[2].get('type')=='plan' and found[2].get('status')==('resolved' if live else 'active'):
            moved='reopen' if live else 'resolve';files.update(_change_set(found[1],found[0],moved,root)[0]);domains=[found[0]]
        files['changes.json']=_registry([c for c in entries if c['id']!=number]+[entry])
        return plan(root,files,'change_'+action,domains,{'id':number,'display':display(number),'title':entry['title'],'fields':sorted(values),'brief_action':moved,'files':len(files)})

def listing(root,status='any',project=None):
    """Registry entries (optionally one status and one project) with display, brief_status and actions, and warnings for what no longer resolves."""
    from .note_format import action_items
    root=Path(root);rows=[];warnings=[]
    known={p['slug'] for p in projects.load(root)['projects']};built={s['slug'] for s in systems.load(root)['systems']}
    for c in load(root)['changes']:
        if status!='any' and c.get('status')!=status:continue
        if project and c.get('project')!=project:continue
        who=display(c['id']);row=dict(c,display=who,brief_status=None,actions={'open':0,'done':0});rows.append(row)
        if c.get('project') not in known:warnings.append(f"{who}: unknown project {c.get('project')}")
        if c.get('system') and c['system'] not in built:warnings.append(f"{who}: unknown system {c['system']}")
        found=_brief(c.get('brief'),root)
        if not found:warnings.append(f"{who}: brief {c.get('brief')} does not resolve to a note");continue
        domain,note_id,note=found;items=action_items(note.get('body') or '')
        row.update(brief=f'{domain}:{note_id}',brief_status=note.get('status','active'),actions={'open':sum(1 for d,_ in items if not d),'done':sum(1 for d,_ in items if d)})
    return {'ok':True,'changes':rows,'warnings':warnings}
