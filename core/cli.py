"""Unified compact script interface. Compatibility scripts delegate here."""
from __future__ import annotations
import argparse,json,os,sys,hashlib,time
from pathlib import Path
from .common import memory_root,load_bindings,resolve_domain,canonical_domain
from .repository import read_json,inventory,parse,store_lock,canonical_json,now
from .note_types import NOTE_TYPES
from .links import note_links

def _payload(args):
    if getattr(args,'payload_file',None):raw=args.payload_file.read_text(encoding='utf-8')
    else:raw=sys.stdin.read()
    if len(raw.encode())>1024*1024:raise ValueError('Payload exceeds 1 MiB')
    return json.loads(raw)
def _reference(value,domain,root):
    """(domain, full note ID) for a ref, a unique ID prefix, or an exact note title."""
    from .notes import resolve_ref
    from .runtime import scope_domains
    parents=scope_domains(domain,load_bindings(root)) if domain else []   # parents and children: what search shows can be named back
    return resolve_ref(value,domain,root,parents)

def _same_pair(entry,domain,note_id):
    return isinstance(entry,dict) and entry.get('ref')==f'{domain}:{note_id}'

def _turn(root,session,turn):
    """The turn a receipt belongs to: explicit, else the session's latest prompt."""
    if turn or not session:return turn
    from .runtime import _pointer
    value=read_json(_pointer(root,session),{}) or {}
    if not value.get('turn'):raise ValueError('No prompt recorded for this session yet; pass --turn')
    return value['turn']

def _session_state(root,session):
    """The hooks' state for a session ({} when there is none or it is unreadable)."""
    if not session:return {}
    from .runtime import _pointer
    try:value=read_json(_pointer(root,session),{})
    except ValueError:return {}
    return value if isinstance(value,dict) else {}

SESSION_ENV='CLAUDE_CODE_SESSION_ID'   # Claude Code gives every shell command its session ID, the one hooks receive
SWITCH={'off','on','status'}
# Commands that still run while memory is off for the session: the switch itself and checks that touch no notes.
OFF_ALLOWED=SWITCH|{'version','doctor','help'}

def _switch(root,cmd,session):
    """alm off / on / status for one session: a flag in the hooks' session state (runtime.session_off)."""
    from .runtime import _pointer,_update,session_hash,env_off
    if not session:raise ValueError(f'no_session: run it inside a Claude Code session (! alm {cmd}) or pass --session ID')
    known=_pointer(root,session).exists()
    if cmd=='on' and env_off():raise ValueError('env_off: this session was started with LIVING_MEMORY_OFF=1; start a new session without it')
    if cmd=='off':_update(root,session,lambda s:s.update(off={'at':now()}))
    elif cmd=='on':
        def back(s):
            if s.pop('off',None):s['resumed']=True   # the next prompt repeats the protocol
            s.pop('off_told',None)
        _update(root,session,back)
    state=_session_state(root,session)
    reason='env' if env_off() else 'switch' if state.get('off') else None
    result={'ok':True,'session':session_hash(session),'memory':'off' if reason else 'on','reason':reason}
    if cmd=='status':result['known']=known
    return result

def _session_domain(root,session):
    """The domain the hooks pinned for this session, if it is still bound."""
    pinned=_session_state(root,session).get('domain')
    return pinned if pinned and pinned in load_bindings(root).get('domains',{}) else None

def _origin(root,session):
    """Where a note came from: harness and a hash of the session, never the session ID itself."""
    from .runtime import session_hash
    return f"{_session_state(root,session).get('harness') or 'unknown'}:{session_hash(session)}"

def _file_args(args,domain,root,existing=()):
    """(files to store or None when unchanged, suggested files) from --file/--files and this turn's edits."""
    from .common import domain_prefixes,display_path
    if getattr(args,'files',None)=='auto' and not args.session:raise ValueError('--files auto needs --session')
    touched=_session_state(root,args.session).get('touched',[]) if args.session else []
    if not args.file and not args.files:return None,[t for t in touched if t not in existing]
    prefixes=domain_prefixes(domain,load_bindings(root));files=[] if args.files=='none' else list(existing)
    for value in args.file or []:
        path=Path(value).expanduser()
        # The bound folder itself (a repo's codebase map) is stored as '.', like any other project-relative path.
        files.append((display_path(path.resolve(),prefixes) or '.') if path.is_absolute() else value)
    if args.files=='auto':files+=touched
    from .repository import clean_files
    files=clean_files(files)
    return files,[t for t in touched if t not in files]

def _file_flags(p):
    p.add_argument('--file',action='append',help='A file this note is about, relative to the project (repeatable)')
    p.add_argument('--files',choices=['auto','none'],help="auto: add the files changed this turn (needs --session); none: clear the list")
    p.add_argument('--command',dest='shell_command',action='append',help='A shell command this note warns about, so running it surfaces the note (repeatable; update adds to the list)')
    p.add_argument('--source',action='append',help='A file this note digests (relative to the project, or absolute; repeatable): its path and current hash are stored, and the note reads STALE once the file changes')

FACET_FLAGS=('subject','concern','platform','technology')
CAPTURE_FACETS=FACET_FLAGS+('project','system')
def _facet_tags(args,facets=FACET_FLAGS):
    """--subject x and --subject subject/x both mean subject/x."""
    return [v if v.startswith(f+'/') else f+'/'+v for f in facets for v in (getattr(args,f) or [])]

# Defaults when a capture names no subject/ or concern/ tag; used only if the tag is in the taxonomy.
SUBJECT_BY_TYPE={'preference':'subject/user'}
CONCERN_BY_TYPE={'decision':'concern/decision','preference':'concern/preference','bug':'concern/bug','knowledge':'concern/knowledge','policy':'concern/workflow','map':'concern/architecture','runbook':'concern/workflow','brief':'concern/knowledge','plan':'concern/workflow','context':'concern/knowledge','status':'concern/knowledge','risk':'concern/constraint','research':'concern/knowledge'}
def _global_names(root):
    from .common import global_domains
    return global_domains(root)

def _infer_tags(item,root,user=False):
    """Adds the default subject and concern tags the taxonomy knows; user marks the user brief (a global brief with no files), which is about the user."""
    from .notes import taxonomy
    known=taxonomy(root);tags=list(item.get('tags') or []);kind=item.get('type') or 'knowledge';added=[]
    for facet,tag in (('subject','subject/user' if user else SUBJECT_BY_TYPE.get(kind,'subject/project')),('concern',CONCERN_BY_TYPE.get(kind))):
        if tag and tag in known and not any(t.startswith(facet+'/') for t in tags):tags.append(tag);added.append(tag)
    item['tags']=tags
    return added

def _redact(values):
    """Replace credentials in title, body, context and commands; [{kind, field}] for what was replaced."""
    from .repository import redact
    found=[]
    for field in ('title','body','context'):
        if isinstance(values.get(field),str):
            values[field],kinds=redact(values[field]);found+=[{'kind':k,'field':field} for k in kinds]
    if isinstance(values.get('commands'),list):
        for i,command in enumerate(values['commands']):
            if isinstance(command,str):values['commands'][i],kinds=redact(command);found+=[{'kind':k,'field':'commands'} for k in kinds]
    return found

PIN_HELP='Protect the note from automatic retirement (archive, resolve, supersede by maintenance); it still allows manual archive, resolve and supersede, factual updates and review reminders'

def _receipt(args,domain,note_id,root,event=None):
    """Updating or superseding is a memory decision for the turn, like a capture; the diary notes it as use."""
    if event:
        from .usage import record
        record(root,event,[domain+':'+note_id],getattr(args,'session',None))
    if getattr(args,'session',None):
        from .capture import finalize
        finalize(args.session,_turn(root,args.session,getattr(args,'turn',None)),[domain+':'+note_id],root)

def doctor(root,cwd,explicit=None,domain=None):
    from .search import _fresh
    from .repository import problems
    import sqlite3
    config=load_bindings(root);domain=domain or resolve_domain(cwd,config);db=sqlite3.connect(':memory:')
    try:db.execute('CREATE VIRTUAL TABLE capability USING fts5(text)');fts=True
    except sqlite3.Error:fts=False
    finally:db.close()
    from .repository import unfinished_operations
    pending,damaged=unfinished_operations(root)
    index='unbound'
    counts={};warnings=[]
    if pending:warnings.append(f"{len(pending)} operation(s) did not finish: run recover ID to complete one, or recover ID --abandon when its files changed since ({', '.join(pending[:5])})")
    if damaged:warnings.append(f"{len(damaged)} operation journal(s) cannot be read and are ignored ({', '.join(damaged[:5])}); history skips them and undo refuses them")
    from . import wiring as hook_wiring
    wired=hook_wiring.wiring()
    for harness,row in wired.items():
        if isinstance(row,dict):warnings+=[f'hooks: {harness}: {problem}' for problem in row.get('problems') or []]
    if 'error' in wired:warnings.append('hooks: '+str(wired['error']))
    unreadable={}
    for directory in (root/'domains').iterdir() if (root/'domains').is_dir() else []:
        if directory.is_dir():
            counts[directory.name]=sum(1 for _ in (directory/'notes').rglob('*.md'))+sum(1 for _ in (directory/'archive').rglob('*.md'))
            found=problems(directory.name,root)
            if found:unreadable[directory.name]=found
    if (root/'improvement').is_dir():warnings.append('improvement/ holds data from a removed feature; nothing reads it and it is safe to delete')
    warnings+=[f'{name}: {count} notes; capture and freshness checks scale linearly and may slow hooks' for name,count in counts.items() if count>1500]
    scope=config.get('search_scope',{}) if isinstance(config.get('search_scope',{}),dict) else {}
    from .common import GLOBAL
    for kind,names,effect in (('global_domains',scope.get('global_domains',['_global']),'no note can be saved there and the no-match title fallback uses the active domain instead; migration-plan points it at _global'),
                              ('foreign_domains',scope.get('foreign_domains',[]),'it is never searched')):
        for name in names:
            try:canonical=canonical_domain(name,root);missing=not (root/'domains'/canonical).is_dir()
            except ValueError:canonical=None;missing=True
            # `capture --global` creates _global's folder on first use; an older name without a folder still
            # warns, because migration-plan points it at _global.
            if kind=='global_domains' and canonical==GLOBAL:missing=False
            if missing:warnings.append(f"search_scope.{kind} names '{name}', which has no domain folder; {effect}")
    from .common import _entries
    for name,_,prefixes in _entries(config):   # BIND-RENAME: a renamed bound folder silently stops resolving to its domain
        for raw in prefixes:
            try:folder=Path(os.path.expandvars(str(raw))).expanduser().resolve()
            except (OSError,ValueError):continue
            if not folder.is_dir():warnings.append(f"bindings: domain '{name}' is bound to {folder}, which does not exist; if the folder moved, run bind {name} NEW_PATH, then unbind {name} --prefix {folder}")
    from .backup import staleness_warning
    from .maintenance import doctor_warnings
    try:warnings+=['maintenance: '+w for w in doctor_warnings(root)]
    except Exception as exc:warnings.append(f'maintenance: cannot be checked ({type(exc).__name__})')
    from .projects import listing
    try:warnings+=['projects: '+w for w in listing(root)['warnings']]
    except ValueError as exc:warnings.append(f'projects.json: {exc}')
    from .systems import listing as systems_listing
    try:warnings+=['systems: '+w for w in systems_listing(root)['warnings']]
    except ValueError as exc:warnings.append(f'systems.json: {exc}')
    from .sources import listing as sources_listing
    try:warnings+=['sources: '+w for w in sources_listing(root)['warnings']]
    except ValueError as exc:warnings.append(f'sources.json: {exc}')
    from .changes import listing as changes_listing
    try:warnings+=['changes: '+w for w in changes_listing(root)['warnings']]
    except ValueError as exc:warnings.append(f'changes.json: {exc}')
    from .views import warnings as views_warnings
    try:warnings+=['views: '+w for w in views_warnings(root)]
    except ValueError as exc:warnings.append(f'views.json: {exc}')
    stale=staleness_warning(root,sum(counts.values()))
    if stale:warnings.append(stale)
    from .logs import summary as log_summary
    logged=log_summary(root)
    if logged['errors_7d']:warnings.append(f"{logged['errors_7d']} errors logged in the last 7 days; run logs --level error --days 7")
    if domain:index='fresh' if _fresh(domain,root,inventory(domain,root,retired=True)) else 'dirty_or_missing'
    last=read_json(root/'hooks/last-delivery.json',{})
    domain_exists=bool(domain and (root/'domains'/domain/'notes').is_dir())
    from .storage import diagnostics
    from .semantic import status as semantic_status
    location = diagnostics(explicit)
    semantic=semantic_status(root)
    if semantic.get('error'):warnings.append(f"semantic model {semantic['model']} cannot be read ({semantic['error']}); search is lexical-only until it is reinstalled or removed")
    return {'semantic':{k:semantic[k] for k in ('enabled','model','backend') if k in semantic},'ok':fts and not pending and not unreadable and (not domain or domain_exists) and not hook_wiring.broken(wired),'problems':unreadable,'interpreter':sys.executable,'python':sys.version.split()[0],'fts5':fts,'root':str(root),'location':location,'domain':domain,'binding':'bound' if domain else 'unbound','domain_data':'present' if domain_exists else 'missing' if domain else 'unbound','index':index,'pending_recovery':pending,'hooks':{'trust':'unknown','last_delivery':last,'wiring':wired},'logs':logged,'capture':'main_agent_via_scripts','protection':'cooperating_agent_governance','notes':counts,'warnings':warnings}

SECTION_ARGS=('summary','detail','link','action','done','important','section')
# update only: add to a list section, or replace one labelled bullet, without restating the rest of it.
AMEND_ARGS=('add_detail','set_detail','add_link','add_action','drop_section')
# A replace flag and the add flag for the same section contradict each other.
AMEND_CLASHES=(('detail','add_detail'),('detail','set_detail'),('link','add_link'),('action','add_action'),('done','add_action'))

def sections_args(p,update=False):
    """Structured note sections (see core/note_format.py); each replaces only its own section."""
    whole=' On update this replaces the whole section.' if update else ''
    p.add_argument('--summary',help='The summary that opens the note: the fact or decision in one or two plain sentences')
    p.add_argument('--section',nargs=2,action='append',metavar=('HEADING','TEXT'),help="A section by heading, e.g. 'Why' 'We chose ...'. A decision has Why and Alternatives considered; a bug Cause, Fix and How to check; a runbook When to use, When not to use, Steps, Check and Roll back; research Findings, Sources and Open questions; a knowledge note sections named after what they explain (repeatable; on update it replaces that section only)")
    p.add_argument('--detail',action='append',help='Details bullet: a fact, or "**Label:** fact" when the label names something (repeatable).'+whole)
    p.add_argument('--link',action='append',help='Links bullet: [[Note]], a URL or a REF (repeatable).'+whole)
    p.add_argument('--action',action='append',help='Open action item; plan, bug and risk notes only (repeatable).'+whole)
    p.add_argument('--done',action='append',help='Completed action item (repeatable).'+whole)
    p.add_argument('--important',help='A caveat the reader must see, shown right after the summary')
    if update:
        p.add_argument('--drop-section',action='append',metavar='HEADING',help='Remove the section with this heading (repeatable)')
        p.add_argument('--add-detail',action='append',help='Append a Details bullet; the others are kept (repeatable)')
        p.add_argument('--set-detail',action='append',help='Replace the Details bullet that starts with the same bold label, e.g. "**Cause:** new text"; appended when no bullet has it (repeatable)')
        p.add_argument('--add-link',action='append',help='Append a Links bullet; the others are kept (repeatable)')
        p.add_argument('--add-action',action='append',help='Append an open action item; the others are kept (repeatable)')
        p.add_argument('--replace-actions',action='store_true',help='Let --action/--done drop action items the note already has')

def _update_body(args,base,note_type=None):
    """The body an update's section flags leave: replace flags overwrite their section, add flags extend it."""
    from . import note_format
    for replace,add in AMEND_CLASHES:
        if getattr(args,replace) and getattr(args,add):
            raise ValueError(f"--{replace} replaces the section and --{add.replace('_','-')} adds to it; pass one of them")
    if (args.action or args.done) and not args.replace_actions:
        kept={text.strip().casefold() for text in (args.action or [])+(args.done or [])}
        dropped=[text for _,text in note_format.action_items(base) if text and text.casefold() not in kept]
        if dropped:
            raise ValueError(f'replace_actions: --action/--done rewrite the whole action items list and would drop {len(dropped)} existing item(s): '
                             +'; '.join(repr(text) for text in dropped[:10])+". Add a step with --add-action, tick one with `check REF 'item'`, "
                             'or pass --replace-actions to rewrite the list')
    body=_sectioned(args,base,note_type) or base
    if any(getattr(args,k) for k in AMEND_ARGS):
        body=note_format.amend(body,args.add_detail or (),args.add_link or (),args.add_action or (),args.set_detail or ())
    for heading in args.drop_section or ():body=note_format.drop_section(body,heading)
    return body

def _replaced(args,stored,body):
    """{section: {before, after}} (lines of content) for each list section an update overwrote or shrank, so
    what a replace flag or a whole new body dropped is reported rather than lost quietly."""
    from .note_format import section_counts
    before,after=section_counts(stored),section_counts(body)
    flags={'details':args.detail,'links':args.link,'actions':args.action or args.done}
    return {key:{'before':before[key],'after':after[key]} for key in before if before[key] and (flags[key] or after[key]<before[key])}

def _sectioned(args,body='',note_type=None):
    """The body with any structured sections from the command line applied, or None if none were given."""
    if not any(getattr(args,k) for k in SECTION_ARGS):return None
    from .note_format import render
    return render(args.summary or '',args.detail or (),args.link or (),args.action or (),args.done or (),args.important or '',body,
                  named=[tuple(pair) for pair in args.section or ()],note_type=note_type)

# source-sync writes only the derived knowledge-base index (as search --rebuild does the notes' one), so subagents and viewers may run it
# (not for a remote source, which it would pull from the network: sources.sync refuses that for them).
READ_ONLY={'help','config','status','search','read','query','doctor','validate','history','logs','open','stats','timeline','recall','list','domains','version','links','projects','systems','ask','sources','source-sync','ingest-scan','changes','views','wiki','log','maintenance','recap','proposals'}
# Viewer: a human-facing app browsing the store. Read-only, and its reads are not usage evidence for the diary.
VIEWER=READ_ONLY|{'taxonomy','review','diff','inspect-plan'}
SERVE_API=2
SERVE_LIMIT=2*1024*1024

def _relative(root,path):
    try:return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:return str(path)

def list_notes(root,domains,status,types,tags,offset,limit,summary=False):
    """One page of notes across domains, newest update first, with the revision a later update must expect."""
    rows=[]
    for d in domains:
        for n in inventory(d,root,retired=status!='active'):
            if status!='any' and n.get('status','active')!=status:continue
            if types and n.get('type') not in types:continue
            if any(t not in (n.get('tags') or []) for t in tags):continue
            rows.append({'ref':f"{d}:{n['id']}",'id':n['id'],'domain':d,'title':n.get('title'),'type':n.get('type'),'status':n.get('status','active'),
                         'tags':n.get('tags') or [],'created':str(n.get('created') or ''),'updated':str(n.get('updated') or ''),'revision':n['revision'],'path':_relative(root,n['path'])})
            if isinstance(n.get('last_retirement'),dict):rows[-1]['last_retirement']={k:n['last_retirement'].get(k) for k in ('action','at','actor','reason')}
            if summary:rows[-1].update(_summary_fields(n))
    rows.sort(key=lambda r:(r['updated'],r['id']),reverse=True)
    return {'ok':True,'domains':domains,'total':len(rows),'offset':offset,'notes':rows[offset:offset+limit]}

def _summary_fields(note):
    """list --with-summary: the summary as plain text (300 characters at most), the files, and action item counts for plan, bug and risk notes."""
    from .note_format import sections,plain,action_items,ACTION_TYPES
    body=note.get('body') or '';text=sections(body).get('summary') or ''
    out={'summary':plain(text)[:300],'files':list(note.get('files') or []),'pinned':note.get('pinned') is True}
    if note.get('type') in ACTION_TYPES:items=action_items(body);out['actions']={'open':sum(1 for d,_ in items if not d),'done':sum(1 for d,_ in items if d)}
    return out

def list_domains(root):
    """Bound domains plus any domain folder on disk, with note counts and the search scope settings."""
    config=load_bindings(root);bound=config.get('domains',{});folder=root/'domains'
    names=sorted(set(bound)|{p.name for p in folder.iterdir() if p.is_dir()} if folder.is_dir() else set(bound))
    count=lambda name,sub:sum(1 for _ in (folder/name/sub).rglob('*.md')) if (folder/name/sub).is_dir() else 0
    scope=config.get('search_scope',{}) if isinstance(config.get('search_scope'),dict) else {}
    return {'ok':True,'domains':[{'name':n,'bound':n in bound,'prefixes':(bound.get(n) or {}).get('prefixes',[]),'active':count(n,'notes'),'archived':count(n,'archive')} for n in names],
            'aliases':config.get('domain_aliases',{}),'global_domains':scope.get('global_domains',['_global']),'foreign_domains':scope.get('foreign_domains',[])}

def _note_hits(root,domain,query,scope,limit,rebuild,filters,config,globals_,primary,foreign,explain=False):
    """The search command's answer: relevance-gated notes across the active, parent and global domains (foreign ones as a fallback).
    explain adds why: the semantic backend per domain and why it fell back, each candidate's gate verdict and scores, and
    for each result the section and level the prompt hook would deliver (refs, headings and numbers; no note text)."""
    from .search import search_domains
    from .runtime import relevance_gate,scope_domains,parent_domains,child_domains
    candidates=[] if explain else None;parents=parent_domains(domain,config);children=child_domains(domain,config)
    gate=relevance_gate(query,root,candidates);gated=True
    # As for prompts: global notes rank like the project's own, a parent's or child's count half.
    weights={**{g:2 for g in globals_},domain:2,**{p:1 for p in scope_domains(domain,config) if p!=domain}}
    def run(domains):
        # Relevance is judged within each domain before the merge, as for prompts, so weak local hits
        # cannot bury a strong match in a parent or global domain. An explicit search still answers:
        # when nothing passes the gate, the best ungated matches are returned and marked.
        nonlocal gated
        hits=search_domains(query,domains,limit,root,filters or None,rebuild,weights=weights,keep=gate)
        if hits:gated=True;return hits
        gated=False;return search_domains(query,domains,limit,root,filters or None,rebuild,weights=weights)
    domains=list(dict.fromkeys(sorted(load_bindings(root).get('domains',{}))+globals_)) if scope=='all' else primary
    hits=run(domains);fallback=False
    if not hits and scope=='fallback' and foreign:domains=foreign;hits=run(domains);fallback=True
    from . import digests
    digests.mark(hits,root,config)
    for hit in hits:
        if hit.get('stale'):hit['snippet']=f"{hit['stale']} — {hit.get('snippet','')}"
    for hit in hits:hit['sourceScope']='active' if hit['domain']==domain else 'global' if hit['domain'] in globals_ else 'parent' if hit['domain'] in parents else 'child' if hit['domain'] in children else 'foreign';hit.pop('path',None)
    decisions={}
    if explain:
        from . import injection
        from .search import stem,query_terms
        from .semantic import threshold
        wanted={stem(t) for t in query_terms(query)};near=threshold(root)
        for hit in hits:
            unit=injection.choose(hit,wanted,near,prompt=query);decisions[f"{hit['domain']}:{hit['id']}"]={'level':injection.level_of(unit,query),'section':unit['heading'] if unit else None}
    for hit in hits:
        # The sections that matched best, by heading and line span; `read REF --section HEADING` shows one.
        units=hit.pop('units',None)
        if units:hit['evidence']=[{k:u[k] for k in ('heading','role','lines','similarity')} for u in units if u['role']!='overview'][:2]
    result={'ok':True,'mode':scope,'domains':domains,'fallback':fallback,'relevant':gated,'results':hits}
    if explain:
        from .search import LAST_SEMANTIC,_vector_identity
        from . import vectors
        result['explain']={'model':_vector_identity(root),'native':vectors.probe(),'native_enabled':vectors.enabled(root),
                           'semantic':{d:LAST_SEMANTIC.get(d) for d in domains if d in LAST_SEMANTIC},'candidates':candidates,'prompt_hook':decisions}
    if hits and not gated:result['hint']='No note passed the relevance check; these are the closest matches, least likely to apply.'
    return result

def _search_scope(root,domain):
    """(bindings, global domains that exist, the domains a search covers first, foreign domains) for a domain."""
    from .runtime import scope_domains
    config=load_bindings(root);scope=config.get('search_scope',{})
    globals_=[g for g in (canonical_domain(g,root) for g in scope.get('global_domains',['_global'])) if (root/'domains'/g).exists()]
    return config,globals_,list(dict.fromkeys([domain]+scope_domains(domain,config)+globals_)),scope.get('foreign_domains',[])

def _each_question(query,limit,run):
    """[{question, ...run(question, per-question limit)}] for a query holding several questions (runtime.questions), else None.
    At most MAX_QUESTIONS are answered; the rest are only counted, so a quiz cannot make the command crawl."""
    from .runtime import questions,MAX_QUESTIONS
    found=questions(query)
    return [dict(question=q,**run(q,min(limit,3))) for q in found[:MAX_QUESTIONS]] if found else None

def _ask(root,domain,query,limit):
    """Notes first (the search command's ranking), then source excerpts from the project knowledge base, each labelled.
    A query of several questions is also answered one by one: `questions` holds each question's own notes and sources."""
    from . import sources
    if not query.strip():raise ValueError('ask needs a question')
    limit=max(1,min(20,limit));config,globals_,primary,foreign=_search_scope(root,domain)
    found=_note_hits(root,domain,query,'fallback',limit,False,{},config,globals_,primary,foreign)
    notes=[dict(h,label='note') for h in found['results']];excerpts=sources.search(root,domain,query,limit);info=sources.state(root,domain)
    result={'ok':True,'query':query,'domain':domain,'notes':notes,'sources':excerpts,'relevant':found['relevant'],
            'knowledge_base':dict(info,syncing=sources.syncing(root,domain)) if info else {'files':0,'syncing':sources.syncing(root,domain)}}
    if notes and not found['relevant']:result['hint']='No note passed the relevance check; the notes are the closest matches, least likely to apply.'
    if not info:result['knowledge_base']['hint']='No knowledge-base index yet: run source-sync (it also starts at session start). Sources: `sources`.'
    elif not excerpts:result['knowledge_base']['hint']='No source excerpt matched; try other words, or read the files.'
    def one(q,n):
        found=_note_hits(root,domain,q,'fallback',n,False,{},config,globals_,primary,foreign)
        return {'notes':[dict(h,label='note') for h in found['results']],'sources':sources.search(root,domain,q,n),'relevant':found['relevant']}
    each=_each_question(query,limit,one)
    if each:result['questions']=each
    return result

def _emit(result,args):
    """Text for a person at a terminal, else compact JSON (agents, apps, pipes, --json)."""
    if not args.json and sys.stdout.isatty():
        from .render import render
        print(render(result))
    else:print(json.dumps(result,ensure_ascii=False,separators=(',',':')))

UNBOUND='unbound_project: no memory domain is bound to this folder'

def error_code(message,exc=None):
    """busy for a lock timeout (common.file_lock); else stale, conflict or invalid, from the message's own wording: the text before any
    quoted value or ': detail', so a note title, tag or path inside the message cannot change the code."""
    import re
    if isinstance(exc,TimeoutError) and message.startswith('timed out locking'):return 'busy'   # common.file_lock
    head=re.split(r''': |["']''',message,maxsplit=1)[0].lower()
    return 'hivemind' if head=='hivemind' else 'stale' if 'stale' in head else 'conflict' if 'conflict' in head else 'invalid'

def next_step(code,message,command=None):
    """One plain next step for an error an agent can act on, else None."""
    if code=='busy':return 'Another memory command holds the store lock; retry in a few seconds.'
    if message.startswith('unbound_project'):return 'Pass --domain DOMAIN (alm domains lists them), or bind this folder with: bind DOMAIN FOLDER'
    if message.startswith('no_store'):return 'Check the --root path. To start a store, create the folder, then run: bind DOMAIN PROJECT_FOLDER --root FOLDER'
    if message.startswith('bad_arguments'):return f"Run: {command or 'alm'} --help"
    if message.startswith('not_found: no operation'):return 'List operation IDs with: history'
    if message.startswith(('Expected one note ID','No note with ID')):return "Find the note's ref with: search 'words from its title'"
    return None

def report_error(message,code,as_json,command=None):
    """Print an error the way main does (one line plus next: at a terminal, else the JSON error shape on stderr); return its exit code."""
    hint=next_step(code,message,command)
    if not as_json and sys.stderr.isatty():print(f'alm: error ({code}): {message}'+(f'\nnext: {hint}' if hint else ''),file=sys.stderr)
    else:print(json.dumps({'ok':False,'error':dict({'code':code,'message':message},**({'next':hint} if hint else {}))},separators=(',',':')),file=sys.stderr)
    return 6 if code=='stale' else 5 if code=='conflict' else 2

class _ArgumentError(Exception):
    def __init__(self,message,prog):super().__init__(message);self.prog=prog

class JsonArgumentParser(argparse.ArgumentParser):
    """Raises on a usage error so main can report it in the JSON error shape; subcommand parsers inherit the class."""
    def error(self,message):raise _ArgumentError('bad_arguments: '+message,self.prog)

def serve(root,cwd,commands):
    """JSON lines on stdin: {"id", "argv", "stdin"?} -> one {"id", "exit_code", "result", "error"} line each, run in-process."""
    import contextlib,io
    real_in,real_out=sys.stdin,sys.stdout
    if hasattr(real_in,'reconfigure'):
        try:real_in.reconfigure(encoding='utf-8')
        except (ValueError,OSError):pass
    def reply(value):real_out.write(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n');real_out.flush()
    while True:
        line=real_in.readline()
        if not line:return 0
        if not line.strip():continue
        ident=None
        try:
            if len(line.encode())>SERVE_LIMIT:raise ValueError('Request exceeds 2 MiB')
            request=json.loads(line)
            if not isinstance(request,dict):raise ValueError('Request must be a JSON object')
            ident=request.get('id');argv=request.get('argv');stdin=request.get('stdin','')
            # The command comes first so it can be checked; its options follow it.
            if not isinstance(argv,list) or not argv or any(not isinstance(v,str) for v in argv) or argv[0] not in commands:raise ValueError('argv must start with a command: '+', '.join(sorted(commands)))
            if not isinstance(stdin,str):raise ValueError('stdin must be a string')
        except ValueError as exc:
            reply({'id':ident,'exit_code':2,'result':None,'error':{'code':'invalid','message':str(exc)}});continue
        out=io.StringIO();error=io.StringIO();sys.stdin=io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out),contextlib.redirect_stderr(error):
                try:code=main(['--json','--root',str(root),'--cwd',str(cwd)]+argv)
                except SystemExit as exc:code=exc.code if isinstance(exc.code,int) else 2
        except Exception as exc:  # one failing request must not end the session
            from .logs import swallowed
            swallowed('cli.serve',exc,root)
            code=1;error=io.StringIO(json.dumps({'ok':False,'error':{'code':'internal','message':f'{type(exc).__name__}: {exc}'}}))
        finally:sys.stdin=real_in
        def decode(text):
            try:return json.loads(text) if text.strip() else None
            except ValueError:return {'text':text[:16000]}
        failure=decode(error.getvalue())
        if isinstance(failure,dict) and 'text' in failure:failure={'code':'invalid','message':failure['text'][:2000]}
        elif isinstance(failure,dict) and 'error' in failure:failure=failure['error']
        reply({'id':ident,'exit_code':code,'result':decode(out.getvalue()),'error':failure if code else None})

def _propose(root,data,domain,session,to_global,redact=False):
    """Stage one draft note for the main agent: checked like a capture where that is cheap, written only to .turns/.
    Credentials and text the user marked <private> are refused (or redacted) before anything reaches disk."""
    from . import proposals
    from .notes import quality_warnings,taxonomy
    from .note_format import format_body
    from .repository import find_secret
    if isinstance(data,list):raise ValueError('propose takes one note; run it once per note')
    data=dict(data)
    if (data.get('type') or 'knowledge') not in NOTE_TYPES:raise ValueError('Unknown note type: '+str(data.get('type'))+'; use one of '+', '.join(NOTE_TYPES))
    if redact:_redact(data)
    # Everything the draft file stores is screened, its digest metadata included.
    found=find_secret('\n'.join([str(data.get(k) or '') for k in ('title','body','context','idempotency_key')]+[c for c in data.get('commands') or [] if isinstance(c,str)]
                                +[json.dumps(data.get(k)) for k in ('files','sources') if data.get(k)]))
    if found:raise ValueError(f'Potential credential detected ({found[0]} at character {found[1]}); remove it, replace it with a placeholder such as <REDACTED>, or pass --redact')
    from .private import check as private_check
    private_check(_session_state(root,session),data)
    if not session:raise ValueError('propose needs --session SESSION (Claude Code sets CLAUDE_CODE_SESSION_ID for shell commands)')
    if to_global:
        from .common import global_domain
        target=global_domain(root)
    else:target=data.get('domain') or domain
    if not target:raise ValueError('Project is unbound; pass --domain or bind it')
    title=str(data.get('title') or '').strip()
    if not title or not str(data.get('body') or '').strip():raise ValueError('A proposal needs a title and content (--summary, --section, --detail or a body)')
    known=taxonomy(root);unknown=[t for t in data.get('tags') or [] if t not in known]
    if unknown:raise ValueError('Unknown tags: '+', '.join(unknown)+'; use tags that taxonomy lists')
    data=dict(data,title=title,body=format_body(data['body']))
    warnings=quality_warnings({'title':title,'body':data['body'],'type':data.get('type') or 'knowledge'})
    row=proposals.write(root,session,target,data,warnings)
    from .usage import record
    record(root,'propose',[None],session,kind=data.get('type') or 'knowledge')
    result={'ok':True,'id':row['id'],'status':'proposed','domain':target,'title':title,
            'next':f"Name proposal {row['id']} in your report; the main agent saves it with capture --from-proposal {row['id']} or rejects it."}
    if warnings:result['quality_warnings']=warnings
    return result

def _maintenance_hint(root):
    """The `review` hint's sentence about automatic maintenance, only while it is enabled."""
    from . import maintenance_state as ms
    try:on=ms.config(root)['enabled']
    except ValueError:on=False
    return 'Automatic maintenance is on for this store: within its policy it archives, resolves and supersedes notes without a plan and records why (maintenance history); restore REF undoes an archive. ' if on else ''

HELP={'setup':'Choose or move the shared memory folder','bind':'Bind a domain to project folders','unbind':'Remove a domain binding','capture':'Save a note','propose':'Stage a draft note for the main agent (subagents)','proposals':'List, show or reject staged draft notes','search':'Find notes','read':'Show one note',
 'update':'Change a note','supersede':'Retire a note in favour of another','review':'Notes that are due, stale or duplicated','open':'Open plans, bugs, risks and action items','check':'Tick an action item',
 'timeline':'Notes around a date or note','ingest-plan':'Plan importing Claude auto-memory files','export':'Write notes to a zip file','import-plan':'Plan importing an exported zip','confirm':'Mark a note still true',
 'resolve':'Finish a plan, bug or risk','reopen':'Reopen a resolved note','restore':'Bring back an archived note','stats':'Usage over the last days','logs':'Show the operational log','plan':'Plan a change to one note',
 'apply':'Apply a plan','inspect-plan':'Show a plan in full','rewrite-plan':'Plan rewriting several note bodies','merge-plan':'Plan merging notes','history':'Recent operations','recap':'Summary of recent activity','log':'Note and change history',
 'undo':'Undo an operation','recover':'Finish or abandon an interrupted operation','diff':'Show what an operation changed','doctor':'Check the install and the store','backup':'Back up the store',
 'restore-plan':'Plan restoring a backup','migration-plan':'Plan a store migration','note-type-plan':'Plan note type changes','baseline-plan':'Plan a verification baseline','rebuild':'Rebuild search indexes',
 'maintenance':'Automatic maintenance','semantic':'Semantic search settings','validate':'Check notes against the rules','query':'Query the store','taxonomy-plan':'Plan a tag taxonomy change','taxonomy':'Show the tag taxonomy',
 'project-plan':'Plan a project registry change','projects':'List projects','system-plan':'Plan a system registry change','systems':'List systems','source-plan':'Plan a knowledge-base source change','sources':'List knowledge-base sources',
 'source-sync':'Re-index knowledge-base sources','ingest-scan':'Scan for files to ingest','ingest-skip':'Skip an ingest candidate','ask':'Ask a question about the project','change-plan':'Plan a tracked change',
 'changes':'List tracked changes','views':'Saved views','wiki':'Domain wiki','batch':'Run several commands from a JSON file','retag-plan':'Plan retagging notes','remap-plan':'Plan moving notes between domains','promote-plan':'Plan promoting notes to the global domain',
 'recall':'Search this session transcript','off':'Turn Living Memory off for this session','on':'Turn Living Memory back on for this session','status':'Whether Living Memory is on for this session','capture-finalize':'Record which notes a turn saved','list':'List notes','domains':'List domains','config':'Show or change store settings','links':'Links between notes',
 'help':'Protocol details by topic','version':'Version and command list','serve':'JSON-lines server on stdin and stdout','sync':'Sync a domain with an Obsidian vault','overlay-build':'Build the Obsidian overlay','overlay-teardown-plan':'Plan removing the overlay','overlay-sync-all':'Sync every domain with the vault'}

# The groups `alm --help` lists the commands under (CLI-2); every command is in exactly one.
HELP_GROUPS=(
 ('Everyday',('help','capture','propose','proposals','search','read','update','supersede','open','check','confirm','resolve','reopen','ask','list','timeline','links','log','recap','recall','capture-finalize','status','off','on')),
 ('Maintenance',('review','maintenance','stats','history','diff','undo','recover','restore','doctor','validate','rebuild','semantic','backup','logs')),
 ('Governance (plans)',('plan','apply','inspect-plan','rewrite-plan','merge-plan','retag-plan','remap-plan','promote-plan','taxonomy-plan','note-type-plan','migration-plan','baseline-plan','restore-plan',
   'import-plan','ingest-plan','project-plan','system-plan','source-plan','change-plan','overlay-teardown-plan')),
 ('Admin',('setup','bind','unbind','config','domains','projects','systems','sources','source-sync','ingest-scan','ingest-skip','taxonomy','changes','views','wiki','export','query','batch','sync','overlay-build','overlay-sync-all','serve','version')))

def _grouped_commands():
    width=max(len(name) for _,names in HELP_GROUPS for name in names)+2
    return '\n\n'.join(heading+':\n'+'\n'.join(f'  {name:<{width}}{HELP.get(name,"")}' for name in names) for heading,names in HELP_GROUPS)

def build_parser():
    parser=JsonArgumentParser(prog='alm',description='Agentic Living Memory: the notes your coding agents keep between sessions. Run alm COMMAND --help for a command.',
                              epilog=_grouped_commands(),formatter_class=argparse.RawDescriptionHelpFormatter)
    def common(p,suppress=False):
        default=argparse.SUPPRESS if suppress else None
        p.add_argument('--root',type=Path,default=default);p.add_argument('--domain',default=default);p.add_argument('--cwd',default=default)
        p.add_argument('--json',action='store_true',default=argparse.SUPPRESS if suppress else False)
        p.add_argument('--actor',choices=['main','subagent','viewer'],default=argparse.SUPPRESS if suppress else os.environ.get('LIVING_MEMORY_ACTOR','main'))
    common(parser);subs=parser.add_subparsers(dest='command',required=True,metavar='COMMAND',help='one of the commands listed below')
    # No help= here, so argparse does not print its own flat list; the epilog lists them by group.
    def sub(name):p=subs.add_parser(name,description=HELP.get(name));common(p,True);return p
    p=sub('setup');p.add_argument('--location',type=Path);p.add_argument('--non-interactive',action='store_true');p.add_argument('--source',type=Path);p.add_argument('--apply-plan');p.add_argument('--inspect-plan');p.add_argument('--expected-plan-sha256')
    from .instructions import TOPICS
    p=sub('help');p.add_argument('topic',nargs='?',choices=list(TOPICS))
    p=sub('bind');p.add_argument('name');p.add_argument('prefix',nargs='+')
    p=sub('config');p.add_argument('action',choices=['get','set']);p.add_argument('key',nargs='?',help='search_scope, repo_aliases, suggest_bind, auto_resolve or cross_project')
    p.add_argument('value',nargs='?',help='set: JSON, for example true, false, ["_global"] or {"repo": ["alias"]}; null restores the default')
    p=sub('unbind');p.add_argument('name',help='A bound domain with no notes left (move them with remap-plan first)')
    p.add_argument('--prefix',help='Remove only this folder from the binding and keep the domain and its notes (after a bound folder moved)')
    # Legacy forms keep working but are left out of help: --tags, --prefix and a third positional (tags).
    def capture_flags(p):
     p.add_argument('items',nargs='*',metavar='TITLE BODY',help='Title and body; or use --title and --summary');p.add_argument('--global',dest='to_global',action='store_true',help='Save to the global domain (knowledge that holds beyond this project); overrides --domain');p.add_argument('--payload-file',type=Path);p.add_argument('--content-file',type=Path);p.add_argument('--title');p.add_argument('--tag',action='append',default=[]);p.add_argument('--tags',default='',help=argparse.SUPPRESS);p.add_argument('--prefix',help=argparse.SUPPRESS);p.add_argument('--type',choices=NOTE_TYPES,help='Note type (default: knowledge)');p.add_argument('--context',default='');p.add_argument('--idempotency-key');p.add_argument('--session');p.add_argument('--turn');p.add_argument('--allow-similar',action='store_true');p.add_argument('--supersedes',action='append',default=[],help="REF of a note this one replaces; it is retired in the same transaction, and its links and files are carried over (see --drop-links)");p.add_argument('--drop-links',action='store_true',help='With --supersedes: do not carry the links and files of the replaced note over');p.add_argument('--review-by',help='YYYY-MM-DD; status/plan/bug/risk/map/runbook/brief notes get a default');p.add_argument('--pin',action='store_true',help=PIN_HELP)
     for facet in CAPTURE_FACETS:p.add_argument('--'+facet,action='append',default=[])
     p.add_argument('--redact',action='store_true',help="Replace credentials with <redacted type='KIND'/> instead of rejecting the note")
     sections_args(p);_file_flags(p)
    p=sub('capture');capture_flags(p)
    p.add_argument('--from-proposal',metavar='ID',help='Settle this subagent proposal: alone, save it as staged; with a full note (title and content), save that edited note instead')
    p=sub('propose');capture_flags(p)
    p=sub('proposals');p.add_argument('action',nargs='?',choices=['list','show','reject'],default='list');p.add_argument('id',nargs='?',help='show, reject: the proposal ID')
    p.add_argument('--status',choices=['pending','accepted','rejected','any'],default='pending',help='list: which proposals (default pending)');p.add_argument('--session',help='list: only this session')
    p.add_argument('--reason',help='reject: why, one line')
    p=sub('search');p.add_argument('query',nargs='?',default='');p.add_argument('--file',help='Notes about this file or folder: listed in their files property, or named in the body');p.add_argument('--scope',choices=['active','fallback','all'],default='fallback');p.add_argument('--limit',type=int,default=5);p.add_argument('--filter',action='append',default=[]);p.add_argument('--rebuild',action='store_true');p.add_argument('--sources',action='store_true',help='Source excerpts from the project knowledge base only');p.add_argument('--explain',action='store_true',help='Add why each note was found: backend, scores, gate and the section the prompt hook would deliver')
    p=sub('read');p.add_argument('reference');p.add_argument('--max-chars',type=int,default=16000);p.add_argument('--metadata-only',action='store_true');p.add_argument('--section',help='Only this section, by heading (as search evidence and prompt lines name it)')
    p=sub('update');p.add_argument('reference');p.add_argument('--title');p.add_argument('--body');p.add_argument('--body-file',type=Path);p.add_argument('--tag',action='append');p.add_argument('--type',choices=NOTE_TYPES);p.add_argument('--context');p.add_argument('--review-by');p.add_argument('--expected-revision');p.add_argument('--payload-file',type=Path);p.add_argument('--session');p.add_argument('--turn')
    pins=p.add_mutually_exclusive_group();pins.add_argument('--pin',dest='pin',action='store_const',const=True,help=PIN_HELP);pins.add_argument('--unpin',dest='pin',action='store_const',const=False,help='Remove retirement protection (a pin change never counts as checking the note)')
    p.add_argument('--distinct',metavar='REF',help='Record that this note and REF (same domain) are different subjects, so neither replaces the other; needs --why')
    p.add_argument('--why',help='With --distinct: the reason, one line of 8 to 200 characters')
    p.add_argument('--not-distinct',metavar='REF',dest='not_distinct',help='Remove the distinct record between this note and REF (kept on either note), so maintenance may review the pair again')
    for facet in FACET_FLAGS:p.add_argument('--'+facet,action='append')
    p.add_argument('--redact',action='store_true',help="Replace credentials with <redacted type='KIND'/> instead of rejecting the note")
    sections_args(p,update=True);_file_flags(p)
    p=sub('supersede');p.add_argument('reference');p.add_argument('--by',required=True,help='REF of the note that replaces it; its links and files are carried over (see --drop-links)');p.add_argument('--drop-links',action='store_true',help='Do not carry the links and files of the replaced note over');p.add_argument('--session');p.add_argument('--turn')
    p=sub('review');p.add_argument('--kind',action='append',choices=['stale','due','interim','duplicate','warned','noisy'],help='Only this kind of row (repeatable): stale, due, interim, duplicate, warned, noisy');p.add_argument('--limit',type=int,default=20);p.add_argument('--all-domains',action='store_true',help='Every domain folder, not only --domain');p.add_argument('--tag',action='append',default=[],help='Only notes with this tag (repeatable: all must match)')
    p=sub('open');p.add_argument('--limit',type=int,default=50);p.add_argument('--all-domains',action='store_true',help='Every domain folder, not parents and global only');p.add_argument('--tag',action='append',default=[],help='Only notes with this tag (repeatable: all must match)')
    p=sub('check');p.add_argument('reference');p.add_argument('item',nargs='?',help='Text of the action item: exact, or a unique part of it');p.add_argument('--index',type=int,help="1-based position among the note's action items, as open lists it");p.add_argument('--undo',action='store_true',help='Untick instead');p.add_argument('--session');p.add_argument('--turn')
    p=sub('timeline');p.add_argument('reference',nargs='?',help='Anchor note; default: the newest note');p.add_argument('--date',help='YYYY-MM-DD: anchor at the first note on or after this date');p.add_argument('--before',type=int,default=5);p.add_argument('--after',type=int,default=5);p.add_argument('--type',choices=NOTE_TYPES,action='append');p.add_argument('--by',choices=['updated','created'],default='updated');p.add_argument('--all',action='store_true',help='Include superseded, archived and resolved notes')
    p=sub('ingest-plan');p.add_argument('--source',type=Path,help="Auto-memory folder; default: Claude Code's folder for --cwd");p.add_argument('--redact',action='store_true',help="Replace credentials with <redacted type='KIND'/> instead of skipping the file");p.add_argument('--allow-similar',action='store_true')
    p=sub('export');p.add_argument('--out',type=Path,required=True,help='New .zip file, outside the store');p.add_argument('--type',choices=NOTE_TYPES,action='append');p.add_argument('--tag',action='append',default=[],help='Only notes with this tag (repeatable: all must match)');p.add_argument('--all',action='store_true',help='Include superseded, archived and resolved notes')
    p=sub('import-plan');p.add_argument('file',type=Path)
    p=sub('confirm');p.add_argument('reference');p.add_argument('--session');p.add_argument('--turn')
    for name in ('resolve','reopen'):p=sub(name);p.add_argument('reference',help='A plan, bug or risk note');p.add_argument('--session');p.add_argument('--turn')
    p=sub('restore');p.add_argument('reference',help='An archived note: it returns to notes/, pinned, with its id, body and verification baseline kept (a resolved note uses reopen, a superseded one is read through its successor)');p.add_argument('--expected-revision');p.add_argument('--session');p.add_argument('--turn')
    p=sub('stats');p.add_argument('--days',type=int,default=30,help='Window, at most 30 (the diary keeps 30 days)');p.add_argument('--limit',type=int,default=10)
    p=sub('logs');p.add_argument('--level',choices=['debug','info','warning','error'],default='info',help='Lowest level shown');p.add_argument('--kind',choices=['hook','command','internal']);p.add_argument('--event',help='Hook event, command name or internal call site');p.add_argument('--days',type=int,default=1,help='Window, at most 30 (logs keep 30 days)');p.add_argument('--limit',type=int,default=50)
    p=sub('plan');p.add_argument('action',choices=['update','rename','retag','resolve','reopen','restore','archive','supersede','delete']);p.add_argument('reference');p.add_argument('--payload-file',type=Path)
    p=sub('apply');p.add_argument('plan_id');p.add_argument('--expected-plan-sha256',required=True)
    p=sub('inspect-plan');p.add_argument('plan_id');p.add_argument('--full',action='store_true',help='Show every diff in full instead of cutting each at 16000 characters')
    # A reviewed plan replacing the bodies of several notes (a prose rewrite); titles never change.
    p=sub('rewrite-plan');p.add_argument('--payload-file',type=Path,help='JSON list of {"ref", "revision", "body"}; default: stdin')
    p=sub('merge-plan');p.add_argument('--payload-file',type=Path)
    p=sub('history');p.add_argument('--note');p.add_argument('--limit',type=int,default=20);p.add_argument('--markdown',action='store_true',help='Return {ok, rows, markdown} with a dated list a person can read')
    p=sub('recap');p.add_argument('--days',type=int,default=7,help='Days back, 1 to 90');p.add_argument('--project',help='Project slug');p.add_argument('--all-domains',action='store_true',help='Every domain folder, not only --domain')
    p=sub('log');p.add_argument('--project',help='Project slug');p.add_argument('--since',help='ISO date or time');p.add_argument('--until',help='ISO date or time, inclusive');p.add_argument('--note',help='One note (REF or ID, deleted notes too) or change (CHG-014)');p.add_argument('--type',help='Note type, change or session');p.add_argument('--event',help='created, updated, confirmed, superseded, resolved, archived, reopened, deleted, promoted, moved, undone, stage, closed or session');p.add_argument('--limit',type=int,default=50)
    p=sub('undo');p.add_argument('operation_id')
    p=sub('recover');p.add_argument('operation_id');p.add_argument('--direction',choices=['forward','backward'],default='forward');p.add_argument('--abandon',action='store_true',help='Stop treating the operation as pending; no note file is changed')
    p=sub('diff');p.add_argument('operation_id')
    p=sub('doctor')
    p=sub('backup');p.add_argument('--keep',type=int,default=7)
    p=sub('restore-plan');p.add_argument('file',type=Path)
    p=sub('migration-plan')
    p=sub('note-type-plan')
    p=sub('baseline-plan')
    p=sub('rebuild');p.add_argument('name',nargs='?')
    p=sub('maintenance');p.add_argument('action',choices=['status','preview','run','enable','disable','history'])
    p.add_argument('--limit',type=int,help='preview, history: at most this many rows (default 10)');p.add_argument('--run',help='history: one run by ID, with its per-note decisions')
    p.add_argument('--dry-run',action='store_true',help='run: call the model and validate its decisions, but change no notes (still uses the daily allowance; works while disabled)')
    p.add_argument('--all-domains',action='store_true',help='enable: every configured non-global domain');p.add_argument('--model',help='enable: model alias (default sonnet)')
    p.add_argument('--max-notes-per-day',type=int,help='enable: notes reviewed per day (default 10)');p.add_argument('--timeout-seconds',type=int,help='enable: run time limit (default 300)')
    p.add_argument('--daily-budget-usd',help='enable: spending limit per day as a decimal string (default "1.00")');p.add_argument('--scheduled',action='store_true',help=argparse.SUPPRESS)
    p=sub('semantic');p.add_argument('action',choices=['status','install','remove','rebuild','native']);p.add_argument('value',nargs='?',choices=['on','off'],help='native: on or off');p.add_argument('--model',help='Model to install (default: the recommended one)');p.add_argument('--from',dest='source',type=Path,help='Folder already holding the model files, for an offline install; hashes are still verified')
    p=sub('validate');p.add_argument('name',nargs='?')
    p=sub('query');p.add_argument('sql')
    p=sub('taxonomy-plan');p.add_argument('tags',nargs='+')
    p=sub('taxonomy')
    p=sub('project-plan');p.add_argument('action',choices=['create','update','archive']);p.add_argument('--payload-file',type=Path)
    p=sub('projects');p.add_argument('--status',choices=['active','paused','archived','any'],default='any')
    p=sub('system-plan');p.add_argument('action',choices=['create','update','retire']);p.add_argument('--payload-file',type=Path)
    p=sub('systems');p.add_argument('--status',choices=['active','deprecated','retired','any'],default='any')
    p=sub('source-plan');p.add_argument('action',choices=['add','remove','implicit']);p.add_argument('--payload-file',type=Path)
    p=sub('sources')
    p=sub('source-sync');p.add_argument('name',nargs='?',help='One source; default: all of the domain');p.add_argument('--rebuild',action='store_true',help='Drop and re-read what was indexed (all sources, or just NAME; a remote NAME is also fetched afresh)')
    p.add_argument('--all-remote',action='store_true',help='Also pull every confluence and notion source of the domain from the network (a remote NAME is always pulled; plain source-sync never touches the network)')
    p=sub('ingest-scan');p.add_argument('--source',help='One source (a `sources` name); default: all of the domain');p.add_argument('--limit',type=int,default=50);p.add_argument('--changed',action='store_true',help='Only files with no digest yet or a stale one')
    p=sub('ingest-skip');p.add_argument('--payload-file',type=Path)
    p=sub('ask');p.add_argument('query',nargs='+',help='A question about the project');p.add_argument('--limit',type=int,default=5,help='Notes and source excerpts shown, at most, of each')
    p=sub('change-plan');p.add_argument('action',choices=['create','update','close']);p.add_argument('--payload-file',type=Path)
    p=sub('changes');p.add_argument('--status',choices=['active','done','cancelled','any'],default='any');p.add_argument('--project',help='Only the changes of this project slug')
    p=sub('views');p.add_argument('action',nargs='?',choices=['list','add','remove','render','published'],help='Default: list');p.add_argument('reference',nargs='?',help='The note (REF, ID prefix or exact title)')
    p.add_argument('--all-domains',action='store_true',help='list: every domain, not only --domain');p.add_argument('--title',help="add: the view's short title (default: the note title)")
    p.add_argument('--all',action='store_true',help='render: every registered view');p.add_argument('--target',choices=['local','artifact'],default='local',help="render: local (dark, reloads itself) or artifact (follows the viewer's theme)")
    p.add_argument('--format',choices=['html','json'],default='html',help='render: the page, or the view model as JSON');p.add_argument('--out',type=Path,help='render: write here instead of the standard path');p.add_argument('--url',help='published: where the artifact page lives')
    p=sub('wiki');p.add_argument('--archived',action='store_true',help='Include retired notes')
    p.add_argument('--format',choices=['html','json'],default='html',help='The page, or the wiki model as JSON (json without --out prints it)');p.add_argument('--out',type=Path,help='Write here instead of <store>/.views/<domain>/wiki.html')
    p=sub('batch');p.add_argument('--payload-file',type=Path)
    p=sub('retag-plan');p.add_argument('--payload-file',type=Path)
    p=sub('remap-plan');p.add_argument('--payload-file',type=Path)
    p=sub('promote-plan');p.add_argument('refs',nargs='*',help='Project notes to move to the global domain');p.add_argument('--suggest',action='store_true',help='List candidates with reasons; writes nothing');p.add_argument('--only',help='With --suggest: one domain instead of all')
    p=sub('recall');p.add_argument('query');p.add_argument('--session',required=True);p.add_argument('--limit',type=int,default=5);p.add_argument('--all',action='store_true',help='Search the whole transcript, not only what precedes the latest compaction')
    for name in sorted(SWITCH):p=sub(name);p.add_argument('--session',help='Session ID; default: $CLAUDE_CODE_SESSION_ID, which Claude Code sets for shell commands')
    p=sub('capture-finalize');p.add_argument('--session',required=True);p.add_argument('--turn',help='Defaults to the latest prompt of the session');p.add_argument('--note',action='append',default=[])
    p=sub('list');p.add_argument('--status',choices=['active','resolved','archived','superseded','any'],default='active');p.add_argument('--type',choices=NOTE_TYPES,action='append');p.add_argument('--tag',action='append',default=[],help='Only notes with this tag (repeatable: all must match)');p.add_argument('--all-domains',action='store_true',help='Every domain folder, not only --domain');p.add_argument('--with-summary',action='store_true',help='Add summary, files and actions {open, done} to each row');p.add_argument('--offset',type=int,default=0);p.add_argument('--limit',type=int,default=200)
    p=sub('domains')
    p=sub('links');p.add_argument('--all-domains',action='store_true',help='Every domain folder, not only --domain');p.add_argument('--status',choices=['active','any'],default='active',help='any: include archived, resolved and superseded notes')
    p=sub('version')
    p=sub('serve');p.add_argument('--stdio',action='store_true',required=True,help='JSON-lines requests on stdin, one reply line each on stdout')
    p=sub('sync');p.add_argument('vault',type=Path)
    p=sub('overlay-build');p.add_argument('vault',type=Path)
    p=sub('overlay-teardown-plan');p.add_argument('vault',type=Path)
    p=sub('overlay-sync-all');p.add_argument('--primary',help='Domain whose view the shared dashboards show (built last)')
    parser.subs=subs   # main() reads the command names from it
    return parser

def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure') and (stream.encoding or '').lower().replace('-','')!='utf8':
            try:stream.reconfigure(encoding='utf-8')
            except (ValueError,OSError):pass
    parser=build_parser();subs=parser.subs
    try:args=parser.parse_args(argv)
    except _ArgumentError as exc:
        given=argv if argv is not None else sys.argv[1:]
        # Leftover flags are reported by the top-level parser; name the command's own help.
        command=next((a for a in given if a in subs.choices),None) if exc.prog=='alm' else None
        return report_error(str(exc),'invalid','--json' in given,f'alm {command}' if command else exc.prog)
    cwd=args.cwd or os.getcwd()
    invocation={'start':time.perf_counter(),'argv':list(argv) if argv is not None else sys.argv[1:]}
    try:
        # Agents started inside a Hivemind terminal inherit HIVEMIND_SESSION; Hivemind's own backend does not. Every path (batch and serve
        # re-enter main) reaches this point before a governed command runs.
        if os.environ.get('HIVEMIND_SESSION') and (args.command in {'apply','undo','recover'} or (args.command=='setup' and args.apply_plan)):
            raise ValueError('hivemind: Memory plans are applied and undone by you in Hivemind, not by an agent. Write the plan; the user applies it.')
        if args.command=='help':
            from .instructions import help_topic
            _emit(help_topic(args.topic),args);return 0
        if args.command=='setup':
            from .storage import setup,apply_relocation,inspect_relocation
            if args.actor in {'subagent','viewer'}:raise ValueError('Subagents are read-only' if args.actor=='subagent' else 'Viewers are read-only')
            if args.inspect_plan:
                if args.apply_plan or args.location or args.source or args.expected_plan_sha256:raise ValueError('Inspection takes only a plan ID')
                result=inspect_relocation(args.inspect_plan)
            elif args.apply_plan:
                if not args.expected_plan_sha256:raise ValueError('--expected-plan-sha256 is required')
                if args.location or args.source:raise ValueError('Apply uses the locations in the approved plan')
                result=apply_relocation(args.apply_plan,args.expected_plan_sha256)
            else:
                if args.expected_plan_sha256:raise ValueError('--apply-plan is required')
                result=setup(args.location,args.non_interactive,args.source)
            _emit(result,args);return 0
        from .storage import resolve_root
        root=resolve_root(args.root)[0]
        if args.root is not None:
            from .storage import require_store
            require_store(root,args.command)
        from . import logs
        logs.use(root);invocation['root']=root
        cmd=args.command
        try:
            domain=args.domain or _session_domain(root,getattr(args,'session',None)) or resolve_domain(cwd,load_bindings(root))
            if domain:domain=canonical_domain(domain,root)
            invocation['domain']=domain
        except ValueError:
            # Snapshots exist to repair a damaged store, so restoring must not depend on readable bindings.
            if cmd not in {'restore-plan','inspect-plan','apply','backup','history'}:raise
            domain=None
        # propose writes only a draft in .turns/, which the main agent accepts or rejects.
        if args.actor=='subagent' and cmd not in READ_ONLY|{'propose'}:raise ValueError('Subagents are read-only; stage a draft with propose for the main agent')
        if cmd=='proposals' and args.action=='reject' and args.actor in {'subagent','viewer'}:raise ValueError('Only the main agent rejects proposals; run it as --actor main')
        if args.actor=='viewer' and cmd not in VIEWER:raise ValueError('Viewers are read-only; run changes as --actor main')
        if cmd=='views' and (args.action or 'list')!='list' and args.actor in {'subagent','viewer'} and not (args.action=='render' and args.format=='json' and not args.out):
            raise ValueError(('Subagents' if args.actor=='subagent' else 'Viewers')+' are read-only: views list and render --format json (no --out) are allowed; run the rest as --actor main')
        if cmd=='wiki' and (args.out or args.format!='json') and args.actor in {'subagent','viewer'}:
            raise ValueError(('Subagents' if args.actor=='subagent' else 'Viewers')+' are read-only: wiki --format json (no --out) is allowed; run the rest as --actor main')
        if cmd=='config' and args.action=='set' and args.actor in {'subagent','viewer'}:
            raise ValueError(('Subagents' if args.actor=='subagent' else 'Viewers')+' are read-only: config get is allowed; run config set as --actor main')
        if cmd=='maintenance' and args.action not in {'status','preview','history'} and args.actor in {'subagent','viewer'}:
            raise ValueError(('Subagents' if args.actor=='subagent' else 'Viewers')+' are read-only: maintenance status, preview and history are allowed; run the rest as --actor main')
        if cmd in SWITCH-{'status'} and args.actor in {'subagent','viewer'}:raise ValueError('Only the user turns Living Memory on or off; run it as --actor main')
        session=getattr(args,'session',None) or os.environ.get(SESSION_ENV)
        if cmd not in OFF_ALLOWED:
            from .runtime import env_off
            if env_off():raise ValueError('memory_off: LIVING_MEMORY_OFF=1 is set in this environment; unset it to use Living Memory')
            if session and _session_state(root,session).get('off'):
                raise ValueError('memory_off: Living Memory is off for this session; run alm on to use it again')
        if cmd=='serve':return serve(root,cwd,set(subs.choices)-{'serve','setup'})
        if cmd in SWITCH:result=_switch(root,cmd,session);invocation['result']=result;_emit(result,args);return 0
        if cmd=='bind':
            from .init_domain import init_domain
            init_domain(args.name,args.prefix,root);result={'ok':True,'domain':args.name,'status':'bound'}
        elif cmd=='config':
            from . import settings
            if args.action=='get':
                if args.value is not None:raise ValueError('config get takes at most a KEY')
                result=settings.get(root,args.key)
            else:
                if args.key is None or args.value is None:raise ValueError('config set needs KEY and VALUE')
                result=settings.set_(root,args.key,args.value)
        elif cmd=='unbind':
            from .init_domain import unbind_domain,unbind_prefix
            if args.prefix:result={'ok':True,'domain':args.name,'prefix':unbind_prefix(args.name,args.prefix,root),'status':'prefix_removed'}
            else:result={'ok':True,'domain':unbind_domain(args.name,root),'status':'unbound'}
        elif cmd in ('capture','propose'):
            from .notes import capture
            structured=any(getattr(args,k) for k in SECTION_ARGS)
            proposal=None
            if cmd=='capture' and args.from_proposal:
                from . import proposals
                proposal=proposals.get(root,args.from_proposal)
                if proposal.get('status')!='pending':raise ValueError(f"Proposal {args.from_proposal} is already {proposal.get('status')}")
                # The draft's domain is the default target, set before files and sources resolve against it: an edit changes the text,
                # not where the draft was meant to go. --domain, --global or a positional domain still choose another.
                if not args.domain and not getattr(args,'to_global',False) and proposal.get('domain'):domain=proposal['domain']
            edited=bool(args.items or args.content_file or structured or args.payload_file)
            if proposal and not edited:
                # Saved as staged. An edit passes the whole note, so a lone --title or --tag would be half an edit: refuse it.
                beside=[flag for flag,given in (('--title',args.title),('--tag',args.tag),('--type',args.type),('--file',args.file),('--files',args.files)) if given]
                if beside:raise ValueError('To edit a proposal pass the whole note (--title with --summary or a body) beside --from-proposal; '+', '.join(beside)+' alone would be lost')
                data=dict(proposal['note'],domain=proposal.get('domain') or domain)
                if args.turn and not args.session:raise ValueError('--turn needs --session')
                if args.session:data.update(session_id=args.session,turn_id=_turn(root,args.session,args.turn))
            elif args.items or args.content_file or structured:
                # With --title, a positional value is a mistake (most often a second value after one --detail flag); read as the
                # positional domain/title/body form it would silently replace the title or body, or name a wrong domain.
                if args.title and args.items:
                    raise ValueError(f"capture --title takes no positional values, got {len(args.items)} ({str(args.items[0])[:60]!r}...): "
                                     "pass each bullet with its own flag (--detail 'A' --detail 'B')")
                if len(args.items)==3:domain,title,body=args.items
                elif len(args.items)==2:title,body=args.items
                elif args.content_file and args.title:title=args.title;body=args.content_file.read_text(encoding='utf-8')
                elif structured and (args.title or len(args.items)==1):title=args.title or args.items[0];body=''
                else:raise ValueError('Use title body, domain title body, --title with --summary/--detail, or a JSON payload')
                body=_sectioned(args,body,args.type or 'knowledge') or body
                tags=args.tag+[t.strip() for t in args.tags.split(',') if t.strip()]
                tags += _facet_tags(args,CAPTURE_FACETS)
                data={'title':title,'body':body,'tags':tags,'prefix':args.prefix,'type':args.type or 'knowledge','context':args.context,'idempotency_key':args.idempotency_key,'domain':domain}
                if args.turn and not args.session:raise ValueError('--turn needs --session')
                if args.session and cmd=='capture':data.update(session_id=args.session,turn_id=_turn(root,args.session,args.turn))
                if args.allow_similar:data['allow_similar']=True
                if args.supersedes:data['supersedes']=args.supersedes
                if args.drop_links:data['drop_links']=True
                if args.pin:data['pinned']=True
                if args.review_by:data['review_by']=args.review_by
                files,_=_file_args(args,domain,root)
                if files:data['files']=files
                if args.shell_command:data['commands']=args.shell_command
                if args.source:
                    from .digests import describe
                    data['sources']=[describe(root,domain,v,cwd) for v in args.source]
                    if len(args.source)==1 and not args.idempotency_key:data['idempotency_key']=f"digest:{data['sources'][0]['path']}:{data['sources'][0]['sha256']}"   # a rerun finds its digest
            else:
                # A payload carries its own fields, so a flag beside it would be dropped without a word: refuse it.
                # The session is the exception: the private-text check, the origin and the receipt depend on it.
                beside=[('--title',args.title),('--tag',args.tag),('--tags',args.tags),('--prefix',args.prefix),('--type',args.type),('--context',args.context),
                        ('--idempotency-key',args.idempotency_key),('--allow-similar',args.allow_similar),('--supersedes',args.supersedes),('--drop-links',args.drop_links),
                        ('--review-by',args.review_by),('--file',args.file),('--files',args.files),('--command',args.shell_command),('--source',args.source),('--pin',args.pin)]+[('--'+f,getattr(args,f)) for f in CAPTURE_FACETS]
                ignored=[flag for flag,given in beside if given]
                if ignored:raise ValueError('A JSON payload carries its own fields; put these in the payload instead of passing '+', '.join(ignored))
                if args.turn and not args.session:raise ValueError('--turn needs --session')
                data=_payload(args)
                for item in (data if isinstance(data,list) else [data]) if args.session else ():
                    if isinstance(item,dict) and not item.get('session_id'):
                        item['session_id']=args.session
                        if args.turn and not item.get('turn_id'):item['turn_id']=args.turn
            if proposal and isinstance(data,list):raise ValueError('--from-proposal settles one proposal with one note')
            if cmd=='propose':
                result=_propose(root,data,domain,session,getattr(args,'to_global',False),args.redact)
                invocation['result']=result;_emit(result,args);return 0
            batch=data if isinstance(data,list) else [data]
            if not 1<=len(batch)<=20:raise ValueError('Capture batch must contain 1..20 items')
            results=[]
            with store_lock(root):
                if proposal:
                    from . import proposals
                    # Checked again under the lock, so two settlements of one proposal cannot both save a note.
                    if proposals.get(root,args.from_proposal).get('status')!='pending':raise ValueError(f'Proposal {args.from_proposal} was settled meanwhile')
                for item in batch:
                    item=dict(item);selected=item.pop('domain',domain);session=item.pop('session_id',None);turn=item.pop('turn_id',None)
                    if getattr(args,'to_global',False):
                        from .common import global_domain
                        selected=global_domain(root)
                    if session and not turn:turn=_turn(root,session,None)
                    if not selected:raise ValueError('Project is unbound; pass --domain or bind it')
                    inferred=_infer_tags(item,root,item.get('type')=='brief' and not item.get('files') and selected in _global_names(root));redacted=_redact(item) if args.redact else []
                    # Project attribution: capture only. cwd counts only for a session (serve passes the app's own cwd to every request).
                    from . import projects
                    ptags,candidates=projects.infer(item,selected,root,_session_state(root,session).get('touched',[]) if session else (),cwd if session else None)
                    if ptags:item['tags']=list(item.get('tags') or [])+ptags;inferred=inferred+ptags
                    # System attribution follows the same rules; a note may belong to a project and a system.
                    from . import systems
                    stags,system_candidates=systems.infer(item,selected,root,_session_state(root,session).get('touched',[]) if session else (),cwd if session else None)
                    if stags:item['tags']=list(item.get('tags') or [])+stags;inferred=inferred+stags
                    if session:
                        from .private import check as private_check
                        private_check(_session_state(root,session),item);item['origin']=_origin(root,session)
                    outcome=capture(domain=selected,root=root,**item);outcome.pop('path',None)
                    # A duplicate found in, or promoted to, another domain is that domain's note.
                    landed=outcome.get('domain') or selected
                    if inferred:outcome['inferred_tags']=inferred
                    if candidates:outcome['project_candidates']=candidates
                    if system_candidates:outcome['system_candidates']=system_candidates
                    if redacted:outcome['redacted']=redacted
                    suggested=[t for t in _session_state(root,session).get('touched',[]) if t not in (item.get('files') or [])] if session else []
                    if suggested and outcome.get('status')=='created':outcome['suggested_files']=suggested
                    results.append(outcome)
                    if outcome.get('status') in {'duplicate','existing','promoted'}:
                        # Re-stating a saved fact is evidence it still holds (#9 reinforcement).
                        from .usage import record
                        record(root,'duplicate',[landed+':'+outcome['id']],session)
                    if session and turn:
                        from .capture import finalize
                        finalize(session,turn,[landed+':'+outcome['id']],root)
                if proposal:
                    saved=f"{landed}:{results[0]['id']}"
                    proposals.settle(root,args.from_proposal,'accepted',note_ref=saved)
            result=results if isinstance(data,list) else results[0]
            if proposal:
                from .usage import record
                record(root,'proposal-accept',[saved],session,kind='edited' if edited else 'as-staged')
                result['proposal']={'id':args.from_proposal,'status':'accepted'}
        elif cmd=='proposals':
            from . import proposals
            if args.action=='list':
                if args.id or args.reason:raise ValueError('proposals list takes only --status and --session')
                rows=proposals.rows(root,args.status,args.session)
                result={'ok':True,'status':args.status,'proposals':[proposals.listing(r) for r in rows],
                        'next':'capture --from-proposal ID saves one (alone as staged; with a full note, edited); proposals reject ID --reason R drops it'}
            else:
                if not args.id:raise ValueError(f'proposals {args.action} needs the proposal ID')
                if args.action=='show':
                    if args.reason:raise ValueError('--reason belongs to reject')
                    result={'ok':True,**proposals.get(root,args.id)}
                else:
                    if not args.reason:raise ValueError('proposals reject needs --reason (one line: why it is not worth a note)')
                    with store_lock(root):row=proposals.settle(root,args.id,'rejected',reason=' '.join(args.reason.split())[:200])
                    from .usage import record
                    record(root,'proposal-reject',[None],session)
                    result={'ok':True,'id':row['id'],'status':'rejected'}
        elif cmd=='search':
            from .search import search_domains,search_domain
            if not domain:raise ValueError(UNBOUND)
            if args.sources:
                if args.file or args.filter:raise ValueError('--sources takes only a query (and --limit)')
                if not args.query.strip():raise ValueError('search needs a query')
                from . import sources
                result={'ok':True,'mode':'sources','domain':domain,'results':sources.search(root,domain,args.query,max(1,min(20,args.limit))),'knowledge_base':sources.state(root,domain)}
                each=_each_question(args.query,max(1,min(20,args.limit)),lambda q,n:{'results':sources.search(root,domain,q,n)})
                if each:result['questions']=each
                invocation['result']=result;_emit(result,args);return 0
            config=load_bindings(root);scope=config.get('search_scope',{});globals_=scope.get('global_domains',['_global'])
            globals_=[canonical_domain(g,root) for g in globals_]
            globals_=[g for g in globals_ if (root/'domains'/g).exists()]
            from .runtime import scope_domains
            primary=list(dict.fromkeys([domain]+scope_domains(domain,config)+globals_));foreign=scope.get('foreign_domains',[])
            if any('=' not in value for value in args.filter):raise ValueError("Search filters are KEY=VALUE with KEY one of prefix, status, tag, type, file (for example --filter type=decision)")
            filters=dict(value.split('=',1) for value in args.filter)
            if set(filters)-{'prefix','status','tag','type','file'}:raise ValueError('Unsupported search filter: use prefix, status, tag, type or file')
            if args.file:
                from .common import domain_prefixes,display_path
                given=Path(args.file).expanduser()
                wanted=(display_path(given.resolve(),domain_prefixes(domain,config)) or args.file) if given.is_absolute() else args.file
                from .runtime import child_domains,child_path
                kids_of=child_domains(domain,config)   # a child's notes name files relative to the child's folder, in every scope
                if not args.query.strip():
                    # Lists notes about a file; --filter narrows a query's results, so it needs a query.
                    if filters:raise ValueError('--filter needs a query; with --file alone, use --scope to choose domains')
                    from .search import file_notes
                    domains=list(dict.fromkeys(sorted(config.get('domains',{}))+globals_)) if args.scope=='all' else primary
                    n=max(1,min(100,args.limit));kids=kids_of
                    hits=file_notes(wanted,[d for d in domains if d not in kids],root,n);fallback=False
                    for kid in kids:   # a child's notes name files relative to the child's folder
                        c=child_path(wanted,domain,kid,config)
                        if c is not None:hits+=[dict(h,file=c) for h in file_notes(c,[kid],root,n)]
                    hits=sorted(sorted(hits,key=lambda h:h.get('updated',''),reverse=True),key=lambda h:h.get('match')!='files')[:n]
                    if not hits and args.scope=='fallback' and foreign:domains=foreign;hits=file_notes(wanted,domains,root,max(1,min(100,args.limit)));fallback=True
                    result={'ok':True,'mode':'file','file':wanted,'domains':domains,'fallback':fallback,'results':hits}
                    invocation['result']=result;_emit(result,args);return 0
                filters['file']={'*':wanted,**{k:child_path(wanted,domain,k,config) for k in kids_of}} if kids_of else wanted
            if not args.query.strip():raise ValueError('search needs a query, or --file PATH')
            result=_note_hits(root,domain,args.query,args.scope,args.limit,args.rebuild,filters,config,globals_,primary,foreign,args.explain)
            each=None if filters else _each_question(args.query,max(1,min(20,args.limit)),lambda q,n:{k:v for k,v in _note_hits(root,domain,q,args.scope,n,args.rebuild,filters,config,globals_,primary,foreign).items() if k in {'results','relevant'}})
            if each:result['questions']=each
        elif cmd=='read':
            from .notes import find_note
            d,n=_reference(args.reference,domain,root);path,note=find_note(n,d,root)
            result={k:v for k,v in note.items() if k not in {'body','path','digest'}}
            from .digests import stale_note
            stale=stale_note(note,root)   # a digest's source file, or a hashed file the note lists, changed since it was written
            if stale:result['stale']=stale
            from .worklist import currency_of
            try:
                info=currency_of(note,root)   # per-file source states (a map's: the cached finding, no git)
                if info:result['currency']=info
            except Exception as exc:
                from .logs import swallowed
                swallowed('cli.read.currency',exc,root)
            if args.section:
                # One section, as the prompt hook's evidence lines name it: "Why", or a path like "Notes > Deeper detail".
                # File line numbers are given only when the file still holds exactly the body served.
                from .units import extract,_norm,_sections,role_of
                from .repository import parse_text
                wanted=_norm(args.section);body=note['body']
                found=[(p,h,rows) for p,h,rows in _sections(body)[1:] if h and (_norm(' > '.join(p))==wanted or _norm(h)==wanted)]
                if not found:raise ValueError(f"no section {args.section!r} in this note; sections: {', '.join(' > '.join(p) for p,h,_ in _sections(body)[1:] if h) or 'none'}")
                path_,heading,rows=found[0];text='\n'.join(t for _,t in rows).strip()
                try:same=(parse_text(Path(path).read_text(encoding='utf-8-sig'),path) or {}).get('body')==body
                except (OSError,UnicodeDecodeError):same=False
                spans={}
                for u in extract(dict(note,path=path)) if same else []:spans.setdefault(u['heading'],u['lines'])
                span=spans.get(' > '.join(path_));last=max(n for n,t in rows if t.strip())
                result.update(section={'heading':' > '.join(path_),'role':role_of(heading),'lines':[span[0],span[0]+last-rows[0][0]] if span else None},
                              body=text[:max(0,min(64000,args.max_chars))],truncated=len(text)>args.max_chars)
            elif not args.metadata_only:result.update(body=note['body'][:max(0,min(64000,args.max_chars))],truncated=len(note['body'])>args.max_chars)
            if args.actor!='viewer':
                from .usage import record
                record(root,'read',[f'{d}:{n}'])
        elif cmd=='list':
            if args.all_domains:domains=list_domains(root)['domains'];domains=[d['name'] for d in domains if (root/'domains'/d['name']/'notes').is_dir()]
            elif domain:domains=[domain]
            else:raise ValueError(UNBOUND)
            result=list_notes(root,domains,args.status,args.type or (),args.tag,max(0,args.offset),max(1,min(1000,args.limit)),args.with_summary)
        elif cmd=='domains':result=list_domains(root)
        elif cmd=='links':
            if args.all_domains:domains=[d['name'] for d in list_domains(root)['domains'] if (root/'domains'/d['name']/'notes').is_dir()]
            elif domain:domains=[domain]
            else:raise ValueError(UNBOUND)
            result=note_links(root,domains,args.status)
        elif cmd=='version':result={'ok':True,'api':SERVE_API,'python':sys.version.split()[0],'commands':sorted(subs.choices),'capabilities':{'restore':1,'pin':1,'history':3,'maintenance':{'version':1,'actions':['status','preview','run','enable','disable','history']}}}
        elif cmd=='update':
            from .notes import change
            d,n=_reference(args.reference,domain,root)
            values=_payload(args) if args.payload_file or not any(getattr(args,k) is not None for k in ('title','body','body_file','tag','type','context','review_by','pin','file','files','shell_command','source','subject','concern','platform','technology','distinct','not_distinct')+SECTION_ARGS+AMEND_ARGS) else {}
            if args.title is not None:values['title']=args.title
            if args.body is not None:values['body']=args.body
            if args.body_file:values['body']=args.body_file.read_text(encoding='utf-8-sig')
            # Read, rebuild and write under one lock: a concurrent update between them would otherwise be lost.
            with store_lock(root):
                from .notes import find_note
                current=find_note(n,d,root)[1];stored=current['body'];replaced={}
                if any(getattr(args,k) for k in SECTION_ARGS+AMEND_ARGS):
                    # Sections given on the command line change only themselves; the rest of the body is kept.
                    values['body']=_update_body(args,values['body'] if 'body' in values else stored,args.type or current.get('type'))
                if isinstance(values.get('body'),str):replaced=_replaced(args,stored,values['body'])
                tags=(args.tag or [])+_facet_tags(args)
                if tags:values['tags']=tags
                for key in ('type','context','review_by'):
                    if getattr(args,key) is not None:values[key]=getattr(args,key)
                if args.pin is not None:values['pinned']=args.pin
                revision=values.pop('expected_revision',None) or args.expected_revision
                if args.shell_command:values['commands']=list(find_note(n,d,root)[1].get('commands') or [])+args.shell_command
                redacted=_redact(values) if args.redact else []
                if args.session:
                    from .private import check as private_check
                    private_check(_session_state(root,args.session),values)
                files,suggested=_file_args(args,d,root,find_note(n,d,root)[1].get('files') or [])
                if files is not None:values['files']=files
                if args.source:
                    from .digests import describe
                    new=[describe(root,d,v,cwd) for v in args.source]   # the file's hash now; a path already listed takes the new hash
                    values['sources']=[s for s in find_note(n,d,root)[1].get('sources') or [] if s['path'] not in {x['path'] for x in new}]+new
                if 'actor' in values:raise ValueError('Cannot update: actor')
                other=None
                if args.distinct or args.not_distinct:
                    from .notes import distinct_with,distinct_without
                    if args.distinct and args.not_distinct:raise ValueError('Pass --distinct or --not-distinct, not both')
                    if args.why and not args.distinct:raise ValueError('--why goes with --distinct')
                    od,on=_reference(args.distinct or args.not_distinct,domain,root)
                    if od!=d:raise ValueError('A distinct pair must be two notes in the same domain')
                    if on==n:raise ValueError('A note cannot be distinct from itself')
                    mine=find_note(n,d,root)[1]
                    if args.distinct:
                        if not args.why:raise ValueError('--distinct needs --why with the reason, one line of 8 to 200 characters')
                        why=args.why
                        if args.redact:
                            from .repository import redact as redact_text
                            why,kinds=redact_text(why);redacted+=[{'kind':k,'field':'why'} for k in kinds]
                        if args.session:
                            from .private import overlap
                            if overlap(_session_state(root,args.session),[('why',why)]):raise ValueError('private_content conflict: why repeats text the user marked <private>; leave it out of memory')
                        values['distinct_from']=distinct_with(mine,d,on,why,'user')
                    else:
                        theirs=find_note(on,d,root)[1]
                        if not any(_same_pair(e,d,on) for e in mine.get('distinct_from') or []) and not any(_same_pair(e,d,n) for e in theirs.get('distinct_from') or []):
                            raise ValueError(f'{d}:{n} and {d}:{on} are not marked distinct')
                        if any(_same_pair(e,d,on) for e in mine.get('distinct_from') or []):values['distinct_from']=distinct_without(mine,d,on)
                        if any(_same_pair(e,d,n) for e in theirs.get('distinct_from') or []):other=(on,distinct_without(theirs,d,n))
                if other:
                    # Both notes change in one journaled transaction, after the expected revision is checked: one undo reverts the pair.
                    from .notes import _change_set,_commit
                    if revision and current['revision']!=revision:raise ValueError('stale_revision')
                    changes,expected={},{}
                    if values:
                        changes,expected,_=_change_set(n,d,'update',root,revision,**values)
                    more,more_expected,target=_change_set(other[0],d,'update',root,None,pending=changes,distinct_from=other[1])
                    changes.update(more)
                    for rel,rev in more_expected.items():expected.setdefault(rel,rev)
                    result=_commit(d,changes,'update',root,expected);result.update(id=n if values else other[0],path=str(find_note(n if values else other[0],d,root)[0]),status='update')
                else:result=change(n,d,'update',root,revision,**values)
            if replaced:result['replaced']=replaced
            if redacted:result['redacted']=redacted
            if suggested:result['suggested_files']=suggested
            _receipt(args,d,n,root,'update')
        elif cmd=='supersede':
            from .notes import change
            d,n=_reference(args.reference,domain,root);bd,bn=_reference(args.by,domain,root)
            if bd!=d:raise ValueError('A note can only be superseded by a note in the same domain')
            result=change(n,d,'supersede',root,superseded_by=bn,drop_links=args.drop_links)
            from .usage import record
            record(root,'supersede',[f'{d}:{n}'],args.session)
            _receipt(args,d,bn,root)
        elif cmd=='review':
            from .worklist import worklist,KINDS
            if args.all_domains:domains=[d['name'] for d in list_domains(root)['domains'] if (root/'domains'/d['name']/'notes').is_dir()]
            elif domain:domains=[domain]
            else:raise ValueError(UNBOUND)
            # Read-only for every actor: the worklist writes no usage rows, so a viewer's browsing is no evidence either.
            found=worklist(root,domains,args.kind or None,max(1,min(100,args.limit)),args.tag,working_tree=True)
            result={'ok':True,'domain':None if args.all_domains else domain,'due':found['total'],'stale_digests':found['counts'].get('stale',0),'kinds':list(KINDS),'counts':found['counts'],'notes':found['items'],
                    'hint':_maintenance_hint(root)+'Each note lists why (kinds, reason) and an action. Still true -> confirm REF; changed -> update REF; replaced -> supersede REF --by NEW; finished -> resolve REF. A STALE note: read its files, then update REF (or confirm REF if it still holds). '
                           'For a duplicate (pair) or an obsolete note, propose plans for archive or merge; the user applies them. Stale notes come first, never-confirmed notes nobody has read lately lead the due ones.'}
        elif cmd=='open':
            from .notes import open_work,short_ref
            from .runtime import scope_domains
            if args.all_domains:scope=[d['name'] for d in list_domains(root)['domains'] if (root/'domains'/d['name']/'notes').is_dir()]
            elif domain:
                from .common import global_domains
                scope=[domain]+scope_domains(domain,load_bindings(root))+global_domains(root)
            else:raise ValueError(UNBOUND)
            found=open_work(list(dict.fromkeys(scope)),root,args.tag)
            items=[{'ref':f"{d}:{n['id']}",'short_ref':short_ref(d,n['id']),'title':n['title'],'type':n['type'],'updated':str(n.get('updated',''))[:10],'index':i,'item':text}
                   for d,n,listed in found for i,text in listed]
            result={'ok':True,'domain':None if args.all_domains else domain,'notes':len(found),'count':len(items),'items':items[:max(1,min(500,args.limit))],
                    'hint':"Tick one when done: check REF 'item text' (or --index N); --undo unticks."}
        elif cmd=='check':
            from .notes import check_action
            d,n=_reference(args.reference,domain,root)
            if (args.item is None)==(args.index is None):raise ValueError('Name the item by its text or pass --index N (not both)')
            result=check_action(n,d,root,args.item,args.index,not args.undo);result.pop('path',None)
            if result.get('status')!='unchanged':_receipt(args,d,n,root,'check')
        elif cmd=='timeline':
            from .notes import short_ref
            if not domain:raise ValueError(UNBOUND)
            if args.reference and args.date:raise ValueError('Anchor on a REF or a --date, not both')
            when=lambda n:str(n.get(args.by) or n.get('created') or '')
            anchor_id=None
            if args.reference:
                d,anchor_id=_reference(args.reference,domain,root)
                if d!=domain:raise ValueError('timeline lists one domain; pass --domain '+d)
            notes=[n for n in inventory(domain,root,retired=args.all or bool(anchor_id)) if args.all or n.get('status','active')=='active' or n['id']==anchor_id]
            notes=sorted((n for n in notes if not args.type or n.get('type') in args.type or n['id']==anchor_id),key=lambda n:(when(n),n['id']))
            if not notes:raise ValueError('No notes to list')
            if anchor_id:at=next(i for i,n in enumerate(notes) if n['id']==anchor_id)
            elif args.date:
                from datetime import date as _date
                try:_date.fromisoformat(args.date)
                except ValueError:raise ValueError('--date must be YYYY-MM-DD') from None
                at=next((i for i,n in enumerate(notes) if when(n)[:10]>=args.date),len(notes)-1)
            else:at=len(notes)-1
            before,after=max(0,min(50,args.before)),max(0,min(50,args.after))
            rows=[{'ref':f"{domain}:{n['id']}",'short_ref':short_ref(domain,n['id']),'date':when(n)[:10],'type':n.get('type'),'title':n['title'],'status':n.get('status','active'),**({'anchor':True} if i==at else {})}
                  for i,n in enumerate(notes) if at-before<=i<=at+after]
            result={'ok':True,'domain':domain,'by':args.by,'anchor':rows[min(at,before)]['ref'],'notes':rows}
        elif cmd=='ingest-plan':
            from .ingest import ingest_plan,default_source
            if not domain:raise ValueError(UNBOUND)
            result=ingest_plan(root,domain,args.source or default_source(cwd),args.redact,args.allow_similar)
        elif cmd=='export':
            from .bundle import export
            if not domain:raise ValueError(UNBOUND)
            result=export(root,domain,args.out,args.type or (),args.tag,args.all)
        elif cmd=='import-plan':
            from .bundle import import_plan
            # The bundle's own domain unless --domain names another; the cwd binding does not choose it.
            result=import_plan(root,args.file,args.domain)
        elif cmd=='confirm':
            from .notes import confirm
            d,n=_reference(args.reference,domain,root)
            result=confirm(n,d,root);result.pop('path',None)
            # Confirming is a memory decision for the turn even when the note was already confirmed today.
            _receipt(args,d,n,root,'confirm' if result.get('status')!='unchanged' else None)
        elif cmd in {'resolve','reopen'}:
            # Finished work leaves the active set in one journaled, undoable step; reopen brings it back.
            from .notes import change,short_ref
            d,n=_reference(args.reference,domain,root)
            result=change(n,d,cmd,root);result.pop('path',None)
            if cmd=='resolve':result['hint']=f'Resolved notes leave session context and search. `reopen {short_ref(d,n)}` brings it back.'
            _receipt(args,d,n,root,cmd)
        elif cmd=='restore':
            # Forward restoration of an archived note: one journaled, undoable step, like reopen.
            from .notes import change
            d,n=_reference(args.reference,domain,root)
            result=change(n,d,'restore',root,args.expected_revision);result.pop('path',None)
            result['hint']='The note is active and pinned, so automatic maintenance will not retire it until `update REF --unpin`.'
            _receipt(args,d,n,root,'restore')
        elif cmd=='stats':
            from .usage import stats
            from .runtime import parent_domains
            if not domain:raise ValueError(UNBOUND)
            from .common import global_domains
            result=stats(root,list(dict.fromkeys([domain]+parent_domains(domain,load_bindings(root))+global_domains(root))),max(1,min(30,args.days)),max(1,min(100,args.limit)))
        elif cmd=='plan':
            from .notes import change
            d,n=_reference(args.reference,domain,root);values=_payload(args) if args.payload_file or args.action in {'update','rename','retag','supersede'} else {}
            if 'actor' in values:raise ValueError('Cannot plan with an actor field: the controller records who acts')
            result=change(n,d,args.action,root,plan_only=True,**values)
        elif cmd=='apply':
            from .governance import apply
            result=apply(root,args.plan_id,args.expected_plan_sha256)
        elif cmd=='inspect-plan':
            from .governance import inspect_plan
            result=inspect_plan(root,args.plan_id,full=args.full)
        elif cmd=='rewrite-plan':
            from .notes import rewrite_plan
            result=rewrite_plan(_payload(args),domain,root)
            if result.get('plan_id'):result['hint']='Review with inspect-plan PLAN_ID --full, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
        elif cmd=='merge-plan':
            from .governance import merge_plan
            result=merge_plan(root,_payload(args))
        elif cmd=='history':
            from .governance import history
            note_id=None
            if args.note:
                try:note_id=_reference(args.note,domain,root)[1]
                except ValueError:
                    # A deleted note has no file left to resolve, but its journals still name it by full ID.
                    note_id=args.note.rpartition(':')[2]
                    if not __import__('re').fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',note_id):raise
            result=history(root,note_id,args.limit)
            if args.markdown:
                from .activity import markdown
                result={'ok':True,'rows':result,'markdown':markdown(result)}
        elif cmd=='recap':
            from .recap import recap
            if not 1<=args.days<=90:raise ValueError('--days must be 1 to 90')
            if args.all_domains:scope=[d['name'] for d in list_domains(root)['domains'] if (root/'domains'/d['name']/'notes').is_dir()]
            elif domain:scope=[domain]
            else:raise ValueError(UNBOUND)
            result=recap(root,scope,args.days,args.project)
        elif cmd=='log':
            import re
            from .activity import log
            note=args.note
            if note and re.fullmatch(r'CHG-0*[1-9]\d*',note,re.I):note='CHG-%03d'%int(note[4:])
            elif note:
                try:note=_reference(note,domain,root)[1]
                except ValueError:
                    # A deleted note has no file left to resolve, but its journals still name it by full ID.
                    note=note.rpartition(':')[2]
                    if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',note):raise
            result=log(root,canonical_domain(args.domain,root) if args.domain else None,args.project,args.since,args.until,note,args.type,args.event,args.limit)
        elif cmd=='undo':
            from .governance import undo
            result=undo(root,args.operation_id)
        elif cmd=='recover':
            from .repository import recover,reconcile,abandon
            if args.abandon:result=abandon(root,args.operation_id)
            else:result=recover(root,args.operation_id,args.direction)
            row=read_json(root/'.operations'/args.operation_id/'journal.json')
            for d in {Path(e['path']).parts[1] for e in row['effects'] if len(Path(e['path']).parts)>2 and Path(e['path']).parts[0]=='domains'}:reconcile(d,root)
        elif cmd=='diff':
            import base64,difflib,re
            if not re.fullmatch('[a-f0-9]{32}',args.operation_id):raise ValueError('Invalid operation ID')
            row=read_json(root/'.operations'/args.operation_id/'journal.json');result=[]
            if not isinstance(row,dict):raise ValueError(f'not_found: no operation {args.operation_id}')
            for e in row['effects']:
                before=base64.b64decode(e['before']).decode() if e['before'] else '';after=base64.b64decode(e['after']).decode() if e['after'] else ''
                result.append({'path':e['path'],'diff':''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True)))[:8000]})
        elif cmd=='doctor':result=doctor(root,cwd,args.root,domain)
        elif cmd=='logs':
            rows,matched=logs.entries(root,args.level,args.kind,args.event,max(1,min(30,args.days)),max(1,min(1000,args.limit)))
            result={'ok':True,'level_setting':logs.setting(),'matched':matched,'entries':rows}
        elif cmd=='backup':
            from .backup import backup
            result=backup(root,max(1,min(100,args.keep)))
            from .common import atomic_write
            from datetime import datetime,timezone   # real time, like backup.maybe_backup that reads this marker; not the test clock
            atomic_write(root/'hooks'/'last-backup.json',canonical_json({'at':datetime.now(timezone.utc).isoformat(),'backup':Path(result['backup']).name}))
        elif cmd=='restore-plan':
            from .backup import restore_plan
            result=restore_plan(root,args.file)
        elif cmd=='migration-plan':
            from .migration import migration_plan
            result=migration_plan(root)
        elif cmd=='baseline-plan':
            from .migration import baseline_plan
            result=baseline_plan(root)
        elif cmd=='note-type-plan':
            from .migration import note_type_plan
            result=note_type_plan(root)
        elif cmd in {'rebuild','validate'}:
            d=args.name or domain
            if not d:raise ValueError(UNBOUND)
            from .validate import reconcile,validate_domain
            result=reconcile(d,root) if cmd=='rebuild' else {'issues':validate_domain(d,root)}
        elif cmd=='maintenance':
            from . import maintenance
            a=args.action;flags=('limit','run','dry_run','scheduled','all_domains','model','max_notes_per_day','timeout_seconds','daily_budget_usd')
            takes={'preview':{'limit'},'history':{'limit','run'},'run':{'dry_run','scheduled'},'enable':{'all_domains','model','max_notes_per_day','timeout_seconds','daily_budget_usd'}}.get(a,set())
            extra=[k for k in flags if getattr(args,k) not in (None,False) and k not in takes]
            if extra:raise ValueError(f"maintenance {a} does not take "+', '.join('--'+k.replace('_','-') for k in extra))
            limit=10 if args.limit is None else args.limit
            if a=='status':result=maintenance.status(root)
            elif a=='preview':result=maintenance.preview(root,limit,domains=[domain] if domain else None)
            elif a=='history':result=maintenance.history(root,limit,args.run)
            elif a=='run':
                result=maintenance.run(root,args.dry_run,allow_disabled=args.dry_run,domains=[domain] if domain else None)
                maintenance.log_run(root,result,args.scheduled)
            elif a=='disable':result=maintenance.disable(root)
            else:
                settings={k:getattr(args,k) for k in ('model','max_notes_per_day','timeout_seconds','daily_budget_usd') if getattr(args,k) is not None}
                names=[]
                if args.all_domains:
                    from .common import global_domains
                    skip=set(global_domains(root));names=[d['name'] for d in list_domains(root)['domains'] if d['bound'] and d['name'] not in skip and (root/'domains'/d['name']/'notes').is_dir()]
                if args.domain or not args.all_domains:
                    if not domain:raise ValueError(UNBOUND)
                    names.append(domain)
                result=maintenance.enable(root,list(dict.fromkeys(names)),bool(args.domain),settings)
        elif cmd=='semantic':
            from . import semantic
            if args.value and args.action!='native':raise ValueError('on/off applies to semantic native only')
            if args.action=='install':result=semantic.install(root,args.model or semantic.DEFAULT_MODEL,args.source)
            elif args.model or args.source:raise ValueError('--model and --from apply to install only')
            elif args.action=='native':
                from . import vectors
                if args.value:vectors.set_enabled(root,args.value=='on')
                result=semantic.status(root)
            elif args.action=='rebuild':rebuilt=semantic.rebuild_all(root,units=True);result={**semantic.status(root),'rebuilt':rebuilt}
            else:result=semantic.remove(root) if args.action=='remove' else semantic.status(root)
        elif cmd=='query':
            from .search import readonly_query
            if not domain:raise ValueError(UNBOUND)
            result=readonly_query(domain,args.sql,root)
        elif cmd=='taxonomy-plan':
            from .tag_taxonomy import plan_update
            result=plan_update(args.tags,root)
        elif cmd=='project-plan':
            from .projects import project_plan
            result=project_plan(root,args.action,_payload(args))
            result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
        elif cmd=='projects':
            from .projects import listing
            result=listing(root,args.status)
        elif cmd=='system-plan':
            from .systems import system_plan
            result=system_plan(root,args.action,_payload(args))
            result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
        elif cmd=='systems':
            from .systems import listing
            result=listing(root,args.status)
        elif cmd=='source-plan':
            from .sources import source_plan
            result=source_plan(root,args.action,_payload(args),domain)
            result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST; source-sync then indexes it.'
        elif cmd=='sources':
            from .sources import listing
            result=listing(root,domain)
        elif cmd=='ingest-scan':
            from .digests import scan
            if not domain:raise ValueError(UNBOUND)
            result=scan(root,domain,args.source,max(1,min(100000,args.limit)),args.changed,load_bindings(root))
        elif cmd=='ingest-skip':
            from .digests import record_skips,clear_skips
            if not domain:raise ValueError(UNBOUND)
            data=_payload(args);items=data.get('skipped') if isinstance(data,dict) else data;clear=data.get('clear') if isinstance(data,dict) else None
            if (items is None and clear is None) or not isinstance(items or [],list) or not isinstance(clear or [],list):raise ValueError('ingest-skip takes {"skipped": [{"path", "reason"}, ...], "clear": ["path", ...]}')
            with store_lock(root):result={'ok':True,'domain':domain,'recorded':record_skips(root,domain,items or [],cwd,load_bindings(root)),'cleared':clear_skips(root,domain,clear or [],cwd,load_bindings(root))}
        elif cmd=='source-sync':
            from .sources import sync
            if not domain:raise ValueError(UNBOUND)
            result=sync(root,domain,args.name,args.rebuild,all_remote=args.all_remote,network=args.actor not in {'subagent','viewer'})
        elif cmd=='ask':
            if not domain:raise ValueError(UNBOUND)
            result=_ask(root,domain,' '.join(args.query),args.limit)
        elif cmd=='change-plan':
            from .changes import change_plan
            result=change_plan(root,args.action,_payload(args))
            result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
        elif cmd=='changes':
            from .changes import listing
            result=listing(root,args.status,args.project)
        elif cmd=='views':
            from . import views
            action=args.action or 'list'
            if action=='list':
                if not domain and not args.all_domains:raise ValueError(UNBOUND)
                result=views.listing(root,None if args.all_domains else domain)
            elif action=='render' and args.all:
                if args.reference:raise ValueError('Name one note or pass --all, not both')
                if args.out:raise ValueError('--out needs exactly one view')
                rows=views.render(root,views.load(root)['views'],args.target,None,args.format)
                result={'ok':True,'count':len(rows),**({'models':[r['model'] for r in rows]} if args.format=='json' else {'rendered':rows})}
            else:
                if not args.reference:raise ValueError(f'views {action} needs a note: REF, ID prefix or exact title')
                try:d,n=_reference(args.reference,domain,root)
                except ValueError:
                    # A view whose note was deleted can still be removed by its registered ID, or an 8+ character prefix of it.
                    import re
                    prefix=args.reference.split(':')[-1].lower()
                    orphan=[v for v in views.load(root)['views'] if v['note'].startswith(prefix)] if action=='remove' and re.fullmatch(r'[0-9a-f][0-9a-f-]{7,}',prefix) else []
                    if len(orphan)!=1 or views._find(orphan[0],root):raise
                    d,n=orphan[0]['domain'],orphan[0]['note']
                registered=next((v for v in views.load(root)['views'] if v['note']==n),None)
                if action=='add':result=views.add(root,d,n,args.title)
                elif action=='remove':result=views.remove(root,n)
                elif action=='published':
                    if not args.url:raise ValueError('views published needs --url')
                    result=views.published(root,n,args.url,views.project_key(cwd))
                else:
                    if not registered and not (args.format=='json' or args.out):raise ValueError('That note has no view; add one with views add REF, or pass --out or --format json')
                    from .notes import find_note
                    entry=registered or views.transient(dict(find_note(n,d,root)[1],domain=d))
                    rows=views.render(root,[entry],args.target,args.out,args.format)
                    result={'ok':True,'model':rows[0]['model']} if args.format=='json' else {'ok':True,'target':args.target}
                    if 'path' in rows[0]:result['path']=rows[0]['path']
        elif cmd=='wiki':
            from . import wiki
            if not domain:raise ValueError(UNBOUND)
            if args.format=='json' and not args.out:result={'ok':True,'model':wiki.wiki_model(domain,root,args.archived)}
            else:result=wiki.export(domain,root,args.archived,args.out,args.format)
        elif cmd=='taxonomy':result=read_json(root/'tag_taxonomy.json',{'tags':[]})
        elif cmd=='batch':
            import contextlib,io
            batch=_payload(args)
            if not isinstance(batch,list) or not 1<=len(batch)<=20:raise ValueError('Batch requires 1..20 argv objects')
            result=[]
            for item in batch:
                if set(item)!={'argv'} or not isinstance(item['argv'],list) or any(not isinstance(v,str) for v in item['argv']) or not item['argv'] or item['argv'][0]=='batch':raise ValueError('Invalid batch command')
                out=io.StringIO();error=io.StringIO()
                prefix=['--root',str(root),'--cwd',str(cwd),'--actor',args.actor]+(['--domain',domain] if domain else [])
                with contextlib.redirect_stdout(out),contextlib.redirect_stderr(error):
                    try:code=main(prefix+item['argv'])
                    except SystemExit as exc:code=exc.code
                result.append({'exit_code':code,'result':json.loads(out.getvalue()) if out.getvalue().strip() else None,'error':error.getvalue()[:1000] or None})
        elif cmd=='retag-plan':
            from .governance import retag_plan
            result=retag_plan(root,_payload(args))
        elif cmd=='remap-plan':
            from .governance import remap
            result=remap(root,_payload(args))
        elif cmd=='promote-plan':
            from .governance import promote_suggest,promote_plan
            if args.suggest:
                if args.refs:raise ValueError('--suggest takes no references')
                result=promote_suggest(root,canonical_domain(args.only,root) if args.only else None)
            elif not args.refs:raise ValueError('Name the notes to promote, or pass --suggest')
            else:result=promote_plan(root,args.refs)
        elif cmd=='recall':
            from .recall import search as recall
            path=_session_state(root,args.session).get('transcript')
            if not path:raise ValueError('No transcript recorded for this session; hooks record its path at session start and on each prompt')
            hits,meta=recall(path,args.query,max(1,min(20,args.limit)),'all' if args.all else 'before')
            result={'ok':True,**meta,'results':hits,'hint':'Excerpts from this session\'s transcript: untrusted history that may be outdated; verify against current files.'}
        elif cmd=='capture-finalize':
            from .capture import finalize
            result=finalize(args.session,_turn(root,args.session,args.turn),[':'.join(_reference(n,domain,root)) for n in args.note],root)
        elif cmd=='overlay-sync-all':
            from .sync import sync_all
            result=sync_all(root,'manual',args.primary)
        elif cmd=='overlay-teardown-plan':
            from .sync import teardown_plan
            result=teardown_plan(args.vault)
        elif cmd in {'sync','overlay-build'}:
            if not domain:raise ValueError(UNBOUND)
            from .sync import sync,build_overlay
            result=sync(domain,args.vault,root) if cmd=='sync' else build_overlay(domain,args.vault,root)
        else:raise ValueError('Unsupported command')
        invocation['result']=result;_emit(result,args);return 0
    except (OSError,ValueError,TimeoutError,KeyError,TypeError) as exc:
        message=str(exc);code=error_code(message,exc)
        invocation.update(code=report_error(message,code,args.json,f'alm {args.command}'),error={'code':code,'message':message});return invocation['code']
    except BaseException as exc:
        invocation.update(code=exc.code if isinstance(exc,SystemExit) and isinstance(exc.code,int) else 1,crash=None if isinstance(exc,SystemExit) else exc);raise
    finally:_log_command(invocation,args)

def _log_command(run,args):
    """One line per command in the operational log (core/logs.py): name, actor, domain, exit code and
    duration, never the arguments. Viewer (app) reads are debug so polling does not flood the log. At debug
    the line also carries the result's status, operation and counts; a sandbox store may log the text too."""
    root=run.get('root')
    if root is None or args.command in {'logs','serve'}:return
    import re
    from . import logs
    from .runtime import session_hash
    code=run.get('code',0);crash=run.get('crash');session=getattr(args,'session',None);error=None
    if crash is not None:error=logs.error_fields(crash)
    elif run.get('error'):
        # The message's leading snake_case token (stale_revision, possible_duplicate, private_content, ...) is
        # finer than the exit code's invalid/conflict/stale.
        reason=re.match(r'[a-z]+_[a-z_]+(?=$|[: ])',run['error']['message'])
        error=dict(run['error'],message=logs.message(run['error']['message']),reason=reason[0] if reason else None)
    level='error' if crash is not None else 'warning' if code else 'debug' if args.actor=='viewer' else 'info'
    extra={};result=run.get('result')
    if logs.enabled('debug') and isinstance(result,dict):
        count=lambda key:len(result[key]) if isinstance(result.get(key),(list,dict)) else None
        ref=f"{result['domain']}:{result['id']}" if result.get('domain') and result.get('id') else result.get('ref')
        extra.update(status=result.get('status'),operation_id=result.get('operation_id'),ref=ref if isinstance(ref,str) else None,
                     inferred_tags=count('inferred_tags'),redacted=count('redacted'),similar=count('similar'),
                     suggested_files=count('suggested_files'),warning=result.get('warning') if isinstance(result.get('warning'),str) else None)
    if logs.content_allowed(root):
        extra['content']=logs.content({'argv':run.get('argv'),'result':result,'error':(run.get('error') or {}).get('message')})
    logs.write(level,'command',args.command,root,actor=args.actor,domain=run.get('domain'),session=session_hash(session) if session else None,
               ms=round((time.perf_counter()-run['start'])*1000,1),exit_code=code,error=error,**extra)
if __name__=='__main__':raise SystemExit(main())
