"""Notebook pages in the Obsidian vault (NS-2B, design sections 8.4 and 12b).

Each searchable or injectable, non-private notebook of a domain with a managed vault is mirrored byte for byte to
<vault>/Notebooks/<domain>/<notebook>/, its attachments under assets/. Edits made in the vault come back after the quiet
period as a page write against the revision last mirrored; when the page changed in memory too, the vault text becomes a
proposal (`proposals list`) and the vault shows the memory version again. A new .md file in a mirrored notebook becomes a page. A
mirrored page deleted in the vault becomes a delete plan, ticked on the Sync Status page like a note's; the tick applies only
while the copy is still gone, the notebook still mirrored and no vault reset is under way. Hidden and private notebooks are
never mirrored, and every file in the mirror of one that turns hidden or private leaves the vault at the next sync: an edited
or new page is kept as a proposal first, anything else as an attachment of the notebook. Vault attachments never flow back.

Vault files are written and removed one by one, each only while it still holds the bytes the decision was made on, and never
through a link or junction. They are not journaled in the vault: a journal there would keep a copy of a page after the notebook
turned private. The mirror state (domains/<D>/notebook-sync.json) belongs to one vault (its vault id); a domain that moves to
another vault starts afresh, and the copies left unchanged in the old one are removed. The vault's Notebooks folder is never
indexed as a source (folder_policy treats it as hidden) and agents may not write it (runtime.protected_rule)."""
from __future__ import annotations
import hashlib,os,re,tempfile,time
from pathlib import Path
from .paths import is_os_junk,remove_empty_dir
from .repository import canonical_json,now,read_json,store_lock,transaction
from .safe_put import _sha,_bytes,_safe,_put

MIRROR='Notebooks'
STATE='notebook-sync.json'
APPROVAL=re.compile(r'^\s*[-*]\s*\[[xX]\].*<!--\s*lm-page:([a-f0-9]{32})\s*-->')
MASS_MISSING=(5,0.2)
SERVED=('searchable','injectable')


def _files(folder,unread=None):
    """{rel: path} of every file under a folder, links and junctions left out. A folder that cannot be listed is added to
    `unread` (the files under it are unknown, not gone)."""
    from .docs import _is_link
    found={};stack=['']
    while stack:
        rel=stack.pop()
        try:entries=list(os.scandir(Path(folder)/rel if rel else folder))
        except OSError:
            if unread is not None:unread.append(rel)
            continue
        for e in entries:
            path=f'{rel}/{e.name}' if rel else e.name
            try:
                if _is_link(Path(e.path)):continue
                if e.is_dir(follow_symlinks=False):stack.append(path)
                elif e.is_file(follow_symlinks=False) and not is_os_junk(e.path):found[path]=Path(e.path)
            except OSError:continue
    return found


def _assets(folder):
    """{name: path} of a notebook's attachments (no links, no folders)."""
    from .docs import _is_link
    base=Path(folder)/'assets'
    if not base.is_dir() or _is_link(base):return {}
    return {p.name:p for p in sorted(base.iterdir()) if p.is_file() and not _is_link(p) and not is_os_junk(p)}


def _prune_dirs(base):
    """Remove the empty folders under base, never entering a link or junction."""
    from .docs import _is_link
    if base is None or not base.is_dir():return
    def walk(folder):
        try:entries=list(os.scandir(folder))
        except OSError:return
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False) and not _is_link(Path(e.path)):
                    walk(e.path);remove_empty_dir(e.path)
            except OSError:continue
    walk(base)


def _leave_vault(state,domain):
    """Remove the copies a previous vault still holds unchanged; the number left there (edited, or behind a link)."""
    from .sync import vault_id
    old=state.get('vault')
    if not old or not Path(old).is_dir() or vault_id(old,create=False)!=state.get('vault_id'):return 0
    kept=0
    tracked=[(k,(v or {}).get('vault')) for k,v in (state.get('pages') or {}).items()]+list((state.get('assets') or {}).items())
    try:
        with store_lock(Path(old)):
            for key,sha in tracked:
                rel=f'{MIRROR}/{domain}/{key}'
                try:
                    path=_safe(old,rel);held=_bytes(path) if path is not None else b''
                    if held is None:continue
                    if path is None or _sha(held)!=sha or not _put(old,rel,None,sha):kept+=1
                except OSError:kept+=1
            _prune_dirs(_safe(old,f'{MIRROR}/{domain}'))
    except (OSError,TimeoutError):return len(tracked)
    return kept


def _plan_ok(value,plan_id,target):
    """Whether a pending deletion plan is the one this sync made: its id, kind and only change, and a digest that matches."""
    if not (isinstance(value,dict) and value.get('id')==plan_id and value.get('kind')=='doc_vault_delete' and isinstance(value.get('digest'),str)):return False
    try:signed=hashlib.sha256(canonical_json({k:v for k,v in value.items() if k!='digest'}).encode()).hexdigest()==value['digest']
    except (TypeError,ValueError):return False
    return signed and value.get('changes')=={target:None}


def _keep_in_store(root,domain,nb,folder,rel,held,path):
    """Keep a vault file of a notebook that is leaving the vault: a page as a proposal, anything else (or a page that cannot be
    one) as an attachment of the notebook. 'proposed', 'attached', or None when neither worked."""
    from . import docs
    if rel.casefold().endswith('.md') and not rel.startswith('assets/'):
        try:
            current=folder/rel
            base=_sha(_bytes(current)) if current.is_file() else None
            docs.propose(root,domain,nb,rel,held,base,'Edited in Obsidian before the notebook left the vault',include_private=True)
            return 'proposed'
        except (OSError,ValueError):pass
    ext=os.path.splitext(rel)[1].lower()
    try:   # the bytes that were judged, not the file as it is now
        with tempfile.TemporaryDirectory(prefix='lm-keep-') as scratch:
            copy=Path(scratch)/'kept';copy.write_bytes(held)
            docs.attach(root,domain,nb,str(copy),'from-obsidian'+(ext if re.fullmatch(r'\.[a-z0-9]{1,10}',ext) else '.bin'),include_private=True)
        return 'attached'
    except (OSError,ValueError):return None


def sync_notebooks(domain,vault,root):
    """Mirror and bring back; returns counts. Runs under the store and vault locks, like the note sync."""
    from . import docs,docs_index
    from .governance import apply,plan
    from .sync import QUIET_SECONDS,_ticked,vault_id
    root=Path(root).resolve();vault=Path(vault).resolve();top=f'{MIRROR}/{domain}'
    state_rel=f'domains/{domain}/{STATE}'
    with store_lock(root),store_lock(vault):
        state=read_json(root/state_rel,None) or {}
        if state.get('version')!=1:state={'version':1}
        errors={};conflicts=[];counts=dict.fromkeys(('mirrored','saved','created','proposed','removed','deferred','planned','approved','kept'),0)
        identity=vault_id(vault)
        if state.get('vault_id')!=identity:   # another vault, or none recorded (the note sync has already named the new vault): it says nothing about this one
            left=_leave_vault(state,domain) if state.get('vault_id') else 0
            if left:errors['(previous vault)']=f'{left} notebook copies were left in the previous vault ({state.get("vault")}) because they were edited there; delete them when you no longer need them'
            state={'version':1,'log':state.get('log') or []}
        state.update(vault_id=identity,vault=str(vault))
        pages=state.setdefault('pages',{});assets=state.setdefault('assets',{});pending=state.setdefault('pending',{});log=state.setdefault('log',[])
        kept_already=state.setdefault('kept',{})   # vault files already kept in memory, by hash: a removal that failed is retried without keeping them twice
        writes=[]   # (vault rel, bytes or None, hash the decision was made on, (kind, key, value), count)

        def record(action,key,operation_id=None):
            log.append({'at':now(),'action':action,'page':key,**({'operation_id':operation_id} if operation_id else {})})

        def fail(key,message):errors[f'{top}/{key}']=message

        def recent(path):
            try:return time.time()-path.stat().st_mtime<QUIET_SECONDS
            except OSError:return False

        def to_vault(key,data,held,value,count,kind='page'):
            writes.append((f'{top}/{key}',data,_sha(held),(kind,key,value),count))

        def plan_file(plan_id,key):
            try:value=read_json(root/'.plans'/(plan_id+'.json'),None)
            except ValueError:return None
            return value if _plan_ok(value,plan_id,f'domains/{domain}/notebooks/{key}') else None

        present=docs_index._notebooks(root,domain)
        if present is None:raise ValueError('The notebooks folder cannot be listed now; the vault mirror waits for the next sync')
        shown={n for n,(_,level) in docs_index.visible(root,domain).items() if level in SERVED and n in present}
        ticked=set(_ticked(vault,domain,APPROVAL))
        tracked=[k for k in pages if k.split('/',1)[0] in shown]
        gone=[]
        for k in tracked:
            path=_safe(vault,f'{top}/{k}')
            if path is not None and not path.exists():gone.append(k)
        reset=len(gone)>=MASS_MISSING[0] and len(gone)>=MASS_MISSING[1]*max(1,len(tracked))   # a vault reset: copied back, never deleted

        def deletion(key):
            """A mirrored page deleted in the vault: a plan the user ticks; the tick applies only now, with the copy still gone."""
            p=pending.get(key);made=plan_file(p['plan_id'],key) if p else None
            if p and made is None:p=None   # pruned or lost: a fresh plan, with a fresh line to tick
            if p and p['plan_id'] in ticked:
                vpath=_safe(vault,f'{top}/{key}')
                if vpath is None or vpath.exists():pending.pop(key,None);return   # the copy came back since it was read: the approval lapses
                try:
                    receipt=apply(root,p['plan_id'],made['digest'],sync_overlay=False);pending.pop(key,None);pages.pop(key,None)
                    record('Deleted',key,receipt.get('operation_id'));counts['approved']+=1
                except ValueError as exc:p['error']='Approval failed: '+str(exc)
                return
            if not p:
                made=plan(root,{f'domains/{domain}/notebooks/{key}':None},'doc_vault_delete',(),{'page':key})
                pending[key]={'plan_id':made['plan_id'],'at':now()};counts['planned']+=1

        for nb in sorted(shown):
            folder=present[nb];mirror=_safe(vault,f'{top}/{nb}')
            if mirror is None:fail(nb,'A link or junction is in the way of this notebook in the vault; it is not mirrored');continue
            unread=[]   # folders that could not be listed, in memory or in the vault: their pages are left alone this time
            try:
                listing=docs_index._walk_pages(folder,None,unread) or {}
                mirrored=(docs_index._walk_pages(mirror,None,unread) or {}) if mirror.is_dir() else {}
            except OSError as exc:fail(nb,f'Cannot be listed now ({exc}); mirrored at the next sync');continue
            if unread:fail(nb,'Some folders cannot be listed now; their pages are mirrored at a later sync')
            keys=sorted({f'{nb}/{r}' for r in listing}|{f'{nb}/{r}' for r in mirrored}|{k for k in list(pages)+list(pending) if k.startswith(nb+'/')})
            for key in keys:
                rel=key.split('/',1)[1];vpath=_safe(vault,f'{top}/{key}')
                if docs_index.under(rel,unread):continue
                if vpath is None:fail(key,'A link or junction is in the way; this copy is skipped');continue
                try:
                    page=_bytes(folder/rel) if rel in listing else None;held=_bytes(vpath)
                    ps,vs=_sha(page),_sha(held);st=pages.get(key)
                    if held is not None:pending.pop(key,None)   # a copy is there again: a deletion waiting for a tick lapses, even one too fresh to read
                    if vs is not None and vs!=ps and vs!=(st or {}).get('vault') and recent(vpath):counts['deferred']+=1;continue   # still being edited
                    if page is not None and held is None:
                        if st and ps==st.get('revision') and not reset:deletion(key);continue   # deleted in the vault
                        pending.pop(key,None);to_vault(key,page,None,{'revision':ps,'vault':ps},'mirrored')
                    elif page is not None:
                        pending.pop(key,None)
                        if vs==ps:pages[key]={'revision':ps,'vault':vs}
                        elif st and vs==st.get('vault'):to_vault(key,page,held,{'revision':ps,'vault':ps},'mirrored')   # changed in memory only
                        elif st and ps==st.get('revision'):   # changed in the vault only
                            written=docs.write_page(root,domain,nb,rel,held,ps);pages[key]={'revision':written['revision'],'vault':vs}
                            record('Saved',key,written.get('operation_id'));counts['saved']+=1
                        else:   # changed on both sides, or met for the first time with other text: memory wins, the vault text is proposed
                            staged=docs.propose(root,domain,nb,rel,held,ps,'Edited in Obsidian while the page changed in memory')
                            to_vault(key,page,held,{'revision':ps,'vault':ps},'mirrored');counts['proposed']+=1
                            conflicts.append({'notebook':nb,'text':f"- {key}: changed in the vault and in memory. The vault shows the memory version again; your text is proposal {staged['id']} (`proposals show {staged['id']}`)."})
                    elif held is not None:
                        pending.pop(key,None)
                        if st and vs==st.get('vault'):to_vault(key,None,held,None,'removed')   # deleted in memory
                        elif st:   # deleted in memory after an edit in the vault: proposed as a new page, then removed
                            staged=docs.propose(root,domain,nb,rel,held,None,'Edited in Obsidian after the page was deleted');to_vault(key,None,held,None,'removed')
                            conflicts.append({'notebook':nb,'text':f"- {key}: deleted in memory after you edited it in the vault; your text is proposal {staged['id']}."});counts['proposed']+=1
                        else:   # a new page written in the vault
                            created=docs.create_page(root,domain,nb,rel,held);pages[key]={'revision':created['revision'],'vault':vs}
                            record('Created',key,created.get('operation_id'));counts['created']+=1
                    else:pages.pop(key,None);pending.pop(key,None)
                except (OSError,ValueError) as exc:fail(key,str(exc))
            names=_assets(folder)
            for name,path in names.items():
                key=f'{nb}/assets/{name}';target=_safe(vault,f'{top}/{key}')
                if target is None:fail(key,'A link or junction is in the way; this attachment is not copied');continue
                try:
                    data=_bytes(path);sha=_sha(data);seen=_bytes(target);current=_sha(seen)   # one read decides and is the expected hash
                    if current==sha:assets[key]=sha
                    elif current is None or current==assets.get(key):to_vault(key,data,seen,sha,'mirrored','asset')
                    else:fail(key,'Changed in the vault; attachments are changed in Hivemind, so this copy is no longer updated')
                except OSError as exc:fail(key,str(exc))
            for key in [k for k in assets if k.startswith(nb+'/') and k.split('/assets/',1)[1] not in names]:   # removed in memory
                target=_safe(vault,f'{top}/{key}')
                try:held=_bytes(target) if target is not None else None
                except OSError:held=None
                if held is not None and _sha(held)==assets[key]:to_vault(key,None,held,None,'removed','asset')
                else:assets.pop(key)   # gone, or edited in the vault: left alone and no longer tracked

        # Notebooks no longer mirrored (hidden, private or deleted): every file of their mirror leaves the vault. An edited or new
        # page is kept as a proposal first, anything else as an attachment; a deleted notebook's edited copies stay, unnamed.
        base=_safe(vault,top)
        try:folders={e.name for e in os.scandir(base) if e.is_dir(follow_symlinks=False)} if base is not None and base.is_dir() else set()
        except OSError:folders=set()
        leaving=({k.split('/',1)[0] for k in list(pages)+list(assets)+list(pending)}|(folders&set(present)))-shown
        orphans=0
        for nb in sorted(leaving):
            unread=[];mirror=_safe(vault,f'{top}/{nb}');files=_files(mirror,unread) if mirror is not None and mirror.is_dir() else {}
            if unread:fail(nb,'Some folders of this notebook in the vault cannot be listed now; their files leave at a later sync')
            known={k.split('/',1)[1]:(v or {}).get('vault') for k,v in pages.items() if k.startswith(nb+'/')}
            known.update({k.split('/',1)[1]:v for k,v in assets.items() if k.startswith(nb+'/')})
            for rel in sorted(set(files)|set(known)):
                key=f'{nb}/{rel}';path=files.get(rel)
                if docs_index.under(rel,unread):continue   # still tracked until its folder can be listed
                if path is None:pages.pop(key,None);assets.pop(key,None);continue
                try:held=_bytes(path)
                except OSError as exc:fail(key,str(exc));continue
                if held is None:pages.pop(key,None);assets.pop(key,None);continue
                if _sha(held)==known.get(rel):to_vault(key,None,held,None,'removed','leave');continue
                if nb not in present:orphans+=1;continue
                kept=kept_already.get(key)==_sha(held) or _keep_in_store(root,domain,nb,present[nb],rel,held,path)
                if kept:kept_already[key]=_sha(held);to_vault(key,None,held,None,'kept','leave')
                else:fail(key,'Could not be kept in memory, so it is still in the vault')
            for key in [k for k in pending if k.startswith(nb+'/')]:pending.pop(key)
        if orphans:errors['(deleted notebooks)']=f'{orphans} edited copies of deleted notebooks are still in the vault under {top}; delete them when you no longer need them'
        if any(c=='kept' for *_,c in writes):conflicts.append('- Pages edited in the vault belonged to a notebook that is no longer mirrored; your text was kept in Hivemind (proposals or attachments).')

        # Each vault file is changed only while it holds the bytes the decision was made on; the state follows only what was done.
        for rel,data,expect,(kind,key,value),count in writes:
            try:done=_put(vault,rel,data,expect)
            except OSError as exc:fail(key,f'The vault copy could not be updated: {exc}');done=False
            if not done:counts['deferred']+=1;continue   # saved again in Obsidian while syncing, or in the way: next time
            counts[count]+=1
            if kind=='page':
                if value is None:pages.pop(key,None)
                else:pages[key]=value
            elif kind=='asset':
                if value is None:assets.pop(key,None)
                else:assets[key]=value
            else:pages.pop(key,None);assets.pop(key,None);kept_already.pop(key,None)
        for key in [k for k in kept_already if k.split('/',1)[0] in shown]:kept_already.pop(key)   # mirrored again: nothing left to remove
        _prune_dirs(base)
        state.update(errors=errors,conflicts=conflicts,log=log[-20:])   # a conflict is reported by the sync that met it; its proposal stays in Hivemind
        transaction(root,{state_rel:canonical_json(state)},'overlay_state',skip_unchanged=True)
        return counts


def status_section(root,domain):
    """The Sync Status page's notebook section, or '' when this domain has never mirrored a notebook. It names only notebooks
    that are mirrored now: the page is in the vault, where a hidden or private notebook's names do not belong."""
    from . import docs_index
    state=read_json(Path(root)/'domains'/domain/STATE,None)
    if not isinstance(state,dict) or not (state.get('pages') or state.get('pending') or state.get('errors') or state.get('conflicts')):return ''
    try:shown={n for n,(_,level) in docs_index.visible(root,domain).items() if level in SERVED}
    except (OSError,ValueError):shown=set()
    prefix=f'{MIRROR}/{domain}/';hidden=0;lines=[]
    def named(key):return key.split('/',1)[0] in shown
    for key,p in sorted((state.get('pending') or {}).items()):
        if not named(key):hidden+=1;continue
        lines.append(f"- [ ] Delete page: {key} <!-- lm-page:{p['plan_id']} -->"+(f"\n  - {p['error']}" if p.get('error') else ''))
    for rel,message in sorted((state.get('errors') or {}).items()):
        if rel.startswith(prefix) and not named(rel[len(prefix):]):hidden+=1;continue
        lines.append(f"- `{rel}`: {message}")
    for c in state.get('conflicts') or []:
        if isinstance(c,dict):
            if c.get('notebook') in shown:lines.append(str(c.get('text')))
            else:hidden+=1
        elif isinstance(c,str):lines.append(c)
    lines+=[f"- {a['at'][:16].replace('T',' ')} {a['action']}: {a['page']}"+(f" (undo: `alm undo {a['operation_id']}`)" if a.get('operation_id') else '')
            for a in reversed([a for a in (state.get('log') or []) if isinstance(a,dict) and named(str(a.get('page','')))][-5:])]
    if hidden:lines.append(f'- {hidden} notices concern notebooks that are no longer mirrored; Hivemind shows them.')
    return '## Notebook pages\n\n'+('\n'.join(lines) if lines else '_Mirrored; nothing waiting._')+'\n'
