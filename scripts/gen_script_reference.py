"""Keep the flag table in SCRIPT_REFERENCE.md in step with the argparse parser of core/cli.py.

    py -3.15 scripts/gen_script_reference.py          # rewrite the table between the markers
    py -3.15 scripts/gen_script_reference.py --check  # exit 1 and list missing flags when the file is out of date

One row per command (alphabetical), listing the flags that command defines itself, sorted. The options every command accepts
(`--root`, `--domain`, `--cwd`, `--json`, `--actor`, `-h`) are described in the prose of the document, and flags hidden from
`--help` (legacy `--tags` and `--prefix` on capture and propose, `--scheduled`) are left out on purpose. A command whose first
positional chooses an action shows the choices after its name, for example `project create/update/archive`. Only the text between
`<!-- flags:begin -->` and `<!-- flags:end -->` is replaced; the rest of the document is hand-written.
"""
from __future__ import annotations
import argparse,re,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
DOC=ROOT/'SCRIPT_REFERENCE.md'
BEGIN='<!-- flags:begin -->'
END='<!-- flags:end -->'
COMMON={'-h','--help','--root','--domain','--cwd','--json','--actor'}
COMMAND='py -3.15 scripts/gen_script_reference.py'

def _rows(parser,path=()):
    """Yield (command words, action choices, flags) for every subcommand, depth first."""
    for action in parser._actions:
        if not isinstance(action,argparse._SubParsersAction):continue
        for name,sub in action.choices.items():
            flags=sorted({o for a in sub._actions if a.help!=argparse.SUPPRESS for o in a.option_strings if o not in COMMON})
            first=next((a for a in sub._actions if not a.option_strings and a.choices and not isinstance(a,argparse._SubParsersAction)),None)
            yield path+(name,),(list(first.choices) if first is not None and first.dest=='action' else []),flags
            yield from _rows(sub,path+(name,))

def render_table(parser=None):
    """The Markdown table, without the markers."""
    if parser is None:
        from core.cli import build_parser
        parser=build_parser()
    rows=sorted(_rows(parser))
    lines=['| Command | Flags of its own |','| --- | --- |']
    for words,actions,flags in rows:
        label=' '.join(words)+(' '+'/'.join(actions) if actions else '')
        lines.append(f'| `{label}` | '+(', '.join(f'`{f}`' for f in flags) if flags else 'none')+' |')
    return '\n'.join(lines)

def block_of(text):
    """The text between the markers (without them), or None when a marker is missing."""
    match=re.search(re.escape(BEGIN)+r'\n(.*?)\n'+re.escape(END),text.replace('\r\n','\n'),re.S)
    return match.group(1) if match else None

def documented(table):
    """The set of 'command --flag' pairs a table lists."""
    pairs=set()
    for line in table.splitlines():
        cells=[c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells)!=2 or not cells[0].startswith('`'):continue
        command=' '.join(w for w in cells[0].strip('`').split(' ') if '/' not in w)
        pairs.update(f'{command} {flag}' for flag in re.findall(r'`(-[^`]+)`',cells[1]))
    return pairs

def update(text,table):
    """text with the block replaced by table, keeping the file's line endings; raises ValueError without markers."""
    newline='\r\n' if '\r\n' in text else '\n'
    plain=text.replace('\r\n','\n')
    if BEGIN not in plain or END not in plain:raise ValueError(f'SCRIPT_REFERENCE.md needs the markers {BEGIN} and {END}')
    head,rest=plain.split(BEGIN,1);_,tail=rest.split(END,1)
    return (head+BEGIN+'\n'+table+'\n'+END+tail).replace('\n',newline)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--check',action='store_true',help='Only report: exit 1 when the table is out of date')
    args=ap.parse_args(argv)
    text=DOC.read_bytes().decode('utf-8')
    table=render_table()
    if args.check:
        current=block_of(text)
        if current==table:print('SCRIPT_REFERENCE.md flag table is current');return 0
        missing=sorted(documented(table)-documented(current or ''))
        print('SCRIPT_REFERENCE.md flag table is out of date; missing: '+(', '.join(missing) or '(rows differ)')+'\nrun: '+COMMAND);return 1
    new=update(text,table)
    if new!=text:DOC.write_bytes(new.encode('utf-8'));print('SCRIPT_REFERENCE.md flag table rewritten')
    else:print('SCRIPT_REFERENCE.md flag table already current')
    return 0

if __name__=='__main__':raise SystemExit(main())
