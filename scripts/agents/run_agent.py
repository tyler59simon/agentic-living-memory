"""Adapt a command-line agent to the eval_learning.py trial contract.

eval_learning.py writes one JSON task to stdin and expects one JSON report on stdout. This adapter turns the task
into a prompt, runs the agent named by an argv template and returns the JSON object the agent printed last, plus
the token, cost and turn counts the agent reports. With a {prompt} placeholder the prompt replaces it; without one
the prompt goes to the agent's stdin, which survives Windows .cmd shims (nothing goes through a shell).

    py -3.15 scripts/eval_learning.py --repeats 3 --label claude-haiku --command-json "[\"py\", \"-3.15\",
        \"scripts/agents/run_agent.py\", \"--\", \"claude\", \"-p\", \"--model\", \"claude-haiku-5-5\",
        \"--output-format\", \"json\", \"--allowedTools\", \"Bash PowerShell Read Grep Glob\"]"

    ... "--", "codex", "exec", "-m", "MODEL", "-c", "model_reasoning_effort=low", "-s", "workspace-write",
        "--skip-git-repo-check", "--ephemeral", "--json", "-"]"

Every condition gets the same prompt; the agent's own Living Memory hooks, pointed at the trial store by
eval_learning.py, supply whatever notes the store holds. Reported usage and tokens are the agent's own claims and
are not independently verified.
"""
import json
import os
import shutil
import subprocess
import sys

PROMPT = """You are working on an Agentic Living Memory store in a throwaway test directory.
Task: {objective}
Store root: {root}   Domain: {domain}
Run the memory CLI as: "{python}" "{cli}" --root "{root}" --domain {domain} COMMAND ...
(In PowerShell, start that line with "& ".)
Work only inside that store. Do not edit its files directly; use the CLI.
The store's Living Memory may hold notes relevant to this task; your session context and the CLI's search show them.
{training}
When finished, print exactly one JSON object as the last thing you output:
{{"outcome": "succeeded|failed|unknown", "refused_stale": true|false, "usage": "used|partially_used|ignored|inapplicable|none"{candidate}}}
"usage" says whether you followed a Living Memory runbook for this task ("none" if you saw none).
{contract}"""

TRAINING = ('After you have checked the repair, record it for learning: run the CLI\'s "learn record" with the checks you '
            'ran ("learn" alone prints the guide), then propose a procedure with "learn candidate create" citing the '
            'evidence IDs it returned. Write the procedure so another agent could repeat this repair on a similar store.')


def prompt(task):
    training = task.get('phase') == 'training'
    return PROMPT.format(objective=task['objective'], root=task['root'], domain=task['domain'], python=sys.executable,
                         cli=task['cli'], training=TRAINING if training else '',
                         candidate=', "candidate_id": "ID"' if training else '', contract=task.get('report_contract', ''))


def last_json(text):
    """The last top-level JSON object in text (objects nested inside it are skipped), or None."""
    decoder = json.JSONDecoder()
    found, index = None, 0
    while True:
        index = text.find('{', index)
        if index < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        if isinstance(value, dict):
            found = value
        index = end


def parse(output):
    """(final message, metrics) from claude --output-format json, codex exec --json JSONL, or plain text."""
    try:
        wrapper = json.loads(output)
    except ValueError:
        wrapper = None
    if isinstance(wrapper, dict) and isinstance(wrapper.get('result'), str):
        usage = wrapper.get('usage') or {}
        metrics = {}
        # Input counts cache reads and writes too, so it compares with Codex's input (which includes cached input).
        parts = [usage.get(k) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
        if isinstance(parts[0], int):
            metrics['input_tokens'] = sum(p for p in parts if isinstance(p, int))
        if isinstance(usage.get('output_tokens'), int):
            metrics['output_tokens'] = usage['output_tokens']
        if isinstance(wrapper.get('total_cost_usd'), (int, float)):
            metrics['cost_usd'] = wrapper['total_cost_usd']
        if isinstance(wrapper.get('num_turns'), int):
            metrics['turns'] = wrapper['num_turns']
        if isinstance(wrapper.get('modelUsage'), dict):
            metrics['models'] = sorted(wrapper['modelUsage'])
        return wrapper['result'], metrics
    texts, usage = [], None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get('item') if isinstance(event.get('item'), dict) else {}
        if event.get('type') == 'item.completed' and item.get('type') == 'agent_message' and isinstance(item.get('text'), str):
            texts.append(item['text'])
        elif event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
            usage = usage or {'input_tokens': 0, 'output_tokens': 0}
            for k in usage:
                if isinstance(event['usage'].get(k), int):
                    usage[k] += event['usage'][k]
    if texts:
        return texts[-1], usage or {}
    return output, {}


def run(template, text, timeout=None):
    """(returncode, stdout, stderr). On timeout the whole process tree is killed (a .cmd shim leaves node behind)."""
    argv = [part.replace('{prompt}', text) for part in template]
    argv[0] = shutil.which(argv[0]) or argv[0]
    stdin = None if any('{prompt}' in part for part in template) else text
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8', env=dict(os.environ, LIVING_MEMORY_HEADLESS='1'))
    try:
        out, err = child.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(child.pid)], capture_output=True)
        else:
            child.kill()
        child.communicate()
        return 124, '', f'Agent timed out after {timeout} seconds'
    return child.returncode, out, err


def main(argv):
    if '--' not in argv or argv.index('--') == len(argv)-1:
        print('usage: run_agent.py -- AGENT ARGV [{prompt}]', file=sys.stderr)
        return 2
    template = argv[argv.index('--')+1:]
    task = json.loads(sys.stdin.read())
    code, output, err = run(template, prompt(task), task.get('timeout'))
    if code:
        print(err[-2000:] or output[-2000:], file=sys.stderr)
        return code
    text, metrics = parse(output)
    report = last_json(text)
    if report is None:
        print('The agent printed no JSON report', file=sys.stderr)
        return 1
    report.update({k: v for k, v in metrics.items() if k not in report})
    print(json.dumps(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
