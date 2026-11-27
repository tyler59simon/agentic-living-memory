"""Pages of folder notebooks (CON-9): `doc read --file` and `doc propose --file` for the Markdown files of a registered folder
outside the store (notebook-folders.json, folder_policy).

Agents never write such a page. They stage the whole new text as a proposal under `domains/D/folder-proposals/<nb>/<id>.json`
(journaled and undoable) and the user settles it in Hivemind. An accept writes the page with
safe_put._put, which keeps its bytes (CRLF, BOM) and never overwrites a save made meanwhile; an undo puts the old bytes back
while the page still holds what the accept wrote. Folder proposals are bookkeeping for history: docs.BOOKKEEPING hides them
unless --include-private."""
from __future__ import annotations
import base64,hashlib,json,os,uuid
from pathlib import Path
from . import docs
from .folder_policy import REGISTRY,Policy,_strictest,key,level_of,load,real
from .repository import digest,now,store_lock
from .safe_put import _bytes,_put,_safe,_sha

SUFFIXES=('.md','.markdown')
KEEP_BYTES=1024*1024   # a proposal keeps the page it was made against, and an accept the page it replaced, up to this size
_REGISTRY={}   # store root -> (registry stamp, {compare key: entry}): the hook asks on every edit


def registered(root):
    """{compare key of the resolved folder: entry} of the registered folder notebooks that are not removed. Cached until the
    registry file changes. A damaged registry raises ValueError."""
    path=Path(root)/REGISTRY
    try:
        info=os.stat(path)
        stamp=(info.st_mtime_ns,info.st_size)
    except OSError:stamp=None
    held=_REGISTRY.get(str(root))
    if held and held[0]==stamp:return held[1]
    found={} if stamp is None else {k:e for k,e in _strictest(load(root)['folders']).items() if not e.get('removed')}
    _REGISTRY[str(root)]=(stamp,found)
    return found


def _no_dot_parts(rel):
    return not any(part.startswith('.') for part in rel.split('/'))


def locate(root,file,cwd=None):
    """(entry, real folder, page path relative to it) of the nearest registered folder notebook above `file`, or None. The file
    need not exist. Only .md and .markdown files outside dot-folders are pages; any other file gives None."""
    if not str(file).casefold().endswith(SUFFIXES):return None
    found=registered(root)
    if not found:return None
    path=Path(os.path.expandvars(str(file))).expanduser()
    path=Path(os.path.abspath(path if path.is_absolute() else Path(cwd or os.getcwd())/path))
    def find_in(p):
        for folder in p.parents:
            entry=found.get(key(real(folder)))
            if entry is not None:
                rel=p.relative_to(folder).as_posix()
                if not _no_dot_parts(rel):return None
                return entry,Path(real(folder)),rel
        return None
    hit=find_in(path)
    if hit is not None:return hit
    resolved=Path(real(path))
    if key(resolved)!=key(path):return find_in(resolved)
    return None


def notebook_id(folder):
    """The id docs_index and sources give a folder notebook: nb- and 10 hex digits of its compare key."""
    return 'nb-'+hashlib.sha256(key(real(folder)).encode()).hexdigest()[:10]


def resolve(root,file,cwd=None,include_private=False):
    """The folder notebook page `file` names, as a dict (domain, folder, nb, rel, full, level). A private folder looks like no
    folder at all unless include_private, so its path is not given away."""
    missing=f'not_found: {file} is not a page of a registered folder notebook (a .md or .markdown file in one)'
    hit=locate(root,file,cwd)
    if hit is None:raise ValueError(missing)
    entry,folder,rel=hit
    level=Policy(root).file(folder/rel)[0]
    if 'private' in (level,level_of(entry.get('visibility'),bool(entry.get('private')))) and not include_private:raise ValueError(missing)
    full=_safe(folder,rel)
    if full is None:raise ValueError('Links and junctions are not allowed inside a notebook')
    if not full.exists():
        for part in rel.split('/'):docs._check_part(part)   # a new page's name must be one every system can hold
    return {'domain':entry['domain'],'folder':entry['path'],'nb':notebook_id(folder),'rel':rel,'full':full,'base':folder}


def read_file(root,file,cwd=None,max_chars=None,include_private=False):
    hit=resolve(root,file,cwd,include_private)
    try:data=hit['full'].read_bytes()
    except FileNotFoundError:raise ValueError(f"not_found: no page {hit['rel']}") from None
    text=data.decode('utf-8',errors='replace')
    result={'ok':True,'origin':'folder','domain':hit['domain'],'notebook':hit['nb'],'folder':hit['folder'],'path':hit['rel'],'file':hit['full'].as_posix(),
            'text':text,'revision':digest(data),'size':len(data),'updated':docs._iso(hit['full'].stat().st_mtime)}
    if max_chars is not None and len(text)>max_chars:result.update(text=text[:max_chars],truncated=True)
    return result


def _b64(data):return base64.b64encode(data).decode('ascii') if data is not None and len(data)<=KEEP_BYTES else None


def propose_file(root,file,body,cwd=None,base_revision=None,note=None,session=None,include_private=False):
    """Stage an edit of a folder notebook page for the user (agents may run it): the base revision must match, a page is at most 4 MiB, and the page as it
    was read is kept with the proposal so Hivemind can merge against it."""
    root=Path(root).resolve()
    warnings=docs._check_body(body)
    if note is not None and (not isinstance(note,str) or len(note)>2000):raise ValueError('A proposal note is at most 2000 characters')
    with store_lock(root):
        hit=resolve(root,file,cwd,include_private)
        data=_bytes(hit['full'])
        current=_sha(data)
        if current is not None and base_revision is None:raise ValueError('--base-revision is required for a page that exists: read it first')
        if base_revision is not None and current!=base_revision:raise ValueError(f"stale_revision: {hit['rel']} changed since revision {base_revision[:12]}; read it again")
        pid=uuid.uuid4().hex[:12]
        domain,nb=hit['domain'],hit['nb']
        value={'schema':1,'kind':'folder','id':pid,'domain':domain,'notebook':nb,'folder':hit['folder'],'path':hit['rel'],'base_revision':base_revision,
               'base':_b64(data),'body':body.decode('utf-8'),'note':note,'created':now(),'status':'pending',
               'session':hashlib.sha256(session.encode()).hexdigest()[:16] if session else None}
        rel=_rel(domain,nb,pid)
        receipt=docs._commit(root,{rel:docs._proposal_text(value)},'doc_propose',_meta(domain,nb),{'domain':domain,'notebook':nb,'path':hit['rel'],'proposal':pid},{rel:None})
        receipt.update(id=pid,status='proposed',next=f"The user reviews it with proposals accept {pid} or proposals reject {pid} (or in a connected UI)")
        if warnings:receipt.setdefault('warnings',[]).extend(warnings)
        return receipt


def _rel(domain,nb,pid):return f'domains/{domain}/folder-proposals/{nb}/{pid}.json'


def _meta(domain,nb,**more):return {'domain':domain,'notebook':nb,'folder_notebook':True,**more}


def _page(value,strict=True):
    """(page path relative to the folder, checked) of a stored proposal: the file is outside the store, so a hand-edited path
    must not climb out of the folder. strict=False checks traversal/dot-segments/suffix without rejecting pre-existing names."""
    rel=value.get('path')
    if not isinstance(rel,str) or not rel or '\\' in rel or '\x00' in rel or not rel.casefold().endswith(SUFFIXES):
        raise ValueError('The proposal does not name a Markdown page')
    parts=rel.split('/')
    if any(part in ('','.','..') or part.startswith('.') for part in parts):
        raise ValueError('The proposal does not name a Markdown page')
    if strict:
        for part in parts:docs._check_part(part)
    return rel


def folder_of(root,value):
    """(real folder, entry) of the registered folder notebook a proposal is for; (None, None) when it is no longer registered."""
    folder=value.get('folder')
    if not isinstance(folder,str):return None,None
    try:entry=registered(root).get(key(real(folder)))
    except ValueError:entry=None
    return (Path(real(folder)),entry) if entry is not None else (None,None)


def hidden(root,value,include_private=False,policy=None):
    """Whether the folder or page of a proposal is private now (a removed private folder, private subfolder or damaged registry
    counts too): it is then not named or shown."""
    if include_private:return False
    if not isinstance(value,dict) or not isinstance(value.get('folder'),str):return True
    try:
        p=policy or Policy(root)
        if p.broken:return True
        folder=real(value['folder'])
        if p.folder(folder)[0]=='private':return True
        rel=value.get('path')
        if isinstance(rel,str) and rel:
            if '\\' in rel or '\x00' in rel or any(part in ('','.','..') for part in rel.split('/')):return True
            if p.file(os.path.join(folder,*rel.split('/')))[0]=='private':return True
        return False
    except (OSError,ValueError):return True


def check_visible(root,value,include_private=False):
    if hidden(root,value,include_private):raise ValueError(f"not_found: no proposal {value.get('id')}")


def _old_hash(value):
    return value['previous_revision'] if 'previous_revision' in value else value.get('base_revision')


def _old_state(value):
    """(hash, bytes or None) of the page before an accepted proposal wrote it. The bytes are None for a new page and for a page
    too large to keep."""
    if 'previous_revision' in value:
        return value['previous_revision'],(base64.b64decode(value['previous']) if value.get('previous') else None)
    return value.get('base_revision'),(base64.b64decode(value['base']) if value.get('base') else None)


def rows(root,domains,nb=None,include_private=False,status='pending',session=None):
    """Listing rows of the folder proposals of these domains. Orphaned: the folder is not registered any more. Applied: for an
    accepted one, whether the page was written (it is not when the process stopped between the two steps; proposals accept finishes it)."""
    out=[]
    policy=Policy(root)
    wanted=hashlib.sha256(session.encode()).hexdigest()[:16] if session else None
    for domain in domains:
        for file in sorted(Path(root).glob(f'domains/{domain}/folder-proposals/*/*.json')):
            if not docs.PROPOSAL_ID.fullmatch(file.stem) or (nb and file.parts[-2]!=nb):continue
            cursor=Path(root)
            if any(docs._is_link(cursor:=cursor/part) for part in ('domains',domain,'folder-proposals',file.parts[-2])) or docs._is_link(file):continue
            value=docs._read_proposal(file)
            if not isinstance(value,dict) or value.get('kind')!='folder' or (status!='all' and value.get('status')!=status):continue
            if (wanted and value.get('session')!=wanted) or hidden(root,value,include_private,policy):continue
            folder,entry=folder_of(root,value)
            safe=None
            if folder is not None:
                try:safe=_safe(folder,_page(value,strict=False))
                except ValueError:safe=None
            row={k:value.get(k) for k in ('id','domain','notebook','path','base_revision','note','created','status','reason','settled','edited','folder')}
            row.update(domain=domain,notebook=file.parts[-2],size=len(str(value.get('body') or '').encode()),orphaned=entry is None,
                       file=safe.as_posix() if safe is not None else None,applied=False)
            if value.get('status')=='accepted':
                try:current=_sha(_bytes(safe)) if safe is not None else None
                except OSError:current=None
                row['applied']=not (safe is not None and current==_old_hash(value) and current!=value.get('revision'))
            out.append(row)
    return out


def accept(root,pid,file,value,expected=None,include_private=False,body=None):
    """Write a folder proposal's text to its page (docs.accept calls it under the store lock).

    Order: settle the record (journaled, `revision` written), then write the page, then, if the write finds the page changed
    meanwhile, undo the settlement and refuse. Running it again on an accepted record whose page was never written finishes the
    write. With `body` (--edited) the user's merge is written instead, against `expected`; the old bytes are then kept in the record."""
    from .governance import undo
    check_visible(root,value,include_private)
    status=value.get('status')
    if status not in ('pending','accepted'):raise ValueError(f'Proposal {pid} is {status}')
    folder,_=folder_of(root,value)
    if folder is None:raise ValueError(f"not_found: the folder notebook {value.get('folder')} is no longer registered; reject proposal {pid}")
    rel=_page(value,strict=False)
    path=_safe(folder,rel)
    if path is None:raise ValueError('Links and junctions are not allowed inside a notebook')
    if not path.exists():
        for part in rel.split('/'):docs._check_part(part)
    domain,nb=file.parts[-4],file.parts[-2]  # where _find_proposal found it, not what the record says
    prel=_rel(domain,nb,pid)
    data=_bytes(path)
    current=_sha(data)
    if status=='accepted':
        old,_=_old_state(value)
        if current==value.get('revision'):return {'ok':True,'unchanged':True,'revision':current}
        if value.get('edited'):raise ValueError(f"Proposal {pid} was accepted with an edit and its page was not written; undo the accept and accept it again")
        if current!=old:raise ValueError(f'stale_revision: {rel} changed since the accept; undo the accept or write the merge yourself')
        text=str(value.get('body') or '').encode('utf-8')
        if not _put(folder,rel,text,current):raise ValueError(f'stale_revision: {rel} changed while the proposal was applied; read it again')
        return {'ok':True,'revision':digest(text),'finished':True}
    warnings=docs._check_body(body) if body is not None else []
    edited=body is not None
    if edited:
        if current!=expected:
            raise ValueError(f'stale_revision: {rel} changed since the merge was made; read it again and merge again' if expected else f'stale_revision: {rel} exists now; read it and merge again')
    elif current!=value.get('base_revision'):raise ValueError(f'stale_revision: {rel} changed since the proposal was made; read both and write the merge')
    elif expected is not None and current!=expected:raise ValueError(f'stale_revision: {rel} is not at revision {expected[:12]}')
    text=body if edited else str(value.get('body') or '').encode('utf-8')
    settled=dict(value,domain=domain,notebook=nb,status='accepted',settled=now(),revision=digest(text),**({'edited':True,'previous_revision':current,'previous':_b64(data)} if edited else {}))
    receipt=docs._commit(root,{prel:docs._proposal_text(settled)},'doc_accept',_meta(domain,nb,proposal=pid),{'domain':domain,'notebook':nb,'path':rel,'proposal':pid,**({'edited':True} if edited else {})},
                         {prel:digest(file.read_bytes())})
    try:written=_put(folder,rel,text,current)
    except BaseException:
        undo(root,receipt['operation_id'],True,revert_files=False)
        raise
    if not written:
        undo(root,receipt['operation_id'],True,revert_files=False)
        raise ValueError(f'stale_revision: {rel} changed while the proposal was applied; read it again')
    receipt['revision']=digest(text)
    if warnings:receipt.setdefault('warnings',[]).extend(warnings)
    return receipt


def reject(root,pid,file,value,reason=None,include_private=False):
    check_visible(root,value,include_private)
    if value.get('status')!='pending':raise ValueError(f"Proposal {pid} is {value.get('status')}")
    domain,nb=file.parts[-4],file.parts[-2]  # where _find_proposal found it, not what the record says
    prel=_rel(domain,nb,pid)
    settled=dict(value,domain=domain,notebook=nb,status='rejected',settled=now(),reason=(reason or '')[:200] or None)
    return docs._commit(root,{prel:docs._proposal_text(settled)},'doc_reject',_meta(domain,nb),
                        {'domain':domain,'notebook':nb,'proposal':pid},{prel:digest(file.read_bytes())})


def revert_accept(root,journal,include_private=False):
    """Before governance.undo sets an accepted folder proposal back to pending: put the page back as it was. Returns a function
    that puts the accepted text back again, for when the settlement itself cannot be undone, or None when no write was needed.
    Refused when the page is neither what the accept wrote nor what it replaced."""
    effect=next((e for e in journal.get('effects') or [] if isinstance(e,dict) and docs.FOLDER_PROPOSALS.match(str(e.get('path')))),None)
    if effect is None or effect.get('before') is None or effect.get('after') is None:return None
    new_record=json.loads(base64.b64decode(effect['after']))
    if not isinstance(new_record,dict):return None
    check_visible(root,new_record,include_private)
    folder,_=folder_of(root,new_record)
    if folder is None:raise ValueError(f"not_found: the folder notebook {new_record.get('folder')} is no longer registered, so its page cannot be put back; restore it from backups or version history")
    rel=_page(new_record,strict=False)
    path=_safe(folder,rel)
    if path is None:raise ValueError('Links and junctions are not allowed inside a notebook')
    old_hash,old_bytes=_old_state(new_record)
    revision=new_record.get('revision')
    current=_sha(_bytes(path))
    if current==old_hash:return None   # the accept never wrote it, or it was put back already
    if current!=revision:raise ValueError(f'{rel} changed since the accept, so it is not put back; restore it from backups or version history')
    if old_hash is not None and old_bytes is None:raise ValueError(f'{rel} was too large to keep, so it cannot be put back here; restore it from backups or version history')
    accepted=_bytes(path)
    if not _put(folder,rel,old_bytes,revision):raise ValueError(f'{rel} changed since the accept, so it is not put back; restore it from backups or version history')
    return lambda:_put(folder,rel,accepted,old_hash)
