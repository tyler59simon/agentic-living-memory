"""Point-in-time snapshots of the canonical store, and restore as an exact-hash approved plan.

Journals make every script operation reversible; snapshots cover what journals cannot: a deleted or
mangled store folder (an external sync tool, a bad manual edit). Indexes, turn state, journals, plans
and staged files are derived or transient and are not included."""
from __future__ import annotations
import json,os,re,zipfile
from datetime import datetime,timezone
from pathlib import Path
from .common import atomic_write,parse_iso,retry_sharing
from .paths import is_os_junk
from .repository import canonical_json,confined,parse_text,read_json,store_lock

KEEP=7
# Rotation never deletes a snapshot holding at least twice the notes of the new one (and this many more):
# a store that shrank that much may have lost a folder, and that snapshot may be the last good copy.
SHRINK_MIN=5
INTERVAL_HOURS=24
STALE_HOURS=48
# Hooks skip automatic snapshots of very large stores; doctor already flags that size.
AUTO_MAX_NOTES=1500
TOP_LEVEL=('bindings.json','tag_taxonomy.json','projects.json','systems.json','sources.json','views.json','note_types.json','schema-version.json',
           'notebook-privacy.json','notebook-folders.json')
# Members an older snapshot may hold that the store no longer keeps: verify accepts them and a restore drops them (Changes removed, CON-17).
LEGACY=('changes.json',)
# ledger/ is append-only history with no other copy (journals do not hold it), so a snapshot saves it and a restore only adds to it.
LEDGER=re.compile(r'ledger/sessions-\d{4}\.jsonl')
MAINT=re.compile(r'maintenance\.json|maintenance/(?:state\.json|runs/\d{4}-\d{2}-\d{2}/[A-Za-z0-9_-]{1,100}\.json)')
MEMBER=re.compile(r'domains/[A-Za-z0-9_.-]+/(?:notes|archive)/(?:[^/\\]+/)*[^/\\]+\.md')
# Notebooks (NS-2A): notebook.json, pages at any depth, attachments and staged proposals. Notes stay MEMBER: every note count uses it.
NOTEBOOK=re.compile(r'(domains/[A-Za-z0-9_.-]+/notebooks/[a-z0-9][a-z0-9-]{1,39})/(?:notebook\.json|\.proposals/[a-f0-9]{12}\.json|assets/[^/\\]+|(?:[^/\\]+/)*[^/\\]+\.(?i:md))')
ASSET=re.compile(r'domains/[^/]+/notebooks/[^/]+/assets/[^/]+')
MANIFEST='manifest.json'
# Automatic snapshots leave out attachments past this many bytes (listed in the manifest as omitted); a manual backup takes all.
ASSET_BUDGET=500*1024*1024
NAME=re.compile(r'lm-(\d{8}-\d{6})(?:-(\d+))?\.zip')

def backup_dir(root):return Path(root)/'backups'

def canonical_files(root):
    """Store-relative POSIX paths of the canonical set."""
    root=Path(root)
    found=[name for name in TOP_LEVEL if (root/name).is_file()]
    if (root/'ledger').is_dir() and not (root/'ledger').is_symlink():found+=sorted(p.relative_to(root).as_posix() for p in (root/'ledger').glob('sessions-*.jsonl') if LEDGER.fullmatch(p.relative_to(root).as_posix()) and p.is_file() and not p.is_symlink())
    found+=_maintenance_files(root)
    for domain in sorted((root/'domains').iterdir()) if (root/'domains').is_dir() else []:
        for folder in ('notes','archive'):
            base=domain/folder
            if base.is_dir() and not base.is_symlink():
                found+=sorted(p.relative_to(root).as_posix() for p in base.rglob('*.md') if p.is_file() and not p.is_symlink() and not is_os_junk(p))
        found+=notebook_files(root,domain)
    return found

def notebook_files(root,domain):
    """Store-relative paths of a domain's notebook files, never following a link or junction."""
    from .docs import _files,_is_link
    base=Path(domain)/'notebooks'
    if _is_link(Path(root)/'domains') or _is_link(domain) or not base.is_dir() or _is_link(base):return []
    found=[]
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and not _is_link(p)):
        found+=[rel for rel in (f'{folder.relative_to(root).as_posix()}/{f}' for f in _files(folder)) if NOTEBOOK.fullmatch(rel)]
    return found

def _notebook(rel):
    """'D/NB' for a notebook member, else None."""
    match=NOTEBOOK.fullmatch(rel)
    return match[1].split('/',1)[1].replace('/notebooks/','/') if match else None

def _maintenance_files(root):
    """maintenance.json, the accounting checkpoint and the run records; never worker.lock."""
    found=[]
    if (root/'maintenance.json').is_file() and not (root/'maintenance.json').is_symlink():found.append('maintenance.json')
    base=root/'maintenance'
    if base.is_dir() and not base.is_symlink():
        found+=sorted(rel for rel in (p.relative_to(root).as_posix() for p in base.rglob('*.json') if p.is_file() and not p.is_symlink()) if MAINT.fullmatch(rel))
    return found

def _allowed(name):
    if not name or name.startswith('/') or '\\' in name or any(part in ('','.','..') for part in name.split('/')):return False
    if NOTEBOOK.fullmatch(name):
        # Notebook members obey the page name rules, so a Windows alias (a trailing dot or space, a device name) cannot land on another file.
        from .docs import _check_part,clean_path
        rest='/'.join(name.split('/')[4:])
        try:
            for part in name.split('/')[4:]:_check_part(part)
            if rest!='notebook.json' and not re.fullmatch(r'\.proposals/[a-f0-9]{12}\.json|assets/[^/]+',rest):clean_path(rest)
        except ValueError:return False
        return True
    return name in TOP_LEVEL or name in LEGACY or name==MANIFEST or bool(MEMBER.fullmatch(name)) or bool(LEDGER.fullmatch(name)) or bool(MAINT.fullmatch(name))

def snapshots(root):
    """Existing snapshots, newest first: by timestamp, then by same-second sequence (lm-T.zip, lm-T-1.zip, ...)."""
    folder=backup_dir(root)
    if not folder.is_dir():return []
    def order(p):
        m=NAME.fullmatch(p.name);return (m[1],int(m[2] or 0))
    return sorted((p for p in folder.glob('lm-*.zip') if NAME.fullmatch(p.name)),key=order,reverse=True)

def _note_count(path):
    try:
        with zipfile.ZipFile(path) as archive:return sum(1 for name in archive.namelist() if MEMBER.fullmatch(name))
    except (OSError,zipfile.BadZipFile):return 0

def backup(root,keep=KEEP,budget=None):
    """Write and verify a snapshot, then keep the newest `keep`. A note that cannot be read as a note is still
    saved byte for byte and reported under `unreadable`: one damaged file must not stop every backup.
    budget: the most attachment bytes to include (automatic snapshots); the rest are listed in the manifest as omitted."""
    root=Path(root).resolve();folder=backup_dir(root);folder.mkdir(parents=True,exist_ok=True)
    # Naming, writing, verification, publication and pruning all hold the store lock, so two sessions
    # backing up in the same second cannot share or delete each other's files.
    with store_lock(root):
        stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S');target=folder/f'lm-{stamp}.zip'
        # Always above the highest same-second sequence: reusing a pruned lower name would sort as older and be pruned.
        taken=[int(m[2] or 0) for m in (NAME.fullmatch(p.name) for p in folder.glob(f'lm-{stamp}*.zip')) if m and m[1]==stamp]
        if taken:target=folder/f'lm-{stamp}-{max(taken)+1}.zip'
        temp=target.with_name('.'+target.name+'.tmp')
        try:
            files=canonical_files(root);omitted=[];used=0
            for rel in [f for f in files if ASSET.fullmatch(f)]:
                size=(root/rel).stat().st_size
                if budget is not None and used+size>budget:omitted.append({'path':rel,'reason':'over the automatic backup budget','size':size});continue
                used+=size
            files=[f for f in files if f not in {o['path'] for o in omitted}]
            manifest={'version':2,'notes':True,'notebooks':sorted({_notebook(f) for f in files if _notebook(f)}),'omitted':omitted}
            with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as archive:
                for rel in files:
                    # Attachments are mostly compressed already (images, PDFs): stored, and streamed from disk.
                    if ASSET.fullmatch(rel):archive.write(root/rel,rel,compress_type=zipfile.ZIP_STORED)
                    else:archive.writestr(rel,(root/rel).read_bytes())
                archive.writestr(MANIFEST,canonical_json(manifest))
            unreadable=verify(temp)
            retry_sharing(lambda:os.replace(temp,target))
        finally:
            if temp.exists():retry_sharing(temp.unlink)
        notes=sum(1 for f in files if MEMBER.fullmatch(f))
        removed=[];retained=[]
        for old in snapshots(root)[max(1,keep):]:
            held=_note_count(old)
            if held>=2*notes and held-notes>=SHRINK_MIN:retained.append(old.name);continue
            retry_sharing(old.unlink);removed.append(old.name)
    result={'ok':True,'backup':str(target),'notes':notes,'files':len(files),'pruned':removed}
    if manifest['notebooks']:result.update(notebooks=len(manifest['notebooks']),pages=sum(1 for f in files if NOTEBOOK.fullmatch(f) and f.lower().endswith('.md')))
    warnings=[]
    from .docs import private_scope,is_private_path
    scope=private_scope(root)
    hidden=sum(1 for o in omitted if is_private_path(o.get('path',''),scope))+sum(1 for u in unreadable or [] if is_private_path(u.get('member',''),scope))
    omitted=[o for o in omitted if not is_private_path(o.get('path',''),scope)] if hidden else omitted
    unreadable=[u for u in unreadable or [] if not is_private_path(u.get('member',''),scope)] if hidden else unreadable
    if hidden:result['private_withheld']=hidden   # entries inside private notebooks, counted but not named
    if omitted:
        result['omitted']=omitted
        warnings.append(f'{len(omitted)} attachment(s) were left out of this automatic snapshot (over its {ASSET_BUDGET//(1024*1024)} MB budget); run backup to include them')
    if unreadable:
        result['unreadable']=unreadable
        warnings.append(f'{len(unreadable)} file(s) are not readable notes or configuration; they were saved as they are (run doctor)')
    if retained:
        result['retained']=retained
        warnings.append(f'this snapshot has {notes} note(s), far fewer than {len(retained)} older snapshot(s), which were kept; '
                        'if notes are missing, restore-plan one of them, and if the drop is intended, delete them by hand')
    if warnings:result['warning']='; '.join(warnings)
    return result

def verify(path):
    """[{member, problem}] for members that are not readable: a note that is not UTF-8 or has no parseable frontmatter, a page
    that is not UTF-8, invalid JSON, an attachment over the size cap. Raises ValueError for a member outside the canonical set."""
    from .docs import MAX_ASSET_BYTES
    found=[];seen={}
    with zipfile.ZipFile(path) as archive:
        infos=archive.infolist()
        for info in infos:
            if info.is_dir() or not _allowed(info.filename):raise ValueError(f'Backup member outside the canonical set: {info.filename}')
            folded=info.filename.casefold() if NOTEBOOK.fullmatch(info.filename) else info.filename
            if folded in seen:raise ValueError(f'Backup members {seen[folded]} and {info.filename} name the same file')
            seen[folded]=info.filename
        # In a notebook, a member may not also be the folder of another, and one folder has one spelling: either would fail half
        # way on Windows. Notes keep the layout the store's own inventory accepts.
        folders={}
        for name in [n for n in seen.values() if NOTEBOOK.fullmatch(n)]:
            parts=name.split('/')
            for i in range(1,len(parts)):
                spelled='/'.join(parts[:i])
                if spelled.casefold() in seen:raise ValueError(f'Backup member {seen[spelled.casefold()]} is also the folder of {name}')
                if folders.setdefault(spelled.casefold(),spelled)!=spelled:raise ValueError(f'Backup members spell the folder {spelled} two ways')
        for info in infos:
            if ASSET.fullmatch(info.filename):
                # Size only; reading it through still checks the CRC, a chunk at a time.
                if info.file_size>MAX_ASSET_BYTES:found.append({'member':info.filename,'problem':'attachment over the size cap'});continue
                try:
                    with archive.open(info) as stream:
                        while stream.read(1024*1024):pass
                except zipfile.BadZipFile:found.append({'member':info.filename,'problem':'damaged in the snapshot'})
                continue
            data=archive.read(info)
            if NOTEBOOK.fullmatch(info.filename) and info.filename.lower().endswith('.md'):
                try:data.decode('utf-8')
                except UnicodeError:found.append({'member':info.filename,'problem':'not UTF-8'})
                continue
            if info.filename.startswith('ledger/'):
                try:
                    for line in data.decode('utf-8').splitlines():
                        if line.strip():json.loads(line)
                except (UnicodeError,ValueError):found.append({'member':info.filename,'problem':'invalid JSON lines'})
            elif MEMBER.fullmatch(info.filename):
                try:text=data.decode('utf-8')
                except UnicodeError:found.append({'member':info.filename,'problem':'not UTF-8'});continue
                if parse_text(text,info.filename) is None:found.append({'member':info.filename,'problem':'does not parse as a note'})
            else:
                try:read_json_bytes(data)
                except ValueError:found.append({'member':info.filename,'problem':'invalid JSON'})
    return found

def read_json_bytes(data):
    import json
    try:return json.loads(data.decode('utf-8-sig'))
    except (UnicodeError,json.JSONDecodeError) as exc:raise ValueError('invalid JSON') from exc

def _age_hours(path):
    return (datetime.now(timezone.utc).timestamp()-path.stat().st_mtime)/3600

def maybe_backup(root):
    """Hook trigger: at most once per INTERVAL_HOURS. Never raises."""
    try:
        root=Path(root).resolve();marker=root/'hooks'/'last-backup.json'
        # An unreadable marker means "no backup known", not "never back up again".
        try:age=(datetime.now(timezone.utc)-parse_iso((read_json(marker,{}) or {})['at'])).total_seconds()/3600
        except (KeyError,TypeError,ValueError):age=None
        if age is not None and age<INTERVAL_HOURS:return None
        if sum(1 for f in canonical_files(root) if MEMBER.fullmatch(f))>AUTO_MAX_NOTES:return None
        result=backup(root,budget=ASSET_BUDGET)
        atomic_write(marker,canonical_json({'at':datetime.now(timezone.utc).isoformat(),'backup':Path(result['backup']).name}))
        return result
    except Exception as exc:
        from .logs import swallowed
        swallowed('backup.maybe_backup',exc,root);return None

def staleness_warning(root,notes=None):
    """A sentence when there is no recent snapshot, else None. notes: the store's note count, when the caller
    has it, so a store too large for automatic snapshots is told to take them itself."""
    found=snapshots(root)
    manual=notes is not None and notes>AUTO_MAX_NOTES
    advice=f'run backup (stores above {AUTO_MAX_NOTES} notes are not snapshotted automatically)' if manual else 'run backup'
    if not found:return 'no backup yet; '+(advice if manual else 'run backup (hooks also take one daily)')
    hours=_age_hours(found[0])
    if hours>STALE_HOURS:return f'newest backup {found[0].name} is {int(hours)} hours old; {advice}'
    return None

def _keep_live_domains(root,text,notebooks):
    """The restored bindings.json, plus the live entry of every domain the snapshot lacks that still holds a notebook the
    snapshot does not cover, so restoring an older snapshot never drops a notebook-only domain."""
    try:restored=read_json_bytes(text.encode('utf-8'))
    except ValueError:return text
    try:live=read_json(Path(root)/'bindings.json',{}) or {}
    except ValueError:return text   # the live file is damaged: the restore is what repairs it
    if not isinstance(restored,dict) or not isinstance(restored.get('domains'),dict) or not isinstance(live.get('domains'),dict):return text
    added=False
    for name,entry in live['domains'].items():
        base=Path(root)/'domains'/name/'notebooks'
        held={f'{name}/{p.parent.name}' for p in base.glob('*/notebook.json')} if base.is_dir() else set()
        if name not in restored['domains'] and held-notebooks:restored['domains'][name]=entry;added=True
        old=restored['domains'].get(name)
        notes=any(MEMBER.fullmatch(f) for f in canonical_files_of(root,name))
        if isinstance(old,dict) and old.get('notebook_only') and isinstance(entry,dict) and not entry.get('notebook_only') and notes:
            restored['domains'][name]=entry;added=True   # converted to a memory domain since the snapshot, and its notes are still here
    return canonical_json(restored) if added else text

def canonical_files_of(root,domain):
    """The note files of one domain (notes/ and archive/)."""
    base=Path(root)/'domains'/domain
    return [p.relative_to(root).as_posix() for folder in ('notes','archive') if (base/folder).is_dir() for p in (base/folder).rglob('*.md') if p.is_file()]

def _merge_privacy(root,text):
    """The privacy registry only grows: a restore keeps every entry the live one has, so old private operations stay redacted."""
    from .docs import merge_privacy
    return merge_privacy(root,text)

def restore_plan(root,source):
    """restore_plan_unlocked under the store lock: the registry merge and the plan's preconditions are read together."""
    with store_lock(Path(root).resolve()):
        return restore_plan_unlocked(root,source)

def restore_plan_unlocked(root,source):
    """An exact-hash plan that makes the canonical set match a snapshot. Notes in domains the snapshot
    does not contain are left alone; apply the plan with inspect-plan and apply, and undo it like any operation."""
    from .governance import plan
    root=Path(root).resolve();source=Path(source)
    # Members that were unreadable when the snapshot was taken are neither restored nor a reason to delete
    # the file that is there now: a damaged copy must not replace, or remove, a possibly good one.
    skipped={problem['member'] for problem in verify(source)}
    with zipfile.ZipFile(source) as archive:
        # Attachments stay bytes (the plan stages them as blobs); everything else is UTF-8 text.
        wanted={info.filename:(archive.read(info) if ASSET.fullmatch(info.filename) else archive.read(info).decode('utf-8')) for info in archive.infolist() if info.filename not in skipped}
    manifest=wanted.pop(MANIFEST,None)
    for rel in LEGACY:wanted.pop(rel,None)
    try:manifest=read_json_bytes(manifest.encode('utf-8')) if manifest is not None else {}
    except ValueError:manifest={}
    # Version 1 snapshots (no manifest) cover no notebook, so a restore from one leaves every live notebook alone.
    notebooks=set(manifest.get('notebooks') or []) if isinstance(manifest,dict) and manifest.get('version')==2 else set()
    kept_omitted={o.get('path') for o in (manifest.get('omitted') or []) if isinstance(o,dict)} if isinstance(manifest,dict) else set()
    for rel in [r for r in wanted if _notebook(r) and _notebook(r) not in notebooks]:wanted.pop(rel)
    for rel in wanted:confined(root,rel)
    # Maintenance files never delete or lower what is there (merge_restore), and the restored configuration is disabled.
    snapshot_maint={rel:wanted.pop(rel) for rel in list(wanted) if MAINT.fullmatch(rel)}
    covered={rel.split('/')[1] for rel in wanted if MEMBER.fullmatch(rel)}
    # A domain that was bound but empty at snapshot time has no note members; its saved binding still covers it.
    try:saved=read_json_bytes(wanted['bindings.json'].encode('utf-8')).get('domains',{}) if 'bindings.json' in wanted else {}
    except (ValueError,AttributeError):saved={}
    # A notebook-only domain has no notes to restore, and reconcile refuses a domain without notes/.
    covered|={name for name,entry in saved.items() if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*',name) and not (isinstance(entry,dict) and entry.get('notebook_only'))}
    if 'bindings.json' in wanted:wanted['bindings.json']=_keep_live_domains(root,wanted['bindings.json'],notebooks)
    if 'notebook-privacy.json' in wanted:wanted['notebook-privacy.json']=_merge_privacy(root,wanted['notebook-privacy.json'])
    if 'notebook-folders.json' in wanted:   # a restore never reopens a folder notebook the user made hidden or private since
        from .folder_policy import merge_registry
        wanted['notebook-folders.json']=merge_registry(root,wanted['notebook-folders.json'])
    # Notes of covered domains, and the files of notebooks the snapshot lists; omitted attachments are never deleted.
    current=[rel for rel in canonical_files(root) if not MAINT.fullmatch(rel) and (rel in TOP_LEVEL or (MEMBER.fullmatch(rel) and rel.split('/')[1] in covered)
                                                                                    or (_notebook(rel) in notebooks and rel not in kept_omitted))]
    changes={};unchanged=0
    for rel,text in wanted.items():
        path=root/rel
        if path.is_dir():raise ValueError(f'Notebook conflict: {rel} is a folder in the store now; move or delete it, then make the restore plan again')
        for parent in list(Path(rel).parents)[:-1]:
            if (root/parent).is_file() and parent.as_posix() in wanted:continue
            if (root/parent).is_file():raise ValueError(f'Notebook conflict: {parent.as_posix()} is a file in the store now and {rel} needs it as a folder; move or delete it, then make the restore plan again')
        if path.is_file() and path.read_bytes()==(text if isinstance(text,bytes) else text.encode('utf-8')):unchanged+=1
        elif LEDGER.fullmatch(rel) and path.is_file() and not text.encode('utf-8').startswith(path.read_bytes()):unchanged+=1   # the ledger only grows: keep a copy that holds rows the snapshot lacks
        else:changes[rel]=text
    for rel in current:
        if rel not in wanted and rel not in skipped and rel not in TOP_LEVEL:changes[rel]=None
    if changes.get('note_types.json') is not None:
        # An older registry must not drop a custom type that notes left after the restore still use.
        from .type_registry import restore_registry
        changes['note_types.json']=restore_registry(root,changes)
    current_maint={}
    for rel in _maintenance_files(root):
        try:current_maint[rel]=(root/rel).read_bytes().decode('utf-8-sig')
        except (OSError,UnicodeDecodeError):pass   # unreadable: treated as missing, so a healthy snapshot copy can replace it
    # Also with an older snapshot that has no maintenance files: an enabled store must come back disabled.
    if snapshot_maint or current_maint:
        from .maintenance_state import merge_restore
        for rel,text in merge_restore(current_maint,snapshot_maint).items():
            if current_maint.get(rel)==text:unchanged+=1
            else:changes[rel]=text
    summary={'source':source.name,'write':sum(1 for v in changes.values() if v is not None),
             'delete':sum(1 for v in changes.values() if v is None),'unchanged':unchanged}
    if skipped:summary['skipped_unreadable']=sorted(skipped)
    from .docs import private_scope,redact_summary
    scope=private_scope(root,{k:v for k,v in changes.items() if isinstance(v,str)} | ({'notebook-privacy.json':wanted['notebook-privacy.json']} if 'notebook-privacy.json' in wanted else {}))
    if not changes:return {'ok':True,'status':'unchanged','summary':redact_summary(summary,scope)}
    if notebooks:summary['notebooks']=sorted(notebooks)
    # A domain the restore makes notebook-only must still hold no notes when the plan is applied.
    guards={}
    try:final=read_json_bytes(wanted['bindings.json'].encode('utf-8')).get('domains') or {} if 'bindings.json' in wanted else {}
    except (ValueError,AttributeError):final={}
    guards['no_notes']=sorted(n for n,e in final.items() if isinstance(e,dict) and e.get('notebook_only'))
    result=plan(root,changes,'restore',covered,summary,guards=guards or None)
    result['summary']=redact_summary(result.get('summary',summary),scope)
    result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
    return result
