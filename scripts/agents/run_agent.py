"""Adapt a command-line agent to the eval_learning.py trial contract.

eval_learning.py writes one JSON task to stdin and expects one JSON report on stdout. This adapter turns the task
into a prompt, runs the agent named by an argv template ({prompt} is replaced, nothing goes through a shell), and
returns the JSON object the agent printed last, plus token counts when the agent reports them.

    py -3.15 scripts/eval_learning.py --repeats 3 --command-json "[\"py\", \"-3.15\", \"scripts/agents/run_agent.py\",
        \"--\", \"claude\", \"-p\", \"{prompt}\", \"--output-format\", \"json\", \"--allowedTools\", \"Bash\"]"

    ... "--", "codex", "exec", "--full-auto", "{prompt}"]"

The agent works in a throwaway store; grant it only the tools that store needs. Reported usage and tokens are
the agent's own claims and are not independently verified.
"""
import json
import subprocess
import sys

PROMPT = """You are repairing an Agentic Living Memory store in a throwaway test directory.
Task: {objective}
Store root: {root}   Domain: {domain}
Run the memory CLI as: "{python}" "{cli}" --root "{root}" --domain {domain} COMMAND ...
Work only inside that store. Do not edit its files directly; use the CLI.
{procedure}
When finished, print exactly one JSON object as the last thing you output:
{{"outcome": "succeeded|failed|unknown", "refused_stale": true|false, "usage": "used|ignored|inapplicable|none"}}
{contract}"""


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


def main(argv):
    if '--' not in argv or argv.index('--') == len(argv)-1:
        print('usage: run_agent.py -- AGENT ARGV with {prompt}', file=sys.stderr)
        return 2
    template = argv[argv.index('--')+1:]
    if not any('{prompt}' in part for part in template):
        print('The agent argv needs a {prompt} placeholder', file=sys.stderr)
        return 2
    task = json.loads(sys.stdin.read())
    hint = ''
    if task.get('procedure'):
        hint = ('A Living Memory note may describe this repair; search the store for it before acting. '
                'If you use a learned procedure, its hint says how to report use.')
    prompt = PROMPT.format(objective=task['objective'], root=task['root'], domain=task['domain'], python=sys.executable,
                           cli=task['cli'], procedure=hint, contract=task.get('report_contract', ''))
    child = subprocess.run([part.replace('{prompt}', prompt) for part in template], text=True, encoding='utf-8',
                           capture_output=True)
    if child.returncode:
        print(child.stderr[-2000:], file=sys.stderr)
        return child.returncode
    output = child.stdout
    tokens = {}
    try:
        wrapper = json.loads(output)
    except ValueError:
        wrapper = None
    if isinstance(wrapper, dict) and isinstance(wrapper.get('result'), str):
        # claude -p --output-format json wraps the final message and reports usage.
        usage = wrapper.get('usage') or {}
        tokens = {k: usage[k] for k in ('input_tokens', 'output_tokens') if isinstance(usage.get(k), int)}
        output = wrapper['result']
    report = last_json(output)
    if report is None:
        print('The agent printed no JSON report', file=sys.stderr)
        return 1
    report.update({k: v for k, v in tokens.items() if k not in report})
    print(json.dumps(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
