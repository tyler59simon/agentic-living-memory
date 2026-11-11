"""Export bundles of one domain's notes, and their reviewed import (#13).

A bundle is a zip holding manifest.json plus notes/NAME.md and archive/NAME.md, the canonical note files as
they are. Import is an exact-hash plan, idempotent by note ID: a note already present unchanged is skipped,
one present with different content is reported as a conflict and left alone, and new notes keep their IDs.
"""
from __future__ import annotations
import hashlib,os,re,zipfile
from datetime import datetime,timezone
from pathlib import Path

SCHEMA=1
MEMBER=re.compile(r'(?:(notes|archive)/[^/\\]+\.md|learning/(?:tasks|attempts|evidence|candidates|procedures|versions|uses|feedback)/[a-f0-9.-]+\.json)')
MAX_MEMBER=1024*1024
MAX_NOTES=5000

def export(root,domain,out,types=(),tags=(),retired=False,include_learning=False):
    """Write a bundle of the domain's notes (active, or all with retired) that match every filter."""
    from .repository import inventory,canonical_json
    root=Path(root).resolve();out=Path(out).expanduser().resolve()
    if out.is_relative_to(root/'domains'):raise ValueError('Write the bundle outside the store domains')
    if out.exists():raise ValueError(f'{out} already exists; choose a new file')
    # Retired notes only on request; suspended learned procedures are still active notes, so they export too.
    notes=[n for n in inventory(domain,root,retired=True) if (retired or n.get('status','active')=='active') and (not types or n.get('type') in types) and all(t in n.get('tags',[]) for t in tags)]
    if not notes:raise ValueError('No notes match')
    entries=[];used=set()
    out.parent.mkdir(parents=True,exist_ok=True);temp=out.with_name('.'+out.name+'.tmp')
    try:
        with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as archive:
            for note in sorted(notes,key=lambda n:n['id']):
                path=Path(note['path']);folder='archive' if path.parent.name=='archive' else 'notes'
                data=path.read_bytes();name=f'{folder}/{path.name}'
                if note.get('learning_procedure'):
                    from .repository import serialize
                    portable=dict(note)
                    if not include_learning:
                        portable={k:v for k,v in portable.items() if not k.startswith('learning_')}
                    data=serialize(portable).encode('utf-8')
                archive.writestr(name,data);used|=set(note.get('tags',[]))
                entries.append({'id':note['id'],'path':name,'sha256':hashlib.sha256(data).hexdigest()})
            manifest={'schema':SCHEMA,'domain':domain,'exported':datetime.now(timezone.utc).isoformat(timespec='seconds'),'notes':entries,'tags':sorted(used)}
            if include_learning:
                from .learning import export_records
                members=export_records(root,domain,[n['learning_procedure'] for n in notes if n.get('learning_procedure')])
                manifest['schema']=2;manifest['learning']=[]
                for name,data in members.items():
                    archive.writestr(name,data)
                    manifest['learning'].append({'path':name,'sha256':hashlib.sha256(data).hexdigest()})
            archive.writestr('manifest.json',canonical_json(manifest))
        os.replace(temp,out)
    finally:
        if temp.exists():temp.unlink()
    return {'ok':True,'bundle':str(out),'domain':domain,'notes':len(entries),'tags':sorted(used)}

def _read(source):
    """(manifest, {member: bytes}) after checking every member against the manifest."""
    import json
    with zipfile.ZipFile(source) as archive:
        infos=archive.infolist()
        if len(infos)>MAX_NOTES+1:raise ValueError('Bundle has too many members')
        members={}
        for info in infos:
            name=info.filename
            if info.is_dir() or (name!='manifest.json' and not MEMBER.fullmatch(name)) or '..' in name.split('/'):
                raise ValueError(f'Bundle member outside the bundle layout: {name}')
            if info.file_size>MAX_MEMBER:raise ValueError(f'Bundle member too large: {name}')
            members[name]=archive.read(info)
    if 'manifest.json' not in members:raise ValueError('Bundle has no manifest.json')
    try:manifest=json.loads(members.pop('manifest.json').decode('utf-8'))
    except (UnicodeError,ValueError) as exc:raise ValueError('Bundle manifest is not valid JSON') from exc
    if not isinstance(manifest,dict) or manifest.get('schema') not in (SCHEMA,2) or not isinstance(manifest.get('notes'),list):raise ValueError('Unsupported bundle manifest')
    listed={e.get('path'):e for e in manifest['notes']+manifest.get('learning',[]) if isinstance(e,dict)}
    if set(listed)!=set(members):raise ValueError('Bundle members do not match its manifest')
    for name,data in members.items():
        if hashlib.sha256(data).hexdigest()!=listed[name].get('sha256'):raise ValueError(f'Bundle member changed after export: {name}')
    return manifest,members

def import_plan(root,source,domain=None):
    """A plan that adds the bundle's new notes to `domain` (default: the bundle's), plus what was skipped."""
    from .common import canonical_domain
    from .repository import parse_text,serialize,validate,inventory,read_json,canonical_json
    from .notes import taxonomy
    from .governance import plan
    root=Path(root).resolve();source=Path(source).expanduser()
    manifest,members=_read(source)
    target=canonical_domain(domain or str(manifest.get('domain') or ''),root)
    if target not in read_json(root/'bindings.json',{}).get('domains',{}):raise ValueError(f"Domain '{target}' is not bound here; bind it or pass --domain")
    import json
    try:imported_records={name:json.loads(data.decode('utf-8')) for name,data in members.items() if name.startswith('learning/')}
    except (UnicodeError,ValueError) as exc:raise ValueError('Bundle learning record is not valid JSON') from exc
    members={name:data for name,data in members.items() if not name.startswith('learning/')}
    parsed={};seen={};learning_candidates=[]
    for name,data in sorted(members.items()):
        try:note=parse_text(data.decode('utf-8'),name)
        except UnicodeError:note=None
        if not note or not isinstance(note.get('id'),str):raise ValueError(f'Bundle note does not parse: {name}')
        expected=next(e['id'] for e in manifest['notes'] if e.get('path')==name)
        if note['id']!=expected:raise ValueError(f'Bundle note ID does not match its manifest: {name}')
        if note['id'] in seen:raise ValueError(f'Bundle holds two notes with ID {note["id"]}: {seen[note["id"]]} and {name}')
        seen[note['id']]=name
        if note.get('learning_procedure') and imported_records:
            pid=note['learning_procedure']
            v=imported_records.get(f'learning/versions/{pid}.{note["learning_version"]}.json')
            if not v:raise ValueError('Bundle is missing the procedure version')
            from .learning import _candidate,_record,_safe,import_closure
            proposal=_record(target,_candidate(root,target,{'content':v['content'],'evidence_ids':[],
                              'step_evidence':[[] for _ in v['content']['steps']],
                              'tags':[t for t in v.get('tags',[]) if t in taxonomy(root)]}))
            proposal['imported_from']={'domain':manifest['domain'],'procedure_id':pid,'version':v['version']}
            # Inert provenance: this procedure's own evidence chain, never local verification.
            proposal['imported_records']=import_closure(imported_records,pid)
            proposal['import_key']=hashlib.sha256(source.read_bytes()+pid.encode()).hexdigest()
            _safe(proposal);learning_candidates.append(proposal)
        else:
            for key in list(note):
                if key.startswith('learning_'):note.pop(key)
            parsed[name]=note
    missing=sorted({t for n in parsed.values() for t in n.get('tags',[]) if isinstance(t,str)}-taxonomy(root))
    if missing:
        raise ValueError('Tags outside this taxonomy: '+', '.join(missing)+'. Add them first with: taxonomy-plan '+' '.join(missing)+', then inspect-plan and apply')
    present={n['id']:n for n in inventory(target,root,retired=True)}
    changes={};created=[];unchanged=[];conflicts=[]
    if learning_candidates:
        from .learning import rows,rel
        keys={r.get('import_key') for r in rows(root,target,'candidates')}
        for proposal in learning_candidates:
            if proposal['import_key'] in keys:continue
            changes[rel(target,'candidates',proposal['id'])]=canonical_json(proposal)
            created.append({'ref':target+':'+proposal['id'],'title':proposal['content']['title'],'status':'candidate'})
    for name,note in parsed.items():
        folder,file_name=name.split('/',1)
        retired=note.get('status','active')!='active'
        if (folder=='archive')!=retired:raise ValueError(f'{name}: status {note.get("status")!r} does not belong in {folder}/')
        note['domain']=target;path=root/'domains'/target/folder/file_name;note['path']=str(path)
        validate(note,target,root)
        text=serialize(note);ref=f"{target}:{note['id']}"
        if note['id'] in present:
            current=Path(present[note['id']]['path'])
            same=current.read_bytes().decode('utf-8').replace('\r\n','\n')==text
            (unchanged if same else conflicts).append({'ref':ref,'title':note['title'],**({} if same else {'reason':'this ID already exists here with different content; update that note instead'})})
            continue
        if path.exists():conflicts.append({'ref':ref,'title':note['title'],'reason':f'{path.name} already exists'});continue
        changes[path.relative_to(root).as_posix()]=text;created.append({'ref':ref,'title':note['title'],'status':note.get('status','active')})
    result={'ok':True,'source':str(source),'domain':target,'created':created,'unchanged':unchanged,'conflicts':conflicts}
    if changes:
        # A second plan adding the same IDs under other filenames would pass its file checks; the guard stops it.
        result.update(plan(root,changes,'import',[target],{'source':source.name,'notes':len(changes)},
                           guards={'absent_ids':[c['ref'].split(':',1)[1] for c in created],
                                   'absent_learning_imports':[target+':'+c['import_key'] for c in learning_candidates if c['import_key'] not in keys]}))
        result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
    else:result['plan_id']=None
    return result
