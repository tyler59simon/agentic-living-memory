"""Test support: run the CLI the way an agent does, reading each note's revision just before writing it.

Since 2026-11-26 every CLI write to an existing note needs the revision the caller read (--expected-revision, or REF@REVISION
for the other notes a command names). Tests about other behaviour call `main` from here: it fills in a missing revision by
running `read REF --metadata-only` as a viewer (no usage rows) with the same root, domain and cwd, so the write sees the note
as the agent just read it. A revision the test passes itself is kept, so stale and missing revisions are still tested
directly against `core.cli.main` (tests/test_revisions.py)."""
import contextlib,io,json
from core.cli import build_parser,main as cli_main
from core.notes import split_revision

WRITES={'update','archive','restore','resolve','reopen','confirm','check','supersede','delete'}


def _read(base,ref):
    out=io.StringIO()
    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()):
        try:code=cli_main([*base,'--actor','viewer','--json','read',ref,'--metadata-only'])
        except SystemExit as exc:code=exc.code
    if code!=0:return None
    try:return json.loads(out.getvalue()).get('revision')
    except ValueError:return None


def _with_at(base,value):
    """REF@REVISION for a plain REF, else the value unchanged."""
    ref,revision=split_revision(value)
    if revision:return value
    current=_read(base,ref)
    return f'{ref}@{current}' if current else value


def _replace_flag(argv,flags,base):
    out=list(argv);i=0
    while i<len(out):
        token=out[i]
        if token in flags and i+1<len(out):out[i+1]=_with_at(base,out[i+1]);i+=2;continue
        flag,eq,value=token.partition('=')
        if eq and flag in flags:out[i]=flag+'='+_with_at(base,value)
        i+=1
    return out


def with_revisions(argv):
    """argv with the revisions an agent would have read just before running it."""
    argv=[str(a) for a in argv]
    try:args,_=build_parser().parse_known_args(argv)
    except BaseException:return argv
    base=['--root',str(args.root)] if args.root else []
    if args.domain:base+=['--domain',args.domain]
    if args.cwd:base+=['--cwd',args.cwd]
    cmd=args.command
    if cmd in WRITES and not getattr(args,'expected_revision',None) and getattr(args,'reference',None):
        current=_read(base,args.reference)
        if current:argv=argv+['--expected-revision',current]
    if cmd=='supersede':argv=_replace_flag(argv,{'--by'},base)
    if cmd in {'capture','propose'}:argv=_replace_flag(argv,{'--supersedes'},base)
    if cmd=='update':argv=_replace_flag(argv,{'--distinct','--not-distinct'},base)
    if cmd=='promote' and not getattr(args,'suggest',False):
        at=argv.index('promote');argv=argv[:at+1]+[_with_at(base,a) if a in args.refs else a for a in argv[at+1:]]
    return argv


def main(argv=None):
    return cli_main(with_revisions(argv) if argv is not None else argv)
