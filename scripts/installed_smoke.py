"""Exercise installed scripts and native hook executables with isolated temporary data."""
import argparse,json,os,subprocess,sys,tempfile
from pathlib import Path

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--package',type=Path,default=Path(__file__).resolve().parents[1]);args=parser.parse_args();package=args.package.resolve()
    with tempfile.TemporaryDirectory(prefix='living-memory-installed-smoke-') as folder:
        root=Path(folder)/'store';project=Path(folder)/'project';project.mkdir();env={**os.environ,'LIVING_MEMORY_ROOT':str(root)}
        def run(script,arguments,payload=None):
            result=subprocess.run([sys.executable,str(package/script)]+arguments,input=json.dumps(payload) if payload is not None else None,text=True,capture_output=True,env=env,cwd=project,timeout=30)
            if result.returncode:raise RuntimeError(result.stderr or result.stdout)
            return json.loads(result.stdout or '{}')
        def cli(arguments,payload=None):return run('living_memory.py',arguments,payload)
        cli(['bind','demo',str(project)]);p=cli(['migration-plan']);cli(['apply',p['plan_id'],'--expected-plan-sha256',p['digest']])
        event={'cwd':str(project),'session_id':'installed-smoke','turn_id':'turn-one','prompt':'How does the cache work?'}
        session=run('hooks/codex_hooks.py',['session-start'],event);prompt=run('hooks/codex_hooks.py',['user-prompt'],event)
        note=cli(['capture'],{'domain':'demo','title':'Cache policy','body':'The cache is rebuildable.','tags':['subject/project','concern/knowledge'],'session_id':'installed-smoke','turn_id':'turn-one'})
        hits=cli(['search','How does our cache work?']);read=cli(['read','demo:'+note['id']]);p=cli(['plan','update','demo:'+note['id']],{'body':'The cache uses a reviewed policy.','expected_revision':read['revision']})
        cli(['inspect-plan',p['plan_id']]);receipt=cli(['apply',p['plan_id'],'--expected-plan-sha256',p['digest']]);undo=cli(['undo',receipt['operation_id']]);cli(['apply',undo['plan_id'],'--expected-plan-sha256',undo['digest']])
        stop=run('hooks/codex_hooks.py',['stop'],event)
        guard=run('hooks/codex_hooks.py',['pre-tool-use'],{**event,'tool_name':'Write','tool_input':{'path':str(root/'domains/demo/notes/unsafe.md')}})
        checks={'session_context':'additionalContext' in session.get('hookSpecificOutput',{}),'prompt_context':'additionalContext' in prompt.get('hookSpecificOutput',{}),'capture_retrieved':hits['results'][0]['id']==note['id'],'approved_edit_and_undo':cli(['read','demo:'+note['id']])['body']=='The cache is rebuildable.','capture_completion_no_block':'decision' not in stop,'raw_write_denied':guard.get('hookSpecificOutput',{}).get('permissionDecision')=='deny','doctor':cli(['doctor'])['ok']}
        print(json.dumps({'ok':all(checks.values()),'checks':checks,'data':'isolated temporary store; removed on exit','host':'real installed Python hook processes, not a live model turn'}));return 0 if all(checks.values()) else 1
if __name__=='__main__':raise SystemExit(main())
