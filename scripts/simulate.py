"""Functionality simulation: drive the real hook adapters and CLI through scripted sessions and check every output.

Each scenario (tests/scenarios/*.json) runs once per harness against a fresh sandbox store: a temporary folder marked
with sandbox.json, selected through LIVING_MEMORY_ROOT, with every harness home pointed at an empty folder. The
user's store and configuration are never read or written. Steps are hook events, sent in each harness's own wire
format, and CLI commands. Expectations are checked against the adapter's parsed reply and against the debug log's
decision detail (core/logs.py). Invariants run after every step: one log line per step that agrees with the reply,
injected refs that match the delivered text and the usage diary, no scenario secret in logs, diary or session
state, and no error line without an injected fault. Each scenario ends with validate and doctor.

    py -3.15 scripts/simulate.py [--scenario NAME|all] [--harness claude|codex|copilot|factory|all]
                                 [--subprocess] [--package DIR] [--content] [--keep] [--out FILE] [--json]

In-process (the default) calls each adapter's main() with patched argv/stdin/stdout. --subprocess runs the hook
scripts and living_memory.py as real processes; --package DIR runs an installed copy instead of this repository.
--content also logs text (LIVING_MEMORY_LOG_CONTENT=1, honoured only because the store is a sandbox) and checks
that the logged context equals the delivered one.
"""
from __future__ import annotations
import argparse,contextlib,copy,io,json,os,re,shutil,subprocess,sys,tempfile,time
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
SCENARIOS=REPO/'tests'/'scenarios'
HARNESSES=('claude','codex','copilot','factory')
DEFAULT_TAGS=['subject/project','concern/decision']
DEFAULT_TAXONOMY=['subject/project','subject/user','concern/decision','concern/knowledge','concern/preference','concern/architecture','concern/workflow']
# Copilot's native event names (hooks/copilot_hooks.py EVENTS, reversed).
COPILOT={'session-start':'sessionStart','user-prompt':'userPromptTransformed','subagent-start':'subagentStart','pre-tool-use':'preToolUse','stop':'agentStop'}
FAULTS={'OSError':OSError,'ValueError':ValueError,'RuntimeError':RuntimeError,'TimeoutError':TimeoutError}

class Sim:
    """One scenario on one harness."""
    def __init__(self,scenario,harness,package,subprocess_mode,content,base):
        self.scenario=scenario;self.harness=harness;self.package=package;self.sub=subprocess_mode;self.content=content
        self.base=base;self.root=base/'store';self.project=base/'project';self.vars={};self.keys={}
        self.session='s1';self.turns=0;self.fault=None;self.fault_skipped=False;self.results=[]

    # ---- environment and store ----
    def env(self):
        homes={h.upper()+'_HOME':str(self.base/('home-'+h)) for h in HARNESSES}
        return {**homes,'LIVING_MEMORY_ROOT':str(self.root),'LIVING_MEMORY_LOG_LEVEL':'debug','LIVING_MEMORY_LOG_CONTENT':'1' if self.content else '',
                'LIVING_MEMORY_ACTOR':'main'}

    def build(self):
        from core.init_domain import init_domain
        from core.notes import capture,ensure_global
        from core.repository import atomic_write,canonical_json
        s=self.scenario
        for name,folder in (s.get('domains') or {'demo':'project'}).items():
            (self.base/folder).mkdir(parents=True,exist_ok=True);init_domain(name,[str(self.base/folder)],self.root)
        atomic_write(self.root/'tag_taxonomy.json',canonical_json({'tags':s.get('taxonomy',DEFAULT_TAXONOMY)}))
        (self.root/'sandbox.json').write_text('{"sandbox": true}\n',encoding='utf-8')
        for folder in s.get('folders',[]):(self.base/folder).mkdir(parents=True,exist_ok=True)   # e.g. an unbound git repo
        for note in s.get('notes',[]):
            domain=note.get('domain','demo')
            if domain=='_global':ensure_global(self.root)
            extra={'files':note['files']} if note.get('files') else {}   # e.g. a codebase map's repo folder
            made=capture(note['title'],note.get('body',''),note.get('tags',DEFAULT_TAGS),domain,self.root,type=note.get('type','knowledge'),allow_similar=True,**extra)
            self.keys[note['key']]={'domain':domain,'id':made['id'],'path':made['path']}
        if s.get('transcript'):
            path=self.base/'transcript.jsonl'
            path.write_text(''.join(json.dumps(row)+'\n' for row in s['transcript']),encoding='utf-8')
            self.vars['transcript']=str(path)

    # ---- templating ----
    def ref(self,key):
        if key not in self.keys:raise ValueError(f'unknown note @{key}')
        return f"{self.keys[key]['domain']}:{self.keys[key]['id']}"

    def names(self,refs):
        """Refs shown as the scenario's @keys where it has one, for readable failures."""
        known={f"{v['domain']}:{v['id']}":'@'+k for k,v in self.keys.items()}
        return sorted(known.get(r,r) for r in refs or [])

    def fill(self,value):
        """{project} {store} {base} {session} {transcript} {memory} {python} {var}, {path:@k} {short:@k} {id:@k}, and a whole '@k' -> ref."""
        if isinstance(value,list):return [self.fill(v) for v in value]
        if isinstance(value,dict):return {k:self.fill(v) for k,v in value.items()}
        if not isinstance(value,str):return value
        if re.fullmatch(r'@[\w-]+',value):return self.ref(value[1:])
        from core.notes import short_ref
        def one(match):
            name=match[1]
            if ':' in name:
                kind,key=name.split(':',1);key=key.lstrip('@')
                if kind=='path':return str(self.keys[key]['path'])
                if kind=='short':return short_ref(self.keys[key]['domain'],self.keys[key]['id'])
                if kind=='id':return self.keys[key]['id']
                if kind=='ref':return self.ref(key)
                return match[0]
            fixed={'project':str(self.project),'store':str(self.root),'base':str(self.base),'session':self.session,'python':sys.executable,
                   'memory':f'"{sys.executable}" "{self.package/"living_memory.py"}" --root "{self.root}" --domain demo'}
            if name in fixed:return fixed[name]
            return str(self.vars[name]) if name in self.vars else match[0]
        return re.sub(r'\{([\w:@-]+)\}',one,value)

    # ---- the log, diary and session state ----
    def log_lines(self):
        rows=[]
        for path in sorted((self.root/'logs').glob('log-*.jsonl')):
            for line in path.read_text(encoding='utf-8').splitlines():
                try:rows.append(json.loads(line))
                except ValueError:pass
        return rows

    def diary(self):
        from core.usage import events
        return events(self.root)

    def private_text(self):
        """Everything that must never hold prompt text: the log (unless --content), the diary and session state."""
        parts=[p.read_text(encoding='utf-8',errors='replace') for p in (self.root/'.turns').glob('*') if p.is_file() and p.suffix in {'.json','.jsonl'}]
        if not self.content:parts+=[p.read_text(encoding='utf-8') for p in (self.root/'logs').glob('*.jsonl')]
        return '\n'.join(parts).casefold()

    # ---- running steps ----
    def wired(self):
        if not hasattr(self,'_wired'):
            from integrations import setup
            groups=setup.hooks(self.harness)
            if self.harness=='copilot':self._wired={e for e,n in COPILOT.items() if n in groups['hooks']}
            else:self._wired={{**setup.EVENTS,'SessionEnd':'session-end'}[e] for e in groups}
            self._matcher=re.compile(f'(?:{setup.PRE_TOOL_MATCHER})\\Z') if self.harness in {'claude','factory'} else None
        return self._wired

    def payload(self,event,given):
        """A Claude-style payload, then translated to the harness's own wire format."""
        p={'cwd':str(self.project),'session_id':self.session,'hook_event_name':event,**given}
        if event=='user-prompt':self.turns+=1
        if self.harness=='codex':p.setdefault('turn_id',f't{self.turns}')
        if self.harness=='copilot':
            rename={'session_id':'sessionId','tool_name':'toolName','agent_id':'agentId','permission_mode':'permissionMode','transcript_path':'transcriptPath',
                    'stop_hook_active':'stopHookActive'}
            p={rename.get(k,k):v for k,v in p.items() if k!='hook_event_name'}
            if 'tool_input' in p:p['toolArgs']=json.dumps(p.pop('tool_input'))
            if 'prompt' in p:p['transformedPrompt']=p['prompt']
            p['timestamp']=int(time.time()*1000)
        return p

    def call_hook(self,native,wire):
        raw=json.dumps(wire)
        if self.sub:
            script=self.package/'hooks'/f'{self.harness}_hooks.py'
            done=subprocess.run([sys.executable,'-X','utf8',str(script),native],input=raw.encode('utf-8'),capture_output=True,timeout=120,env={**os.environ,**self.env()})
            return done.stdout.decode('utf-8','replace'),done.stderr.decode('utf-8','replace')
        import importlib
        module=importlib.import_module('hooks.claude_hooks' if self.harness=='factory' else f'hooks.{self.harness}_hooks')
        out,err=io.StringIO(),io.StringIO();stdin=io.TextIOWrapper(io.BytesIO(raw.encode('utf-8')),encoding='utf-8')
        with patch.object(sys,'argv',['hook',native]),patch.object(sys,'stdin',stdin),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            module.main('factory') if self.harness=='factory' else module.main()
        return out.getvalue(),err.getvalue()

    def parse(self,event,stdout,wire):
        """The adapter's reply as {context, deny, allow, reminder, system_message, instructions}."""
        n=dict.fromkeys(('context','deny','allow','reminder','system_message','instructions'))
        text=stdout.strip()
        if event=='pre-compact' and self.harness in {'claude','factory'}:n['instructions']=text or None;return n
        try:out=json.loads(text.splitlines()[-1]) if text else {}
        except ValueError:raise AssertionError(f'reply is not JSON: {text[:200]!r}')
        if self.harness=='copilot':
            n['context']=out.get('additionalContext')
            if 'modifiedTransformedPrompt' in out:
                prefix=str(wire.get('transformedPrompt',wire.get('prompt','')))+'\n\n';m=out['modifiedTransformedPrompt']
                n['context']=m[len(prefix):] if m.startswith(prefix) else m
            decision=out.get('permissionDecision');reason=out.get('permissionDecisionReason')
        else:
            body=out.get('hookSpecificOutput') or {}
            if body.get('hookEventName')=='Stop':n['reminder']=body.get('additionalContext')
            else:n['context']=body.get('additionalContext')
            decision=body.get('permissionDecision');reason=body.get('permissionDecisionReason')
            n['system_message']=out.get('systemMessage')
        if decision in {'deny','allow'}:n[decision]=reason
        if out.get('decision')=='block':n['reminder']=out.get('reason')
        return n

    def call_cli(self,argv,stdin):
        args=['--root',str(self.root),'--cwd',str(self.project)]+argv
        if self.sub:
            done=subprocess.run([sys.executable,'-X','utf8',str(self.package/'living_memory.py')]+args,input=(stdin or '').encode('utf-8'),
                                capture_output=True,timeout=300,env={**os.environ,**self.env()})
            return done.returncode,done.stdout.decode('utf-8','replace'),done.stderr.decode('utf-8','replace')
        from core.cli import main
        out,err=io.StringIO(),io.StringIO()
        with patch.object(sys,'stdin',io.StringIO(stdin or '')),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:code=main(args)
            except SystemExit as exc:code=exc.code if isinstance(exc.code,int) else 2
        return code,out.getvalue(),err.getvalue()

    def step(self,i,step):
        record={'i':i,'kind':'hook' if 'hook' in step else 'cli' if 'cli' in step else 'fault' if 'fault' in step else 'session','harness':self.harness,'ok':True,'failures':[]}
        fail=record['failures'].append
        if step.get('only') and self.harness not in step['only']:record.update(skipped='not for this harness');return record
        if 'session' in step and len(step)==1:self.session=step['session'];record['event']='session';return record
        if 'fault' in step:
            record['event']=step['fault']
            if self.sub:self.fault_skipped=True;record.update(skipped='fault injection needs in-process mode');return record
            self.fault=(step['fault'],FAULTS[step.get('raise','OSError')]);return record
        if self.fault_skipped:self.fault_skipped=False;record.update(skipped='follows a skipped fault');return record
        expect=self.fill({**step.get('expect',{}),**(step.get('expect_by_harness',{}).get(self.harness) or {})})
        lines_before=len(self.log_lines());diary_before=len(self.diary());start=time.perf_counter()
        fault=self.fault;self.fault=None
        guard=patch(fault[0],side_effect=fault[1]('simulated fault')) if fault else contextlib.nullcontext()
        with guard:
            if 'hook' in step:self.run_hook(step,expect,record,fail)
            else:self.run_cli(step,expect,record,fail)
        record['ms']=round((time.perf_counter()-start)*1000,1)
        if record.get('skipped'):return record
        new=self.log_lines()[lines_before:];diary=self.diary()[diary_before:]
        self.invariants(step,expect,record,new,diary,bool(fault),fail)
        record['ok']=not record['failures'];return record

    def run_hook(self,step,expect,record,fail):
        event=step['hook'];record['event']=event
        if event not in self.wired():record.update(skipped=f'{self.harness} does not wire {event}');return
        given=self.fill(step.get('payload',{}))
        if 'session' in step:self.session=step['session']
        if event=='pre-tool-use' and self._matcher and not self._matcher.match(str(given.get('tool_name',''))):
            record.update(skipped=f"{self.harness} does not send {given.get('tool_name')} to the hook");return
        wire=self.payload(event,given)
        native=COPILOT.get(event,event) if self.harness=='copilot' else event
        stdout,stderr=self.call_hook(native,wire)
        try:reply=self.parse(event,stdout,wire)
        except AssertionError as error:fail(str(error));return
        record['reply']={k:v for k,v in reply.items() if v};record['stderr']=stderr[-500:] or None
        record['_wire']=wire

    def run_cli(self,step,expect,record,fail):
        argv=self.fill(step['cli']);record['event']=command_name(argv)
        code,stdout,stderr=self.call_cli(argv,self.fill(step.get('stdin')))
        try:result=json.loads(stdout) if stdout.strip() else None
        except ValueError:result={'text':stdout[:2000]}
        try:error=json.loads(stderr).get('error') if stderr.strip() else None
        except (ValueError,AttributeError):error={'message':stderr[:2000]}
        record.update(exit=code,result=result,error=error)
        if isinstance(result,dict):
            for name,path in (step.get('save') or {}).items():
                value=result
                for part in path.split('.'):value=value.get(part) if isinstance(value,dict) else None
                self.vars[name]=value
            if step.get('as') and result.get('id'):
                self.keys[step['as']]={'domain':result.get('domain','demo'),'id':result['id'],'path':result.get('path')}

    # ---- checks ----
    def invariants(self,step,expect,record,new,diary,faulted,fail):
        kind='hook' if 'hook' in step else 'command'
        mine=[r for r in new if r.get('kind')==kind]
        detail={}
        if kind=='hook':
            event=record['event']
            # Claude and Factory answer settled Read/Glob/Grep calls in core/hookfast.py: an empty reply and no log line.
            fast=event=='pre-tool-use' and not mine and not record.get('reply') and self.harness in {'claude','factory'}
            if 'fast' in expect and expect['fast']!=fast:fail(f"fast: expected {expect['fast']!r}, got {fast!r}")
            if fast:record['fast']=True;return
            if len(mine)!=1 or mine[0].get('event')!=event:fail(f'expected one hook line for {event}, got {[r.get("event") for r in mine]}');return
            line=mine[0];detail=line.get('detail') or {};record['detail']=detail;record['level']=line.get('level')
            reply=record.get('reply',{});outcome=set(line.get('outcome') or [])
            if bool(reply.get('deny'))!=('deny' in outcome):fail(f"log outcome {sorted(outcome)} disagrees with the reply's deny")
            if bool(reply.get('reminder'))!=('continue_reason' in outcome):fail(f"log outcome {sorted(outcome)} disagrees with the reply's reminder")
            if event!='pre-compact' and bool(reply.get('context'))!=('context' in outcome):fail(f"log outcome {sorted(outcome)} disagrees with the reply's context")
            if event=='user-prompt':
                from core.notes import short_ref
                injected=detail.get('injected') or []
                context=reply.get('context') or ''
                for ref in injected:
                    domain,note_id=ref.split(':',1)
                    if short_ref(domain,note_id) not in context:fail(f'{ref} is logged as injected but is not in the delivered context')
                prompted={r['ref'] for r in diary if r.get('e')=='inject' and r.get('k')=='prompt'}
                if prompted!=set(injected):fail(f'usage diary injected {sorted(prompted)}, log says {sorted(injected)}')
                maps=(detail.get('maps') or {}).get('injected') or []
                for ref in maps:
                    if short_ref(*ref.split(':',1)) not in context:fail(f'map {ref} is logged as injected but is not in the delivered context')
                mapped={r['ref'] for r in diary if r.get('e')=='inject' and r.get('k')=='prompt-map'}
                if mapped!=set(maps):fail(f'usage diary injected maps {sorted(mapped)}, log says {sorted(maps)}')
                briefs=(detail.get('brief') or {}).get('injected') or []
                for ref in briefs:
                    if short_ref(*ref.split(':',1)) not in context:fail(f'brief {ref} is logged as injected but is not in the delivered context')
                briefed={r['ref'] for r in diary if r.get('e')=='inject' and r.get('k')=='prompt-brief'}
                if briefed!=set(briefs):fail(f'usage diary injected briefs {sorted(briefed)}, log says {sorted(briefs)}')
            if self.content:
                logged=(line.get('content') or {});delivered={'context':reply.get('context') if event!='pre-compact' else None,'continue_reason':reply.get('reminder')}
                for key,value in delivered.items():
                    if value and logged.get(key)!=value:fail(f'content mode: logged {key} differs from the delivered text')
            self.expect_hook(expect,record,detail,new,fail)
        else:
            commands=[r for r in mine if r.get('event')==record['event']]
            if not commands:fail(f"no command line for {record['event']}");return
            line=commands[-1]
            if line.get('exit_code')!=record['exit']:fail(f"log exit_code {line.get('exit_code')} != {record['exit']}")
            record['reason']=(line.get('error') or {}).get('reason')
            self.expect_cli(expect,record,line,fail)
        errors=[r for r in new if r.get('level')=='error']
        if errors and not faulted:fail(f"error lines without an injected fault: {[r.get('event') for r in errors]}")
        text=self.private_text()
        for secret in self.scenario.get('secrets',[]):
            if secret.casefold() in text:fail(f'secret {secret!r} reached the log, diary or session state')

    def expect_hook(self,e,record,detail,new,fail):
        reply=record.get('reply',{})
        def check(name,actual,wanted):
            if actual!=wanted:fail(f'{name}: expected {wanted!r}, got {actual!r}')
        if 'inject' in e:check('inject',self.names(detail.get('injected')),self.names(e['inject']))
        for ref in e.get('inject_includes',[]):
            if ref not in (detail.get('injected') or []):fail(f'inject_includes: {self.names([ref])[0]} not injected (got {self.names(detail.get("injected"))})')
        for key,gate in (e.get('gates') or {}).items():
            ref=self.ref(key.lstrip('@'));found=[c.get('gate') for c in detail.get('candidates') or [] if c.get('ref')==ref]
            if gate is None:
                if found:fail(f'gates: {key} was a candidate ({found[0]}), expected none')
            elif found[:1]!=[gate]:fail(f'gates: {key} expected {gate}, got {found[0] if found else "not a candidate"}')
        if 'level' in e:check('level',record.get('level'),e['level'])
        if 'path' in e:check('path',detail.get('path'),e['path'])
        for text in e.get('context_contains',[]):
            if text not in (reply.get('context') or ''):fail(f'context lacks {text!r}')
        for text in e.get('context_excludes',[]):
            if text in (reply.get('context') or ''):fail(f'context contains {text!r}')
        if 'deny' in e:
            if e['deny'] is False:check('deny',detail.get('decision','none').startswith('deny'),False)
            else:
                if not reply.get('deny'):fail('expected a deny in the reply')
                if isinstance(e['deny'],str):check('deny',detail.get('decision'),'deny:'+e['deny'])
        if 'allow' in e:check('allow',detail.get('decision')=='allow',e['allow'])
        if 'reminder' in e:
            if e['reminder'] is None:check('reminder',bool(reply.get('reminder')),False)
            else:
                if not reply.get('reminder'):fail('expected a Stop reminder in the reply')
                check('reminder',detail.get('reason'),'reminded:'+e['reminder'])
        if 'stop_reason' in e:check('stop_reason',detail.get('reason'),e['stop_reason'])
        for text in e.get('reminder_contains',[]):
            if text not in (reply.get('reminder') or ''):fail(f'reminder lacks {text!r}')
        if 'mutation' in e:check('mutation',detail.get('mutation'),e['mutation'])
        if 'hints' in e:check('hints',self.names(detail.get('hints')),self.names(e['hints']))
        if 'recall' in e:check('recall hits',(detail.get('recall') or {}).get('hits',0),e['recall'])
        if 'private_notice' in e:
            from core.private import NOTICE
            check('private_notice',NOTICE in (reply.get('context') or ''),e['private_notice'])
        if 'instructions' in e:check('instructions',bool(reply.get('instructions')),e['instructions'])
        if 'system_message' in e:check('system_message',bool(reply.get('system_message')),e['system_message'])
        for event in e.get('internal',[]):
            if event not in [r.get('event') for r in new if r.get('kind')=='internal']:fail(f'no internal log line {event}')
        if 'detail' in e:
            problem=subset(e['detail'],detail)
            if problem:fail('detail '+problem)

    def expect_cli(self,e,record,line,fail):
        if 'exit' in e and record['exit']!=e['exit']:fail(f"exit: expected {e['exit']}, got {record['exit']} ({(record.get('error') or {}).get('message','')[:200]})")
        if 'status' in e:
            status=record['result'].get('status') if isinstance(record.get('result'),dict) else None
            if status!=e['status']:fail(f"status: expected {e['status']!r}, got {status!r}")
        if 'reason' in e and record.get('reason')!=e['reason']:fail(f"reason: expected {e['reason']!r}, got {record.get('reason')!r}")
        if 'result' in e:
            problem=subset(e['result'],record.get('result'))
            if problem:fail('result '+problem)

    def finish(self):
        """Every domain validates, doctor is ok and nothing awaits recovery."""
        from core.validate import validate_domain
        problems=[]
        for folder in sorted((self.root/'domains').iterdir()):
            issues=validate_domain(folder.name,self.root)
            if issues:problems.append(f'validate {folder.name}: {issues[:3]}')
        code,stdout,_=self.call_cli(['doctor'],None)
        try:doctor=json.loads(stdout)
        except ValueError:doctor={}
        if code or not doctor.get('ok'):problems.append(f"doctor not ok: {doctor.get('warnings')} {doctor.get('problems')}")
        if doctor.get('pending_recovery'):problems.append(f"pending recovery: {doctor['pending_recovery']}")
        if (doctor.get('logs') or {}).get('content')!=('on' if self.content else 'off'):problems.append(f"content logging is {(doctor.get('logs') or {}).get('content')}")
        return problems

    def run(self):
        steps=[]
        with patch.dict(os.environ,self.env()):
            self.build()
            for i,step in enumerate(self.scenario['steps']):
                try:steps.append(self.step(i,step))
                except Exception as error:  # report the crash as this step's failure and stop the scenario
                    steps.append({'i':i,'kind':'crash','harness':self.harness,'ok':False,'failures':[f'{type(error).__name__}: {error}']});break
            final=self.finish()
        for s in steps:s.pop('_wire',None)
        ok=all(s['ok'] for s in steps) and not final
        return {'scenario':self.scenario['name'],'harness':self.harness,'mode':'subprocess' if self.sub else 'in-process','ok':ok,
                'steps':steps,'final':final,'store':str(self.root)}

def command_name(argv):
    """The subcommand in a CLI argv, past the global options (--root/--domain/--cwd/--actor VALUE, --json)."""
    index=0
    while index<len(argv) and argv[index].startswith('--'):index+=1 if argv[index]=='--json' else 2
    return argv[index] if index<len(argv) else ''

def subset(expected,actual,where=''):
    """None when every key/value of expected matches in actual (lists compared whole), else the first mismatch."""
    if isinstance(expected,dict):
        if not isinstance(actual,dict):return f'{where or "value"}: expected an object, got {actual!r}'
        for key,value in expected.items():
            problem=subset(value,actual.get(key),f'{where}.{key}' if where else key)
            if problem:return problem
        return None
    return None if expected==actual else f'{where or "value"}: expected {expected!r}, got {actual!r}'

def load(names):
    found=sorted(SCENARIOS.glob('*.json'))
    scenarios=[json.loads(p.read_text(encoding='utf-8')) for p in found]
    if names in (None,'all'):return scenarios
    wanted=set(names.split(','));picked=[s for s in scenarios if s['name'] in wanted]
    missing=wanted-{s['name'] for s in picked}
    if missing:raise SystemExit(f'unknown scenario(s): {", ".join(sorted(missing))}; known: {", ".join(s["name"] for s in scenarios)}')
    return picked

def simulate(scenarios,harnesses=None,package=None,subprocess_mode=False,content=False,keep=False):
    """Run each scenario on each of its harnesses; returns the report."""
    package=Path(package or REPO).resolve();runs=[]
    for scenario in scenarios:
        for harness in scenario.get('harnesses',HARNESSES):
            if harnesses and harness not in harnesses:continue
            if subprocess_mode and scenario.get('in_process_only'):
                runs.append({'scenario':scenario['name'],'harness':harness,'mode':'subprocess','ok':True,'skipped':'needs in-process fault injection',
                             'steps':[],'final':[]});continue
            base=Path(tempfile.mkdtemp(prefix=f"lm-sim-{scenario['name']}-{harness}-"))
            try:runs.append(Sim(copy.deepcopy(scenario),harness,package,subprocess_mode,content,base).run())
            finally:
                if not keep:shutil.rmtree(base,ignore_errors=True)
    return {'ok':all(r['ok'] for r in runs),'at':datetime.now(timezone.utc).isoformat(timespec='seconds'),'package':str(package),
            'runs':runs,'passed':sum(r['ok'] and not r.get('skipped') for r in runs),'failed':sum(not r['ok'] for r in runs),
            'skipped':sum(bool(r.get('skipped')) for r in runs)}

def summary(report):
    lines=[]
    for run in report['runs']:
        if run.get('skipped'):lines.append(f"SKIP  {run['scenario']:<24} {run['harness']:<8} {run['skipped']}");continue
        skipped=sum(1 for s in run['steps'] if s.get('skipped'))
        lines.append(f"{'PASS' if run['ok'] else 'FAIL'}  {run['scenario']:<24} {run['harness']:<8} {len(run['steps'])} steps"+(f', {skipped} skipped' if skipped else ''))
        for step in run['steps']:
            for failure in step.get('failures',[]):lines.append(f"      step {step['i']} {step.get('kind')} {step.get('event','')}: {failure}")
        for problem in run['final']:lines.append(f'      end: {problem}')
    lines.append(f"{report['passed']} passed, {report['failed']} failed"+(f", {report['skipped']} skipped" if report.get('skipped') else ''))
    return '\n'.join(lines)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--scenario',default='all',help='Name, comma-separated names, or all')
    parser.add_argument('--harness',default='all',choices=HARNESSES+('all',))
    parser.add_argument('--subprocess',action='store_true',help='Run hooks and the CLI as real processes')
    parser.add_argument('--package',type=Path,help='Package folder to run (default: this repository); implies --subprocess')
    parser.add_argument('--content',action='store_true',help='Log text too (sandbox stores only) and check it')
    parser.add_argument('--keep',action='store_true',help='Keep each temporary store and print where it is')
    parser.add_argument('--out',type=Path,help='Report file (default .cache/simulation/<UTC time>.json)')
    parser.add_argument('--json',action='store_true',help='Print the report as JSON')
    args=parser.parse_args(argv)
    if args.package:
        args.subprocess=True
        if not (args.package/'living_memory.py').is_file():raise SystemExit(f'{args.package} has no living_memory.py')
        sys.path.insert(0,str(args.package.resolve()))   # check against the package's own code
    else:sys.path.insert(0,str(REPO))
    report=simulate(load(args.scenario),None if args.harness=='all' else {args.harness},args.package,args.subprocess,args.content,args.keep)
    out=args.out or REPO/'.cache'/'simulation'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=1,default=str),encoding='utf-8')
    print(json.dumps(report,default=str) if args.json else summary(report)+f'\nreport: {out}')
    if args.keep:print('stores:\n'+'\n'.join(r['store'] for r in report['runs']))
    return 0 if report['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
