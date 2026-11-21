"""Notebook pages in the Obsidian vault (NS-2B, design sections 8.4 and 12b).

Each searchable or injectable, non-private notebook of a domain with a managed vault is mirrored byte for byte to
<vault>/Notebooks/<domain>/<notebook>/, its attachments under assets/. Edits made in the vault come back after the quiet
period as a page write against the revision last mirrored; when the page changed in memory too, the vault text becomes a
proposal (`doc proposals`) and the vault shows the memory version again. A new .md file in a mirrored notebook becomes a page. A
mirrored page deleted in the vault becomes a delete plan, ticked on the Sync Status page like a note's. Hidden and private
notebooks are never mirrored, and the mirror of one that turns hidden or private is removed at the next sync (an edited copy is
kept as a proposal first). Vault attachments never flow back. The vault's Notebooks folder is never indexed as a source
(folder_policy treats it as hidden) and agents may not write it (runtime.protected_rule)."""
from __future__ import annotations
import hashlib,os,re,shutil,tempfile,time
from pathlib import Path
from .repository import canonical_json,confined,now,read_json,store_lock,transaction

MIRROR='Notebooks'
STATE='notebook-sync.json'
APPROVAL=re.compile(r'^\s*[-*]\s*\[[xX]\].*<!--\s*lm-page:([a-f0-9]{32})\s*-->')
MASS_MISSING=(5,0.2)


def _sha(data):return hashlib.sha256(data).hexdigest() if data is not None else None


def _bytes(path):
    try:return path.read_bytes()
    except (FileNotFoundError,NotADirectoryError):return None


def _assets(folder):
    """{name: path} of a notebook's attachments (no links, no folders)."""
    from .docs import _is_link
    base=Path(folder)/'assets'
    if not base.is_dir() or _is_link(base):return {}
    return {p.name:p for p in sorted(base.iterdir()) if p.is_file() and not _is_link(p)}


def _copy(source,target):
    """Copy a file through a temporary file beside the target, so a stop leaves the old copy or the new one."""
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.',suffix='.mirror',dir=target.parent);os.close(fd)
    try:shutil.copyfile(source,temp);os.replace(temp,target)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def _prune_dirs(base):
    if not base.is_dir():return
    for d in sorted((p for p in base.rglob('*') if p.is_dir() and not p.is_symlink()),key=lambda p:len(p.parts),reverse=True):
        try:d.rmdir()
        except OSError:pass


def sync_notebooks(domain,vault,root):
    """Mirror and bring back; returns counts. Runs under the store and vault locks, like the note sync."""
    from . import docs,docs_index
    from .governance import apply,plan
    from .sync import QUIET_SECONDS,_ticked,status_rel
    root=Path(root).resolve();vault=Path(vault).resolve();base=vault/MIRROR/domain
    state_rel=f'domains/{domain}/{STATE}'
    with store_lock(root),store_lock(vault):
        state=read_json(root/state_rel,None) or {}
        if state.get('version')!=1:state={'version':1}
        pages=state.setdefault('pages',{});assets=state.setdefault('assets',{});pending=state.setdefault('pending',{})
        errors={};conflicts=[];log=state.setdefault('log',[]);counts=dict.fromkeys(('mirrored','saved','created','proposed','removed','deferred','planned','approved'),0)
        def record(action,page,operation_id=None):
            log.append({'at':now(),'action':action,'page':page,**({'operation_id':operation_id} if operation_id else {})})
        recent=lambda path:path.exists() and time.time()-path.stat().st_mtime<QUIET_SECONDS
        # Deletions ticked on the Sync Status page.
        for plan_id in _ticked(vault,domain,APPROVAL):
            key=next((k for k,p in pending.items() if p.get('plan_id')==plan_id),None)
            value=read_json(root/'.plans'/(plan_id+'.json'),None) if key else None
            if not value:continue
            try:
                receipt=apply(root,plan_id,value['digest'],sync_overlay=False);pending.pop(key);pages.pop(key,None)
                record('Deleted',key,receipt.get('operation_id'));counts['approved']+=1
            except ValueError as exc:pending[key]['error']='Approval failed: '+str(exc)
        present=docs_index._notebooks(root,domain)
        shown={n for n,(_,level) in docs_index.visible(root,domain).items() if level in ('searchable','injectable')}
        changes={};seen={}   # vault rel -> bytes or None; vault rel -> hash the decision was made on
        def to_vault(key,data,vault_bytes):
            rel=f'{MIRROR}/{domain}/{key}';changes[rel]=data;seen[rel]=_sha(vault_bytes)
        owned=[k for k in pages if k.split('/',1)[0] in shown]
        missing=[k for k in owned if k not in pending and (base/k).exists() is False]
        reset=len(missing)>=MASS_MISSING[0] and len(missing)>=MASS_MISSING[1]*max(1,len(owned))
        for nb in sorted(shown):
            folder=present[nb];listing=docs_index._walk_pages(folder) or {};mirrored=docs_index._walk_pages(base/nb) if (base/nb).is_dir() else {}
            keys=sorted({f'{nb}/{r}' for r in listing}|{f'{nb}/{r}' for r in mirrored or {}}|{k for k in pages if k.startswith(nb+'/')})
            for key in keys:
                rel=key.split('/',1)[1];page=_bytes(folder/rel) if rel in listing else None;vpath=base/key;held=_bytes(vpath)
                ps,vs=_sha(page),_sha(held);st=pages.get(key)
                if vs is not None and st and vs!=st.get('vault') and recent(vpath):counts['deferred']+=1;continue
                if vs is not None and not st and page is None and recent(vpath):counts['deferred']+=1;continue
                try:
                    if page is not None and held is None:
                        if st and ps==st.get('revision') and not reset:   # deleted in the vault: a plan to approve
                            if key not in pending:
                                made=plan(root,{f'domains/{domain}/notebooks/{key}':None},'doc_vault_delete',(),{'page':key})
                                pending[key]={'plan_id':made['plan_id'],'at':now()};counts['planned']+=1
                            continue
                        pending.pop(key,None);to_vault(key,page,held);pages[key]={'revision':ps,'vault':ps};counts['mirrored']+=1
                    elif page is not None:
                        pending.pop(key,None)
                        if vs==ps:pages[key]={'revision':ps,'vault':vs}
                        elif st and vs==st.get('vault'):to_vault(key,page,held);pages[key]={'revision':ps,'vault':ps};counts['mirrored']+=1   # changed in memory only
                        elif st and ps==st.get('revision'):   # changed in the vault only
                            written=docs.write_page(root,domain,nb,rel,held,ps);pages[key]={'revision':written['revision'],'vault':vs}
                            record('Saved',key,written.get('operation_id'));counts['saved']+=1
                        else:   # changed on both sides, or met for the first time with other text: memory wins, the vault text is proposed
                            staged=docs.propose(root,domain,nb,rel,held,ps,'Edited in Obsidian while the page changed in memory')
                            to_vault(key,page,held);pages[key]={'revision':ps,'vault':ps};counts['proposed']+=1
                            conflicts.append(f"- {key}: changed in the vault and in memory. The vault shows the memory version again; your text is proposal {staged['id']} (`doc proposal {staged['id']}`).")
                    elif held is not None:
                        if st and vs==st.get('vault'):to_vault(key,None,held);pages.pop(key);counts['removed']+=1   # deleted in memory
                        elif st:   # deleted in memory after an edit in the vault: proposed as a new page, then removed
                            staged=docs.propose(root,domain,nb,rel,held,None,'Edited in Obsidian after the page was deleted');to_vault(key,None,held);pages.pop(key)
                            conflicts.append(f"- {key}: deleted in memory after you edited it in the vault; your text is proposal {staged['id']}.");counts['proposed']+=1
                        else:   # a new page written in the vault
                            created=docs.create_page(root,domain,nb,rel,held);pages[key]={'revision':created['revision'],'vault':vs}
                            record('Created',key,created.get('operation_id'));counts['created']+=1
                    else:pages.pop(key,None);pending.pop(key,None)
                except ValueError as exc:errors[f'{MIRROR}/{domain}/{key}']=str(exc)
            for name,path in _assets(folder).items():
                key=f'{nb}/assets/{name}';target=base/key;sha=_sha(_bytes(path));current=_sha(_bytes(target))
                if current==sha:assets[key]=sha
                elif current is None or current==assets.get(key):_copy(path,target);assets[key]=sha;counts['mirrored']+=1
                else:errors[f'{MIRROR}/{domain}/{key}']='Changed in the vault; attachments are changed in Hivemind, so this copy is no longer updated'
            for key in [k for k in assets if k.startswith(nb+'/') and k.split('/assets/',1)[1] not in _assets(folder)]:
                if _sha(_bytes(base/key))==assets[key]:(base/key).unlink(missing_ok=True)
                assets.pop(key)
        # Notebooks no longer mirrored (hidden, private or deleted): their copies leave the vault; an edited page is proposed first.
        for key in [k for k in pages if k.split('/',1)[0] not in shown]:
            nb,rel=key.split('/',1);held=_bytes(base/key)
            if held is not None and _sha(held)!=pages[key].get('vault') and nb in present:
                try:docs.propose(root,domain,nb,rel,held,_sha(_bytes(present[nb]/rel)),'Edited in Obsidian before the notebook left the vault',include_private=True)
                except ValueError as exc:errors[f'{MIRROR}/{domain}/{key}']=str(exc);continue
                conflicts.append('- A page edited in the vault belonged to a notebook that is no longer mirrored; your text was kept as a proposal in Hivemind.')
            if held is not None and (nb in present or _sha(held)==pages[key].get('vault')):to_vault(key,None,held);counts['removed']+=1
            pages.pop(key);pending.pop(key,None)
        for key in [k for k in assets if k.split('/',1)[0] not in shown]:
            if _sha(_bytes(base/key))==assets[key] or key.split('/',1)[0] in present:(base/key).unlink(missing_ok=True)
            assets.pop(key)
        for rel in [r for r in changes if _sha(_bytes(confined(vault,r)))!=seen[r]]:   # saved again in Obsidian while syncing: next time
            changes.pop(rel);counts['deferred']+=1
        if changes:transaction(vault,changes,'overlay_sync',skip_unchanged=True)
        _prune_dirs(base)
        state.update(errors=errors,conflicts=conflicts,log=log[-20:])   # a conflict is reported by the sync that met it; its proposal stays in Hivemind
        transaction(root,{state_rel:canonical_json(state)},'overlay_state',skip_unchanged=True)
        return counts


def status_section(root,domain):
    """The Sync Status page's notebook section, or '' when this domain has never mirrored a notebook."""
    state=read_json(Path(root)/'domains'/domain/STATE,None)
    if not isinstance(state,dict) or not (state.get('pages') or state.get('pending') or state.get('errors') or state.get('conflicts')):return ''
    lines=[f"- [ ] Delete page: {key} <!-- lm-page:{p['plan_id']} -->"+(f"\n  - {p['error']}" if p.get('error') else '') for key,p in sorted((state.get('pending') or {}).items())]
    lines+=[f"- `{rel}`: {message}" for rel,message in sorted((state.get('errors') or {}).items())]
    lines+=list(state.get('conflicts') or [])
    lines+=[f"- {a['at'][:16].replace('T',' ')} {a['action']}: {a['page']}"+(f" (undo: `alm undo {a['operation_id']}`)" if a.get('operation_id') else '') for a in reversed((state.get('log') or [])[-5:])]
    return '## Notebook pages\n\n'+('\n'.join(lines) if lines else '_Mirrored; nothing waiting._')+'\n'
