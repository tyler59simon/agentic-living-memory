"""Reviewed import of Claude Code auto-memory files (#13): one plan of new notes, applied by digest.

Auto-memory keeps one topic file per memory under CLAUDE_HOME/projects/<encoded cwd>/memory, with frontmatter
`name`, `description` and `metadata.type` (user, feedback, project, reference) and an index in MEMORY.md.
Each file becomes one note, created exactly as capture would create it. The file's SHA-256 is the note's
capture key, so a re-run, or a run after the plan was applied, adds nothing.
"""
from __future__ import annotations
import hashlib,re,uuid
from pathlib import Path

MAX_BYTES=64*1024
TYPE_MAP={'user':'preference','feedback':'preference','project':'context','reference':'knowledge'}
KEY_PREFIX='auto-memory:'

def default_source(cwd):
    """Claude Code's auto-memory folder for a working directory: every character that is not a letter or
    digit becomes '-' (C:\\Users\\me\\Project -> C--Users-me-Project)."""
    from .instructions import harness_home
    return harness_home('claude')/'projects'/re.sub(r'[^A-Za-z0-9]','-',str(Path(cwd).resolve()))/'memory'

def parse(text):
    """(frontmatter dict, body) for the small YAML subset auto-memory writes: `key: value` lines and one
    level of nesting (`metadata:` then indented `type: ...`)."""
    text=text.replace('\r\n','\n').lstrip('\ufeff')
    if not text.startswith('---\n'):return {},text.strip()
    head,sep,body=text[4:].partition('\n---')
    if not sep:return {},text.strip()
    meta={};parent=None
    for line in head.split('\n'):
        if not line.strip() or line.lstrip().startswith('#'):continue
        indented=line[:1] in (' ','\t');key,colon,value=line.strip().partition(':')
        if not colon:continue
        value=value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in '"\'':value=value[1:-1]
        if indented and parent:meta.setdefault(parent,{})[key.strip()]=value
        elif not value:parent=key.strip();meta[parent]={}
        else:parent=None;meta[key.strip()]=value
    return meta,body.split('\n',1)[1].strip() if '\n' in body else ''

def _title(name,fallback):
    words=re.sub(r'[-_]+',' ',str(name or fallback)).strip()
    return (words[:1].upper()+words[1:]) if words else fallback

def _details(body):
    """Details bullets from a free-form body; a heading becomes a bold label."""
    lines=[]
    for line in body.split('\n'):
        stripped=line.strip()
        if not stripped or stripped=='---':continue
        heading=re.match(r'^#+\s*(.+)$',stripped)
        lines.append(f'**{heading[1].strip()}**' if heading else stripped)
    return lines

def ingest_plan(root,domain,source,redact=False,allow_similar=False):
    """A plan of new notes for the auto-memory files under source, plus what was skipped and why."""
    from .notes import build_note,similar_title,quality_warnings,taxonomy
    from .note_format import render
    from .repository import inventory,find_secrets,redact as redact_text
    from .governance import plan
    from .cli import _infer_tags
    root=Path(root).resolve();source=Path(source).expanduser()
    if not source.is_dir():raise ValueError(f'No auto-memory folder at {source}')
    existing=inventory(domain,root,retired=True);keys={n.get('capture_key'):n for n in existing if n.get('capture_key')}
    active=[n for n in existing if n.get('status','active')=='active']
    changes={};created=[];unchanged=[];skipped=[]
    for path in sorted(source.glob('*.md')):
        name=path.name
        if name.upper()=='MEMORY.MD':continue
        if path.is_symlink():skipped.append({'file':name,'reason':'symbolic link'});continue
        if path.stat().st_size>MAX_BYTES:skipped.append({'file':name,'reason':f'larger than {MAX_BYTES//1024} KB'});continue
        raw=path.read_bytes();key=KEY_PREFIX+hashlib.sha256(raw).hexdigest()
        if key in keys:unchanged.append({'file':name,'ref':f"{domain}:{keys[key]['id']}"});continue
        try:meta,body=parse(raw.decode('utf-8'))
        except UnicodeDecodeError:skipped.append({'file':name,'reason':'not UTF-8 text'});continue
        nested=meta.get('metadata') if isinstance(meta.get('metadata'),dict) else {}
        kind=str(nested.get('type') or meta.get('type') or '')
        note_type=TYPE_MAP.get(kind.strip().lower())
        if not note_type:skipped.append({'file':name,'reason':f'unknown auto-memory type {kind!r}'});continue
        title=_title(meta.get('name'),path.stem);summary=str(meta.get('description') or '').strip() or title
        text=render(summary,_details(body))
        item={'title':title,'body':text,'type':note_type,'tags':[]}
        found=find_secrets('\n'.join((title,text)))
        if found and not redact:
            skipped.append({'file':name,'reason':'credential: '+', '.join(sorted({k for k,_,_ in found}))+' (re-run with --redact to replace it)'});continue
        redacted=[]
        if found:
            item['title'],kinds=redact_text(item['title']);redacted+=kinds
            item['body'],kinds=redact_text(item['body']);redacted+=kinds
        inferred=_infer_tags(item,root)
        if not any(t.startswith('concern/') for t in item['tags']) and 'concern/knowledge' in taxonomy(root):
            # Context notes (auto-memory "project") have no type default; they record what is known.
            item['tags'].append('concern/knowledge');inferred.append('concern/knowledge')
        similar=None if allow_similar else similar_title(item['title'],active)
        if similar:
            skipped.append({'file':name,'reason':f"similar to {domain}:{similar['id']} \"{similar['title']}\"; update that note, or re-run with --allow-similar"});continue
        # The ID comes from the file's hash, so two proposals for the same file target the same path: once one
        # is applied, the other fails as stale instead of creating a second copy.
        note_id=str(uuid.uuid5(uuid.NAMESPACE_URL,key))
        try:target,note,serialized=build_note(item['title'],item['body'],item['tags'],domain,root,note_type,key=key,note_id=note_id)
        except ValueError as error:skipped.append({'file':name,'reason':str(error)});continue
        changes[target.relative_to(root).as_posix()]=serialized;active.append(note)
        entry={'file':name,'ref':f"{domain}:{note['id']}",'title':note['title'],'type':note_type,'tags':note['tags']}
        if inferred:entry['inferred_tags']=inferred
        if redacted:entry['redacted']=sorted(set(redacted))
        warnings=quality_warnings(note)
        if warnings:entry['quality_warnings']=warnings
        created.append(entry)
    result={'ok':True,'source':str(source),'domain':domain,'created':created,'unchanged':unchanged,'skipped':skipped}
    if changes:
        result.update(plan(root,changes,'ingest',[domain],{'source':str(source),'notes':len(changes)},
                           guards={'absent_ids':[c['ref'].split(':',1)[1] for c in created]}))
        result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
    else:result['plan_id']=None
    return result
