"""Managed Obsidian copies, staged canonical edits, dashboards, canvases and templates.

Vault layout (all vault-relative):
  Domains/<domain>/      active managed note copies; new notes dropped here are captured
  Archive/<domain>/      retired managed note copies
  Dashboards/            shared dashboards (describe the most recently built domain)
  Canvas/<domain>.canvas per-domain canvas
  Templates/             one Obsidian template per note type (never scanned)
  .living-memory/        vault identity and ownership manifests (hidden from Obsidian)
Vaults built with the legacy single-folder layout ('Living Memory/...') migrate on the next sync/build.
"""
from __future__ import annotations
import hashlib,json,re,sys,time,uuid
from pathlib import Path
from .common import memory_root,atomic_write,load_bindings
from .repository import inventory,parse,parse_text,validate,serialize,store_lock,read_json,canonical_json,transaction,digest,confined,now,reconcile,clean_files
from .governance import plan
from .note_types import normalize,NOTE_TYPES,review_due,STALE_DAYS
from .notes import slug

NOTES='Domains';ARCHIVE='Archive';DASH='Dashboards';CANVAS='Canvas';TEMPLATES='Templates';META='.living-memory'
COPIES=f'{META}/copies.json';GENERATED=f'{META}/generated.json';VAULT_ID=f'{META}/vault.json'
LEGACY='Living Memory';LEGACY_COPIES=f'{LEGACY}/.copies.json';LEGACY_GENERATED=f'{LEGACY}/.generated.json'
SCRIPT=Path(__file__).resolve().parents[1]/'living_memory.py'
# A file saved this recently may still be open in Obsidian; it is picked up at the next sync.
QUIET_SECONDS=30
# Losing this many managed copies at once is a vault reset or move, not a request to delete notes.
MASS_MISSING=(5,0.2)
from .note_format import sections,HEADINGS,TITLE_MARK,CALLOUT,ACTION_TYPES
TEMPLATE_PROMPTS={
    'knowledge':'Established fact, explanation or corrected understanding future work can rely on.',
    'decision':'The choice that was made, why, and the tradeoffs considered.',
    'preference':'Whose preference, the desired default, and its scope.',
    'context':'Background and circumstances needed to interpret this project or situation.',
    'status':'Current state, progress or outcome as of a stated date.',
    'bug':'Observed vs expected behavior, reproduction conditions and evidence.',
    'plan':'Intended steps, dependencies, how success is checked; proposed or approved.',
    'risk':'Possible problem: trigger, impact, mitigation and owner if known.',
    'research':'Question, sources or experiments, observations vs inference, open questions.',
    'policy':'The mandatory rule, its authority and where it applies.'}

def _hash(path):
    try:return digest(path.read_bytes())
    except FileNotFoundError:return None

def _text(path):
    """Exact file text (no newline translation), so hashes of written text match the bytes."""
    try:return path.read_bytes().decode('utf-8')
    except FileNotFoundError:return None

def _recent(path):
    try:return time.time()-path.stat().st_mtime<QUIET_SECONDS
    except OSError:return False

def _canvas_key(text):
    """Layout identity of a canvas, ignoring formatting and fields Obsidian adds on open."""
    try:
        data=json.loads(text)
        nodes=sorted([n.get('id'),n.get('file'),n.get('x'),n.get('y'),n.get('width'),n.get('height')] for n in data.get('nodes',[]))
        edges=sorted([e.get('id'),e.get('fromNode'),e.get('toNode'),e.get('label')] for e in data.get('edges',[]))
        return digest(canonical_json({'nodes':nodes,'edges':edges}).encode())
    except (ValueError,TypeError,AttributeError):return None

def _canvas_on_grid(text):
    """True when every card still sits where the generator placed it (no user layout)."""
    try:
        nodes=json.loads(text).get('nodes',[])
        return all(n.get('width')==380 and n.get('height')==240 and n.get('x',1)%420==0 and n.get('y',1)%280==0 for n in nodes)
    except (ValueError,TypeError,AttributeError):return False

def _read(path):
    try:return path.read_text(encoding='utf-8')
    except FileNotFoundError:return None

def note_rel(domain,name,active=True):
    return f"{NOTES if active else ARCHIVE}/{domain}/{name}"

def _prune_legacy_dirs(vault):
    legacy=vault/LEGACY
    if not legacy.is_dir() or legacy.is_symlink():return
    for d in sorted((p for p in legacy.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True)+[legacy]:
        try:d.rmdir()
        except OSError:pass

def vault_id(vault,create=True):
    """Stable identity of a vault, so sync state is never applied to a different (new, moved) vault."""
    data=read_json(Path(vault)/VAULT_ID,None)
    if isinstance(data,dict) and data.get('id'):return data['id']
    if not create:return None
    value=uuid.uuid4().hex;atomic_write(Path(vault)/VAULT_ID,canonical_json({'id':value,'created':now()}))
    return value

def is_managed(vault):
    """A torn-down vault (manifests removed) must not be silently rebuilt by later agent writes."""
    vault=Path(vault)
    return (vault/VAULT_ID).exists() or (vault/COPIES).exists() or (vault/LEGACY_COPIES).exists()

EDITABLE=('title','tags','type','context','review_by','files')
LIFECYCLE={'archived':'archive','resolved':'resolve'}
DONE={'archive':'Archived','resolve':'Resolved','delete':'Deleted'}
APPROVAL_LINE=re.compile(r'^\s*[-*]\s*\[[xX]\].*<!--\s*lm:([a-f0-9]{32})\s*-->')
KEEP_LINE=re.compile(r'^\s*[-*]\s*\[[xX]\].*<!--\s*lm-keep:([0-9a-f-]{36})\s*-->')
FRONTMATTER=re.compile(r'\A(﻿?---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*\r?\n)',re.S)

def status_rel(domain):return f'{DASH}/Sync Status - {domain}.md'

def _plan_applied(root,plan_id):
    journal=read_json(root/'.operations'/plan_id/'journal.json',None)
    return bool(journal) and journal.get('state')=='committed'

def _ticked(vault,domain,pattern):
    page=_read(confined(vault,status_rel(domain)))
    return [m[1] for m in (pattern.match(line) for line in (page or '').splitlines()) if m]

def _approve_ticked(domain,vault,root,entries):
    """Apply plans whose checkbox was ticked on this domain's Sync Status page."""
    ours={e['pending_plan']:(note_id,e) for note_id,e in entries.items() if e.get('pending_plan')}
    approved=[]
    from .governance import apply
    for plan_id in _ticked(vault,domain,APPROVAL_LINE):
        if plan_id not in ours or _plan_applied(root,plan_id):continue
        note_id,entry=ours[plan_id];value=read_json(root/'.plans'/(plan_id+'.json'),None)
        try:
            if not value:raise ValueError('Plan unavailable')
            receipt=apply(root,plan_id,value['digest'],sync_overlay=False)
            approved.append((plan_id,note_id,entry,receipt.get('operation_id',plan_id)))
        except ValueError as exc:entry['pending_error']='Approval failed: '+str(exc)
    return approved

def _canonical_changes(note,updated,domain,root):
    """Changes for writing an edited note, including a rename and links that point at it."""
    src=Path(note['path']);target=src.with_name(f"{updated['prefix']} - {slug(updated['title'])}-{note['id'][:8]}.md")
    updated['path']=str(target);validate(updated,domain,root)
    if target!=src and target.exists():raise ValueError('Filename collision with another note')
    changes={target.relative_to(root).as_posix():serialize(updated)};expected={src.relative_to(root).as_posix():digest(src.read_bytes())}
    if target!=src:
        changes[src.relative_to(root).as_posix()]=None
        from .migration import rewrite_type_links
        for referring in inventory(domain,root,retired=True):
            if referring['id']==note['id']:continue
            old=referring['body'];new=rewrite_type_links(old,Path(referring['path']),{src.resolve():target})
            if new!=old:
                referring['body']=new;referring['updated']=now();rel=Path(referring['path']).relative_to(root).as_posix()
                changes[rel]=serialize(referring);expected[rel]=referring['revision']
    changes[f'domains/{domain}/.derived-dirty']='dirty\n'
    return target,changes,expected

def _frontmatter_keys(block):
    """Top-level frontmatter keys with their full source lines (block-list items included)."""
    keys=[];current=None
    for line in block.splitlines():
        match=re.match(r'([^\s:#][^:]*):(\s|$)',line)
        if match:current=[match[1],[line]];keys.append(current)
        elif current is not None:current[1].append(line)
    return keys

def _vault_only(vault_text,canonical_text):
    """Frontmatter lines the user added in Obsidian (aliases, cssclasses, ...) that memory does not store."""
    if not vault_text:return []
    mine=FRONTMATTER.match(vault_text);theirs=FRONTMATTER.match(canonical_text)
    if not mine or not theirs:return []
    known={k for k,_ in _frontmatter_keys(theirs[2])}|set(EDITABLE)|{'status','prefix'}
    return [line for key,lines in _frontmatter_keys(mine[2]) if key not in known for line in lines]

def _with_vault_only(canonical_text,vault_text):
    extra=_vault_only(vault_text,canonical_text)
    if not extra:return canonical_text
    match=FRONTMATTER.match(canonical_text)
    return match[1]+match[2]+'\n'+'\n'.join(extra)+match[3]+canonical_text[match.end():]

def _session_ids(state,vault):
    """Adopt the state when it describes this vault; otherwise start fresh (never propose deletes)."""
    current=vault_id(vault)
    known=state.get('vault_id')
    if known==current or (known is None and state.get('vault')==str(vault)):return state,False
    fresh={'version':3,'entries':{},'applied':state.get('applied',[])}
    return fresh,bool(state.get('entries'))

def sync(domain,vault,root=None):
    root=Path(root or memory_root()).resolve();vault=Path(vault).resolve();vault.mkdir(parents=True,exist_ok=True)
    state_path=root/'domains'/domain/'sync-state.json'
    with store_lock(root),store_lock(vault):
        state=read_json(state_path,{'version':3,'entries':{}})
        if state.get('version')!=3:state={'version':3,'entries':{}}
        state,new_vault=_session_ids(state,vault)
        state['vault']=str(vault);state['vault_id']=vault_id(vault)
        entries=state['entries'];applied_log=state.setdefault('applied',[]);capture_errors={}
        copied=conflicts=staged=deferred=restored=renamed=0;plans=[];applied=[];view_changes={};staging_changes={};wrote_canonical=False
        owner={}
        def log(action,note_id,title,operation_id):
            applied.append(operation_id);applied_log.append({'action':action,'id':note_id,'title':title,'operation_id':operation_id,'at':now()})
        titles={n['id']:n['title'] for n in inventory(domain,root,retired=True)}
        approved=[]
        for plan_id,note_id,entry,operation_id in _approve_ticked(domain,vault,root,entries):
            approved.append(plan_id);wrote_canonical=True
            log(DONE.get(str(entry.get('pending_action','')).lower(),'Applied'),note_id,titles.get(note_id,Path(entry.get('overlay','?')).stem),operation_id)
        notes=inventory(domain,root,retired=True);by_id={n['id']:n for n in notes}
        # Plans applied since the last sync (ticked here or applied elsewhere): let canonical win.
        for note_id,entry in list(entries.items()):
            if note_id.startswith('capture-') or not entry.get('pending_plan') or not _plan_applied(root,entry['pending_plan']):continue
            if note_id not in by_id:
                path=confined(vault,entry['overlay'])
                if path.exists() and _hash(path)==entry.get('pending_hash'):view_changes[entry['overlay']]=None
                entries.pop(note_id);copied+=1;continue
            current=_hash(confined(vault,entry['overlay']))
            entries[note_id]={'overlay':entry['overlay'],'overlay_hash':current,'canonical_hash':None} if current else {}
        # "Keep my vault version" ticks: put the saved vault copy back so it is applied below as an edit.
        forced=set()
        for note_id in _ticked(vault,domain,KEEP_LINE):
            entry=entries.get(note_id) or {};conflict=entry.get('conflict')
            if not isinstance(conflict,dict) or note_id not in by_id:continue
            saved=root/conflict.get('staged','')
            target=confined(vault,entry['overlay'])
            if saved.is_file() and _hash(target)==entry.get('overlay_hash') and _hash(Path(by_id[note_id]['path']))==entry.get('canonical_hash'):
                atomic_write(target,_text(saved));forced.add(note_id)
            entry.pop('conflict',None)
        # Files renamed or moved inside Obsidian keep their id: follow them instead of proposing a delete.
        overlays={e.get('overlay') for e in entries.values()}
        moved={}
        for path in sorted((vault/NOTES/domain).rglob('*.md')) if (vault/NOTES/domain).is_dir() else []:
            rel=path.relative_to(vault).as_posix()
            if rel in overlays:continue
            found=parse_text(_text(path) or '',path)
            note_id=found.get('id') if found else None
            entry=entries.get(note_id) if note_id else None
            if entry and entry.get('overlay') and not (vault/entry['overlay']).exists():moved[note_id]=rel
        missing=[n['id'] for n in notes if n.get('status')=='active' and entries.get(n['id'],{}).get('overlay')
                 and not entries[n['id']].get('pending_hash') and n['id'] not in moved and not (vault/entries[n['id']]['overlay']).exists()]
        managed=sum(1 for n in notes if n.get('status')=='active' and n['id'] in entries)
        vault_reset=len(missing)>=MASS_MISSING[0] and len(missing)>=MASS_MISSING[1]*max(1,managed)
        known_paths=set()

        def place(note_id,old_rel,rel,text,current_text,current_hash):
            """Write a canonical copy at rel (keeping vault-only properties), removing old_rel. Returns its hash."""
            out=_with_vault_only(text,current_text)
            if current_hash is not None:owner.setdefault(note_id,(old_rel,current_hash))
            if old_rel!=rel and current_hash is not None:view_changes[old_rel]=None
            view_changes[rel]=out;known_paths.add(rel)
            return digest(out.encode())

        for note in notes:
            note_id=note['id'];src=Path(note['path']);source_text=_text(src);source_hash=digest(source_text.encode())
            active=note.get('status')=='active'
            rel=note_rel(domain,src.name,active)
            entry=dict(entries.get(note_id,{}));old_rel=entry.get('overlay',rel)
            if note_id in moved:old_rel=moved[note_id];renamed+=1
            dst=confined(vault,old_rel);known_paths.add(old_rel);target=confined(vault,rel);known_paths.add(rel)
            if dst.is_symlink() or target.is_symlink():raise ValueError('Overlay symlinks are forbidden')
            overlay_text=_text(dst);overlay_hash=digest(overlay_text.encode()) if overlay_text is not None else None
            old_source=entry.get('canonical_hash');old_overlay=entry.get('overlay_hash')
            # Only an open "keep my version" choice persists; informational conflicts are shown once.
            conflict=entry.get('conflict') if source_hash==old_source and isinstance(entry.get('conflict'),dict) and entry['conflict'].get('keep') else None
            pending=False;after_rel=rel;after_hash=source_hash;after_source=source_hash
            if overlay_hash not in (None,old_overlay,source_hash) and _recent(dst) and note_id not in forced:
                # Still being edited in Obsidian: leave both sides alone until it is quiet.
                deferred+=1
                if entry:entries[note_id]=entry
                continue
            if entry.get('pending_hash') and overlay_hash==entry['pending_hash'] and source_hash==old_source:
                pending=True;staged+=1   # waiting for approval or for a fix; nothing changed since
            elif entry.get('pending_hash')=='deleted' and overlay_hash is None:
                pending=True;staged+=1
            elif overlay_hash==source_hash:
                if old_rel!=rel:place(note_id,old_rel,rel,source_text,overlay_text,overlay_hash);copied+=1
            elif overlay_hash is not None and overlay_hash==old_overlay and source_hash==old_source and old_rel==rel:
                after_hash=overlay_hash   # unchanged since the last sync (vault-only properties kept)
            elif not active:
                if overlay_hash is not None and old_overlay is not None and overlay_hash!=old_overlay:
                    conflicts+=1
                    saved=f'domains/{domain}/staged/{note_id}-{overlay_hash[:12]}.conflict.md';staging_changes[saved]=overlay_text
                    conflict={'message':'Retired notes are read-only in the vault; your edit was saved to '+saved,'staged':saved}
                after_hash=place(note_id,old_rel,rel,source_text,overlay_text,overlay_hash);copied+=1
            elif not entry and overlay_hash is None:
                after_hash=place(note_id,old_rel,rel,source_text,None,None);copied+=1
            elif overlay_hash is None and entry:
                if vault_reset:
                    after_hash=place(note_id,old_rel,rel,source_text,None,None);restored+=1
                else:
                    pending=True;staged+=1
                    proposed=plan(root,{src.relative_to(root).as_posix():None},'overlay_delete',[domain],{'id':note_id,'action':'delete'})
                    plans.append(proposed);entry.update(pending_plan=proposed['plan_id'],pending_hash='deleted',pending_action='Delete')
                    entry.pop('pending_error',None)
            elif overlay_hash!=old_overlay and (source_hash!=old_source or not entry) and note_id not in forced:
                # Changed on both sides. Memory wins in the vault; the vault version is kept and can be restored.
                conflicts+=1
                saved=f'domains/{domain}/staged/{note_id}-{overlay_hash[:12]}.conflict.md';staging_changes[saved]=overlay_text
                conflict={'message':'Changed in memory and in the vault at the same time. The vault now shows the memory version; yours was saved to '+saved,
                          'staged':saved,'keep':True}
                after_hash=place(note_id,old_rel,rel,source_text,overlay_text,overlay_hash)
            elif overlay_hash!=old_overlay:
                proposed_note=parse_text(overlay_text,dst)
                staging_changes[f'domains/{domain}/staged/{note_id}-{overlay_hash[:12]}.md']=overlay_text
                try:
                    if not proposed_note:raise ValueError('Frontmatter could not be read')
                    if proposed_note.get('id')!=note_id:raise ValueError('ID cannot change')
                    wanted=proposed_note.get('status') or note.get('status')
                    if wanted!=note.get('status') and wanted not in LIFECYCLE:
                        raise ValueError(f"Status '{wanted}' can't be set from the vault; ask Claude")
                    updated=dict(note)
                    for k in EDITABLE:
                        if k in proposed_note:updated[k]=proposed_note[k]
                    if 'files' in updated:
                        updated['files']=clean_files(updated['files'])
                        if not updated['files']:updated.pop('files')
                    updated['body']=proposed_note['body'];updated=normalize(updated)
                    if serialize({**updated,'updated':''})!=serialize({**normalize(note),'updated':''}):
                        updated['updated']=now()
                        canonical_target,changes,expected=_canonical_changes(note,updated,domain,root)
                        receipt=transaction(root,changes,'overlay_auto_update',expected);wrote_canonical=True
                        log('Edited',note_id,updated['title'],receipt.get('operation_id'))
                        new_text=changes[canonical_target.relative_to(root).as_posix()];new_hash=digest(new_text.encode())
                    else:
                        canonical_target,new_text,new_hash=src,source_text,source_hash
                    if wanted!=note.get('status'):
                        from .notes import change
                        proposed=change(note_id,domain,LIFECYCLE[wanted],root,plan_only=True)
                        plans.append(proposed);pending=True;staged+=1
                        entry={'overlay':old_rel,'overlay_hash':overlay_hash,'canonical_hash':new_hash,'pending_hash':overlay_hash,
                               'pending_plan':proposed['plan_id'],'pending_action':LIFECYCLE[wanted].capitalize()}
                    else:
                        after_rel=note_rel(domain,canonical_target.name);after_source=new_hash
                        after_hash=place(note_id,old_rel,after_rel,new_text,overlay_text,overlay_hash);copied+=1
                        conflict=None
                except ValueError as exc:
                    pending=True;staged+=1
                    entry={**{k:v for k,v in entry.items() if k not in ('pending_plan','pending_action','conflict')},'pending_hash':overlay_hash,'pending_error':str(exc)}
            else:
                after_hash=place(note_id,old_rel,rel,source_text,overlay_text,overlay_hash);copied+=1
            if pending:entries[note_id]={**entry,'overlay':old_rel}
            else:
                entries[note_id]={'overlay':after_rel,'canonical_hash':after_source,'overlay_hash':after_hash,'status':note.get('status')}
                if conflict:entries[note_id]['conflict']=conflict
        active_ids={n['id'] for n in notes}
        for note_id,entry in list(entries.items()):
            if note_id in active_ids:continue
            if note_id.startswith('capture-'):
                if entry.get('note_id') in active_ids:entries.pop(note_id)
                continue
            path=vault/entry['overlay'];known_paths.add(entry['overlay'])
            if _hash(path)==entry.get('overlay_hash'):
                view_changes[entry['overlay']]=None;entries.pop(note_id);copied+=1
            elif path.exists():conflicts+=1
        scan_dirs=[vault/NOTES/domain,vault/LEGACY/'domains'/domain/'notes']
        for path in (p for d in scan_dirs if d.is_dir() for p in sorted(d.rglob('*.md'))):
            confined(vault,path.relative_to(vault));path_rel=path.relative_to(vault).as_posix()
            if path_rel in known_paths or path_rel in view_changes:continue
            if _recent(path):deferred+=1;continue
            text=_text(path);note=parse_text(text,path);h=digest(text.encode())
            if 'capture-'+h in entries:continue
            staging_changes[f'domains/{domain}/staged/capture-{h[:16]}.md']=text
            try:
                if not note:raise ValueError('No frontmatter with a title; start the note from a template')
                note_type=str(note.get('type') or 'knowledge')
                if sections(note.get('body','')).get('summary','').strip()==TEMPLATE_PROMPTS.get(note_type,'').strip():
                    raise ValueError('Still the template text; replace it with the note, and it is saved at the next sync')
                # Notes written from an Obsidian template carry no stable identity yet. The ID derives from the
                # file's content, so if a sync stops after the canonical write, the retry finds that note
                # instead of capturing the same text again under a new random ID.
                derived=not note.get('id')
                if derived:note['id']=str(uuid.uuid5(uuid.NAMESPACE_URL,f'living-memory-vault-capture:{domain}:{h}'))
                replay=next((n for n in notes if n['id']==note['id']),None) if derived else None
                if replay:
                    destination=Path(replay['path']);new_text=destination.read_text(encoding='utf-8').replace('\r\n','\n')
                    log('Captured',note['id'],note['title'],'already saved by an interrupted sync')
                else:
                    if not note.get('domain'):note['domain']=domain
                    if not note.get('status'):note['status']='active'
                    stamp=now();note.setdefault('created',stamp);note.setdefault('updated',stamp)
                    note=normalize(note)
                    destination=root/'domains'/domain/'notes'/f"{note['prefix']} - {slug(note['title'])}-{note['id'][:8]}.md"
                    note['path']=str(destination)
                    validate(note,domain,root,new=True)
                    if any(n['id']==note['id'] for n in notes):raise ValueError('Duplicate ID')
                    if destination.exists():raise ValueError('Filename collision')
                    new_text=serialize(note)
                    receipt=transaction(root,{destination.relative_to(root).as_posix():new_text,f'domains/{domain}/.derived-dirty':'dirty\n'},'overlay_capture');wrote_canonical=True
                    log('Captured',note['id'],note['title'],receipt.get('operation_id'))
                new_rel=note_rel(domain,destination.name)
                view_changes[path_rel]=None;view_changes[new_rel]=new_text;copied+=1
                entries[note['id']]={'overlay':new_rel,'canonical_hash':digest(new_text.encode()),'overlay_hash':digest(new_text.encode()),'status':'active'}
            except ValueError as exc:
                staged+=1;capture_errors[path_rel]=str(exc)
        # A file saved in Obsidian since it was read is left alone; the next sync sees it as an edit.
        skipped=0
        for note_id,(old_rel,seen) in owner.items():
            touched=[r for r in view_changes if r==old_rel or entries.get(note_id,{}).get('overlay')==r]
            if _hash(confined(vault,old_rel))==seen:continue
            for r in touched:view_changes.pop(r,None)
            if note_id in entries:entries[note_id]={**entries[note_id],'overlay':old_rel,'overlay_hash':seen}
            skipped+=1
        state['capture_errors']=capture_errors;state['applied']=applied_log[-20:]
        copy_manifest=read_json(vault/COPIES,None)
        if copy_manifest is None:copy_manifest=read_json(vault/LEGACY_COPIES,{'files':{}})
        if (vault/LEGACY_COPIES).exists():view_changes[LEGACY_COPIES]=None
        for entry in entries.values():
            if entry.get('overlay_hash') and not entry.get('pending_hash'):copy_manifest['files'][entry['overlay']]=entry['overlay_hash']
        for rel,value in view_changes.items():
            if value is None:copy_manifest['files'].pop(rel,None)
        view_changes[COPIES]=canonical_json(copy_manifest)
        transaction(vault,view_changes,'overlay_sync')
        state['last_sync']={'at':now(),'copied':copied,'applied':len(applied),'approved':len(approved),'conflicts':conflicts,
                            'deferred':deferred,'restored':restored,'renamed':renamed,'new_vault':new_vault}
        staging_changes[state_path.relative_to(root).as_posix()]=canonical_json(state)
        transaction(root,staging_changes,'overlay_state')
        if wrote_canonical:reconcile(domain,root,sync_overlay=False)
        _prune_legacy_dirs(vault)
        return {'ok':True,'domain':domain,'copied':copied,'staged':staged,'conflicts':conflicts,'plans':plans,'applied':applied,'approved':approved,
                'deferred':deferred+skipped,'restored':restored,'renamed':renamed,'new_vault':new_vault}

def _undo_command(domain,operation_id):
    return f'& "{sys.executable}" "{SCRIPT}" --domain {domain} undo {operation_id}'

def _status_page(domain,root,notes,view_conflicts=(),trigger=None,failure=None):
    state=read_json(root/'domains'/domain/'sync-state.json',{'entries':{}});by_id={n['id']:n for n in notes}
    def name(note_id,entry):
        n=by_id.get(note_id);return n['title'] if n else Path(entry.get('overlay','?')).stem
    waiting=[];errors=[];conflicts=[]
    for note_id,entry in state.get('entries',{}).items():
        if note_id.startswith('capture-'):continue
        if entry.get('pending_plan') and not _plan_applied(root,entry['pending_plan']):
            waiting.append(f"- [ ] {entry.get('pending_action','Apply')}: {name(note_id,entry)} <!-- lm:{entry['pending_plan']} -->")
        if entry.get('pending_error'):errors.append(f"- {name(note_id,entry)}: {entry['pending_error']}")
        conflict=entry.get('conflict')
        if isinstance(conflict,dict):
            if conflict.get('keep'):conflicts.append(f"- [ ] Keep my vault version: {name(note_id,entry)} <!-- lm-keep:{note_id} -->\n  - {conflict['message']}")
            else:conflicts.append(f"- {name(note_id,entry)}: {conflict['message']}")
        elif conflict:conflicts.append(f"- {name(note_id,entry)}: {conflict}")
    conflicts+=[f"- `{rel}` was edited, so it is no longer regenerated. Delete it to get a fresh copy." for rel in view_conflicts]
    errors+=[f"- `{rel}`: {msg}" for rel,msg in sorted(state.get('capture_errors',{}).items())]
    recent=[f"- {a['at'][:16].replace('T',' ')} {a['action']}: {a['title']} — undo: `{_undo_command(domain,a['operation_id'])}`"
            for a in reversed(state.get('applied',[])[-10:])]
    last=state.get('last_sync') or {}
    if failure:header=f"**Last sync failed** at {now()[:16].replace('T',' ')} UTC: {failure}"
    elif last:
        parts=[f"{last.get(k)} {label}" for k,label in (('applied','edits saved'),('approved','approvals'),('conflicts','conflicts'),('deferred','still being edited'),('restored','missing copies restored'),('renamed','renamed files given back their memory names')) if last.get(k)]
        header=f"Last sync: {last['at'][:16].replace('T',' ')} UTC"+(f" ({trigger})" if trigger else '')+(' — '+', '.join(parts) if parts else ' — no changes')
        if last.get('new_vault'):header+='. This vault was new to memory, so notes were copied and nothing was proposed for deletion.'
    else:header='Not synced yet.'
    section=lambda title,lines,empty:f'## {title}\n\n'+('\n'.join(lines) if lines else f'_{empty}_')+'\n'
    return (f'# Sync Status — {domain}\n\n{header}\n\nTick a box to approve it; the change is applied at the next sync (session end). '
            'Leave it unticked to wait. This page is regenerated on every sync.\n\n'
            +section('Waiting for approval',waiting,'Nothing waiting.')+'\n'
            +section("Couldn't save",errors,'No problems.')+'\n'
            +section('Conflicts',conflicts,'No conflicts.')+'\n'
            +section('Recently applied',recent,'Nothing yet.'))

def _template(note_type):
    """Frontmatter and section skeleton in the note layout; sections left unfilled are dropped at capture."""
    meta={'title':'{{title}}','type':note_type,'status':'active','tags':['subject/project','concern/knowledge'],'context':''}
    parts=[TITLE_MARK+'{{title}}','## '+HEADINGS['summary']+'\n'+TEMPLATE_PROMPTS[note_type],'---',
           '## '+HEADINGS['details']+'\n* **Focus Point:** ','## '+HEADINGS['links']+'\n* ']
    if note_type in ACTION_TYPES:parts.append('## '+HEADINGS['actions']+'\n- [ ] ')
    parts.append(CALLOUT+'\n> ')
    return '---\n'+'\n'.join(f'{k}: {json.dumps(v)}' for k,v in meta.items())+'\n---\n\n'+'\n\n'.join(parts)+'\n'

def build_overlay(domain,vault,root=None,trigger=None):
    root=Path(root or memory_root()).resolve();vault=Path(vault).resolve()
    try:sync_result=sync(domain,vault,root)
    except Exception as exc:
        _report_failure(domain,vault,root,exc);raise
    notes=inventory(domain,root,retired=True);active=[n for n in notes if n.get('status')=='active'];changes={}
    def link(n):
        target=note_rel(domain,Path(n['path']).name,n.get('status')=='active')[:-3]
        return f"[[{target}|{n['title']}]]"
    stale=[n for n in active if review_due(n)]
    pages={'Home':'# Living Memory\n\n'+f'Domain: {domain}\n\nActive: {len(active)}; retired: {len(notes)-len(active)}; due for review: {len(stale)}\n\n'
                  +'\n'.join(f'- [[{DASH}/{p}|{p}]]' for p in ('Domain Overview','Timeline','Stale Notes','Taxonomy','Archive'))+f'\n- [[{DASH}/Sync Status - {domain}|Sync Status]]\n- [[{CANVAS}/{domain}.canvas|Canvas]]',
           'Taxonomy':'# Taxonomy\n\n'+'\n'.join(f"- {t}" for t in sorted({t for n in notes for t in n.get('tags',[])})),
           'Stale Notes':'# Stale Notes\n\n'+(f'Notes whose review date has passed, or that have not changed in a long time ({STALE_DAYS} days; 30-90 for status, plan, bug and risk notes).\n\n'
                         +'\n'.join(f"- {link(n)} — {('review by '+str(n['review_by'])[:10]) if n.get('review_by') else 'updated '+str(n.get('updated',''))[:10]}" for n in stale) if stale else '_Nothing is due for review._'),
           'Timeline':'# Timeline\n\n'+'\n'.join(f"- {n.get('updated','')[:10]} {link(n)}" for n in sorted(notes,key=lambda n:n.get('updated',''),reverse=True)),
           'Domain Overview':'# Domain Overview\n\n'+f'Domain: {domain}\n\n'+'\n'.join('- '+link(n) for n in active),
           'Archive':'# Archive\n\n'+'\n'.join('- '+link(n) for n in notes if n.get('status')!='active')}
    for name,text in pages.items():changes[f'{DASH}/{name}.md']=text+'\n'
    for t in NOTE_TYPES:changes[f'{TEMPLATES}/{t.capitalize()}.md']=_template(t)
    nodes=[{'id':n['id'],'type':'file','file':note_rel(domain,Path(n['path']).name),'x':(i%4)*420,'y':(i//4)*280,'width':380,'height':240} for i,n in enumerate(active)]
    edges=[]
    for i,a in enumerate(active):
        for b in active[i+1:]:
            shared=set(a.get('tags',[]))&set(b.get('tags',[]))
            if shared:edges.append({'id':hashlib.sha256((a['id']+b['id']).encode()).hexdigest()[:16],'fromNode':a['id'],'toNode':b['id'],'label':', '.join(sorted(shared))})
    changes[f'{CANVAS}/{domain}.canvas']=canonical_json({'nodes':nodes,'edges':edges})
    with store_lock(vault):
        manifest=read_json(vault/GENERATED,{'files':{}});semantic=manifest.setdefault('semantic',{})
        legacy_conflicts=[];view_conflicts=[]
        legacy=read_json(vault/LEGACY_GENERATED,None)
        if legacy is not None:
            # Remove unchanged legacy-layout views; user-edited ones are left in place and disowned.
            for rel,owned in legacy.get('files',{}).items():
                if rel in changes:continue
                current=_hash(confined(vault,rel))
                if current is None:continue
                if current==owned or (rel.endswith('.canvas') and _canvas_on_grid(_read(confined(vault,rel)))):changes[rel]=None
                else:legacy_conflicts.append(rel)
            changes[LEGACY_GENERATED]=None
        for rel,text in list(changes.items()):
            if text is None:continue
            path=confined(vault,rel);current=_hash(path);owned=manifest['files'].get(rel)
            if current is not None and (owned is None or current!=owned):
                # Templates are seeded once and are the user's to customize afterwards.
                if rel.startswith(TEMPLATES+'/'):changes.pop(rel);manifest['files'].pop(rel,None);continue
                if rel.endswith('.canvas') and owned is not None:
                    # Obsidian reformats canvases on open; only a layout change is a user edit.
                    existing=_read(path)
                    if _canvas_key(existing)==semantic.get(rel) or (rel not in semantic and _canvas_on_grid(existing)):continue
                # A user-edited dashboard or canvas is preserved and no longer regenerated (delete it to reset).
                changes.pop(rel);view_conflicts.append(rel)
        # The status page is a control surface: ticks were consumed by sync, so it is always rewritten.
        changes[status_rel(domain)]=_status_page(domain,root,notes,view_conflicts,trigger)
        for rel,text in changes.items():
            if text is not None and rel.endswith('.canvas'):semantic[rel]=_canvas_key(text)
        manifest['files'].update({rel:digest(text.encode()) for rel,text in changes.items() if text is not None})
        changes[GENERATED]=canonical_json(manifest)
        transaction(vault,changes,'overlay_views')
    _prune_legacy_dirs(vault)
    return {**sync_result,'dashboards':7,'templates':len(NOTE_TYPES),'canvas_nodes':len(nodes),'canvas_edges':len(edges),'legacy_conflicts':legacy_conflicts,'view_conflicts':view_conflicts}

def _report_failure(domain,vault,root,exc):
    """Best effort: a failed sync must still be visible where the user looks."""
    try:
        notes=inventory(domain,root,retired=True)
        atomic_write(confined(vault,status_rel(domain)),_status_page(domain,root,notes,failure=str(exc)[:500]))
    except Exception:pass

def sync_all(root=None,trigger='manual',primary=None):
    """Sync every domain that has a managed vault. Continues past failures and records the outcome."""
    root=Path(root or memory_root()).resolve()
    targets=[]
    for directory in sorted((root/'domains').iterdir()) if (root/'domains').is_dir() else []:
        state=read_json(directory/'sync-state.json',{}) if (directory/'sync-state.json').exists() else {}
        vault=state.get('vault') if isinstance(state,dict) else None
        if vault and is_managed(vault):targets.append((directory.name,vault,len(list((directory/'notes').glob('*.md')))))
    # Shared dashboards describe the last domain built: the chosen primary, else the largest.
    targets.sort(key=lambda t:(t[0]==primary,t[2] if primary is None else 0,t[0]))
    results=[]
    for domain,vault,_ in targets:
        try:
            r=build_overlay(domain,vault,root,trigger)
            results.append({'domain':domain,'vault':vault,'ok':True,'applied':len(r['applied']),'approved':len(r['approved']),'conflicts':r['conflicts'],'plans':len(r['plans'])})
        except Exception as exc:
            results.append({'domain':domain,'vault':vault,'ok':False,'error':str(exc)[:500]})
    summary={'ok':all(r['ok'] for r in results),'at':now(),'trigger':trigger,'results':results}
    try:atomic_write(root/'hooks'/'last-sync.json',canonical_json(summary))
    except OSError:pass
    return summary

def teardown_plan(vault):
    vault=Path(vault).resolve();manifest={'files':{}};semantic={};changes={}
    for rel in (GENERATED,COPIES,LEGACY_GENERATED,LEGACY_COPIES,VAULT_ID):
        if (vault/rel).exists():
            data=read_json(vault/rel,{'files':{}});manifest['files'].update(data.get('files',{}));semantic.update(data.get('semantic',{}));changes[rel]=None
    for rel,expected in manifest['files'].items():
        current=_hash(vault/rel)
        if current is None:continue
        if current!=expected and not (rel in semantic and _canvas_key(_read(vault/rel))==semantic[rel]):
            raise ValueError('Modified managed file preserved: '+rel)
        changes[rel]=None
    return plan(vault,changes,'overlay_teardown')
def main(argv=None):
    import argparse
    p=argparse.ArgumentParser();p.add_argument('domain');p.add_argument('vault',type=Path);p.add_argument('--root',type=Path,default=memory_root());a=p.parse_args(argv);print(json.dumps(sync(a.domain,a.vault,a.root)));return 0
if __name__=='__main__':raise SystemExit(main())
