"""Drive headless agents through the ingest-scan worklist to write digest notes (project knowledge base, layer 2).

Living Memory never calls a model; this script does the calling, one batch of files per agent run, and the agent writes
its digests through the normal LM CLI (`capture ... --source PATH`, so each digest records the file's hash and reads STALE
when the file changes). Only new and stale files are sent (--all adds current and skipped ones); a file the agent skips as trivial is
remembered (ingest-skip, a sidecar in the domain folder) until its content changes. Reruns are idempotent: a digest's capture key is digest:<path>:<sha256>, so a file whose content
was already digested is left alone, and a changed file's new digest replaces the old one.

    py -3.15 scripts/ingest_run.py --domain demo --batch 8 --max-batches 5 --budget-usd 3 [--all] [--dry-run]
    ... --command-json '["claude","-p","{prompt}","--model","{model}","--output-format","json","--allowedTools","Read","Bash"]'

The agent argv template takes {prompt} and {model}; without {prompt} the prompt goes to the agent's stdin. No shell is used.
The default template is `claude -p {prompt} --model MODEL --output-format json --permission-mode acceptEdits --allowedTools ...`
(files to read, the LM CLI to run); on Windows a .cmd shim gets the prompt on stdin instead, as it cannot carry a multi-line argument.
Each agent ends with one JSON object, {"digested": [paths], "skipped": [{"path", "reason"}], "failed": [{"path", "reason"}]}
("failed" is a file whose capture was refused or errored; it is never remembered as a skip, and neither is any skip of a batch in which Claude reported
permission denials); cost is read from the agent's own JSON
(total_cost_usd or cost_usd) and is the agent's claim. A run log (JSON lines) goes to the store's logs/ folder, or --log. The summary
on stdout also counts how many of the batch files really have a current digest afterwards (a fresh ingest-scan, not the agents' word).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / 'living_memory.py'
DEFAULT_COMMAND = ['claude', '-p', '{prompt}', '--model', '{model}', '--output-format', 'json', '--permission-mode', 'acceptEdits']
TYPES = 'decision for decision records (ADRs), runbook for operational docs (deploy, on-call, setup), knowledge for everything else'

PROMPT = """You are writing digest notes for a project knowledge base kept in Living Memory (LM). Use only the LM CLI for notes: {cmd} SUBCOMMAND ...
Read each of these {count} files (the location after "read:"), then write ONE digest note per file:
{cmd} capture --type TYPE --title '<path>: <what it is>' --summary '<1-2 sentences>' --detail '**Label:** load-bearing fact (path:line)' ... --source '<path>' --file '<path>'
- TYPE: {types}. Subject and concern tags are inferred.
- 300-1,200 characters of load-bearing facts in all: what the file is, its key rules, decisions, constraints and gotchas, and how it connects to the rest. Cite lines as path:line. Do not copy code or long passages; never put a secret in a note.
- Give --source the path exactly as listed; add --file '<path>' unless the file is marked (no --file).
- Skip a trivial file (empty, boilerplate, generated, a few lines): capture nothing, give a one-line reason. "skipped" is only for trivial files.
- If a capture of a file fails (the command is refused or errors, or a permission prompt blocks it), do not retry it in another way and never report it as skipped: list it under "failed" with the reason.
- A file marked "stale" has an old digest: capture the new one anyway and it replaces the old one. If a capture is refused as a possible duplicate, read the note it names and update it instead.
Files:
{files}
After the files: if this batch changed what the project brief or the codebase map should say, update them (find them with {cmd} list --type brief and {cmd} list --type map, then {cmd} update REF --set-detail '**Label:** ...'); otherwise leave them alone.
Finish with exactly one JSON object as the last thing you print: {{"digested": ["path", ...], "skipped": [{{"path": "...", "reason": "..."}}], "failed": [{{"path": "...", "reason": "..."}}]}}"""


def lm(args, *argv, check=True, stdin=None):
    """Run the LM CLI; its JSON reply."""
    cmd = [sys.executable, str(args.cli), '--root', str(args.root), '--cwd', args.cwd] + (['--domain', args.domain] if args.domain else []) + list(argv)
    done = subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding='utf-8', env=dict(os.environ, PYTHONUTF8='1'), cwd=args.cwd)
    if done.returncode and check:
        raise SystemExit(f'living_memory {" ".join(argv[:2])} failed: {(done.stderr or done.stdout).strip()[:500]}')
    return json.loads(done.stdout) if done.stdout.strip().startswith('{') else {}


def command_line(args):
    """The LM CLI as the agent should type it, quoted for a shell-less reader."""
    return f'"{sys.executable}" "{args.cli}" --root "{args.root}"' + (f' --domain {args.domain}' if args.domain else '') + f' --cwd "{args.cwd}"'


def prompt_for(args, items):
    lines = []
    for number, item in enumerate(items, 1):
        notes = f"{item['kind']}, {item['size']} bytes, {item['status']}" + ('' if item.get('file') else ', no --file')
        lines.append(f"{number}. {json.dumps(item['path'])} ({notes}) read: {item['abs']}")
    return PROMPT.format(cmd=command_line(args), count=len(items), types=TYPES, files='\n'.join(lines))


def agent_argv(args, prompt):
    """(argv, stdin text or None): the template with {prompt} and {model} filled in; no {prompt} (or a Windows .cmd shim) sends the prompt on stdin."""
    template = json.loads(args.command_json) if args.command_json else DEFAULT_COMMAND + ['--allowedTools'] + args.allowed_tools
    argv = [part.replace('{model}', args.model) for part in template]
    exe = shutil.which(argv[0])
    argv[0] = exe or argv[0]
    shim = os.name == 'nt' and argv[0].lower().endswith(('.cmd', '.bat'))
    if any('{prompt}' in part for part in argv) and not shim:
        return [part.replace('{prompt}', prompt) for part in argv], None
    return [part for part in argv if '{prompt}' not in part], prompt


def last_json(text, keys=()):
    """The last top-level JSON object in text (that has one of keys, when given), else None."""
    decoder, found, index = json.JSONDecoder(), None, 0
    while True:
        index = text.find('{', index)
        if index < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        if isinstance(value, dict) and (not keys or set(keys) & set(value)):
            found = value
        index = end


def read_report(stdout):
    """(cost, digested paths, skipped rows, failed rows, permission denials) from an agent's stdout: Claude's `--output-format json` wraps the agent's last message in
    {"result": "...", "total_cost_usd": ...}; a plain agent prints the report itself."""
    outer = last_json(stdout) or {}
    inner = last_json(str(outer.get('result') or ''), ('digested', 'skipped', 'failed')) if isinstance(outer.get('result'), str) else None
    report = inner or (outer if {'digested', 'skipped', 'failed'} & set(outer) else {})
    cost = next((v for v in (outer.get('total_cost_usd'), outer.get('cost_usd'), report.get('cost_usd')) if isinstance(v, (int, float))), 0.0)
    digested = [p for p in report.get('digested') or [] if isinstance(p, str)]
    skipped = [s for s in report.get('skipped') or [] if isinstance(s, dict) and isinstance(s.get('path'), str)]
    failed = [s for s in report.get('failed') or [] if isinstance(s, dict) and isinstance(s.get('path'), str)]
    denials = outer.get('permission_denials')
    return float(cost), digested, skipped, failed, len(denials) if isinstance(denials, list) else 0


def run_batch(args, number, items):
    prompt = prompt_for(args, items)
    argv, stdin = agent_argv(args, prompt)
    started = time.perf_counter()
    row = {'batch': number, 'paths': [i['path'] for i in items], 'cost_usd': 0.0, 'digested': [], 'skipped': [], 'failed': [], 'permission_denials': 0}
    try:
        done = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding='utf-8', timeout=args.timeout, cwd=args.cwd, env=dict(os.environ, PYTHONUTF8='1', LIVING_MEMORY_HEADLESS='1'))
        row.update(returncode=done.returncode, stderr=done.stderr[-500:])
        if done.returncode == 0:
            row['cost_usd'], row['digested'], row['skipped'], row['failed'], row['permission_denials'] = read_report(done.stdout)
            if row['permission_denials']:   # a blocked capture may have been reported as a skip: remember none of this batch's skips
                row['failed'], row['skipped'] = row['failed'] + row['skipped'], []
    except (OSError, subprocess.TimeoutExpired) as exc:
        row.update(returncode=-1, stderr=f'{type(exc).__name__}: {exc}')
    row['seconds'] = round(time.perf_counter() - started, 2)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, help="Memory store (default: the configured store, as the CLI resolves it)")
    parser.add_argument('--domain')
    parser.add_argument('--cwd', default=os.getcwd(), help='Project folder the agents work in (default: here)')
    parser.add_argument('--source', help='One knowledge-base source (a `sources` name)')
    parser.add_argument('--batch', type=int, default=8, help='Files per agent run (default 8)')
    parser.add_argument('--max-batches', type=int, help='Stop after this many batches')
    parser.add_argument('--budget-usd', type=float, help='Stop starting batches once the reported costs add up to this')
    parser.add_argument('--parallel', type=int, default=1, help='Agents at once (default 1: memory is limited)')
    parser.add_argument('--all', action='store_true', help='Also send files that are current or were skipped (default: only new and stale ones)')
    parser.add_argument('--changed-only', action='store_true', help=argparse.SUPPRESS)   # the default now; accepted for compatibility
    parser.add_argument('--model', default='sonnet')
    parser.add_argument('--command-json', help='Agent argv as a JSON array, with {prompt} and {model} placeholders')
    parser.add_argument('--allowed-tools', nargs='*', help='Tools for the default agent command (default: Read Glob Grep and this LM CLI)')
    parser.add_argument('--timeout', type=int, default=1800, help='Seconds per batch (default 1800)')
    parser.add_argument('--log', type=Path, help="Run log (JSON lines; default: the store's logs/ folder)")
    parser.add_argument('--cli', type=Path, default=CLI, help='living_memory.py to use (default: this repository\'s)')
    parser.add_argument('--dry-run', action='store_true', help='Print the batches and their prompts; run no agent')
    args = parser.parse_args(argv)
    if args.command_json:
        template = json.loads(args.command_json)
        if not isinstance(template, list) or not template or any(not isinstance(p, str) for p in template):
            parser.error('--command-json must be a nonempty argv array')
    if args.batch < 1 or args.parallel < 1 or (args.max_batches is not None and args.max_batches < 1):
        parser.error('--batch, --parallel and --max-batches must be at least 1')
    if args.root is None:
        sys.path.insert(0, str(REPO))
        from core.storage import resolve_root
        args.root = resolve_root(None)[0]
    args.root, args.cwd = Path(args.root).resolve(), str(Path(args.cwd).resolve())
    if args.allowed_tools is None:
        args.allowed_tools = ['Read', 'Glob', 'Grep', f'Bash({sys.executable} {args.cli}:*)']
    scan = lm(args, 'ingest-scan', '--limit', '1000000', *(['--source', args.source] if args.source else []), *([] if args.all else ['--changed']))
    items = [i for i in scan['items'] if args.all or i['status'] in ('new', 'stale')]
    batches = [items[i:i + args.batch] for i in range(0, len(items), args.batch)][:args.max_batches]
    if args.dry_run:
        print(json.dumps({'ok': True, 'dry_run': True, 'domain': scan['domain'], 'files': sum(map(len, batches)), 'counts': scan['counts'],
                          'batches': [{'batch': n, 'paths': [i['path'] for i in b], 'prompt': prompt_for(args, b)} for n, b in enumerate(batches, 1)]}, indent=1))
        return 0
    log = args.log or args.root / 'logs' / f"ingest-run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    spent, rows, failed = 0.0, [], 0
    with open(log, 'a', encoding='utf-8') as out, ThreadPoolExecutor(args.parallel) as pool:
        for start in range(0, len(batches), args.parallel):
            if args.budget_usd is not None and spent >= args.budget_usd:
                print(f'budget reached (${spent:.2f} of ${args.budget_usd:.2f}); stopping', file=sys.stderr)
                break
            wave = [(n, batches[n - 1]) for n in range(start + 1, min(start + args.parallel, len(batches)) + 1)]
            for row in pool.map(lambda w: run_batch(args, *w), wave):
                spent += row['cost_usd']
                failed += bool(row['returncode'])
                rows.append(row)
                if row['skipped']:   # remembered at the files' present hashes, so they are not offered again until they change
                    lm(args, 'ingest-skip', stdin=json.dumps({'skipped': row['skipped']}), check=False)
                out.write(json.dumps(row, ensure_ascii=False) + '\n')
                out.flush()
                print(f"batch {row['batch']}: {len(row['digested'])} digested, {len(row['skipped'])} skipped, {len(row['failed'])} failed, ${row['cost_usd']:.2f}"
                      + (f", {row['permission_denials']} permission denials" if row['permission_denials'] else '') + (f", agent exit {row['returncode']}" if row['returncode'] else ''), file=sys.stderr)
        ran = {p for r in rows for p in r['paths']}
        after = {i['path']: i['status'] for i in lm(args, 'ingest-scan', '--limit', '1000000', *(['--source', args.source] if args.source else []))['items']} if rows else {}
        summary = {'ok': not failed, 'domain': scan['domain'], 'batches_run': len(rows), 'batches_planned': len(batches), 'files_digested': len({p for r in rows for p in r['digested']}),
                   'files_skipped': len({s['path'] for r in rows for s in r['skipped']}), 'files_failed': len({s['path'] for r in rows for s in r['failed']}), 'cost_usd': round(spent, 4),
                   'verified': {'current': sum(after.get(p) == 'current' for p in ran), 'skipped': sum(after.get(p) == 'skipped' for p in ran), 'still_open': sum(after.get(p) in ('new', 'stale') for p in ran)},
                   'batches_failed': failed, 'log': str(log)}
        out.write(json.dumps({'summary': summary}) + '\n')
    print(json.dumps(summary, indent=1))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
