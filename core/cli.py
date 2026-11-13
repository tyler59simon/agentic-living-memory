"""Unified compact script interface. Compatibility scripts delegate here."""
from __future__ import annotations
import argparse,json,os,sys,hashlib,time
from pathlib import Path
from .common import memory_root,load_bindings,resolve_domain,canonical_domain
from .repository import read_json,inventory,parse,store_lock,canonical_json,now
from .note_types import NOTE_TYPES

def _payload(args):
    if getattr(args,'payload_file',None):raw=args.payload_file.read_text(encoding='utf-8')
    else:raw=sys.stdin.read()
    if len(raw.encode())>1024*1024:raise ValueError('Payload exceeds 1 MiB')
    return json.loads(raw)
def _reference(value,domain,root):
    """(domain, full note ID) for a ref, a unique ID prefix, or an exact note title."""
    from .notes import resolve_ref
    from .runtime import parent_domains
    parents=parent_domains(domain,load_bindings(root)) if domain else []
    return resolve_ref(value,domain,root,parents)

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
            from .learning import problems as learning_problems
            found+=learning_problems(root,directory.name)
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
    from .backup import staleness_warning
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
READ_ONLY={'search','read','query','doctor','validate','history','logs','open','stats','timeline','recall','list','domains','version','links','projects','systems','ask','sources','source-sync','ingest-scan','changes','log'}
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
            if summary:rows[-1].update(_summary_fields(n))
    rows.sort(key=lambda r:(r['updated'],r['id']),reverse=True)
    return {'ok':True,'domains':domains,'total':len(rows),'offset':offset,'notes':rows[offset:offset+limit]}

def _summary_fields(note):
    """list --with-summary: the summary as plain text (300 characters at most), the files, and action item counts for plan, bug and risk notes."""
    from .note_format import sections,plain,action_items,ACTION_TYPES
    body=note.get('body') or '';text=sections(body).get('summary') or ''
    out={'summary':plain(text)[:300],'files':list(note.get('files') or [])}
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

def note_links(root,domains,status):
    """The link graph of these domains: [[Title]] links (resolved like a title REF: the note's own domain, then its
    parent domains, then the global domains; case-insensitive) and supersedes, with links that match no note."""
    from .sync import WIKILINK
    from .runtime import parent_domains
    from .common import global_domains
    config=load_bindings(root);globals_=global_domains(root);notes={};titles={}
    def notes_of(d):
        if d not in notes:notes[d]=inventory(d,root,retired=status!='active') if (root/'domains'/d/'notes').is_dir() else []
        return notes[d]
    def by_title(d):
        if d not in titles:
            # An active note wins a title over a retired one that shares it.
            titles[d]={n['title'].strip().casefold():n for n in sorted(notes_of(d),key=lambda n:n.get('status','active')=='active')}
        return titles[d]
    nodes={};edges={};unresolved={}
    def node(d,n):
        ref=f"{d}:{n['id']}"
        nodes.setdefault(ref,{'ref':ref,'id':n['id'],'domain':d,'title':n.get('title'),'type':n.get('type'),'status':n.get('status','active'),'tags':n.get('tags') or []})
        return ref
    for d in domains:
        order=list(dict.fromkeys([d]+parent_domains(d,config)+globals_))
        ids={n['id']:n for n in notes_of(d)}
        for n in notes_of(d):
            source=node(d,n)
            for target in WIKILINK.findall(n.get('body','')):
                key=target.strip().casefold()
                hit=next(((o,by_title(o)[key]) for o in order if key in by_title(o)),None)
                if hit is None:unresolved.setdefault((source,target.strip()),None);continue
                found=node(*hit)
                if found!=source:edges.setdefault((source,found),'link')
            # Newer note -> the note it replaced, from either side of the pair; a link between the two wins.
            pairs=[(n,ids.get(str(s).split(':')[-1])) for s in n.get('supersedes') or []]
            pairs.append((ids.get(str(n.get('superseded_by') or '').split(':')[-1]),n))
            for newer,older in pairs:
                if newer and older and newer['id']!=older['id']:edges.setdefault((node(d,newer),node(d,older)),'supersedes')
    return {'ok':True,'domains':domains,'nodes':sorted(nodes.values(),key=lambda r:r['ref']),
            'edges':[{'from':a,'to':b,'kind':k} for (a,b),k in sorted(edges.items())],
            'unresolved':[{'from':a,'target':t} for a,t in sorted(unresolved)]}

def _note_hits(root,domain,query,scope,limit,rebuild,filters,config,globals_,primary,foreign,explain=False):
    """The search command's answer: relevance-gated notes across the active, parent and global domains (foreign ones as a fallback).
    explain adds why: the semantic backend per domain and why it fell back, each candidate's gate verdict and scores, and
    for each result the section and level the prompt hook would deliver (refs, headings and numbers; no note text)."""
    from .search import search_domains
    from .runtime import relevance_gate,parent_domains
    candidates=[] if explain else None
    gate=relevance_gate(query,root,candidates);gated=True
    # As for prompts: global notes rank like the project's own, a parent's count half.
    weights={**{g:2 for g in globals_},domain:2,**{p:1 for p in parent_domains(domain,config) if p!=domain}}
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
    for hit in hits:hit['sourceScope']='active' if hit['domain']==domain else 'global' if hit['domain'] in globals_ else 'foreign';hit.pop('path',None)
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
    from .runtime import parent_domains
    config=load_bindings(root);scope=config.get('search_scope',{})
    globals_=[g for g in (canonical_domain(g,root) for g in scope.get('global_domains',['_global'])) if (root/'domains'/g).exists()]
    return config,globals_,list(dict.fromkeys([domain]+parent_domains(domain,config)+globals_)),scope.get('foreign_domains',[])

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

def error_code(message):
    """stale, conflict or invalid, from the message's own wording: the text before any quoted value or ': detail',
    so a note title, tag or path inside the message cannot change the code."""
    import re
    head=re.split(r''': |["']''',message,maxsplit=1)[0].lower()
    return 'stale' if 'stale' in head else 'conflict' if 'conflict' in head else 'invalid'

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
                try:code=main(['--root',str(root),'--cwd',str(cwd)]+argv)
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

def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure') and (stream.encoding or '').lower().replace('-','')!='utf8':
            try:stream.reconfigure(encoding='utf-8')
            except (ValueError,OSError):pass
    parser=argparse.ArgumentParser(description=__doc__)
    def common(p,suppress=False):
        default=argparse.SUPPRESS if suppress else None
        p.add_argument('--root',type=Path,default=default);p.add_argument('--domain',default=default);p.add_argument('--cwd',default=default)
        p.add_argument('--json',action='store_true',default=argparse.SUPPRESS if suppress else False)
        p.add_argument('--actor',choices=['main','subagent','viewer'],default=argparse.SUPPRESS if suppress else os.environ.get('LIVING_MEMORY_ACTOR','main'))
    common(parser);subs=parser.add_subparsers(dest='command',required=True)
    def sub(name):p=subs.add_parser(name);common(p,True);return p
    p=sub('setup');p.add_argument('--location',type=Path);p.add_argument('--non-interactive',action='store_true');p.add_argument('--source',type=Path);p.add_argument('--apply-plan');p.add_argument('--inspect-plan');p.add_argument('--expected-plan-sha256')
    p=sub('bind');p.add_argument('name');p.add_argument('prefix',nargs='+')
    p=sub('capture');p.add_argument('items',nargs='*');p.add_argument('--global',dest='to_global',action='store_true',help='Save to the global domain (knowledge that holds beyond this project); overrides --domain');p.add_argument('--payload-file',type=Path);p.add_argument('--content-file',type=Path);p.add_argument('--title');p.add_argument('--tag',action='append',default=[]);p.add_argument('--tags',default='');p.add_argument('--prefix',help='Compatibility assertion only; must equal the uppercase type');p.add_argument('--type',choices=NOTE_TYPES,help='Note type (default: knowledge)');p.add_argument('--context',default='');p.add_argument('--idempotency-key');p.add_argument('--session');p.add_argument('--turn');p.add_argument('--allow-similar',action='store_true');p.add_argument('--supersedes',action='append',default=[],help="REF of a note this one replaces; it is retired in the same transaction, and its links and files are carried over (see --drop-links)");p.add_argument('--drop-links',action='store_true',help='With --supersedes: do not carry the links and files of the replaced note over');p.add_argument('--review-by',help='YYYY-MM-DD; status/plan/bug/risk/map/runbook/brief notes get a default')
    for facet in CAPTURE_FACETS:p.add_argument('--'+facet,action='append',default=[])
    p.add_argument('--redact',action='store_true',help="Replace credentials with <redacted type='KIND'/> instead of rejecting the note")
    sections_args(p);_file_flags(p)
    p=sub('search');p.add_argument('query',nargs='?',default='');p.add_argument('--file',help='Notes about this file or folder: listed in their files property, or named in the body');p.add_argument('--scope',choices=['active','fallback','all'],default='fallback');p.add_argument('--limit',type=int,default=5);p.add_argument('--filter',action='append',default=[]);p.add_argument('--rebuild',action='store_true');p.add_argument('--sources',action='store_true',help='Source excerpts from the project knowledge base only');p.add_argument('--explain',action='store_true',help='Add why each note was found: backend, scores, gate and the section the prompt hook would deliver')
    p=sub('read');p.add_argument('reference');p.add_argument('--max-chars',type=int,default=16000);p.add_argument('--metadata-only',action='store_true');p.add_argument('--section',help='Only this section, by heading (as search evidence and prompt lines name it)')
    p=sub('update');p.add_argument('reference');p.add_argument('--title');p.add_argument('--body');p.add_argument('--body-file',type=Path);p.add_argument('--tag',action='append');p.add_argument('--type',choices=NOTE_TYPES);p.add_argument('--context');p.add_argument('--review-by');p.add_argument('--expected-revision');p.add_argument('--payload-file',type=Path);p.add_argument('--session');p.add_argument('--turn')
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
    p.add_argument('--include-learning',action='store_true',help='Include procedure evidence and history; imports require fresh review')
    p=sub('import-plan');p.add_argument('file',type=Path)
    p=sub('confirm');p.add_argument('reference');p.add_argument('--session');p.add_argument('--turn')
    for name in ('resolve','reopen'):p=sub(name);p.add_argument('reference',help='A plan, bug or risk note');p.add_argument('--session');p.add_argument('--turn')
    p=sub('stats');p.add_argument('--days',type=int,default=30,help='Window, at most 30 (the diary keeps 30 days)');p.add_argument('--limit',type=int,default=10)
    p=sub('logs');p.add_argument('--level',choices=['debug','info','warning','error'],default='info',help='Lowest level shown');p.add_argument('--kind',choices=['hook','command','internal']);p.add_argument('--event',help='Hook event, command name or internal call site');p.add_argument('--days',type=int,default=1,help='Window, at most 30 (logs keep 30 days)');p.add_argument('--limit',type=int,default=50)
    p=sub('plan');p.add_argument('action',choices=['update','rename','retag','resolve','reopen','archive','supersede','delete']);p.add_argument('reference');p.add_argument('--payload-file',type=Path)
    p=sub('apply');p.add_argument('plan_id');p.add_argument('--expected-plan-sha256',required=True)
    p=sub('inspect-plan');p.add_argument('plan_id');p.add_argument('--full',action='store_true',help='Show every diff in full instead of cutting each at 16000 characters')
    # A reviewed plan replacing the bodies of several notes (a prose rewrite); titles never change.
    p=sub('rewrite-plan');p.add_argument('--payload-file',type=Path,help='JSON list of {"ref", "revision", "body"}; default: stdin')
    p=sub('merge-plan');p.add_argument('--payload-file',type=Path)
    p=sub('history');p.add_argument('--note');p.add_argument('--limit',type=int,default=20)
    p=sub('log');p.add_argument('--project',help='Project slug');p.add_argument('--since',help='ISO date or time');p.add_argument('--until',help='ISO date or time, inclusive');p.add_argument('--note',help='One note (REF or ID, deleted notes too) or change (CHG-014)');p.add_argument('--type',help='Note type, change or session');p.add_argument('--event',help='created, updated, confirmed, superseded, resolved, archived, reopened, deleted, promoted, moved, undone, stage, closed or session');p.add_argument('--limit',type=int,default=50)
    p=sub('undo');p.add_argument('operation_id')
    p=sub('recover');p.add_argument('operation_id');p.add_argument('--direction',choices=['forward','backward'],default='forward');p.add_argument('--abandon',action='store_true',help='Stop treating the operation as pending; no note file is changed')
    p=sub('diff');p.add_argument('operation_id')
    p=sub('doctor')
    p=sub('backup');p.add_argument('--keep',type=int,default=7)
    p=sub('restore-plan');p.add_argument('file',type=Path)
    p=sub('migration-plan')
    p=sub('note-type-plan')
    p=sub('rebuild');p.add_argument('name',nargs='?')
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
    p=sub('batch');p.add_argument('--payload-file',type=Path)
    p=sub('retag-plan');p.add_argument('--payload-file',type=Path)
    p=sub('remap-plan');p.add_argument('--payload-file',type=Path)
    p=sub('promote-plan');p.add_argument('refs',nargs='*',help='Project notes to move to the global domain');p.add_argument('--suggest',action='store_true',help='List candidates with reasons; writes nothing');p.add_argument('--only',help='With --suggest: one domain instead of all')
    p=sub('recall');p.add_argument('query');p.add_argument('--session',required=True);p.add_argument('--limit',type=int,default=5);p.add_argument('--all',action='store_true',help='Search the whole transcript, not only what precedes the latest compaction')
    p=sub('capture-finalize');p.add_argument('--session',required=True);p.add_argument('--turn',help='Defaults to the latest prompt of the session');p.add_argument('--note',action='append',default=[])
    p=sub('list');p.add_argument('--status',choices=['active','resolved','archived','superseded','any'],default='active');p.add_argument('--type',choices=NOTE_TYPES,action='append');p.add_argument('--tag',action='append',default=[],help='Only notes with this tag (repeatable: all must match)');p.add_argument('--all-domains',action='store_true',help='Every domain folder, not only --domain');p.add_argument('--with-summary',action='store_true',help='Add summary, files and actions {open, done} to each row');p.add_argument('--offset',type=int,default=0);p.add_argument('--limit',type=int,default=200)
    from .learning import ENTITIES,RESULTS,METHODS,OUTCOMES,USAGES,CONTRIBUTIONS
    p=sub('learn');p.add_argument('entity',nargs='?',choices=ENTITIES,help='Omit for the guide');p.add_argument('action',nargs='?');p.add_argument('reference',nargs='?')
    p.add_argument('--payload-file',type=Path,help='JSON payload; without flags, the payload is read from stdin');p.add_argument('--all-domains',action='store_true')
    for flag in ('objective','family','project','key','task','attempt','retry-of','observer','excerpt','limitations','source','reason',
                 'expected-revision','procedure','deviations','use','target','title','summary','fallback','session','kind'):p.add_argument('--'+flag)
    for flag in ('criterion','evidence','tag','use-when','do-not-use-when','step','done-when'):p.add_argument('--'+flag,action='append')
    p.add_argument('--result',choices=RESULTS);p.add_argument('--method',choices=METHODS);p.add_argument('--outcome',choices=OUTCOMES)
    p.add_argument('--usage',choices=USAGES);p.add_argument('--contribution',choices=CONTRIBUTIONS)
    p.add_argument('--version',dest='learn_version',type=int,help='Procedure version (default: current)');p.add_argument('--days',type=int)
    p.add_argument('--enable',dest='enabled',action='store_const',const=True);p.add_argument('--disable',dest='enabled',action='store_const',const=False)
    p.add_argument('--global',dest='global_',action='store_const',const=True)
    p=sub('domains')
    p=sub('links');p.add_argument('--all-domains',action='store_true',help='Every domain folder, not only --domain');p.add_argument('--status',choices=['active','any'],default='active',help='any: include archived, resolved and superseded notes')
    p=sub('version')
    p=sub('serve');p.add_argument('--stdio',action='store_true',required=True,help='JSON-lines requests on stdin, one reply line each on stdout')
    p=sub('sync');p.add_argument('vault',type=Path)
    p=sub('overlay-build');p.add_argument('vault',type=Path)
    p=sub('overlay-teardown-plan');p.add_argument('vault',type=Path)
    p=sub('overlay-sync-all');p.add_argument('--primary',help='Domain whose view the shared dashboards show (built last)')
    args=parser.parse_args(argv);cwd=args.cwd or os.getcwd()
    invocation={'start':time.perf_counter(),'argv':list(argv) if argv is not None else sys.argv[1:]}
    try:
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
            print(json.dumps(result,ensure_ascii=False,separators=(',',':')));return 0
        from .storage import resolve_root
        root=resolve_root(args.root)[0]
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
        if cmd=='learn':
            from .learning import is_read
            learning_read=is_read(args.entity,args.action)
        else:learning_read=False
        if args.actor=='subagent' and cmd not in READ_ONLY and not learning_read:raise ValueError('Subagents are read-only; return mutation proposals to the main agent')
        if args.actor=='viewer' and cmd not in VIEWER and not learning_read:raise ValueError('Viewers are read-only; run changes as --actor main')
        if cmd=='serve':return serve(root,cwd,set(subs.choices)-{'serve','setup'})
        if cmd=='bind':
            from .init_domain import init_domain
            init_domain(args.name,args.prefix,root);result={'ok':True,'domain':args.name,'status':'bound'}
        elif cmd=='capture':
            from .notes import capture
            structured=any(getattr(args,k) for k in SECTION_ARGS)
            if args.items or args.content_file or structured:
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
                if args.session:data.update(session_id=args.session,turn_id=_turn(root,args.session,args.turn))
                if args.allow_similar:data['allow_similar']=True
                if args.supersedes:data['supersedes']=args.supersedes
                if args.drop_links:data['drop_links']=True
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
                        ('--review-by',args.review_by),('--file',args.file),('--files',args.files),('--command',args.shell_command),('--source',args.source)]+[('--'+f,getattr(args,f)) for f in CAPTURE_FACETS]
                ignored=[flag for flag,given in beside if given]
                if ignored:raise ValueError('A JSON payload carries its own fields; put these in the payload instead of passing '+', '.join(ignored))
                if args.turn and not args.session:raise ValueError('--turn needs --session')
                data=_payload(args)
                for item in (data if isinstance(data,list) else [data]) if args.session else ():
                    if isinstance(item,dict) and not item.get('session_id'):
                        item['session_id']=args.session
                        if args.turn and not item.get('turn_id'):item['turn_id']=args.turn
            batch=data if isinstance(data,list) else [data]
            if not 1<=len(batch)<=20:raise ValueError('Capture batch must contain 1..20 items')
            results=[]
            with store_lock(root):
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
            result=results if isinstance(data,list) else results[0]
        elif cmd=='search':
            from .search import search_domains,search_domain
            if not domain:raise ValueError('unbound_project')
            if args.sources:
                if args.file or args.filter:raise ValueError('--sources takes only a query (and --limit)')
                if not args.query.strip():raise ValueError('search needs a query')
                from . import sources
                result={'ok':True,'mode':'sources','domain':domain,'results':sources.search(root,domain,args.query,max(1,min(20,args.limit))),'knowledge_base':sources.state(root,domain)}
                each=_each_question(args.query,max(1,min(20,args.limit)),lambda q,n:{'results':sources.search(root,domain,q,n)})
                if each:result['questions']=each
                invocation['result']=result;print(json.dumps(result,ensure_ascii=False,separators=(',',':')));return 0
            config=load_bindings(root);scope=config.get('search_scope',{});globals_=scope.get('global_domains',['_global'])
            globals_=[canonical_domain(g,root) for g in globals_]
            globals_=[g for g in globals_ if (root/'domains'/g).exists()]
            from .runtime import parent_domains
            primary=list(dict.fromkeys([domain]+parent_domains(domain,config)+globals_));foreign=scope.get('foreign_domains',[])
            filters=dict(value.split('=',1) for value in args.filter)
            if set(filters)-{'prefix','status','tag','type','file'}:raise ValueError('Unsupported search filter')
            if args.file:
                from .common import domain_prefixes,display_path
                given=Path(args.file).expanduser()
                wanted=(display_path(given.resolve(),domain_prefixes(domain,config)) or args.file) if given.is_absolute() else args.file
                if not args.query.strip():
                    # Lists notes about a file; --filter narrows a query's results, so it needs a query.
                    if filters:raise ValueError('--filter needs a query; with --file alone, use --scope to choose domains')
                    from .search import file_notes
                    domains=sorted(config.get('domains',{})) if args.scope=='all' else primary
                    hits=file_notes(wanted,domains,root,max(1,min(100,args.limit)));fallback=False
                    if not hits and args.scope=='fallback' and foreign:domains=foreign;hits=file_notes(wanted,domains,root,max(1,min(100,args.limit)));fallback=True
                    result={'ok':True,'mode':'file','file':wanted,'domains':domains,'fallback':fallback,'results':hits}
                    invocation['result']=result;print(json.dumps(result,ensure_ascii=False,separators=(',',':')));return 0
                filters['file']=wanted
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
            if args.section:
                # One section, as the prompt hook's evidence lines name it: "Why", or a path like "Notes > Deeper detail".
                # The text comes from the note as served (an approved learned runbook's approved body, not an unreviewed edit of
                # its file); file line numbers are given only when the file still holds exactly that body.
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
            if note.get('learning_procedure') and note.get('learning_state')=='active':
                from .learning import use_hint
                result['hint']=use_hint(note['learning_procedure'],note.get('learning_version'))
            if args.actor!='viewer':
                from .usage import record
                record(root,'read',[f'{d}:{n}'])
        elif cmd=='list':
            if args.all_domains:domains=list_domains(root)['domains'];domains=[d['name'] for d in domains if (root/'domains'/d['name']/'notes').is_dir()]
            elif domain:domains=[domain]
            else:raise ValueError('unbound_project')
            result=list_notes(root,domains,args.status,args.type or (),args.tag,max(0,args.offset),max(1,min(1000,args.limit)),args.with_summary)
        elif cmd=='domains':result=list_domains(root)
        elif cmd=='links':
            if args.all_domains:domains=[d['name'] for d in list_domains(root)['domains'] if (root/'domains'/d['name']/'notes').is_dir()]
            elif domain:domains=[domain]
            else:raise ValueError('unbound_project')
            result=note_links(root,domains,args.status)
        elif cmd=='version':result={'ok':True,'api':SERVE_API,'python':sys.version.split()[0],'commands':sorted(subs.choices),'capabilities':{'learning':1}}
        elif cmd=='learn':
            from .learning import dispatch,flag_payload
            payload={}
            if not learning_read:
                flags=flag_payload(args.entity,args.action,vars(args))
                # A JSON payload comes from --payload-file, or from stdin when no flags were given (the stdio bridge).
                if args.payload_file:payload=_payload(args)
                elif not flags and sys.stdin and not sys.stdin.isatty():
                    raw=sys.stdin.read()
                    if len(raw.encode())>1024*1024:raise ValueError('Payload exceeds 1 MiB')
                    payload=json.loads(raw) if raw.strip() else {}
                if not isinstance(payload,dict):raise ValueError('Learning payload must be a JSON object')
                payload.update(flags)
            result=dispatch(root,domain,args.entity,args.action,args.reference,payload,args.all_domains)
        elif cmd=='update':
            from .notes import change
            d,n=_reference(args.reference,domain,root)
            values=_payload(args) if args.payload_file or not any(getattr(args,k) is not None for k in ('title','body','body_file','tag','type','context','review_by','file','files','shell_command','source','subject','concern','platform','technology')+SECTION_ARGS+AMEND_ARGS) else {}
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
                result=change(n,d,'update',root,revision,**values)
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
            else:raise ValueError('unbound_project')
            # Read-only for every actor: the worklist writes no usage rows, so a viewer's browsing is no evidence either.
            found=worklist(root,domains,args.kind or None,max(1,min(100,args.limit)),args.tag)
            result={'ok':True,'domain':None if args.all_domains else domain,'due':found['total'],'stale_digests':found['counts'].get('stale',0),'kinds':list(KINDS),'counts':found['counts'],'notes':found['items'],
                    'hint':'Each note lists why (kinds, reason) and an action. Still true -> confirm REF; changed -> update REF; replaced -> supersede REF --by NEW; finished -> resolve REF. A STALE note: read its files, then update REF (or confirm REF if it still holds). '
                           'For a duplicate (pair) or an obsolete note, propose plans for archive or merge; the user applies them. Stale notes come first, never-confirmed notes nobody has read lately lead the due ones.'}
        elif cmd=='open':
            from .notes import open_work,short_ref
            from .runtime import parent_domains
            if args.all_domains:scope=[d['name'] for d in list_domains(root)['domains'] if (root/'domains'/d['name']/'notes').is_dir()]
            elif domain:
                from .common import global_domains
                scope=[domain]+parent_domains(domain,load_bindings(root))+global_domains(root)
            else:raise ValueError('unbound_project')
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
            if not domain:raise ValueError('unbound_project')
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
            if not domain:raise ValueError('unbound_project')
            result=ingest_plan(root,domain,args.source or default_source(cwd),args.redact,args.allow_similar)
        elif cmd=='export':
            from .bundle import export
            if not domain:raise ValueError('unbound_project')
            result=export(root,domain,args.out,args.type or (),args.tag,args.all,args.include_learning)
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
        elif cmd=='stats':
            from .usage import stats
            from .runtime import parent_domains
            if not domain:raise ValueError('unbound_project')
            from .common import global_domains
            result=stats(root,list(dict.fromkeys([domain]+parent_domains(domain,load_bindings(root))+global_domains(root))),max(1,min(30,args.days)),max(1,min(100,args.limit)))
        elif cmd=='plan':
            from .notes import change
            d,n=_reference(args.reference,domain,root);values=_payload(args) if args.payload_file or args.action in {'update','rename','retag','supersede'} else {}
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
            atomic_write(root/'hooks'/'last-backup.json',canonical_json({'at':now(),'backup':Path(result['backup']).name}))
        elif cmd=='restore-plan':
            from .backup import restore_plan
            result=restore_plan(root,args.file)
        elif cmd=='migration-plan':
            from .migration import migration_plan
            result=migration_plan(root)
        elif cmd=='note-type-plan':
            from .migration import note_type_plan
            result=note_type_plan(root)
        elif cmd in {'rebuild','validate'}:
            d=args.name or domain
            if not d:raise ValueError('unbound_project')
            from .validate import reconcile,validate_domain
            result=reconcile(d,root) if cmd=='rebuild' else {'issues':validate_domain(d,root)}
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
            if not domain:raise ValueError('unbound_project')
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
            if not domain:raise ValueError('unbound_project')
            result=scan(root,domain,args.source,max(1,min(100000,args.limit)),args.changed,load_bindings(root))
        elif cmd=='ingest-skip':
            from .digests import record_skips,clear_skips
            if not domain:raise ValueError('unbound_project')
            data=_payload(args);items=data.get('skipped') if isinstance(data,dict) else data;clear=data.get('clear') if isinstance(data,dict) else None
            if (items is None and clear is None) or not isinstance(items or [],list) or not isinstance(clear or [],list):raise ValueError('ingest-skip takes {"skipped": [{"path", "reason"}, ...], "clear": ["path", ...]}')
            with store_lock(root):result={'ok':True,'domain':domain,'recorded':record_skips(root,domain,items or [],cwd,load_bindings(root)),'cleared':clear_skips(root,domain,clear or [],cwd,load_bindings(root))}
        elif cmd=='source-sync':
            from .sources import sync
            if not domain:raise ValueError('unbound_project')
            result=sync(root,domain,args.name,args.rebuild,all_remote=args.all_remote,network=args.actor not in {'subagent','viewer'})
        elif cmd=='ask':
            if not domain:raise ValueError('unbound_project')
            result=_ask(root,domain,' '.join(args.query),args.limit)
        elif cmd=='change-plan':
            from .changes import change_plan
            result=change_plan(root,args.action,_payload(args))
            result['hint']='Review with inspect-plan PLAN_ID, then apply PLAN_ID --expected-plan-sha256 DIGEST.'
        elif cmd=='changes':
            from .changes import listing
            result=listing(root,args.status,args.project)
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
            if not domain:raise ValueError('unbound_project')
            from .sync import sync,build_overlay
            result=sync(domain,args.vault,root) if cmd=='sync' else build_overlay(domain,args.vault,root)
        else:raise ValueError('Unsupported command')
        invocation['result']=result;print(json.dumps(result,ensure_ascii=False,separators=(',',':')));return 0
    except (OSError,ValueError,TimeoutError,KeyError,TypeError) as exc:
        message=str(exc);code=error_code(message)
        print(json.dumps({'ok':False,'error':{'code':code,'message':message}},separators=(',',':')),file=sys.stderr)
        invocation.update(code=6 if code=='stale' else 5 if code=='conflict' else 2,error={'code':code,'message':message});return invocation['code']
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
