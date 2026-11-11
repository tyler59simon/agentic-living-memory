"""Isolated maintenance trials. --self-test tests fixtures, not model efficacy.

An optional argv JSON command receives one JSON task on stdin and returns JSON; scripts/agents/run_agent.py
adapts a CLI agent (claude -p, codex exec) to that contract. No shell interpolation, downloads or model
invocation without an explicit command.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import repository, learning, governance, backup
from core.init_domain import init_domain
from core.notes import capture, find_note, change
from core.search import rebuild_index, search_domain, _fresh

SCENARIOS = {
    'stale-index': ('Repair stale retrieval without changing canonical notes.', 'Rebuild the disposable domain index; check expected retrieval.'),
    'stale-plan': ('Reject an outdated plan without overwriting the newer note.', 'Inspect the plan; refuse stale application and preserve the newer canonical note.'),
    'interrupted-operation': ('Recover an interrupted journaled operation.', 'Recover the pending operation forward and verify its intended content.'),
    'restore': ('Restore a missing canonical note from the supplied backup.', 'Create a restore plan, inspect it, apply its digest, and verify the note.'),
}


def setup(root, scenario, variant='training'):
    init_domain('demo', [str(root.parent/'project')], root)
    (root/'tag_taxonomy.json').write_text(json.dumps({'tags': ['subject/project', 'concern/workflow']}), encoding='utf-8')
    n = capture('Fixture maintenance fact '+variant, 'original fact', ['subject/project', 'concern/workflow'], 'demo', root)
    path, note = find_note(n['id'], 'demo', root)
    context = dict(scenario=scenario, note_id=n['id'], objective=SCENARIOS[scenario][0])
    if scenario == 'stale-index':
        note.update(body='expectedfreshmarker canonical fact', updated=repository.now())
        path.write_text(repository.serialize(note), encoding='utf-8')
        context['canonical_hash'] = repository.digest(path.read_bytes())
    elif scenario == 'stale-plan':
        plan = change(n['id'], 'demo', 'update', root, plan_only=True, body='outdated proposal')
        change(n['id'], 'demo', 'update', root, body='newer preserved fact')
        context['plan'] = plan
    elif scenario == 'interrupted-operation':
        original = repository._write
        counter = [0]
        def crash(p, data):
            counter[0] += 1
            if counter[0] == 1: raise OSError('fixture interruption')
            return original(p, data)
        operation = 'learning-benchmark-recover'
        with patch.object(repository, '_write', crash):
            try: repository.transaction(root, {'fixture-result.txt': 'recovered'}, 'fixture', operation_id=operation)
            except OSError: pass
        context['operation_id'] = operation
    else:
        context['backup'] = backup.backup(root)['backup']
        path.unlink()
    return context


# What each reference repair checks, recorded as the training run's acceptance evidence.
CHECKS = {
    'stale-index': 'canonical note bytes unchanged, index fresh, expected note returned by search',
    'stale-plan': 'apply refused as stale and the newer canonical note kept',
    'interrupted-operation': 'operation rolled forward with its intended content and no unfinished operations',
    'restore': 'restored note readable with its original content',
}


def reference(root, context):
    """The scripted repair. `actions` lists what it did as CLI steps; the learned procedure is built from them."""
    scenario = context['scenario']; actions = []
    if scenario == 'stale-index':
        rebuild_index('demo', root); actions.append("CMD search 'EXPECTED TERM' --rebuild (rebuilds the disposable index only)")
    elif scenario == 'stale-plan':
        p = context['plan']
        actions.append('CMD inspect-plan PLAN_ID, then CMD apply PLAN_ID --expected-plan-sha256 DIGEST')
        try: governance.apply(root, p['plan_id'], p['digest'])
        except ValueError as exc:
            if 'stale' not in str(exc).lower(): raise
            actions.append('On "stale plan", stop: keep the newer canonical note; do not regenerate or force the plan')
            return dict(outcome='succeeded', refused_stale=True, actions=actions)
        raise AssertionError('Stale plan unexpectedly applied')
    elif scenario == 'interrupted-operation':
        repository.recover(root, context['operation_id'])
        actions.append('CMD doctor to find the unfinished operation, then CMD recover OPERATION_ID (forward)')
    else:
        p = backup.restore_plan(root, context['backup'])
        governance.inspect_plan(root, p['plan_id'])
        governance.apply(root, p['plan_id'], p['digest'])
        actions += ['CMD restore-plan BACKUP', 'CMD inspect-plan PLAN_ID', 'CMD apply PLAN_ID --expected-plan-sha256 DIGEST']
    return dict(outcome='succeeded', actions=actions)


def verify(root, context, report):
    scenario = context['scenario']
    if scenario == 'stale-index':
        path, _ = find_note(context['note_id'], 'demo', root)
        return (repository.digest(path.read_bytes()) == context['canonical_hash']
                and _fresh('demo', root, repository.inventory('demo', root, retired=True))
                and bool(search_domain('expectedfreshmarker', 'demo', root=root)))
    if scenario == 'stale-plan':
        return bool(report.get('refused_stale')) and 'newer preserved fact' in find_note(context['note_id'], 'demo', root)[1]['body']
    if scenario == 'interrupted-operation':
        p = root/'fixture-result.txt'
        return p.exists() and p.read_text() == 'recovered' and not repository.unfinished_operations(root)[0]
    return 'original fact' in find_note(context['note_id'], 'demo', root)[1]['body']


def seed(root, scenario, mode):
    if mode == 'baseline': return None
    recipe = SCENARIOS[scenario][1]
    if mode == 'manual':
        return capture('Maintenance procedure '+scenario, recipe, ['subject/project', 'concern/workflow'], 'demo', root, type='runbook')
    # Execute the training procedure in a separate store; test variants do not
    # contribute to candidate generation or the training verification record.
    with tempfile.TemporaryDirectory(prefix='alm-learning-training-') as temp:
        train = Path(temp)/'store'; context = setup(train, scenario)
        report = reference(train, context)
        if not verify(train, context, report): raise AssertionError('Training fixture failed')
    def call(entity, action, ident=None, **p): return learning.dispatch(root, 'demo', entity, action, ident, p)
    # The learned procedure is what the training run did and checked, not the hand-written recipe.
    recorded = call('record', 'create', objective=SCENARIOS[scenario][0], family='alm.'+scenario, acceptance_criteria=['repair verified'],
                    outcome='succeeded', evidence=[dict(result='pass', method='tool_check', observer='scenario verifier',
                    excerpt='Reference repair passed: '+CHECKS[scenario],
                    limitations='Synthetic training store; no model benefit or generalization established')])
    eid = recorded['evidence'][0]['id']
    c = call('candidate', 'create', content=dict(title='Maintenance procedure '+scenario, body=SCENARIOS[scenario][0],
             use_when=[SCENARIOS[scenario][0]], do_not_use_when=['Canonical notes themselves are corrupt'],
             steps=report['actions'], done_when=[CHECKS[scenario]], fallback='Inspect state with doctor; never delete canonical notes'),
             evidence_ids=[eid], step_evidence=[[eid] for _ in report['actions']])['record']
    p = call('candidate', 'promote-plan', c['id'], expected_revision=c['revision'])
    governance.apply(root, p['plan_id'], p['digest'])
    return dict(procedure_id=p['summary']['procedure_id'], version=1)


def run(command=None, repeats=1, self_test=False, timeout=600):
    results = []
    for repeat in range(repeats):
        for scenario in SCENARIOS:
            # Counterbalance condition order across repeats.
            modes = ['baseline', 'manual', 'learned']
            modes = modes[repeat % 3:]+modes[:repeat % 3]
            for mode in modes:
                with tempfile.TemporaryDirectory(prefix='alm-learning-eval-') as temp:
                    root = Path(temp)/'store'
                    context = setup(root, scenario, 'held-out-'+str(repeat))
                    # Seed creates ordinary transactions; recover fixture must remain
                    # pending until the actual trial, so seed before re-interrupting it.
                    if scenario == 'interrupted-operation': reference(root, context)
                    recipe = seed(root, scenario, mode)
                    if scenario == 'stale-index':
                        path,note=find_note(context['note_id'],'demo',root)
                        rebuild_index('demo',root)
                        note.update(updated=repository.now(),body='expectedfreshmarker held-out variant '+str(repeat))
                        path.write_text(repository.serialize(note),encoding='utf-8')
                        context['canonical_hash']=repository.digest(path.read_bytes())
                    if scenario == 'interrupted-operation':
                        (root/'fixture-result.txt').unlink()
                        operation='learning-benchmark-trial'
                        with patch.object(repository,'_write',side_effect=OSError('fixture interruption')):
                            try:repository.transaction(root,{'fixture-result.txt':'recovered'},'fixture',operation_id=operation)
                            except OSError:pass
                        context['operation_id']=operation
                    request = dict(context, root=str(root), domain='demo', mode=mode, procedure=recipe,
                                   cli=str(Path(__file__).resolve().parents[1]/'living_memory.py'),
                                   report_contract='Return JSON: outcome, refused_stale if appropriate, usage, optional input_tokens/output_tokens. Work only inside this temporary store.')
                    started=time.perf_counter(); error=None
                    try:
                        if self_test: report=reference(root,context)
                        else:
                            child=subprocess.run(command,input=json.dumps(request),text=True,encoding='utf-8',capture_output=True,
                                                 timeout=timeout,env=dict(os.environ,PYTHONUTF8='1'),cwd=temp)
                            if child.returncode: raise ValueError('Agent command failed: '+child.stderr[:500])
                            report=json.loads(child.stdout)
                            if not isinstance(report,dict):raise ValueError('Agent report must be an object')
                        checked=verify(root,context,report)
                    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
                        report={};checked=False;error=str(exc)
                    results.append(dict(scenario=scenario,mode=mode,repeat=repeat,checked_success=checked,
                         reported_outcome=report.get('outcome','unknown'),seconds=round(time.perf_counter()-started,4),
                         reported_usage=report.get('usage','unknown'),input_tokens=report.get('input_tokens'),
                         output_tokens=report.get('output_tokens'),error=error))
    return dict(schema=1,kind='fixture_self_test' if self_test else 'agent_comparison',
                caveat='Fixture self-tests establish harness correctness, not learning efficacy. Agent-reported costs/usage are not independently verified.',results=results)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--self-test',action='store_true');group.add_argument('--command-json')
    parser.add_argument('--repeats',type=int,default=1)
    parser.add_argument('--timeout',type=int,default=600,help='Seconds per agent trial (default 600)')
    args=parser.parse_args()
    command=json.loads(args.command_json) if args.command_json else None
    if command is not None and (not isinstance(command,list) or not command or any(not isinstance(x,str) for x in command)):
        parser.error('--command-json must be a nonempty argv array')
    if not 1<=args.repeats<=10:parser.error('--repeats must be between 1 and 10')
    result=run(command,args.repeats,args.self_test,args.timeout)
    print(json.dumps(result,indent=2))
    sys.exit(0 if all(r['checked_success'] for r in result['results']) else 1)
