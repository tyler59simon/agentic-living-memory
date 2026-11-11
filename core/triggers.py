"""Path triggers: which edited files record a decision (ADRs, changelogs, boards, tickets), and which files a
repo's git status says changed during a turn. Paths only; file content is read by the caller, never here."""
from __future__ import annotations
import functools,re,subprocess
from pathlib import Path

DEFAULT_INCLUDE=('**/adr/**','**/adrs/**','**/decisions/**','docs/**/*.md','CHANGELOG*','**/board/**','**/tickets/**','**/handoff/**')
DEFAULT_EXCLUDE=('**/standup/**',)
GIT_TIMEOUT=2

@functools.lru_cache(maxsize=256)
def glob_regex(pattern):
    """A glob as a regex: ** is any number of folders, * and ? stay inside one path segment. Case-insensitive.
    (Python 3.10 has no PurePath.full_match.)"""
    parts=str(pattern).strip().replace('\\','/').strip('/').split('/');out=[]
    for i,part in enumerate(parts):
        last=i==len(parts)-1
        if part=='**':out.append('.*' if last else '(?:[^/]+/)*')
        else:out.append(''.join('[^/]*' if c=='*' else '[^/]' if c=='?' else re.escape(c) for c in part)+('' if last else '/'))
    return re.compile(''.join(out),re.I)

def matches(path,patterns):
    """True when a glob matches the path or any trailing part of it, so docs/** also finds repo/docs/x.md."""
    parts=str(path).replace('\\','/').strip('/').split('/')
    rx=[glob_regex(p) for p in patterns]
    return any(r.fullmatch('/'.join(parts[i:])) for i in range(len(parts)) for r in rx)

def _globs(value,default):
    return tuple(x for x in value if isinstance(x,str) and x.strip()) if isinstance(value,list) else default

def decision_docs(paths,bindings):
    """The paths (as shown to notes) that are decision documents. bindings.json may set a top-level
    "decision_docs": {"include": [...], "exclude": [...]} to replace either default list."""
    config=(bindings or {}).get('decision_docs')
    config=config if isinstance(config,dict) else {}
    include=_globs(config.get('include'),DEFAULT_INCLUDE);exclude=_globs(config.get('exclude'),DEFAULT_EXCLUDE)
    return [p for p in dict.fromkeys(paths) if matches(p,include) and not matches(p,exclude)]

def changed_since(repo,since):
    """Existing files in the repo's `git status` whose mtime is after `since` (epoch seconds). Never raises."""
    try:
        done=subprocess.run(['git','-c','core.quotepath=off','-C',str(repo),'status','--porcelain','-uall'],capture_output=True,timeout=GIT_TIMEOUT)
        if done.returncode:return []
        found=[]
        for line in done.stdout.decode('utf-8','replace').splitlines():
            if len(line)<4 or 'D' in line[:2]:continue
            path=Path(repo)/line[3:].split(' -> ')[-1].strip().strip('"')
            try:
                if path.is_file() and path.stat().st_mtime>since:found.append(path)
            except OSError:continue
        return found
    except (OSError,subprocess.SubprocessError,ValueError):return []
