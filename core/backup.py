"""Point-in-time snapshots of the canonical store, and restore as one direct, journaled write (`restore-backup FILE`).

Journals make every script operation reversible; snapshots cover what journals cannot: a deleted or
mangled store folder (an external sync tool, a bad manual edit). Indexes, turn state, journals
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
           'notebook-folders.json')
# Members an older snapshot may hold that the store no longer keeps: verify accepts them and a restore drops them (Changes removed, CON-17;
# hosted notebooks removed, CON-18: their privacy registry and every file under domains/D/notebooks/).
LEGACY=('changes.json','notebook-privacy.json')
LEGACY_NOTEBOOK=re.compile(r'domains/[A-Za-z0-9_.-]+/notebooks/[^/\\]+/(?:[^/\\]+/)*[^/\\]+')
# ledger/ is append-only history with no other copy (journals do not hold it), so a snapshot saves it and a restore only adds to it.
LEDGER=re.compile(r'ledger/sessions-\d{4}\.jsonl')
MAINT=re.compile(r'maintenance\.json|maintenance/(?:state\.json|runs/\d{4}-\d{2}-\d{2}/[A-Za-z0-9_-]{1,100}\.json)')
MEMBER=re.compile(r'domains/[A-Za-z0-9_.-]+/(?:notes|archive)/(?:[^/\\]+/)*[^/\\]+\.md')
MANIFEST='manifest.json'
NAME=re.compile(r'lm-(\d{8}-\d{6})(?:-(\d+))?\.zip')

def backup_dir(root):return Path(root)/'backups'

def canonical_files(root):
    """Store-relative POSIX paths of the canonical set."""
    from .paths import is_link_or_junction
    root=Path(root)
    found=[name for name in TOP_LEVEL if (root/name).is_file() and not is_link_or_junction(root/name)]
    if (root/'ledger').is_dir() and not is_link_or_junction(root/'ledger'):found+=sorted(p.relative_to(root).as_posix() for p in (root/'ledger').glob('sessions-*.jsonl') if LEDGER.fullmatch(p.relative_to(root).as_posix()) and p.is_file() and not is_link_or_junction(p))
    found+=_maintenance_files(root)
    for domain in sorted((root/'domains').iterdir()) if (root/'domains').is_dir() and not is_link_or_junction(root/'domains') else []:
        if is_link_or_junction(domain):continue
        for folder in ('notes','archive'):
            base=domain/folder
            if base.is_dir() and not is_link_or_junction(base):
                found+=sorted(p.relative_to(root).as_posix() for p in base.rglob('*.md') if p.is_file() and not is_link_or_junction(p) and not is_os_junk(p))
    return found

def _maintenance_files(root):
    """maintenance.json, the accounting checkpoint and the run records; never worker.lock."""
    from .paths import is_link_or_junction
    found=[]
    if (root/'maintenance.json').is_file() and not is_link_or_junction(root/'maintenance.json'):found.append('maintenance.json')
    base=root/'maintenance'
    if base.is_dir() and not is_link_or_junction(base):
        found+=sorted(rel for rel in (p.relative_to(root).as_posix() for p in base.rglob('*.json') if p.is_file() and not is_link_or_junction(p)) if MAINT.fullmatch(rel))
    return found

def _allowed(name):
    if not name or name.startswith('/') or '\\' in name or any(part in ('','.','..') for part in name.split('/')):return False
    return name in TOP_LEVEL or name in LEGACY or bool(LEGACY_NOTEBOOK.fullmatch(name)) or name==MANIFEST or bool(MEMBER.fullmatch(name)) or bool(LEDGER.fullmatch(name)) or bool(MAINT.fullmatch(name))

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

def backup(root,keep=KEEP):
    """Write and verify a snapshot, then keep the newest `keep`. A note that cannot be read as a note is still
    saved byte for byte and reported under `unreadable`: one damaged file must not stop every backup."""
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
            files=canonical_files(root)
            manifest={'version':3,'notes':True}
            with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as archive:
                for rel in files:archive.writestr(rel,(root/rel).read_bytes())
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
    warnings=[]
    if unreadable:
        result['unreadable']=unreadable
        warnings.append(f'{len(unreadable)} file(s) are not readable notes or configuration; they were saved as they are (run doctor)')
    if retained:
        result['retained']=retained
        warnings.append(f'this snapshot has {notes} note(s), far fewer than {len(retained)} older snapshot(s), which were kept; '
                        'if notes are missing, restore-backup one of them, and if the drop is intended, delete them by hand')
    if warnings:result['warning']='; '.join(warnings)
    return result

MAX_MEMBERS=50000
MAX_MEMBER_BYTES=16*1024*1024
MAX_TOTAL_BYTES=256*1024*1024

def _read_zip_member(archive,info,remaining):
    """Read one ZIP member with hard per-member and total uncompressed byte caps (never trusting spoofable info.file_size)."""
    cap=min(MAX_MEMBER_BYTES,remaining)
    with archive.open(info) as handle:
        data=handle.read(cap+1)
    if len(data)>MAX_MEMBER_BYTES:raise ValueError(f'Backup member exceeds size cap: {info.filename}')
    if len(data)>remaining:raise ValueError('Backup uncompressed size exceeds total cap')
    return data

def verify(path):
    """[{member, problem}] for members that are not readable: a note that is not UTF-8 or has no parseable frontmatter, a page
    that is not UTF-8, invalid JSON, an attachment over the size cap. Raises ValueError for a member outside the canonical set."""
    from .paths import name_key
    found=[];seen={};remaining=MAX_TOTAL_BYTES
    with zipfile.ZipFile(path) as archive:
        infos=archive.infolist()
        if len(infos)>MAX_MEMBERS:raise ValueError('Backup has too many members')
        for info in infos:
            if info.is_dir() or not _allowed(info.filename):raise ValueError(f'Backup member outside the canonical set: {info.filename}')
            key=name_key(info.filename)
            if key in seen:raise ValueError(f'Backup members {seen[key]} and {info.filename} name the same file')
            seen[key]=info.filename
        for info in infos:
            if LEGACY_NOTEBOOK.fullmatch(info.filename):continue   # dropped by a restore: not read
            data=_read_zip_member(archive,info,remaining);remaining-=len(data)
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
        result=backup(root)
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

def restore_backup(root,source):
    """One direct, journaled write that makes the canonical set match a snapshot, read and written under one store lock.
    Notes in domains the snapshot does not contain are left alone; `undo OPERATION` reverses it like any operation."""
    with store_lock(Path(root).resolve()):
        return _restore_backup(root,source)

def _restore_backup(root,source):
    from .governance import commit
    root=Path(root).resolve();source=Path(source)
    # Members that were unreadable when the snapshot was taken are neither restored nor a reason to delete
    # the file that is there now: a damaged copy must not replace, or remove, a possibly good one.
    skipped={problem['member'] for problem in verify(source)}
    remaining=MAX_TOTAL_BYTES;wanted={}
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            if info.filename in skipped or LEGACY_NOTEBOOK.fullmatch(info.filename):continue
            raw=_read_zip_member(archive,info,remaining);remaining-=len(raw)
            wanted[info.filename]=raw.decode('utf-8')
    wanted.pop(MANIFEST,None)
    for rel in LEGACY:wanted.pop(rel,None)
    for rel in wanted:confined(root,rel)
    # Maintenance files never delete or lower what is there (merge_restore), and the restored configuration is disabled.
    snapshot_maint={rel:wanted.pop(rel) for rel in list(wanted) if MAINT.fullmatch(rel)}
    covered={rel.split('/')[1] for rel in wanted if MEMBER.fullmatch(rel)}
    # A domain that was bound but empty at snapshot time has no note members; its saved binding still covers it.
    try:saved=read_json_bytes(wanted['bindings.json'].encode('utf-8')).get('domains',{}) if 'bindings.json' in wanted else {}
    except (ValueError,AttributeError):saved={}
    covered|={name for name in saved if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*',name)}
    if 'bindings.json' in wanted:   # restored stores never inherit enabled paid digest refresh (REL-S5)
        try:
            bcfg=read_json_bytes(wanted['bindings.json'].encode('utf-8'))
            if isinstance(bcfg,dict) and isinstance(bcfg.get('digest_refresh'),dict) and bcfg['digest_refresh'].get('enabled'):
                bcfg['digest_refresh']=dict(bcfg['digest_refresh'],enabled=False)
                wanted['bindings.json']=canonical_json(bcfg)
        except ValueError:pass
    elif (root/'bindings.json').is_file():
        try:
            bcfg=read_json_bytes((root/'bindings.json').read_bytes())
            if isinstance(bcfg,dict) and isinstance(bcfg.get('digest_refresh'),dict) and bcfg['digest_refresh'].get('enabled'):
                bcfg['digest_refresh']=dict(bcfg['digest_refresh'],enabled=False)
                wanted['bindings.json']=canonical_json(bcfg)
        except (OSError,ValueError):pass
    if 'notebook-folders.json' in wanted:   # a restore never reopens a folder notebook the user made hidden or private since
        from .folder_policy import merge_registry
        wanted['notebook-folders.json']=merge_registry(root,wanted['notebook-folders.json'])
    # Notes of covered domains and the top-level files.
    current=[rel for rel in canonical_files(root) if not MAINT.fullmatch(rel) and (rel in TOP_LEVEL or (MEMBER.fullmatch(rel) and rel.split('/')[1] in covered))]
    changes={};unchanged=0
    for rel,text in wanted.items():
        path=root/rel
        if path.is_dir():raise ValueError(f'Restore conflict: {rel} is a folder in the store now; move or delete it, then restore again')
        for parent in list(Path(rel).parents)[:-1]:
            if (root/parent).is_file() and parent.as_posix() in wanted:continue
            if (root/parent).is_file():raise ValueError(f'Restore conflict: {parent.as_posix()} is a file in the store now and {rel} needs it as a folder; move or delete it, then restore again')
        if path.is_file() and path.read_bytes()==(text if isinstance(text,bytes) else text.encode('utf-8')):unchanged+=1
        elif LEDGER.fullmatch(rel) and path.is_file() and not text.encode('utf-8').startswith(path.read_bytes()):unchanged+=1   # the ledger only grows: keep a copy that holds rows the snapshot lacks
        else:changes[rel]=text
    for rel in current:
        if rel not in wanted and rel not in skipped and rel not in TOP_LEVEL:changes[rel]=None
    for rel in changes:   # undo writes text: a file it could not put back is moved aside by the user first, never lost
        try:(root/rel).is_file() and (root/rel).read_bytes().decode('utf-8')
        except UnicodeDecodeError:raise ValueError(f'Restore conflict: {rel} in the store is not UTF-8 text, so undo could not put it back; move it out of the store, then restore again') from None
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
    from .docs import redact_summary
    if not changes:return {'ok':True,'status':'unchanged','summary':redact_summary(summary)}
    # The store as the restore leaves it must not hold a note whose custom type is unregistered or takes no action items.
    from .type_registry import check_undo
    try:check_undo(root,changes)
    except ValueError as exc:raise ValueError('Restore would break notes that use a custom type: '+str(exc)+'; retype those notes first') from None
    result=commit(root,changes,'restore',covered,summary)
    result['summary']=redact_summary(result.get('summary',summary))
    return result
