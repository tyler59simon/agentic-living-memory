"""Isolated maintenance trials: baseline, manual-runbook and learned-runbook conditions on held-out stores.

--self-test runs the scripted reference repairs; it tests fixtures and verifiers, not model efficacy. With
--command-json an argv agent command receives one JSON task on stdin and returns JSON; scripts/agents/run_agent.py
adapts claude -p and codex exec to that contract. Each trial runs in a throwaway store bound to its own folder (the
agent's cwd), and LIVING_MEMORY_ROOT points the agent's own Living Memory hooks and CLI at that store. In the
learned condition the runbook is what the agent itself recorded and proposed in a separate training run, admitted
only when the harness verified the training repair and `learn candidate evaluate` found it eligible (a stand-in for
reviewer approval). The missing-note scenario looks like a stale index but is a deleted note: it is seeded with the
stale-index runbook to count incorrect applications. No shell interpolation, downloads or model invocation without
an explicit command; --summarize reads result files and runs nothing.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import repository, learning, governance, backup, storage
from core.init_domain import init_domain
from core.notes import capture, find_note, change
from core.search import rebuild_index, search_domain, _fresh

CLI = str(Path(__file__).resolve().parents[1]/'living_memory.py')
DOMAIN = 'demo'
TAGS = ['subject/project', 'concern/workflow']
TRAINED = ('stale-index', 'stale-plan', 'interrupted-operation', 'restore')
LOOKALIKE = {'missing-note': 'stale-index'}   # trap scenario -> the runbook it is seeded with, which does not apply
SCENARIOS = TRAINED+tuple(LOOKALIKE)
MODES = ('baseline', 'manual', 'learned')
USED = ('used', 'partially_used')
TOPICS = ('release checklist', 'cache warmup', 'billing export', 'sensor calibration', 'invoice routing',
          'log rotation', 'theme tokens', 'queue backoff')

# What a procedure for each scenario is for, independent of the variant.
PURPOSE = {
    'stale-index': 'Repair stale retrieval without changing canonical notes.',
    'stale-plan': 'Reject an outdated plan without overwriting the newer note.',
    'interrupted-operation': 'Recover an interrupted journaled operation.',
    'restore': 'Restore a missing canonical note from a backup.',
}

# What each verifier checks, recorded as the training run's acceptance evidence.
CHECKS = {
    'stale-index': 'canonical note bytes unchanged, index fresh, expected note returned by search',
    'stale-plan': 'apply refused as stale and the newer canonical note kept',
    'interrupted-operation': 'operation rolled forward with its intended content and no unfinished operations',
    'restore': 'restored note readable with its original content',
    'missing-note': 'restored note readable with its original content',
}

# Hand-written runbooks, as structured as a learned one (same fields, same projection), written without
# looking at any agent's output.
MANUAL = {
    'stale-index': dict(
        title='Repair a stale search index', body='Rebuild the disposable search index when canonical notes are intact but search lags behind them.',
        use_when=['Search misses or returns outdated text for notes whose canonical Markdown is intact',
                  'doctor or search reports that the index is stale'],
        do_not_use_when=['The canonical note file is missing or unreadable: restore or repair it instead'],
        steps=['Run CMD doctor and confirm the canonical notes are readable',
               'Rebuild the disposable index: CMD rebuild DOMAIN',
               "Search for the expected term (CMD search 'TERM') and confirm the note is returned"],
        done_when=['Search returns the expected note from a fresh index', 'No canonical note changed'],
        fallback='If the note is still missing after a rebuild, check whether its canonical file exists and restore it '
                 'from a backup with restore-plan if not. Never edit or delete canonical notes to fix search.'),
    'stale-plan': dict(
        title='Apply a reviewed plan safely', body='Apply a reviewed plan only while it still matches the notes it changes.',
        use_when=['Applying a reviewed plan whose target note may have changed since the plan was made'],
        do_not_use_when=[],
        steps=['Inspect the plan: CMD inspect-plan PLAN_ID',
               'Apply it with its digest: CMD apply PLAN_ID --expected-plan-sha256 DIGEST',
               'If apply reports a stale plan, stop: keep the newer canonical note and do not regenerate or force the plan'],
        done_when=['A current plan is applied, or a stale plan is refused with the newer note unchanged'],
        fallback='Report the refusal and ask the user whether a new plan against the current note is wanted.'),
    'interrupted-operation': dict(
        title='Recover an interrupted operation', body='Finish a journaled write that stopped part-way.',
        use_when=['doctor reports an unfinished journaled operation', 'A write failed part-way through'],
        do_not_use_when=['doctor lists no unfinished operation'],
        steps=['Run CMD doctor and note the unfinished operation ID',
               'Roll it forward: CMD recover OPERATION_ID --direction forward',
               'Run CMD doctor again and confirm no unfinished operation remains'],
        done_when=["The operation's files hold their intended content", 'doctor lists no unfinished operations'],
        fallback='If forward recovery fails, roll back with --direction backward and report it; never edit journal files.'),
    'restore': dict(
        title='Restore a deleted note from a backup', body='Bring back a deleted canonical note from a backup through a reviewed plan.',
        use_when=['A canonical note was deleted or damaged and a backup containing it exists'],
        do_not_use_when=['The note is present and only search misses it: rebuild the index instead'],
        steps=['Choose the backup that contains the note (the backups folder in the store)',
               'Create a plan: CMD restore-plan BACKUP',
               'Inspect it: CMD inspect-plan PLAN_ID and confirm it writes the note and removes nothing needed',
               'Apply it: CMD apply PLAN_ID --expected-plan-sha256 DIGEST',
               'Read the note and confirm its content'],
        done_when=['The note is readable with its original content', 'No other note was lost'],
        fallback='If the plan would remove notes made after the backup, stop and ask the user; undo an applied plan with CMD undo.'),
}


def terms(variant):
    """Variant-specific marker words and topics, so training and held-out stores differ in more than one string."""
    h = hashlib.sha256(variant.encode()).digest()
    word = lambda chunk: ''.join(chr(97+b % 26) for b in chunk)
    first = h[20] % len(TOPICS)
    return word(h[:9]), word(h[9:18]), [TOPICS[(first+i) % len(TOPICS)] for i in range(4)]


def objective(context):
    s, title = context['scenario'], context['title']
    if s == 'stale-index':
        return (f"The note '{title}' was edited outside the CLI, so the search index no longer matches the canonical notes "
                f"(its new term '{context['fresh']}' is not indexed). Repair retrieval without changing canonical notes.")
    if s == 'stale-plan':
        return (f"Plan {context['plan']['plan_id']} proposes an update to the note '{title}'. Apply it if it is still valid "
                'and keep the store consistent.')
    if s == 'interrupted-operation':
        return 'A Living Memory write was interrupted part-way. Find the operation and complete it as intended.'
    if s == 'restore':
        return (f"The canonical note '{title}' was deleted by mistake. Restore it from the backup "
                f"{Path(context['backup']).name} without losing other notes.")
    return (f"Search for '{context['marker']}' no longer returns the note '{title}'; the search index may be stale. "
            'Make the note findable again without losing other notes.')


def setup(root, scenario, variant, mode='baseline', learned=None):
    """Base notes, then the condition's runbook, then the scenario's damage, so seeding cannot disturb the damage."""
    init_domain(DOMAIN, [str(root.parent)], root)
    (root/'tag_taxonomy.json').write_text(json.dumps({'tags': TAGS}), encoding='utf-8')
    marker, fresh, topics = terms(variant)
    title = topics[0].capitalize()+' notes'
    original = f'{marker} {topics[0]}: original fact recorded for this store.'
    n = capture(title, original, TAGS, DOMAIN, root, allow_similar=True)
    for topic in topics[1:]:
        capture(topic.capitalize()+' notes', f'Unrelated {topic} fact.', TAGS, DOMAIN, root, allow_similar=True)
    path, note = find_note(n['id'], DOMAIN, root)
    context = dict(scenario=scenario, variant=variant, note_id=n['id'], note_path=path.relative_to(root).as_posix(),
                   title=title, marker=marker, fresh=fresh, original=original)
    context['procedure'] = seed(root, scenario, mode, learned)
    if scenario == 'stale-index':
        rebuild_index(DOMAIN, root)
        note.update(body=f'{marker} {fresh} {topics[0]}: corrected fact edited outside the CLI.', updated=repository.now())
        path.write_text(repository.serialize(note), encoding='utf-8')
        context['canonical_hash'] = repository.digest(path.read_bytes())
    elif scenario == 'stale-plan':
        context['plan'] = change(n['id'], DOMAIN, 'update', root, plan_only=True, body=f'{marker} outdated proposal')
        change(n['id'], DOMAIN, 'update', root, body=f'{marker} newer preserved fact')
    elif scenario == 'interrupted-operation':
        original_write, calls = repository._write, [0]
        def crash(p, data):
            calls[0] += 1
            if calls[0] == 1: raise OSError('fixture interruption')
            return original_write(p, data)
        operation = 'learning-eval-'+variant
        with patch.object(repository, '_write', crash):
            try: repository.transaction(root, {'fixture-result.txt': 'recovered'}, 'fixture', operation_id=operation)
            except OSError: pass
        context['operation_id'] = operation
    else:
        context['backup'] = backup.backup(root)['backup']
        # Count the snapshot as today's, so session hooks do not snapshot the damaged store and blur the choice.
        (root/'hooks').mkdir(exist_ok=True)
        (root/'hooks'/'last-backup.json').write_text(json.dumps(
            {'at': datetime.now(timezone.utc).isoformat(), 'backup': Path(context['backup']).name}), encoding='utf-8')
        path.unlink()
    return context


def seed(root, scenario, mode, learned):
    """The condition's runbook: none, the hand-written one, or the admitted learned content for that scenario."""
    if mode == 'baseline':
        return None
    target = LOOKALIKE.get(scenario, scenario)
    if mode == 'manual':
        n = capture(MANUAL[target]['title'], learning.body(MANUAL[target]), TAGS, DOMAIN, root, type='runbook', allow_similar=True)
        return dict(kind='manual', note_id=n['id'])
    content = (learned or {}).get(target)
    if not content:
        return None
    def call(entity, action, ident=None, **p): return learning.dispatch(root, DOMAIN, entity, action, ident, p)
    recorded = call('record', 'create', objective=PURPOSE[target], family='alm.'+target, acceptance_criteria=['repair verified'],
                    outcome='succeeded', evidence=[dict(result='pass', method='tool_check', observer='eval harness verifier',
                    excerpt='The training repair passed the harness check: '+CHECKS[target],
                    limitations='One synthetic training store; no model benefit or generalization established')])
    eid = recorded['evidence'][0]['id']
    c = call('candidate', 'create', content=content, evidence_ids=[eid], step_evidence=[[eid] for _ in content['steps']])['record']
    p = call('candidate', 'promote-plan', c['id'], expected_revision=c['revision'])
    governance.apply(root, p['plan_id'], p['digest'])
    return dict(kind='learned', procedure_id=p['summary']['procedure_id'], version=1)


def reference(root, context):
    """The scripted repair. `actions` lists what it did as CLI steps; the scripted learned procedure is built from them."""
    scenario = context['scenario']; actions = []
    if scenario == 'stale-index':
        rebuild_index(DOMAIN, root); actions.append('CMD rebuild DOMAIN (rebuilds the disposable index only)')
        actions.append("CMD search 'EXPECTED TERM' and confirm the note is returned")
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
    try:
        if scenario == 'stale-index':
            path, _ = find_note(context['note_id'], DOMAIN, root)
            return (repository.digest(path.read_bytes()) == context['canonical_hash']
                    and _fresh(DOMAIN, root, repository.inventory(DOMAIN, root, retired=True))
                    and any(h['id'] == context['note_id'] for h in search_domain(context['fresh'], DOMAIN, root=root)))
        if scenario == 'stale-plan':
            return bool(report.get('refused_stale')) and 'newer preserved fact' in find_note(context['note_id'], DOMAIN, root)[1]['body']
        if scenario == 'interrupted-operation':
            p = root/'fixture-result.txt'
            return p.exists() and p.read_text() == 'recovered' and not repository.unfinished_operations(root)[0]
        return context['original'] in find_note(context['note_id'], DOMAIN, root)[1]['body']
    except (OSError, ValueError, KeyError):
        return False


def note_hashes(root):
    prefix = f'domains/{DOMAIN}/notes/'
    return {rel: repository.digest((root/rel).read_bytes()) for rel in backup.canonical_files(root) if rel.startswith(prefix)}


def real_store_fingerprint():
    """Paths, sizes and mtimes of the configured (real) store's notes and learning records, or None without one."""
    try:
        real = storage._configured()
    except ValueError:
        return None
    if real is None or not (real/'domains').is_dir():
        return None
    h = hashlib.sha256()
    for folder in sorted((real/'domains').glob('*/notes'))+sorted((real/'domains').glob('*/learning')):
        for p in sorted(folder.rglob('*')):
            if p.is_file():
                st = p.stat(); h.update(f'{p}|{st.st_size}|{st.st_mtime_ns}\n'.encode())
    return h.hexdigest()


def request_for(root, context, phase, timeout):
    """The task the agent sees. It names no condition: every condition gets the same task text."""
    return dict(phase=phase, objective=objective(context), root=str(root), domain=DOMAIN, cli=CLI, timeout=timeout,
                report_contract='Work only inside this temporary store.')


def invoke(command, request, cwd, timeout):
    """The agent's report. Its environment points Living Memory at the trial store and is not a child of this session."""
    env = {k: v for k, v in os.environ.items()
           if not (k == 'CLAUDECODE' or k == 'CLAUDE_PID' or k.startswith('CLAUDE_CODE_') or k.startswith('LIVING_MEMORY_'))}
    env.update(PYTHONUTF8='1', LIVING_MEMORY_ROOT=request['root'])
    argv = [shutil.which(command[0]) or command[0]]+command[1:]
    child = subprocess.run(argv, input=json.dumps(request), text=True, encoding='utf-8', capture_output=True,
                           timeout=timeout+60, env=env, cwd=cwd)
    if child.returncode:
        raise ValueError('Agent command failed: '+(child.stderr or child.stdout)[-500:])
    report = json.loads(child.stdout)
    if not isinstance(report, dict):
        raise ValueError('Agent report must be an object')
    return report


def metrics(report):
    return {k: report.get(k) for k in ('input_tokens', 'output_tokens', 'cost_usd', 'turns', 'models')}


def attempt(root, context, phase, command, timeout):
    """(report, checked, error, seconds, real store changed) for one run: the agent's, or the scripted reference."""
    started, error, fingerprint = time.perf_counter(), None, None
    try:
        if command is None:
            report = reference(root, context)
        else:
            fingerprint = real_store_fingerprint()
            report = invoke(command, request_for(root, context, phase, timeout), str(root.parent), timeout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report, error = {}, str(exc)
    seconds = round(time.perf_counter()-started, 4)
    changed = command is not None and fingerprint != real_store_fingerprint()
    return report, (error is None and verify(root, context, report)), error, seconds, changed


def train(scenario, command, timeout, label, keep):
    """One training run: the agent repairs a training store and proposes a procedure (scripted without a command)."""
    temp = Path(tempfile.mkdtemp(prefix='alm-learning-train-'))
    try:
        root = temp/'store'; context = setup(root, scenario, 'training')
        report, checked, error, seconds, changed = attempt(root, context, 'training', command, timeout)
        if command is None:
            content = dict(title='Maintenance procedure '+scenario, body=PURPOSE[scenario], use_when=[PURPOSE[scenario]],
                           do_not_use_when=['Canonical notes themselves are corrupt'], steps=report.get('actions', []),
                           done_when=[CHECKS[scenario]], fallback='Inspect state with doctor; never delete canonical notes')
            evaluation, count = dict(eligible=True, errors=[], warnings=['Scripted from the reference repair']), 0
        else:
            candidates = learning.rows(root, DOMAIN, 'candidates'); count = len(candidates)
            wanted = str(report.get('candidate_id') or '')
            chosen = next((c for c in candidates if len(wanted) >= 8 and c['id'].startswith(wanted)), None) \
                or max(candidates, key=lambda c: c.get('created', ''), default=None)
            content = chosen['content'] if chosen else None
            evaluation = learning.dispatch(root, DOMAIN, 'candidate', 'evaluate', chosen['id']) if chosen else \
                dict(eligible=False, errors=['No candidate was proposed'], warnings=[])
        return dict(phase='training', agent=label, scenario=scenario, checked_success=checked, error=error,
                    reported_outcome=report.get('outcome', 'unknown'), candidates=count, candidate=content,
                    eligible=bool(evaluation['eligible']), evaluation_errors=evaluation['errors'],
                    evaluation_warnings=evaluation['warnings'], admitted=bool(checked and content and evaluation['eligible']),
                    seconds=seconds, real_store_changed=changed, store=str(temp) if keep else None, **metrics(report))
    finally:
        if not keep: shutil.rmtree(temp, ignore_errors=True)


def trial(scenario, mode, repeat, command, learned, timeout, label, keep):
    temp = Path(tempfile.mkdtemp(prefix='alm-learning-eval-'))
    try:
        root = temp/'store'
        context = setup(root, scenario, 'held-out-'+str(repeat), mode, learned)
        before = note_hashes(root)
        report, checked, error, seconds, changed = attempt(root, context, 'test', command, timeout)
        after = note_hashes(root)
        target = context['note_path']
        uses = [dict(procedure_id=u['procedure_id'], usage=u['usage']) for u in learning.rows(root, DOMAIN, 'uses')]
        procedure = context['procedure']
        reported_usage = report.get('usage', 'unknown')
        used = reported_usage in USED or any(u['usage'] in USED for u in uses)
        return dict(phase='test', agent=label, scenario=scenario, mode=mode, repeat=repeat, checked_success=checked,
                    reported_outcome=report.get('outcome', 'unknown'),
                    false_success=report.get('outcome') == 'succeeded' and not checked,
                    procedure_available=procedure is not None,
                    # Following the seeded runbook is fine when it leads to the real fix (its fallback may);
                    # a misapplication is following it and leaving the note unrestored.
                    misapplied=scenario in LOOKALIKE and procedure is not None and used and not checked,
                    reported_usage=reported_usage, recorded_uses=uses,
                    collateral=sorted(rel for rel in before if rel != target and after.get(rel) != before[rel]),
                    new_notes=len(set(after)-set(before)-{target}), seconds=seconds, real_store_changed=changed,
                    error=error, store=str(temp) if keep else None, **metrics(report))
    finally:
        if not keep: shutil.rmtree(temp, ignore_errors=True)


def run(command=None, repeats=1, timeout=600, label='scripted', learned_from=None, scenarios=SCENARIOS, modes=MODES,
        out=None, keep=False):
    learned_from = learned_from or ('agent' if command else 'scripted')
    results = []
    def emit(row):
        results.append(row)
        if out:
            with open(out, 'a', encoding='utf-8') as f: f.write(json.dumps(row)+'\n')
    learned = {}
    if 'learned' in modes:
        for scenario in dict.fromkeys(LOOKALIKE.get(s, s) for s in scenarios):
            row = train(scenario, command if learned_from == 'agent' else None, timeout, label, keep)
            emit(row)
            learned[scenario] = row['candidate'] if row['admitted'] else None
    for repeat in range(repeats):
        for scenario in scenarios:
            order = list(modes)[repeat % len(modes):]+list(modes)[:repeat % len(modes)]   # counterbalance order
            for mode in order:
                emit(trial(scenario, mode, repeat, command, learned, timeout, label, keep))
    return dict(schema=2, kind='agent_comparison' if command else 'fixture_self_test', learned_from=learned_from,
                caveat='Fixture self-tests establish harness correctness, not learning efficacy. Agent-reported costs and '
                       'usage are not independently verified. Counts from small samples; no significance is claimed.',
                results=results)


def _median(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(values), 2) if values else None


def summarize(rows):
    tests = [r for r in rows if r.get('phase', 'test') == 'test']
    groups = defaultdict(list); cells = defaultdict(list)
    for r in tests:
        groups[(r.get('agent', '?'), r['mode'])].append(r)
        cells[(r.get('agent', '?'), r['scenario'], r['mode'])].append(r)
    conditions = []
    for (agent, mode), rs in sorted(groups.items()):
        lookalike = [r for r in rs if r['scenario'] in LOOKALIKE and r.get('procedure_available')]
        tokens = [r['input_tokens']+r['output_tokens'] for r in rs
                  if isinstance(r.get('input_tokens'), int) and isinstance(r.get('output_tokens'), int)]
        costs = [r['cost_usd'] for r in rs if isinstance(r.get('cost_usd'), (int, float))]
        conditions.append(dict(agent=agent, mode=mode, trials=len(rs), checked=sum(r['checked_success'] for r in rs),
                               false_success=sum(bool(r.get('false_success')) for r in rs),
                               misapplied=f"{sum(bool(r.get('misapplied')) for r in lookalike)}/{len(lookalike)}",
                               collateral=sum(bool(r.get('collateral')) for r in rs),
                               procedure_available=sum(bool(r.get('procedure_available')) for r in rs),
                               reported_used=sum(r.get('reported_usage') in USED for r in rs),
                               recorded_use=sum(bool(r.get('recorded_uses')) for r in rs),
                               errors=sum(bool(r.get('error')) for r in rs),
                               median_seconds=_median([r['seconds'] for r in rs]), median_tokens=_median(tokens),
                               total_cost_usd=round(sum(costs), 4) if costs else None,
                               real_store_changed=sum(bool(r.get('real_store_changed')) for r in rs)))
    scenarios = [dict(agent=a, scenario=s, mode=m, checked=f"{sum(r['checked_success'] for r in rs)}/{len(rs)}")
                 for (a, s, m), rs in sorted(cells.items())]
    training = [{k: r.get(k) for k in ('agent', 'scenario', 'checked_success', 'candidates', 'eligible', 'admitted',
                                       'evaluation_errors', 'seconds', 'cost_usd')}
                for r in rows if r.get('phase') == 'training']
    return dict(conditions=conditions, scenarios=scenarios, training=training)


def markdown(summary):
    def table(rows):
        if not rows: return '(none)\n'
        keys = list(rows[0])
        lines = ['| '+' | '.join(keys)+' |', '|'+'---|'*len(keys)]
        lines += ['| '+' | '.join('' if r[k] is None else str(r[k]) for k in keys)+' |' for r in rows]
        return '\n'.join(lines)+'\n'
    by_cell = defaultdict(dict)
    for r in summary['scenarios']:
        by_cell[(r['agent'], r['scenario'])][r['mode']] = r['checked']
    grid = [dict(agent=a, scenario=s, **{m: modes.get(m, '') for m in MODES}) for (a, s), modes in sorted(by_cell.items())]
    return ('### Conditions\n\n'+table(summary['conditions'])+'\n### Checked success by scenario\n\n'+table(grid)
            +'\n### Training runs\n\n'+table(summary['training']))


def load(paths):
    rows = []
    for p in paths:
        text = Path(p).read_text(encoding='utf-8').strip()
        if text.startswith('{') and '\n{' not in text:
            value = json.loads(text); rows += value['results'] if 'results' in value else [value]
        else:
            rows += [json.loads(line) for line in text.splitlines() if line.strip()]
    return rows


def choices(value, allowed, name):
    picked = [v.strip() for v in value.split(',') if v.strip()]
    unknown = [v for v in picked if v not in allowed]
    if unknown or not picked:
        raise argparse.ArgumentTypeError(f'{name} must be a comma list of: '+', '.join(allowed))
    return tuple(picked)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--self-test', action='store_true')
    group.add_argument('--command-json', help='Agent argv as a JSON array; one JSON task on stdin, one JSON report on stdout')
    group.add_argument('--summarize', nargs='+', metavar='FILE', help='Summarize result files (JSON or JSON lines); runs nothing')
    parser.add_argument('--markdown', action='store_true', help='With --summarize: Markdown tables instead of JSON')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--timeout', type=int, default=600, help='Seconds per agent run (default 600)')
    parser.add_argument('--label', help='Agent label recorded on every result (default: scripted, or agent with a command)')
    parser.add_argument('--learned-from', choices=['agent', 'scripted'],
                        help='Source of the learned runbook: the agent\'s own training run (default with a command) or the reference repair')
    parser.add_argument('--scenarios', type=lambda v: choices(v, SCENARIOS, '--scenarios'), default=SCENARIOS)
    parser.add_argument('--modes', type=lambda v: choices(v, MODES, '--modes'), default=MODES)
    parser.add_argument('--out', help='Append one JSON line per run to this file as it finishes')
    parser.add_argument('--keep', action='store_true', help='Keep each trial folder for inspection (debugging)')
    args = parser.parse_args()
    if args.summarize:
        summary = summarize(load(args.summarize))
        print(markdown(summary) if args.markdown else json.dumps(summary, indent=2))
        sys.exit(0)
    command = json.loads(args.command_json) if args.command_json else None
    if command is not None and (not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command)):
        parser.error('--command-json must be a nonempty argv array')
    if command is not None:
        # Agents run in their trial folder, so a script path relative to here must become absolute.
        command = [str(Path(x).resolve()) if not Path(x).is_absolute() and Path(x).is_file() else x for x in command]
    if not 1 <= args.repeats <= 10: parser.error('--repeats must be between 1 and 10')
    if args.learned_from == 'agent' and command is None: parser.error('--learned-from agent needs --command-json')
    result = run(command, args.repeats, args.timeout, args.label or ('agent' if command else 'scripted'), args.learned_from,
                 args.scenarios, args.modes, args.out, args.keep)
    print(json.dumps(result, indent=2))
    sys.exit(0 if all(r['checked_success'] for r in result['results']) else 1)
