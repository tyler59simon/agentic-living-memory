"""Point-in-time snapshots of the canonical store, and restore as an exact-hash approved plan.

Journals make every script operation reversible; snapshots cover what journals cannot: a deleted or
mangled store folder (an external sync tool, a bad manual edit). Indexes, turn state, journals, plans
and staged files are derived or transient and are not included."""
from __future__ import annotations
import os,re,zipfile
from datetime import datetime,timezone
from pathlib import Path
from .common import atomic_write,retry_sharing
from .repository import canonical_json,confined,parse_text,read_json,store_lock

KEEP=7
# Rotation never deletes a snapshot holding at least twice the notes of the new one (and this many more):
# a store that shrank that much may have lost a folder, and that snapshot may be the last good copy.
SHRINK_MIN=5
INTERVAL_HOURS=24
STALE_HOURS=48
# Hooks skip automatic snapshots of very large stores; doctor already flags that size.
AUTO_MAX_NOTES=1500
TOP_LEVEL=('bindings.json','tag_taxonomy.json','projects.json','systems.json','schema-version.json','learning.json')
MEMBER=re.compile(r'domains/[A-Za-z0-9_.-]+/(?:notes|archive)/(?:[^/\\]+/)*[^/\\]+\.md')
NAME=re.compile(r'lm-(\d{8}-\d{6})(?:-(\d+))?\.zip')

def backup_dir(root):return Path(root)/'backups'

def canonical_files(root):
    """Store-relative POSIX paths of the canonical set."""
    root=Path(root)
    found=[name for name in TOP_LEVEL if (root/name).is_file()]
    for domain in sorted((root/'domains').iterdir()) if (root/'domains').is_dir() else []:
        for folder in ('notes','archive'):
            base=domain/folder
            if base.is_dir() and not base.is_symlink():
                found+=sorted(p.relative_to(root).as_posix() for p in base.rglob('*.md') if p.is_file() and not p.is_symlink())
        learning=domain/'learning'
        if learning.is_dir() and not learning.is_symlink():
            for p in sorted(learning.rglob('*.json')):
                rel=p.relative_to(root).as_posix()
                confined(root,rel)
                if _allowed(rel):found.append(rel)
    return found

def _allowed(name):
    learning=re.fullmatch(r'domains/[A-Za-z0-9_.-]+/learning/(?:tasks|attempts|evidence|candidates|procedures|versions|uses|feedback)/[a-f0-9.-]+\.json',name)
    return (name in TOP_LEVEL or bool(MEMBER.fullmatch(name)) or bool(learning)) and '..' not in name.split('/')

def snapshots(root):
    """Existing snapshots, newest first: by timestamp, then by same-second sequence (lm-T.zip, lm-T-1.zip, ...)."""
    folder=backup_dir(root)
    if not folder.is_dir():return []
    def order(p):
        m=NAME.fullmatch(p.name);return (m[1],int(m[2] or 0))
    return sorted((p for p in folder.glob('lm-*.zip') if NAME.fullmatch(p.name)),key=order,reverse=True)

def _note_count(path):
    try:
        with zipfile.ZipFile(path) as archive:return sum(1 for name in archive.namelist() if name.endswith('.md'))
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
            with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as archive:
                for rel in files:archive.writestr(rel,(root/rel).read_bytes())
            unreadable=verify(temp)
            retry_sharing(lambda:os.replace(temp,target))
        finally:
            if temp.exists():retry_sharing(temp.unlink)
        notes=sum(1 for f in files if f.endswith('.md'))
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
                        'if notes are missing, restore-plan one of them, and if the drop is intended, delete them by hand')
    if warnings:result['warning']='; '.join(warnings)
    return result

def verify(path):
    """[{member, problem}] for members that are not readable notes or configuration: not UTF-8, no parseable
    frontmatter, invalid JSON. Raises ValueError for a member outside the canonical set."""
    found=[]
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not _allowed(info.filename):raise ValueError(f'Backup member outside the canonical set: {info.filename}')
            data=archive.read(info)
            if info.filename.endswith('.md'):
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
        try:age=(datetime.now(timezone.utc)-datetime.fromisoformat((read_json(marker,{}) or {})['at'])).total_seconds()/3600
        except (KeyError,TypeError,ValueError):age=None
        if age is not None and age<INTERVAL_HOURS:return None
        if sum(1 for f in canonical_files(root) if f.endswith('.md'))>AUTO_MAX_NOTES:return None
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

def restore_plan(root,source):
    """An exact-hash plan that makes the canonical set match a snapshot. Notes in domains the snapshot
    does not contain are left alone; apply the plan with inspect-plan and apply, and undo it like any operation."""
    from .governance import plan
    root=Path(root).resolve();source=Path(source)
    # Members that were unreadable when the snapshot was taken are neither restored nor a reason to delete
    # the file that is there now: a damaged copy must not replace, or remove, a possibly good one.
    skipped={problem['member'] for problem in verify(source)}
    with zipfile.ZipFile(source) as archive:
        wanted={info.filename:archive.read(info).decode('utf-8') for info in archive.infolist() if info.filename not in skipped}
    for rel in wanted:confined(root,rel)
    covered={rel.split('/')[1] for rel in wanted if rel.startswith('domains/')}
    # A domain that was bound but empty at snapshot time has no note members; its saved binding still covers it.
    try:saved=read_json_bytes(wanted['bindings.json'].encode('utf-8')).get('domains',{}) if 'bindings.json' in wanted else {}
    except (ValueError,AttributeError):saved={}
    covered|={name for name in saved if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*',name)}
    current=[rel for rel in canonical_files(root) if rel in TOP_LEVEL or rel.split('/')[1] in covered]
    changes={};unchanged=0
    for rel,text in wanted.items():
        path=root/rel
        if path.is_file() and path.read_bytes()==text.encode('utf-8'):unchanged+=1
        else:changes[rel]=text
    for rel in current:
        if rel not in wanted and rel not in skipped and rel not in TOP_LEVEL:changes[rel]=None
    summary={'source':source.name,'write':sum(1 for v in changes.values() if v is not None),
             'delete':sum(1 for v in changes.values() if v is None),'unchanged':unchanged}
    if skipped:summary['skipped_unreadable']=sorted(skipped)
    if not changes:return {'ok':True,'status':'unchanged','summary':summary}
    result=plan(root,changes,'restore',covered,summary)
    result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
    return result
