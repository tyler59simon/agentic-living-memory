"""Point-in-time snapshots of the canonical store, and restore as an exact-hash approved plan.

Journals make every script operation reversible; snapshots cover what journals cannot: a deleted or
mangled store folder (an external sync tool, a bad manual edit). Indexes, turn state, journals, plans
and staged files are derived or transient and are not included."""
from __future__ import annotations
import os,re,zipfile
from datetime import datetime,timezone
from pathlib import Path
from .common import atomic_write
from .repository import canonical_json,confined,parse_text,read_json,store_lock

KEEP=7
INTERVAL_HOURS=24
STALE_HOURS=48
# Hooks skip automatic snapshots of very large stores; doctor already flags that size.
AUTO_MAX_NOTES=1500
TOP_LEVEL=('bindings.json','tag_taxonomy.json','projects.json','schema-version.json')
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
    return found

def _allowed(name):
    return name in TOP_LEVEL or bool(MEMBER.fullmatch(name)) and '..' not in name.split('/')

def snapshots(root):
    """Existing snapshots, newest first: by timestamp, then by same-second sequence (lm-T.zip, lm-T-1.zip, ...)."""
    folder=backup_dir(root)
    if not folder.is_dir():return []
    def order(p):
        m=NAME.fullmatch(p.name);return (m[1],int(m[2] or 0))
    return sorted((p for p in folder.glob('lm-*.zip') if NAME.fullmatch(p.name)),key=order,reverse=True)

def backup(root,keep=KEEP):
    """Write and verify a snapshot, then keep the newest `keep`."""
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
            verify(temp)
            os.replace(temp,target)
        finally:
            if temp.exists():temp.unlink()
        removed=[]
        for old in snapshots(root)[max(1,keep):]:
            old.unlink();removed.append(old.name)
    notes=sum(1 for f in files if f.endswith('.md'))
    return {'ok':True,'backup':str(target),'notes':notes,'files':len(files),'pruned':removed}

def verify(path):
    """Every member is in the canonical set, and every note parses. Raises ValueError otherwise."""
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not _allowed(info.filename):raise ValueError(f'Backup member outside the canonical set: {info.filename}')
            data=archive.read(info)
            if info.filename.endswith('.md'):
                try:text=data.decode('utf-8')
                except UnicodeError as exc:raise ValueError(f'Backup note is not UTF-8: {info.filename}') from exc
                if parse_text(text,info.filename) is None:raise ValueError(f'Backup note does not parse: {info.filename}')
            else:
                try:read_json_bytes(data)
                except ValueError as exc:raise ValueError(f'Backup configuration is invalid: {info.filename}') from exc

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
        last=read_json(marker,{}) or {}
        try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(last['at'])).total_seconds()/3600
        except (KeyError,TypeError,ValueError):age=None
        if age is not None and age<INTERVAL_HOURS:return None
        if sum(1 for f in canonical_files(root) if f.endswith('.md'))>AUTO_MAX_NOTES:return None
        result=backup(root)
        atomic_write(marker,canonical_json({'at':datetime.now(timezone.utc).isoformat(),'backup':Path(result['backup']).name}))
        return result
    except Exception as exc:
        from .logs import swallowed
        swallowed('backup.maybe_backup',exc,root);return None

def staleness_warning(root):
    found=snapshots(root)
    if not found:return 'no backup yet; run backup (hooks also take one daily)'
    hours=_age_hours(found[0])
    if hours>STALE_HOURS:return f'newest backup {found[0].name} is {int(hours)} hours old; run backup'
    return None

def restore_plan(root,source):
    """An exact-hash plan that makes the canonical set match a snapshot. Notes in domains the snapshot
    does not contain are left alone; apply the plan with inspect-plan and apply, and undo it like any operation."""
    from .governance import plan
    root=Path(root).resolve();source=Path(source)
    verify(source)
    with zipfile.ZipFile(source) as archive:
        wanted={info.filename:archive.read(info).decode('utf-8') for info in archive.infolist()}
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
        if rel not in wanted and rel not in TOP_LEVEL:changes[rel]=None
    summary={'source':source.name,'write':sum(1 for v in changes.values() if v is not None),
             'delete':sum(1 for v in changes.values() if v is None),'unchanged':unchanged}
    if not changes:return {'ok':True,'status':'unchanged','summary':summary}
    result=plan(root,changes,'restore',covered,summary)
    result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
    return result
