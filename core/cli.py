"""Unified compact script interface. Compatibility scripts delegate here."""
from __future__ import annotations
import argparse,json,os,sys,hashlib
from pathlib import Path
from .common import memory_root,load_bindings,resolve_domain,canonical_domain
from .repository import read_json,inventory,parse,store_lock,canonical_json,now
from .note_types import NOTE_TYPES

def _payload(args):
    if getattr(args,'payload_file',None):raw=args.payload_file.read_text(encoding='utf-8')
    else:raw=sys.stdin.read()
    if len(raw.encode())>1024*1024:raise ValueError('Payload exceeds 1 MiB')
    return json.loads(raw)
def _reference(value,domain):
    if ':' in value:return value.split(':',1)
    if not domain:raise ValueError('Pass domain:id, --domain, or --cwd for a bound project')
    return domain,value

def _turn(root,session,turn):
    """The turn a receipt belongs to: explicit, else the session's latest prompt."""
    if turn or not session:return turn
    from .runtime import _pointer
    value=read_json(_pointer(root,session),{}) or {}
    if not value.get('turn'):raise ValueError('No prompt recorded for this session yet; pass --turn')
    return value['turn']

def _receipt(args,domain,note_id,root):
    """Updating or superseding is a memory decision for the turn, like a capture."""
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
    pending=[]
    for path in (root/'.operations').glob('*/journal.json'):
        value=read_json(path)
        if value['state'] not in {'committed','rolled_back','abandoned'}:pending.append(value['id'])
    index='unbound'
    counts={};warnings=[]
    unreadable={}
    for directory in (root/'domains').iterdir() if (root/'domains').is_dir() else []:
        if directory.is_dir():
            counts[directory.name]=sum(1 for _ in (directory/'notes').rglob('*.md'))+sum(1 for _ in (directory/'archive').rglob('*.md'))
            found=problems(directory.name,root)
            if found:unreadable[directory.name]=found
    if (root/'improvement').is_dir():warnings.append('improvement/ holds data from a removed feature; nothing reads it and it is safe to delete')
    warnings+=[f'{name}: {count} notes; capture and freshness checks scale linearly and may slow hooks' for name,count in counts.items() if count>1500]
    scope=config.get('search_scope',{}) if isinstance(config.get('search_scope',{}),dict) else {}
    for kind,names,effect in (('global_domains',scope.get('global_domains',['_global']),'the no-match title fallback uses the active domain instead'),
                              ('foreign_domains',scope.get('foreign_domains',[]),'it is never searched')):
        for name in names:
            try:missing=not (root/'domains'/canonical_domain(name,root)).is_dir()
            except ValueError:missing=True
            if missing:warnings.append(f"search_scope.{kind} names '{name}', which has no domain folder; {effect}")
    if domain:index='fresh' if _fresh(domain,root,inventory(domain,root,retired=True)) else 'dirty_or_missing'
    last=read_json(root/'hooks/last-delivery.json',{})
    domain_exists=bool(domain and (root/'domains'/domain/'notes').is_dir())
    from .storage import diagnostics
    location = diagnostics(explicit)
    return {'ok':fts and not pending and not unreadable and (not domain or domain_exists),'problems':unreadable,'interpreter':sys.executable,'python':sys.version.split()[0],'fts5':fts,'root':str(root),'location':location,'domain':domain,'binding':'bound' if domain else 'unbound','domain_data':'present' if domain_exists else 'missing' if domain else 'unbound','index':index,'pending_recovery':pending,'hooks':{'trust':'unknown','last_delivery':last},'capture':'main_agent_via_scripts','protection':'cooperating_agent_governance','notes':counts,'warnings':warnings}

SECTION_ARGS=('summary','detail','link','action','done','important')

def sections_args(p):
    """Structured note sections (see core/note_format.py); each replaces only its own section."""
    p.add_argument('--summary',help='Executive Summary: one or two sentences')
    p.add_argument('--detail',action='append',help='Key Details bullet, e.g. "**Cause:** ..." (repeatable)')
    p.add_argument('--link',action='append',help='Resource Links bullet: [[Note]], a URL or a REF (repeatable)')
    p.add_argument('--action',action='append',help='Open Action Item; plan, bug and risk notes only (repeatable)')
    p.add_argument('--done',action='append',help='Completed Action Item (repeatable)')
    p.add_argument('--important',help='Important Note callout: a critical caveat or warning')

def _sectioned(args,body=''):
    """The body with any structured sections from the command line applied, or None if none were given."""
    if not any(getattr(args,k) for k in SECTION_ARGS):return None
    from .note_format import render
    return render(args.summary or '',args.detail or (),args.link or (),args.action or (),args.done or (),args.important or '',body)

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
        p.add_argument('--actor',choices=['main','subagent'],default=argparse.SUPPRESS if suppress else os.environ.get('LIVING_MEMORY_ACTOR','main'))
    common(parser);subs=parser.add_subparsers(dest='command',required=True)
    def sub(name):p=subs.add_parser(name);common(p,True);return p
    p=sub('setup');p.add_argument('--location',type=Path);p.add_argument('--non-interactive',action='store_true');p.add_argument('--source',type=Path);p.add_argument('--apply-plan');p.add_argument('--inspect-plan');p.add_argument('--expected-plan-sha256')
    p=sub('bind');p.add_argument('name');p.add_argument('prefix',nargs='+')
    p=sub('capture');p.add_argument('items',nargs='*');p.add_argument('--payload-file',type=Path);p.add_argument('--content-file',type=Path);p.add_argument('--title');p.add_argument('--tag',action='append',default=[]);p.add_argument('--tags',default='');p.add_argument('--prefix',help='Compatibility assertion only; must equal the uppercase type');p.add_argument('--type',choices=NOTE_TYPES,default='knowledge');p.add_argument('--context',default='');p.add_argument('--idempotency-key');p.add_argument('--session');p.add_argument('--turn');p.add_argument('--allow-similar',action='store_true');p.add_argument('--supersedes',action='append',default=[],help='REF of a note this one replaces; it is retired in the same transaction');p.add_argument('--review-by',help='YYYY-MM-DD; status/plan/bug/risk notes get a default')
    for facet in ('subject','concern','platform','technology'):p.add_argument('--'+facet,action='append',default=[])
    sections_args(p)
    p=sub('search');p.add_argument('query');p.add_argument('--scope',choices=['active','fallback','all'],default='fallback');p.add_argument('--limit',type=int,default=5);p.add_argument('--filter',action='append',default=[]);p.add_argument('--rebuild',action='store_true')
    p=sub('read');p.add_argument('reference');p.add_argument('--max-chars',type=int,default=16000);p.add_argument('--metadata-only',action='store_true')
    p=sub('update');p.add_argument('reference');p.add_argument('--title');p.add_argument('--body');p.add_argument('--body-file',type=Path);p.add_argument('--tag',action='append');p.add_argument('--type',choices=NOTE_TYPES);p.add_argument('--context');p.add_argument('--review-by');p.add_argument('--expected-revision');p.add_argument('--payload-file',type=Path);p.add_argument('--session');p.add_argument('--turn')
    for facet in ('subject','concern','platform','technology'):p.add_argument('--'+facet,action='append')
    sections_args(p)
    p=sub('supersede');p.add_argument('reference');p.add_argument('--by',required=True,help='REF of the note that replaces it');p.add_argument('--session');p.add_argument('--turn')
    p=sub('review');p.add_argument('--limit',type=int,default=20)
    p=sub('plan');p.add_argument('action',choices=['update','rename','retag','resolve','reopen','archive','supersede','delete']);p.add_argument('reference');p.add_argument('--payload-file',type=Path)
    p=sub('apply');p.add_argument('plan_id');p.add_argument('--expected-plan-sha256',required=True)
    p=sub('inspect-plan');p.add_argument('plan_id')
    p=sub('merge-plan');p.add_argument('--payload-file',type=Path)
    p=sub('history');p.add_argument('--note');p.add_argument('--limit',type=int,default=20)
    p=sub('undo');p.add_argument('operation_id')
    p=sub('recover');p.add_argument('operation_id');p.add_argument('--direction',choices=['forward','backward'],default='forward');p.add_argument('--abandon',action='store_true',help='Stop treating the operation as pending; no note file is changed')
    p=sub('diff');p.add_argument('operation_id')
    p=sub('doctor')
    p=sub('migration-plan')
    p=sub('note-type-plan')
    p=sub('rebuild');p.add_argument('name',nargs='?')
    p=sub('validate');p.add_argument('name',nargs='?')
    p=sub('query');p.add_argument('sql')
    p=sub('taxonomy-plan');p.add_argument('tags',nargs='+')
    p=sub('taxonomy')
    p=sub('batch');p.add_argument('--payload-file',type=Path)
    p=sub('retag-plan');p.add_argument('--payload-file',type=Path)
    p=sub('remap-plan');p.add_argument('--payload-file',type=Path)
    p=sub('capture-finalize');p.add_argument('--session',required=True);p.add_argument('--turn',help='Defaults to the latest prompt of the session');p.add_argument('--note',action='append',default=[])
    p=sub('sync');p.add_argument('vault',type=Path)
    p=sub('overlay-build');p.add_argument('vault',type=Path)
    p=sub('overlay-teardown-plan');p.add_argument('vault',type=Path)
    p=sub('overlay-sync-all');p.add_argument('--primary',help='Domain whose view the shared dashboards show (built last)')
    args=parser.parse_args(argv);cwd=args.cwd or os.getcwd()
    try:
        if args.command=='setup':
            from .storage import setup,apply_relocation,inspect_relocation
            if args.actor=='subagent':raise ValueError('Subagents are read-only')
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
        domain=args.domain or resolve_domain(cwd,load_bindings(root))
        if domain:domain=canonical_domain(domain,root)
        cmd=args.command
        if args.actor=='subagent' and cmd not in {'search','read','query','doctor','validate','history'}:raise ValueError('Subagents are read-only; return mutation proposals to the main agent')
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
                body=_sectioned(args,body) or body
                tags=args.tag+[t.strip() for t in args.tags.split(',') if t.strip()]
                tags += [facet+'/'+value for facet in ('subject','concern','platform','technology') for value in getattr(args,facet)]
                data={'title':title,'body':body,'tags':tags,'prefix':args.prefix,'type':args.type,'context':args.context,'idempotency_key':args.idempotency_key,'domain':domain}
                if args.turn and not args.session:raise ValueError('--turn needs --session')
                if args.session:data.update(session_id=args.session,turn_id=_turn(root,args.session,args.turn))
                if args.allow_similar:data['allow_similar']=True
                if args.supersedes:data['supersedes']=args.supersedes
                if args.review_by:data['review_by']=args.review_by
            else:data=_payload(args)
            batch=data if isinstance(data,list) else [data]
            if not 1<=len(batch)<=20:raise ValueError('Capture batch must contain 1..20 items')
            results=[]
            with store_lock(root):
                for item in batch:
                    item=dict(item);selected=item.pop('domain',domain);session=item.pop('session_id',None);turn=item.pop('turn_id',None)
                    if session and not turn:turn=_turn(root,session,None)
                    if not selected:raise ValueError('Project is unbound; pass --domain or bind it')
                    outcome=capture(domain=selected,root=root,**item);outcome.pop('path',None);results.append(outcome)
                    if session and turn:
                        from .capture import finalize
                        finalize(session,turn,[selected+':'+outcome['id']],root)
            result=results if isinstance(data,list) else results[0]
        elif cmd=='search':
            from .search import search_domains,search_domain
            if not domain:raise ValueError('unbound_project')
            config=load_bindings(root);scope=config.get('search_scope',{});globals_=scope.get('global_domains',['_global'])
            globals_=[canonical_domain(g,root) for g in globals_]
            globals_=[g for g in globals_ if (root/'domains'/g).exists()]
            from .runtime import parent_domains
            primary=list(dict.fromkeys([domain]+parent_domains(domain,config)+globals_));foreign=scope.get('foreign_domains',[])
            filters=dict(value.split('=',1) for value in args.filter)
            if set(filters)-{'prefix','status','tag','type'}:raise ValueError('Unsupported search filter')
            def run(domains):
                return search_domains(args.query,domains,args.limit,root,filters or None,args.rebuild)
            domains=sorted(load_bindings(root).get('domains',{})) if args.scope=='all' else primary
            hits=run(domains);fallback=False
            if not hits and args.scope=='fallback' and foreign:domains=foreign;hits=run(domains);fallback=True
            for hit in hits:hit['sourceScope']='active' if hit['domain']==domain else 'global' if hit['domain'] in globals_ else 'foreign';hit.pop('path',None)
            result={'ok':True,'mode':args.scope,'domains':domains,'fallback':fallback,'results':hits}
        elif cmd=='read':
            from .notes import find_note
            d,n=_reference(args.reference,domain);path,note=find_note(n,d,root)
            result={k:v for k,v in note.items() if k not in {'body','path','digest'}}
            if not args.metadata_only:result.update(body=note['body'][:max(0,min(64000,args.max_chars))],truncated=len(note['body'])>args.max_chars)
        elif cmd=='update':
            from .notes import change
            d,n=_reference(args.reference,domain)
            values=_payload(args) if args.payload_file or not any(getattr(args,k) is not None for k in ('title','body','body_file','tag','type','context','review_by','subject','concern','platform','technology')+SECTION_ARGS) else {}
            if args.title is not None:values['title']=args.title
            if args.body is not None:values['body']=args.body
            if args.body_file:values['body']=args.body_file.read_text(encoding='utf-8-sig')
            if any(getattr(args,k) for k in SECTION_ARGS):
                # Sections given on the command line replace only themselves; the rest of the body is kept.
                from .notes import find_note
                base=values['body'] if 'body' in values else find_note(n,d,root)[1]['body']
                values['body']=_sectioned(args,base)
            tags=(args.tag or [])+[f+'/'+v for f in ('subject','concern','platform','technology') for v in (getattr(args,f) or [])]
            if tags:values['tags']=tags
            for key in ('type','context','review_by'):
                if getattr(args,key) is not None:values[key]=getattr(args,key)
            revision=values.pop('expected_revision',None) or args.expected_revision
            result=change(n,d,'update',root,revision,**values)
            _receipt(args,d,n,root)
        elif cmd=='supersede':
            from .notes import change
            d,n=_reference(args.reference,domain);bd,bn=_reference(args.by,domain)
            if bd!=d:raise ValueError('A note can only be superseded by a note in the same domain')
            result=change(n,d,'supersede',root,superseded_by=bn)
            _receipt(args,d,bn,root)
        elif cmd=='review':
            from .note_types import review_due
            if not domain:raise ValueError('unbound_project')
            due=[n for n in inventory(domain,root) if review_due(n)]
            due.sort(key=lambda n:(str(n.get('review_by') or n.get('updated',''))))
            result={'ok':True,'domain':domain,'due':len(due),'notes':[{'ref':f"{domain}:{n['id']}",'title':n['title'],'type':n.get('type'),'updated':str(n.get('updated',''))[:10],'review_by':n.get('review_by')} for n in due[:max(1,min(100,args.limit))]],
                    'hint':'For each note: update it if it is still true (this resets its review date), supersede it if it was replaced, or ask for an archive plan.'}
        elif cmd=='plan':
            from .notes import change
            d,n=_reference(args.reference,domain);values=_payload(args) if args.payload_file or args.action in {'update','rename','retag','supersede'} else {}
            result=change(n,d,args.action,root,plan_only=True,**values)
        elif cmd=='apply':
            from .governance import apply
            result=apply(root,args.plan_id,args.expected_plan_sha256)
        elif cmd=='inspect-plan':
            from .governance import inspect_plan
            result=inspect_plan(root,args.plan_id)
        elif cmd=='merge-plan':
            from .governance import merge_plan
            result=merge_plan(root,_payload(args))
        elif cmd=='history':
            from .governance import history
            result=history(root,args.note,args.limit)
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
        elif cmd=='query':
            from .search import readonly_query
            if not domain:raise ValueError('unbound_project')
            result=readonly_query(domain,args.sql,root)
        elif cmd=='taxonomy-plan':
            from .tag_taxonomy import plan_update
            result=plan_update(args.tags,root)
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
        elif cmd=='capture-finalize':
            from .capture import finalize
            result=finalize(args.session,_turn(root,args.session,args.turn),args.note,root)
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
        print(json.dumps(result,ensure_ascii=False,separators=(',',':')));return 0
    except (OSError,ValueError,TimeoutError,KeyError,TypeError) as exc:
        message=str(exc);code='stale' if 'stale' in message.lower() else 'conflict' if 'conflict' in message.lower() else 'invalid'
        print(json.dumps({'ok':False,'error':{'code':code,'message':message}},separators=(',',':')),file=sys.stderr);return 6 if code=='stale' else 5 if code=='conflict' else 2
if __name__=='__main__':raise SystemExit(main())
