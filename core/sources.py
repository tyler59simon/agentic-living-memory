"""Project knowledge base, layer 1: a deterministic, rebuildable library of source excerpts.

Sources are folders of docs, exported wikis or code. A bound repo is an implicit source (every git repo under a bound
folder) unless the registry turns that off for the domain; other sources are registered in sources.json through
source-plan (hash-approved, journaled, undoable), like projects.json. `sync` walks each source, skips binaries, large
files and vendored folders, honours the top-level .gitignore, redacts credentials, cuts every file into chunks of about
1,200 characters at headings or top-level definitions, and keeps them in domains/<D>/sources.sqlite3: derived data
(FTS5, optional vectors), never canonical, never backed up, rebuilt by `source-sync --rebuild`. Only (mtime, size)
then sha256 decide what is re-chunked. Layer 2 (agent-written digests) and layer 3 (live wiki connectors) would add
kinds and callers; the registry and the chunk table are what they would share."""
from __future__ import annotations
import hashlib,math,os,re,sqlite3,subprocess,sys,tempfile,time
from collections import Counter
from pathlib import Path
from .common import atomic_write,canonical_domain,domain_prefixes,file_lock,load_bindings
from .repository import canonical_json,find_secrets,now,read_json,store_lock
from .projects import SLUG,_repos,_text

KINDS=('folder','html-export','git-wiki')
FIELDS={'add':{'name','domain','kind','path','include','exclude'},'remove':{'name'},'implicit':{'domain','enabled'}}
VERSION=1   # chunker and schema: a different one in the database means a full rebuild
MAX_BYTES=1<<20
TARGET=1200
OVERLAP=150
MIN_UNIT=250
SYMBOL_MAX=120
SKIP_DIRS=frozenset('.git node_modules target build dist .venv venv __pycache__ .idea .gradle vendor bin obj .next .nuxt .tox .mypy_cache .pytest_cache site-packages coverage'.split())
DOC_EXT=frozenset({'.md','.markdown','.rst','.txt','.adoc','.asciidoc'})
HTML_EXT=frozenset({'.html','.htm'})
LANG={'.py':'python','.java':'java','.cs':'java','.scala':'java','.kt':'kotlin','.kts':'kotlin','.ts':'js','.tsx':'js','.js':'js','.jsx':'js','.mjs':'js','.cjs':'js',
      '.go':'go','.rs':'rust','.cbl':'cobol','.cob':'cobol','.cpy':'cobol','.sql':'sql'}
OTHER_CODE=frozenset('.c .h .cpp .hpp .cc .rb .php .swift .sh .ps1 .bat .yml .yaml .toml .xml .gradle .properties .cfg .ini'.split())
CODE_EXT=frozenset(LANG)|OTHER_CODE
BARE_DOCS=frozenset({'readme','changelog','contributing','architecture','notice','authors'})
MAX_FILES=100000   # per source: a larger tree is indexed up to here, in walk order, and doctor-style warnings say so
READERS=8
READ_BATCH=200
SPAWN_COOLDOWN=300
STALE_SECONDS=0.1
DISCOVER_DIRS=3000
DISCOVER_DEPTH=3
EMBED_SECONDS=20
EMBED_MAX=4000
SNIPPET=330

def _path(root):return Path(root)/'sources.json'
def db_path(root,domain):return Path(root)/'domains'/canonical_domain(domain,root)/'sources.sqlite3'

# ---- registry -------------------------------------------------------------------------------------------------------
def load(root):
    """The registry as {'version':1,'sources':[...],'implicit':{domain: false}}; a missing file is an empty registry."""
    data=read_json(_path(root),{'version':1,'sources':[]})
    if not isinstance(data,dict) or not isinstance(data.get('sources'),list) or any(not isinstance(s,dict) or not isinstance(s.get('name'),str) for s in data['sources']) or not isinstance(data.get('implicit',{}),dict):
        raise ValueError(f'Invalid or unreadable configuration preserved: {_path(root)}')
    return data

def _registry(entries,implicit):
    value={'version':1,'sources':sorted(entries,key=lambda s:s['name'])}
    if implicit:value['implicit']=dict(sorted(implicit.items()))
    return canonical_json(value)

def _globs(value,field):
    if value is None:return []
    if not isinstance(value,list) or len(value)>20:raise ValueError(f'{field} must be a list of at most 20 glob patterns')
    return list(dict.fromkeys(_text(v,field,1,200) for v in value))

def source_plan(root,action,payload,domain=None):
    """A reviewed plan adding or removing one registered source, or switching a domain's implicit repo sources on or off
    (kinds source_add, source_remove, source_implicit)."""
    from .governance import plan
    root=Path(root).resolve()
    if action not in FIELDS:raise ValueError('source-plan action must be add, remove or implicit')
    if not isinstance(payload,dict):raise ValueError('Payload must be a JSON object')
    extra=set(payload)-FIELDS[action]
    if extra:raise ValueError('Unknown payload fields: '+', '.join(sorted(extra)))
    with store_lock(root):
        data=load(root);entries=data['sources'];implicit=dict(data.get('implicit',{}))
        if action=='implicit':
            named=_bound(root,payload.get('domain') or domain)
            if not isinstance(payload.get('enabled'),bool):raise ValueError('implicit requires enabled true or false')
            if payload['enabled']:implicit.pop(named,None)
            else:implicit[named]=False
            return plan(root,{'sources.json':_registry(entries,implicit)},'source_implicit',[],{'domain':named,'enabled':payload['enabled'],'files':1})
        name=payload.get('name')
        if not isinstance(name,str) or not SLUG.fullmatch(name):raise ValueError('name must match [a-z0-9][a-z0-9-]{0,39}')
        if action=='remove':
            if name not in {s['name'] for s in entries}:raise ValueError(f'Unknown source {name}')
            return plan(root,{'sources.json':_registry([s for s in entries if s['name']!=name],implicit)},'source_remove',[],{'name':name,'files':1})
        if name in {s['name'] for s in entries}:raise ValueError(f'Source {name} already exists')
        if payload.get('kind') not in KINDS:raise ValueError('kind must be one of '+', '.join(KINDS))
        if not isinstance(payload.get('path'),str):raise ValueError('add requires path (an absolute folder)')
        folder=_repos([payload['path']])[0]
        if not Path(folder).is_dir():raise ValueError(f'path is not a folder: {folder}')
        entry={'name':name,'domain':_bound(root,payload.get('domain') or domain),'kind':payload['kind'],'path':folder,
               'include':_globs(payload.get('include'),'include'),'exclude':_globs(payload.get('exclude'),'exclude'),'added':now()}
        found=find_secrets('\n'.join([name,folder]+entry['include']+entry['exclude']))
        if found:raise ValueError(f'Potential credential detected ({found[0][0]}) in the source fields; remove it')
        return plan(root,{'sources.json':_registry(entries+[entry],implicit)},'source_add',[],{'name':name,'domain':entry['domain'],'kind':entry['kind'],'path':folder,'files':1})

def _bound(root,value):
    """The canonical domain a source belongs to: it must be bound or have a folder already."""
    if not value:raise ValueError('Name the domain (payload domain, or run from a bound project)')
    name=canonical_domain(value,root)
    if name not in load_bindings(root).get('domains',{}) and not (Path(root)/'domains'/name).is_dir():raise ValueError(f'domain {value} is not a bound domain')
    return name

# ---- which sources a domain has -------------------------------------------------------------------------------------
def _repos_under(prefix,deadline=None):
    """Git repos at or under a bound folder (a .git folder or file): the folder itself, else those found within
    DISCOVER_DEPTH levels, without descending into a repo, a vendored folder or a hidden one. Bounded in dirs and time."""
    if (prefix/'.git').exists():return [prefix]
    found=[];queue=[(prefix,0)];seen=0
    while queue and seen<DISCOVER_DIRS and not (deadline and time.monotonic()>deadline):
        folder,depth=queue.pop(0)
        try:children=sorted((e for e in os.scandir(folder) if e.is_dir(follow_symlinks=False)),key=lambda e:e.name)
        except OSError:continue
        for entry in children:
            if entry.name in SKIP_DIRS or entry.name.startswith('.'):continue
            seen+=1;child=Path(entry.path)
            if (child/'.git').exists():found.append(child)
            elif depth+1<DISCOVER_DEPTH:queue.append((child,depth+1))
    return found

def implicit_sources(domain,bindings,deadline=None):
    """One folder source per git repo under the domain's bound folders, named by folder (a clash gains its parent folder)."""
    repos=[]
    for prefix in domain_prefixes(domain,bindings):
        for repo in _repos_under(prefix,deadline):
            if repo not in repos:repos.append(repo)
    names=Counter(r.name for r in repos);found=[]
    for repo in sorted(repos):
        name=repo.name if names[repo.name]==1 else f'{repo.parent.name}-{repo.name}'
        found.append({'name':name.lower(),'domain':domain,'kind':'folder','path':repo.as_posix(),'include':[],'exclude':[],'implicit':True})
    return found

def resolve(root,domain,bindings=None,deadline=None):
    """The domain's sources: registered ones, then implicit repos unless the registry turned them off. A broken registry
    leaves only the implicit ones."""
    bindings=bindings if bindings is not None else load_bindings(root)
    try:registry=load(root)
    except ValueError:registry={'sources':[]}
    found=[dict(s,implicit=False) for s in registry['sources'] if s.get('domain')==domain and s.get('kind') in KINDS and isinstance(s.get('path'),str)]
    taken={s['name'] for s in found}
    if registry.get('implicit',{}).get(domain,True) is not False:found+=[s for s in implicit_sources(domain,bindings,deadline) if s['name'] not in taken]
    for spec in found:   # the memory store is not project source, whatever folder it sits in
        try:spec['exclude']=list(spec.get('exclude') or [])+['/'+Path(root).resolve().relative_to(Path(spec['path']).resolve()).as_posix()]
        except (ValueError,OSError):pass
    return found

# ---- gitignore, globs and the walk ----------------------------------------------------------------------------------
def _glob_re(pattern):
    """A gitignore-style glob as regex source: * and ? stay within a path segment, ** crosses them, [..] is a class."""
    out='';i=0
    while i<len(pattern):
        c=pattern[i]
        if c=='*':
            if pattern[i:i+2]=='**':
                i+=2
                if pattern[i:i+1]=='/':out+='(?:.*/)?';i+=1
                else:out+='.*'
                continue
            out+='[^/]*'
        elif c=='?':out+='[^/]'
        elif c=='[' and pattern.find(']',i+2)>0:
            j=pattern.find(']',i+2);body=pattern[i+1:j];body='^'+body[1:] if body[:1]=='!' else body
            out+='['+body.replace('\\','\\\\')+']';i=j
        else:out+=re.escape(c)
        i+=1
    return out

def _compile(pattern):
    """(regex, negated, directories only) for one gitignore line, else None."""
    text=pattern.strip()
    if not text or text.startswith('#'):return None
    negated=text.startswith('!');text=text[1:] if negated else text
    only_dirs=text.endswith('/');text=text.rstrip('/')
    if not text:return None
    anchored='/' in text;text=text.lstrip('/')
    return re.compile(('^' if anchored else '(?:^|.*/)')+_glob_re(text)+'$'),negated,only_dirs

def gitignore(folder):
    """The top-level .gitignore of a folder as compiled rules (nested ones are not read)."""
    try:lines=(Path(folder)/'.gitignore').read_text(encoding='utf-8',errors='replace').splitlines()
    except OSError:return []
    return [r for r in map(_compile,lines) if r]

def ignored(rules,rel,is_dir):
    """The last matching rule decides, as in git."""
    result=False
    for regex,negated,only_dirs in rules:
        if (only_dirs and not is_dir) or not regex.match(rel):continue
        result=not negated
    return result

def _patterns(globs):return [r[0] for r in map(_compile,globs) if r]
def _any(regexes,rel):return any(r.match(rel) for r in regexes)

def _eligible(extensions,name,rel,include):
    """Whether a file is a candidate: matched by an include glob, else of a kind's default extensions (exts: the walk's set of them)."""
    if include:return _any(include,rel)
    name=name.lower();dot=name.rfind('.')
    if dot<=0:return name in BARE_DOCS
    return name[dot:] in extensions and not name.endswith(('.min.js','.min.css','.map'))

def walk(base,spec,deadline=None,ordered=True):
    """(is_dir, rel path, size, mtime_ns) for every folder and candidate file of a source, in name order unless ordered is False: no
    links, hidden or vendored folders, nothing the top-level .gitignore or the source's exclude globs name. Stops at the deadline."""
    base=Path(base);rules=gitignore(base);include=_patterns(spec.get('include') or []);exclude=_patterns(spec.get('exclude') or [])
    extensions=DOC_EXT|(HTML_EXT if spec.get('kind','folder') in {'html-export','git-wiki'} else CODE_EXT)
    stack=['']
    while stack:
        rel=stack.pop()
        if deadline and time.monotonic()>deadline:return
        try:entries=os.scandir(base/rel if rel else base);entries=sorted(entries,key=lambda e:e.name,reverse=True) if ordered else list(entries)
        except OSError:continue
        for entry in entries:
            path=f'{rel}/{entry.name}' if rel else entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIP_DIRS or (entry.name.startswith('.') and entry.name!='.github') or ignored(rules,path,True) or _any(exclude,path) or getattr(entry,'is_junction',bool)():continue
                    stack.append(path);yield True,path,0,os.stat(entry.path).st_mtime_ns   # not the scan's cached time: a folder's is updated lazily
                elif entry.is_file(follow_symlinks=False) and _eligible(extensions,entry.name,path,include) and not ignored(rules,path,False) and not _any(exclude,path):
                    info=entry.stat();yield False,path,info.st_size,info.st_mtime_ns
            except OSError:continue

# ---- text extraction and chunking -----------------------------------------------------------------------------------
def _decode(data):
    try:return data.decode('utf-8-sig')
    except UnicodeDecodeError:return data.decode('cp1252',errors='replace')

def redact_text(text):
    """(text with each credential replaced as repository.redact does but keeping its line count, how many were replaced)."""
    spans=find_secrets(text)
    for kind,start,end in reversed(spans):text=text[:start]+f"<redacted type='{kind}'/>"+'\n'*text.count('\n',start,end)+text[end:]
    return text,len(spans)

_HTML=[]

def _html_parser():
    """The HTML-to-lines parser class, built on first use: html.parser costs more to import than a hook should pay."""
    if not _HTML:
        import html.parser
        class Html(html.parser.HTMLParser):
            """HTML to lines of text with their source line numbers: h1-h6 become markdown headings, table rows stay on one line
            (cells joined by |), and script, style, nav, header, footer and breadcrumb or sidebar elements are dropped."""
            SKIP=frozenset({'script','style','nav','header','footer','noscript','svg','template'})
            CHROME=re.compile(r'breadcrumb|navbar|nav-menu|sidebar|site-?header|site-?footer|skip-?(?:link|to)|pagination|cookie',re.I)
            BLOCK=frozenset('p div section article main ul ol li tr table pre blockquote br hr dl dt dd figure figcaption details summary form tbody thead'.split())
            HEADINGS={f'h{i}':i for i in range(1,7)}
            def __init__(self):
                super().__init__(convert_charrefs=True);self.lines=[];self.cur='';self.at=1;self.skip=None;self.depth=0;self.title='';self.in_title=False;self.heading=0;self.pre=False
            def _flush(self,prefix=''):
                text=self.cur.strip();self.cur=''
                if text:self.lines.append((self.at,prefix+text))
            def handle_starttag(self,tag,attrs):
                if self.skip:
                    if tag==self.skip:self.depth+=1
                    return
                label=' '.join(str(v) for k,v in attrs if k in {'class','id'} and v)
                if tag in self.SKIP or (tag in {'div','ul','ol','span','section','aside','p','table'} and self.CHROME.search(label)):self.skip=tag;self.depth=1;return
                if tag=='title':self.in_title=True;return
                if tag in self.HEADINGS:self._flush();self.heading=self.HEADINGS[tag]
                elif tag in self.BLOCK:
                    self._flush('    ' if self.pre else '')
                    if tag=='li':self.cur='- '
                    if tag=='pre':self.pre=True
                elif tag in {'td','th'} and self.cur.strip():self.cur+=' | '
                self.at=self.getpos()[0]
            def handle_endtag(self,tag):
                if self.skip:
                    if tag==self.skip:
                        self.depth-=1
                        if not self.depth:self.skip=None
                    return
                if tag=='title':self.in_title=False;return
                if tag in self.HEADINGS and self.heading:self._flush('#'*self.heading+' ');self.heading=0
                elif tag in self.BLOCK:
                    self._flush('    ' if self.pre else '')
                    if tag=='pre':self.pre=False
            def handle_data(self,data):
                if self.skip:return
                if self.in_title:self.title+=data;return
                if not self.cur.strip():self.at=self.getpos()[0]
                if self.pre:
                    parts=data.split('\n')
                    for i,part in enumerate(parts):
                        self.cur+=part
                        if i<len(parts)-1:self._flush('    ');self.at=self.getpos()[0]+i+1
                else:self.cur+=re.sub(r'\s+',' ',data)
            def close(self):
                super().close();self._flush()
        _HTML.append(Html)
    return _HTML[0]

def html_rows(text):
    """[(source line, text line)] for an HTML page: the extracted text, led by its <title> as a heading when no heading says it."""
    parser=_html_parser()()
    try:parser.feed(text);parser.close()
    except Exception:pass   # sloppy markup: keep what was read
    rows=parser.lines;title=' '.join(parser.title.split())
    if title and not any(t.lstrip('# ').strip()==title for _,t in rows[:3]):rows.insert(0,(1,'# '+title))
    return rows

ATX=re.compile(r'^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$')
ADOC=re.compile(r'^(={1,6})\s+(\S.*)$')
RST_BAR=re.compile(r'^([=\-~^"`#*+._:\'])\1{2,}\s*$')
FENCE=re.compile(r'^\s{0,3}(```|~~~)')

def _headings(texts,style):
    """[(line index, level, title)] of a document; markdown skips fenced code and front matter."""
    found=[];fence=None;rst={};n=len(texts);i=0
    if style=='md' and texts and texts[0].strip()=='---':
        i=next((j+1 for j in range(1,min(n,60)) if texts[j].strip()=='---'),0)
    while i<n:
        line=texts[i]
        if style=='md':
            m=FENCE.match(line)
            if m:fence=None if fence==m[1] else (fence or m[1])
            if fence:i+=1;continue
            m=ATX.match(line)
            if m and m[2]:found.append((i,len(m[1]),m[2]))
            elif line.strip() and i+1<n and re.fullmatch(r'=+|-+',texts[i+1].strip() or 'x') and len(texts[i+1].strip())>=3 and not re.match(r'\s*(?:[-*+>|#]|\d+[.)]\s)',line) and (i==0 or not texts[i-1].strip()):
                found.append((i,1 if texts[i+1].strip()[0]=='=' else 2,line.strip()))
        elif style=='adoc':
            m=ADOC.match(line)
            if m:found.append((i,len(m[1]),m[2]))
        elif style=='rst':
            bar=RST_BAR.match(texts[i+1]) if i+1<n else None
            if line.strip() and not RST_BAR.match(line) and bar and len(texts[i+1].strip())>=len(line.strip()):
                level=rst.setdefault(bar[1],len(rst)+1);found.append((i,level,line.strip()))
        i+=1
    return found

def heading_units(rows,style):
    """[(heading path, rows)] cut at each heading; text before the first heading has an empty path."""
    texts=[t for _,t in rows];marks=_headings(texts,style)
    if not marks:return [('',rows)]
    units=[];path=[]
    if marks[0][0]:units.append(('',rows[:marks[0][0]]))
    for k,(i,level,title) in enumerate(marks):
        path=[p for p in path if p[0]<level]+[(level,title)]
        units.append((' > '.join(t for _,t in path),rows[i:marks[k+1][0] if k+1<len(marks) else len(rows)]))
    return units

STATEMENT=frozenset('if else for while switch catch return new throw try do case yield await assert synchronized using lock foreach elif with'.split())
MODIFIERS=r'(?:(?:public|private|protected|internal|static|final|abstract|sealed|partial|open|data|override|virtual|async|synchronized|native|default|extern|unsafe|readonly|suspend|inline|operator|infix|tailrec|inner|annotation|companion|enum|non-sealed|strictfp)\s+)*'
JAVA_TYPE=MODIFIERS+r'(?P<k>class|interface|enum|record|struct|trait|object)\s+(?P<n>\w+)'
# (regex, kind): 'class' opens a scope whose name prefixes the members after it; 'def' is a function or member.
RULES={
 'python':[(re.compile(r'^class\s+(?P<n>\w+)'),'class'),(re.compile(r'^(?: {4}|\t)?(?:async\s+def|def)\s+(?P<n>\w+)'),'def')],
 'java':[(re.compile(r'^\s{0,4}(?:@\w+(?:\([^)]*\))?\s+)*'+JAVA_TYPE),'class'),
         (re.compile(r'^(?: {4,8}|\t{1,2})(?:@\w+(?:\([^)]*\))?\s+)*'+MODIFIERS+r'(?:[\w<>\[\],.?]+\s+)*(?P<n>\w+)\s*\([^;]*$'),'def')],
 'kotlin':[(re.compile(r'^\s{0,4}(?:@\w+\s+)*'+JAVA_TYPE),'class'),
           (re.compile(r'^\s{0,8}(?:@\w+\s+)*'+MODIFIERS+r'fun\s+(?:<[^>]*>\s*)?(?:[\w.<>?]+\.)?(?P<n>\w+)\s*\('),'def')],
 'js':[(re.compile(r'^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:class|interface|enum|namespace)\s+(?P<n>\w+)'),'class'),
       (re.compile(r'^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<n>\w+)|^(?:export\s+)?type\s+(?P<n2>\w+)\s*[=<]'
                   r'|^(?:export\s+)?(?:const|let|var)\s+(?P<n3>\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*(?::\s*[^=]+)?=>|\w+\s*=>)'),'def'),
       (re.compile(r'^(?: {2,4}|\t)(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*(?P<n>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^;]*\)\s*(?::\s*[^{;]+)?\{\s*$'),'def')],
 'go':[(re.compile(r'^type\s+(?P<n>\w+)\s+(?:struct|interface)\b'),'class'),(re.compile(r'^func\s+(?:\(\s*\w*\s*\*?(?P<r>\w+)(?:\[[^\]]*\])?\s*\)\s*)?(?P<n>\w+)'),'def')],
 'rust':[(re.compile(r'^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union|mod)\s+(?P<n>\w+)'),'class'),(re.compile(r'^impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?(?P<n>\w+)'),'class'),
         (re.compile(r'^(?: {4}|\t)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+(?P<n>\w+)'),'def')],
 'cobol':[(re.compile(r'^ {7}(?P<n>[A-Z][A-Z0-9-]*(?: [A-Z-]+)? (?:DIVISION|SECTION))\.'),'class'),(re.compile(r'^ {7}(?P<n>[0-9A-Z][0-9A-Z-]*)\.\s*$'),'def')],
}
COBOL_STATEMENTS=frozenset({'EXIT','GOBACK','CONTINUE','END-IF','END-PERFORM','END-EVALUATE','END-READ','END-CALL','END-STRING','NEXT'})
SQL_START=re.compile(r'^(?:create|alter|drop|insert|update|delete|merge|truncate|grant|select|with)\b',re.I)
SQL_NAME=re.compile(r'^(create(?:\s+or\s+replace)?\s+(?:unique\s+)?(?:materialized\s+)?(?:table|view|index|function|procedure|trigger|schema|sequence|type)\s+(?:if\s+not\s+exists\s+)?[\w."\[\]`]+|\w+(?:\s+\w+){0,2})',re.I)
LEAD=re.compile(r'\s*(?:@\w|//|/\*|\*|#|--)')
NESTED_CLASS=('java','kotlin','js')

def code_units(rows,lang):
    """[(symbol, rows)] cut at each top-level definition (and member, in the languages that nest them), with the comment and
    decorator lines directly above it; the lines before the first one form a unit of their own."""
    texts=[t for _,t in rows];starts=[];top=''
    rules=RULES.get(lang)
    if lang!='sql' and not rules:return [('',rows)]
    for i,line in enumerate(texts):
        if not line.strip():continue
        symbol=None
        if lang=='sql':
            if SQL_START.match(line):
                before=next((t.strip() for t in reversed(texts[max(0,i-6):i]) if t.strip() and not t.strip().startswith('--')),'')
                if not before or before.endswith(';'):
                    named=SQL_NAME.match(line);symbol=(named[1] if named and named[1].lower().startswith('create') else line.strip())[:70]
        else:
            for regex,kind in rules:
                m=regex.match(line)
                if not m:continue
                name=next((v for k,v in m.groupdict().items() if k.startswith('n') and v),None)
                if not name or (lang in {'java','kotlin','js'} and name in STATEMENT) or (lang=='cobol' and name in COBOL_STATEMENTS):continue
                if lang=='java' and kind=='def':
                    before=line.split('(')[0].split()
                    if before[0] in STATEMENT or (len(before)<2 and not name[:1].isupper()):continue
                if kind=='class':top=name;symbol=name
                else:
                    if m.groupdict().get('r'):name=f"{m['r']}.{name}"
                    symbol=f'{top}.{name}' if top and lang in NESTED_CLASS+('python','rust') and line[:1] in {' ','\t'} else name
                    if line[:1] not in {' ','\t'} and lang!='cobol':top=name if lang=='python' else top
                break
        if symbol:
            s=i
            while s>0 and (not starts or s-1>starts[-1][0]) and texts[s-1].strip() and LEAD.match(texts[s-1]) and lang!='cobol':s-=1
            starts.append((s,symbol))
    if not starts:return [('',rows)]
    units=[('',rows[:starts[0][0]])] if starts[0][0] else []
    return units+[(symbol[:SYMBOL_MAX],rows[s:starts[k+1][0] if k+1<len(starts) else len(rows)]) for k,(s,symbol) in enumerate(starts)]

def _size(rows):return sum(len(t)+1 for _,t in rows)

def _symbols(a,b):
    """The label of two merged units: the more specific one when the other is its parent (a heading path, a class), else both."""
    if not a or a==b or b.startswith((a+' > ',a+'.')):return b or a
    return (a+'; '+b)[:SYMBOL_MAX] if b else a

def pack(units,docs=False):
    """[(symbol, line_start, line_end, text)]: small neighbouring units merge (a heading alone, in a document; under
    MIN_UNIT characters, in code), large ones split at line boundaries into chunks of about TARGET characters, each
    starting with a few lines of the previous one (OVERLAP characters)."""
    merged=[];small=150 if docs else MIN_UNIT
    for symbol,rows in units:
        if merged and _size(merged[-1][1])<small and _size(merged[-1][1])+_size(rows)<=TARGET:merged[-1]=(_symbols(merged[-1][0],symbol),merged[-1][1]+rows)
        else:merged.append((symbol,list(rows)))
    out=[]
    for symbol,rows in merged:
        cur=[];size=0
        def emit():
            body=list(cur)
            while body and not body[0][1].strip():body.pop(0)
            while body and not body[-1][1].strip():body.pop()
            if body:out.append((symbol[:SYMBOL_MAX],body[0][0],body[-1][0],'\n'.join(t for _,t in body)))
        for number,line in rows:
            pieces=[line[i:i+TARGET] for i in range(0,len(line),TARGET)] or ['']
            for piece in pieces:
                if cur and size+len(piece)+1>TARGET:
                    emit();tail=[];kept=0
                    for row in reversed(cur):
                        if kept+len(row[1])+1>OVERLAP:break
                        tail.insert(0,row);kept+=len(row[1])+1
                    cur,size=tail,kept
                cur.append((number,piece));size+=len(piece)+1
        if cur:emit()
    return out

def chunk_file(rel,text,kind='folder'):
    """[{symbol, line_start, line_end, text, kind}] for one file's (already redacted) text; kind is the extraction:
    'html', 'doc' (headings), 'code' or 'text'."""
    ext=os.path.splitext(rel)[1].lower();text=text.replace('\r\n','\n').replace('\r','\n')
    rows=[(i+1,l.rstrip()) for i,l in enumerate(text.split('\n'))]
    if ext in HTML_EXT:units,how=heading_units(html_rows(text),'md'),'html'
    elif ext in {'.md','.markdown'}:units,how=heading_units(rows,'md'),'doc'
    elif ext=='.rst':units,how=heading_units(rows,'rst'),'doc'
    elif ext in {'.adoc','.asciidoc'}:units,how=heading_units(rows,'adoc'),'doc'
    elif ext in LANG:units,how=code_units(rows,LANG[ext]),'code'
    else:units,how=[('',rows)],'code' if ext in OTHER_CODE else 'text'
    if kind=='git-wiki' and how in {'doc','html'}:   # a wiki page is named by its file: Retry-Policy.md is "Retry Policy"
        title=re.sub(r'[-_+]+',' ',os.path.splitext(rel.rsplit('/',1)[-1])[0]).strip();units=[(s or title,r) for s,r in units]
    return [{'symbol':s,'line_start':a,'line_end':b,'text':t,'kind':how} for s,a,b,t in pack(units,how in {'doc','html'})]

# ---- the index ------------------------------------------------------------------------------------------------------
def split_camel(text):
    """fooBar and XMLParser read as separate words, so a question in words finds an identifier."""
    return re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])',' ',text)

def _names(symbol,text):
    """The words of camelCase identifiers in a chunk, as an extra FTS column (underscored names split on their own)."""
    found=set(re.findall(r'\b[A-Za-z_]\w*[a-z0-9][A-Z]\w*\b',symbol+' '+text))
    return ' '.join(split_camel(w) for w in sorted(found)[:300])

def _stems(symbol,path,text):
    from .search import stems
    return stems(split_camel(symbol+' '+path+' '+text))

SCHEMA=('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)',
 'CREATE TABLE source(name TEXT PRIMARY KEY,kind TEXT,root TEXT,implicit INTEGER,config TEXT,files INTEGER DEFAULT 0,chunks INTEGER DEFAULT 0,synced TEXT)',
 'CREATE TABLE file(id INTEGER PRIMARY KEY,source TEXT NOT NULL,path TEXT NOT NULL,mtime_ns INTEGER,size INTEGER,sha256 TEXT,chunks INTEGER DEFAULT 0,skipped TEXT,redacted INTEGER DEFAULT 0,UNIQUE(source,path))',
 'CREATE TABLE chunk(id INTEGER PRIMARY KEY,file_id INTEGER NOT NULL,source TEXT NOT NULL,path TEXT NOT NULL,kind TEXT,symbol TEXT,line_start INTEGER,line_end INTEGER,sha256 TEXT)',
 'CREATE INDEX chunk_file ON chunk(file_id)',
 "CREATE VIRTUAL TABLE chunk_fts USING fts5(symbol,path,names,text,tokenize='porter unicode61')",
 'CREATE TABLE vocab(stem TEXT PRIMARY KEY,df INTEGER NOT NULL)')

def _create(path):
    db=sqlite3.connect(path,timeout=30)
    for statement in SCHEMA:db.execute(statement)
    db.execute('INSERT INTO meta VALUES(?,?)',('version',str(VERSION)));db.commit();return db

def _meta(db,key):
    row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return row[0] if row else None
def _set_meta(db,key,value):db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

def _readonly(root,domain,timeout=1.0):
    """A read-only connection to the domain's index, or None when there is none or it is unreadable."""
    try:
        path=db_path(root,domain)
        if not path.is_file():return None
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=timeout)
        if _meta(db,'version')!=str(VERSION):db.close();return None
        return db
    except (ValueError,sqlite3.Error):return None

def _drop_file(db,file_id,delta):
    """Remove a file's chunks (and what they added to the vocabulary, noted in delta)."""
    for symbol,path,text in db.execute('SELECT symbol,path,text FROM chunk_fts WHERE rowid IN (SELECT id FROM chunk WHERE file_id=?)',(file_id,)).fetchall():
        for s in _stems(symbol,path,text):delta[s]-=1
    db.execute('DELETE FROM chunk_fts WHERE rowid IN (SELECT id FROM chunk WHERE file_id=?)',(file_id,))
    if _has_vectors(db):db.execute('DELETE FROM vec WHERE rowid IN (SELECT id FROM chunk WHERE file_id=?)',(file_id,))
    db.execute('DELETE FROM chunk WHERE file_id=?',(file_id,))

def _has_vectors(db):return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='vec'").fetchone())

def _apply_vocab(db,delta):
    rows=[(s,n) for s,n in delta.items() if n]
    db.executemany('INSERT INTO vocab VALUES(?,?) ON CONFLICT(stem) DO UPDATE SET df=df+excluded.df',rows)
    db.execute('DELETE FROM vocab WHERE df<=0');delta.clear()

def _config(spec):return hashlib.sha256(canonical_json([spec['kind'],spec['path'],spec.get('include') or [],spec.get('exclude') or []]).encode()).hexdigest()[:16]

def _read(base,rel,size):
    """A file's bytes; None for one over MAX_BYTES (not read), False for one that cannot be read."""
    if size>MAX_BYTES:return None
    try:return (base/rel).read_bytes()
    except OSError:return False

def _sync_source(db,spec,delta,force=False):
    """Bring one source's rows up to date; returns its counts. Only files whose (mtime, size) differ are read, and only
    those whose sha256 differs are re-chunked."""
    name=spec['name'];base=Path(spec['path']);config=_config(spec)
    known=db.execute('SELECT config FROM source WHERE name=?',(name,)).fetchone()
    count=dict.fromkeys(('added','changed','removed','unchanged','skipped','redacted'),0)
    if force or (known and known[0]!=config):
        for (file_id,) in db.execute('SELECT id FROM file WHERE source=?',(name,)).fetchall():_drop_file(db,file_id,delta)
        db.execute('DELETE FROM file WHERE source=?',(name,))
    db.execute('INSERT INTO source(name,kind,root,implicit,config) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,root=excluded.root,implicit=excluded.implicit,config=excluded.config',
               (name,spec['kind'],spec['path'],int(bool(spec.get('implicit'))),config))
    have={path:(i,m,s,h) for i,path,m,s,h in db.execute('SELECT id,path,mtime_ns,size,sha256 FROM file WHERE source=?',(name,))}
    seen=set();todo=[]
    for is_dir,rel,size,mtime in walk(base,spec):
        if is_dir:continue
        if len(seen)>=MAX_FILES:count['truncated']=1;break
        seen.add(rel);old=have.get(rel)
        if old and (old[1],old[2])==(mtime,size):count['unchanged']+=1
        else:todo.append((rel,size,mtime,old))
    # Opening a file is the slow part (virus scanners, network drives) and waits without the GIL, so a batch is read by several threads.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(READERS) as pool:
        for start in range(0,len(todo),READ_BATCH):
            group=todo[start:start+READ_BATCH];raw=list(pool.map(lambda item:_read(base,item[0],item[1]),group))
            for (rel,size,mtime,old),data in zip(group,raw):
                reason=None;digest=None;chunks=[];redacted=0
                if data is False:continue   # unreadable now: left as it was
                if data is None:reason='large'
                elif b'\0' in data[:8192]:reason='binary'
                else:
                    digest=hashlib.sha256(data).hexdigest()
                    if old and old[3]==digest:
                        db.execute('UPDATE file SET mtime_ns=?,size=? WHERE id=?',(mtime,size,old[0]));count['unchanged']+=1;continue
                    text,redacted=redact_text(_decode(data));chunks=chunk_file(rel,text,spec['kind'])
                if old:_drop_file(db,old[0],delta);db.execute('DELETE FROM file WHERE id=?',(old[0],));count['changed']+=1
                else:count['added']+=1
                if reason:count['skipped']+=1
                file_id=db.execute('INSERT INTO file(source,path,mtime_ns,size,sha256,chunks,skipped,redacted) VALUES(?,?,?,?,?,?,?,?)',(name,rel,mtime,size,digest,len(chunks),reason,redacted)).lastrowid
                count['redacted']+=redacted
                for c in chunks:
                    cid=db.execute('INSERT INTO chunk(file_id,source,path,kind,symbol,line_start,line_end,sha256) VALUES(?,?,?,?,?,?,?,?)',
                                   (file_id,name,rel,c['kind'],c['symbol'],c['line_start'],c['line_end'],hashlib.sha256(c['text'].encode()).hexdigest())).lastrowid
                    db.execute('INSERT INTO chunk_fts(rowid,symbol,path,names,text) VALUES(?,?,?,?,?)',(cid,c['symbol'],rel,_names(c['symbol'],c['text']),c['text']))
                    for s in _stems(c['symbol'],rel,c['text']):delta[s]+=1
            _apply_vocab(db,delta);db.commit()
    for path,(file_id,*_) in have.items():
        if path not in seen:_drop_file(db,file_id,delta);db.execute('DELETE FROM file WHERE id=?',(file_id,));count['removed']+=1
    files,chunks=db.execute('SELECT count(*),coalesce(sum(chunks),0) FROM file WHERE source=? AND skipped IS NULL',(name,)).fetchone()
    db.execute('UPDATE source SET files=?,chunks=?,synced=? WHERE name=?',(files,chunks,now(),name))
    return dict(count,name=name,files=files,chunks=chunks)

def _embed(db,root,deadline):
    """Vectors for chunks that have none, while a model is installed: only changed chunks, at most EMBED_MAX and EMBED_SECONDS per sync."""
    from . import semantic
    from .search import _vector_identity
    model=semantic.load(root);identity=_vector_identity(root) if model else None
    have=_has_vectors(db)
    if not model:
        if have:db.execute('DROP TABLE vec');_set_meta(db,'vectors','')
        return None
    if have and _meta(db,'vectors')!=identity:db.execute('DROP TABLE vec');have=False
    if not have:db.execute('CREATE TABLE vec(rowid INTEGER PRIMARY KEY,vec BLOB NOT NULL)')
    _set_meta(db,'vectors',identity);done=0
    for cid,symbol,text in db.execute('SELECT c.id,f.symbol,f.text FROM chunk c JOIN chunk_fts f ON f.rowid=c.id LEFT JOIN vec v ON v.rowid=c.id WHERE v.rowid IS NULL LIMIT ?',(EMBED_MAX,)).fetchall():
        if time.monotonic()>deadline:break
        vector=model.embed((symbol+'\n'+text)[:2000])
        db.execute('INSERT INTO vec VALUES(?,?)',(cid,semantic.to_blob(vector) if vector else b''));done+=1
    return {'embedded':done,'pending':db.execute('SELECT count(*) FROM chunk c LEFT JOIN vec v ON v.rowid=c.id WHERE v.rowid IS NULL').fetchone()[0]}

def _lock(root,domain,timeout):return file_lock(Path(root)/'hooks'/f'sources-{domain}.lock',timeout=timeout)

def sync(root,domain,name=None,rebuild=False,wait=300):
    """Incremental sync of the domain's sources into its index. name limits it to one source; rebuild drops what was
    indexed (a whole new database, or just that source's rows with name). One sync per domain at a time: another
    caller waits up to `wait` seconds, then gets status 'busy'."""
    root=Path(root).resolve();domain=canonical_domain(domain,root)
    try:
        with _lock(root,domain,wait):return _sync(root,domain,name,rebuild)
    except TimeoutError:return {'ok':True,'status':'busy','domain':domain,'hint':'Another sync of this domain is running; ask works on the index that exists.'}

def _sync(root,domain,name,rebuild):
    started=time.time();clock=time.perf_counter();specs=resolve(root,domain)
    if name:
        specs=[s for s in specs if s['name']==name]
        if not specs:raise ValueError(f'Unknown source {name}')
    target=db_path(root,domain);target.parent.mkdir(parents=True,exist_ok=True)
    fresh=(rebuild and not name) or not target.is_file();db=None;temp=None
    try:
        if not fresh:
            try:
                db=sqlite3.connect(target,timeout=30)
                if _meta(db,'version')!=str(VERSION):db.close();db=None;fresh=True
            except sqlite3.Error:db=None;fresh=True
        if fresh:
            fd,temp=tempfile.mkstemp(prefix='.sources-',suffix='.sqlite3',dir=target.parent);os.close(fd);os.unlink(temp);db=_create(temp)
        delta=Counter();results=[]
        for spec in specs:results.append(_sync_source(db,spec,delta,force=bool(rebuild and name)));_apply_vocab(db,delta);db.commit()
        if not name:
            for (gone,) in db.execute('SELECT name FROM source').fetchall():
                if gone not in {s['name'] for s in specs}:
                    for (file_id,) in db.execute('SELECT id FROM file WHERE source=?',(gone,)).fetchall():_drop_file(db,file_id,delta)
                    db.execute('DELETE FROM file WHERE source=?',(gone,));db.execute('DELETE FROM source WHERE name=?',(gone,))
            _apply_vocab(db,delta)
        vectors=_embed(db,root,time.monotonic()+EMBED_SECONDS)
        _set_meta(db,'synced',now());_set_meta(db,'synced_epoch',started);db.commit()
        totals=db.execute('SELECT coalesce(sum(files),0),coalesce(sum(chunks),0) FROM source').fetchone();db.close();db=None
        if temp:os.replace(temp,target);temp=None
    finally:
        if db is not None:db.close()
        if temp and os.path.exists(temp):os.unlink(temp)
    return {'ok':True,'domain':domain,'rebuilt':fresh,'sources':results,'files':totals[0],'chunks':totals[1],'vectors':vectors,'ms':round((time.perf_counter()-clock)*1000)}

# ---- state, listing, staleness and the background sync --------------------------------------------------------------
def state(root,domain):
    """What the domain's index holds ({files, chunks, sources, synced, epoch}), else None. Reads one small table."""
    db=_readonly(root,domain)
    if not db:return None
    try:
        files,chunks=db.execute('SELECT coalesce(sum(files),0),coalesce(sum(chunks),0) FROM source').fetchone()
        return {'files':files,'chunks':chunks,'sources':db.execute('SELECT count(*) FROM source').fetchone()[0],'synced':_meta(db,'synced'),'epoch':float(_meta(db,'synced_epoch') or 0)}
    except (sqlite3.Error,ValueError):return None
    finally:db.close()

def listing(root,domain=None,bindings=None):
    """Registered and implicit sources (of one domain, else every registered one) with what the index holds for each."""
    root=Path(root);warnings=[];rows=[]
    try:registry=load(root)
    except ValueError as exc:registry={'sources':[]};warnings.append(str(exc))
    specs=resolve(root,domain,bindings) if domain else [dict(s,implicit=False) for s in registry['sources']]
    held={}
    db=_readonly(root,domain) if domain else None
    if db:
        try:held={n:(f,c,s) for n,f,c,s in db.execute('SELECT name,files,chunks,synced FROM source')}
        except sqlite3.Error:pass
        finally:db.close()
    for s in specs:
        f,c,when=held.get(s['name'],(0,0,None))
        rows.append({'name':s['name'],'domain':s.get('domain'),'kind':s['kind'],'path':s['path'],'include':s.get('include') or [],'exclude':s.get('exclude') or [],
                     'implicit':bool(s.get('implicit')),'files':f,'chunks':c,'synced':when,'state':'indexed' if when else 'not synced'})
        if not Path(s['path']).is_dir():warnings.append(f"{s['name']}: folder does not exist: {s['path']}")
    for s in registry['sources']:
        if s.get('domain') and not (root/'domains'/str(s['domain'])).is_dir() and s['domain'] not in load_bindings(root).get('domains',{}):warnings.append(f"{s['name']}: domain {s['domain']} is not bound")
    off=[d for d,v in registry.get('implicit',{}).items() if v is False]
    return {'ok':True,'domain':domain,'implicit_disabled':off,'sources':rows,'index':state(root,domain) if domain else None,'warnings':warnings}

def stale(root,domain,bindings=None,limit=STALE_SECONDS):
    """True when a sync is due: sources the index lacks or has changed, or a folder or file touched since the last sync
    started, found by a stat walk of at most `limit` seconds (it stops at the first change). Never raises."""
    try:
        deadline=time.monotonic()+limit;specs=resolve(root,domain,bindings,deadline)
        if not specs:return False
        info=state(root,domain)
        if not info:return True
        db=_readonly(root,domain)
        try:held=dict(db.execute('SELECT name,config FROM source').fetchall())
        finally:db.close()
        if {s['name']:_config(s) for s in specs}!=held:return True
        cutoff=int(info['epoch']*1e9)
        for spec in specs:
            top=Path(spec['path'])
            if top.stat().st_mtime_ns>cutoff or any(g.is_file() and g.stat().st_mtime_ns>cutoff for g in (top/'.git'/'HEAD',top/'.git'/'index')):return True   # a commit, checkout or add
            for _,_,_,mtime in walk(top,spec,deadline,False):
                if mtime>cutoff:return True
        # A tree too big to walk in the time cap goes unchecked past its start, so an index older than a day is refreshed anyway.
        return time.monotonic()>deadline and time.time()-info['epoch']>86400
    except Exception as exc:
        from .logs import swallowed
        swallowed('sources.stale',exc,root);return False

def syncing(root,domain):
    """True while another process holds the domain's sync lock."""
    try:
        with _lock(root,domain,0):return False
    except (TimeoutError,OSError):return True

def spawn(root,domain):
    """Start `source-sync` as a detached background process, at most once per SPAWN_COOLDOWN seconds per domain. Never waits
    and never raises; True when a process was started. LIVING_MEMORY_NO_BACKGROUND=1 turns it off."""
    try:
        if os.environ.get('LIVING_MEMORY_NO_BACKGROUND') or syncing(root,domain):return False
        marker=Path(root)/'hooks'/f'sources-{domain}.spawned'
        try:
            if time.time()-float(marker.read_text())<SPAWN_COOLDOWN:return False
        except (OSError,ValueError):pass
        script=Path(__file__).resolve().parents[1]/'living_memory.py'
        argv=[sys.executable,str(script),'--root',str(root),'--domain',domain,'source-sync']
        options={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL,'close_fds':True,'cwd':tempfile.gettempdir()}   # not the store: a running process would block deleting or moving it
        if os.name=='nt':
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, and out of the host's job object when it allows that (CREATE_BREAKAWAY_FROM_JOB).
            flags=0x00000008|0x00000200
            try:subprocess.Popen(argv,creationflags=flags|0x01000000,**options)
            except OSError:subprocess.Popen(argv,creationflags=flags,**options)
        else:subprocess.Popen(argv,start_new_session=True,**options)
        atomic_write(marker,str(time.time()))
        return True
    except Exception as exc:
        from .logs import swallowed
        swallowed('sources.spawn',exc,root);return False

def session_line(root,domain,bindings,trace=None):
    """The SessionStart line about the knowledge base ('' when the domain has no sources or nothing indexable), starting a
    background sync when one is due."""
    clock=time.perf_counter()
    try:
        due=stale(root,domain,bindings);started=spawn(root,domain) if due else False
        info=state(root,domain)
        if isinstance(trace,dict):trace.update(stale=due,spawned=started,files=info['files'] if info else None,ms=round((time.perf_counter()-clock)*1000,1))
        if info and info['files']:return f"Knowledge base: {info['files']} files indexed (synced {str(info['synced'])[:10]}); for questions about the project, CMD ask '...'"
        if started or (due and not info):return "Knowledge base: the first index is being built in the background; for questions about the project, CMD ask '...' once it is done"
    except Exception as exc:
        from .logs import swallowed
        swallowed('sources.session_line',exc,root)
    return ''

# ---- search ---------------------------------------------------------------------------------------------------------
def _vocab(db,wanted):
    """(chunks, {stem: df}) for these words from an open index."""
    from .search import stem
    total=db.execute('SELECT count(*) FROM chunk').fetchone()[0]
    ask=sorted({v for w in wanted for v in (stem(w),stem(w)[:-1] if stem(w).endswith('e') else stem(w)+'e')})
    return total,dict(r for i in range(0,len(ask),500) for r in db.execute(f"SELECT stem,df FROM vocab WHERE stem IN ({','.join('?'*len(ask[i:i+500]))})",ask[i:i+500]))

def vocabulary(root,domain,wanted):
    """(chunks, {stem: df}) for these words, like search.vocabulary but over the sources index; (0, {}) without one."""
    db=_readonly(root,domain)
    if not db:return 0,{}
    try:return _vocab(db,wanted)
    except sqlite3.Error:return 0,{}
    finally:db.close()

def _window(text,line_start,wanted,width):
    """(excerpt, first line, last line): the densest run of whole lines within width characters; a single long line is cut around its first match."""
    from .search import stem
    lines=text.split('\n')
    if len(text)<=width:return text,line_start,line_start+len(lines)-1
    score=[sum(1 for w in re.findall(r'[^\W_]+',split_camel(l).casefold()) if stem(w) in wanted) for l in lines]
    best=(-1,0,0)
    for i in range(len(lines)):
        size=0;total=0;j=i
        while j<len(lines) and (size+len(lines[j])+1<=width or j==i):size+=len(lines[j])+1;total+=score[j];j+=1
        if total>best[0]:best=(total,i,j)
    _,i,j=best;piece='\n'.join(lines[i:j])
    if len(piece)>width:
        hit=next((m.start() for m in re.finditer(r'[^\W_]+',piece) if stem(m[0].casefold()) in wanted),0);a=max(0,hit-width//4);piece=('… ' if a else '')+piece[a:a+width]+' …'
    return piece,line_start+i,line_start+j-1

def _covers(stemset,term):
    from .search import stem
    s=stem(term);return s in stemset or (s[:-1] if s.endswith('e') else s+'e') in stemset or (s[:-1]+'i' if s.endswith('y') else s) in stemset

def search(root,domain,query,limit=5,vocab=None,informative=False,gate=None,least=2,width=SNIPPET):
    """Source excerpts for a query, best first: chunks with every term, then chunks with some, at most two per file and
    none overlapping another. Terms no chunk carries are dropped; informative (the prompt hook) also drops words most
    chunks carry and keeps the 16 rarest. gate (a minimum share of the query's weight a chunk must carry, with `least`
    terms) judges each chunk; without it every match counts. Each hit has its citation (path and line range), symbol or
    heading and a snippet."""
    from .search import terms,stem,df_of,idf_terms
    db=_readonly(root,domain)
    if not db:return []
    try:
        words=list(dict.fromkeys(terms(split_camel(query))))[:32]
        total,df=vocab if vocab and vocab[0] else _vocab(db,words)
        words=idf_terms(split_camel(query),(total,df)) if informative else [w for w in words if df_of(df,w)]
        if not words or (gate is not None and len(words)<max(2,least)):return []   # too few words to vouch for a chunk
        weight={w:math.log((total+1)/(df_of(df,w)+1))+0.1 for w in words}
        wanted={stem(w) for w in words};rows=[];listed=set()
        for joiner in (' AND ',' OR '):
            match=joiner.join('"'+w+'"' for w in words)
            batch=db.execute('SELECT c.id,c.source,c.path,c.kind,c.symbol,c.line_start,c.line_end,f.text,bm25(chunk_fts,6.0,3.0,1.5,1.0) FROM chunk_fts f JOIN chunk c ON c.id=f.rowid WHERE chunk_fts MATCH ? ORDER BY bm25(chunk_fts,6.0,3.0,1.5,1.0) LIMIT 60',(match,)).fetchall()
            rows+=[r for r in batch if r[0] not in listed];listed|={r[0] for r in batch}
            if len(rows)>=limit*3 or len(words)==1:break
        rows=_fuse_vectors(db,root,query,rows)
        roots={n:r for n,r in db.execute('SELECT name,root FROM source')}
        out=[];per_file={};taken=[]
        for cid,source,path,kind,symbol,start,end,text,score in rows[:40]:
            have=_stems(symbol,path,text);hit=[w for w in words if _covers(have,w)]
            share=sum(weight[w] for w in hit)/sum(weight.values())
            if gate is not None and (len(hit)<max(2,least) or share<gate):continue
            if per_file.get((source,path),0)>=2 or any(s==source and p==path and a<=end and start<=b for s,p,a,b in taken):continue
            per_file[(source,path)]=per_file.get((source,path),0)+1;taken.append((source,path,start,end))
            piece,a,b=_window(text,start,wanted,width)
            if kind=='html':a,b=start,end
            out.append({'type':'source','label':'source excerpt','source':source,'path':path,'file':(Path(roots.get(source,''))/path).as_posix(),'kind':kind,'symbol':symbol,'line_start':a,'line_end':b,
                        'citation':f'{path}:{a}-{b}','snippet':piece,'score':round(-score,3),'relevance':round(share,2),'matched':len(hit),'terms':len(words),'rank':len(out)+1})
            if len(out)>=limit:break
        return out
    except sqlite3.Error:return []
    finally:db.close()

def _fuse_vectors(db,root,query,rows):
    """With a model and stored vectors, fuse the lexical order with the candidates' meaning order (reciprocal rank)."""
    try:
        from . import semantic
        model=semantic.load(root)
        if not model or not rows or not _has_vectors(db):return rows
        wanted=model.embed(query)
        if wanted is None:return rows
        blobs=dict(db.execute(f"SELECT rowid,vec FROM vec WHERE rowid IN ({','.join('?'*len(rows))})",[r[0] for r in rows]).fetchall())
        similar=sorted((r for r in rows if blobs.get(r[0])),key=lambda r:-semantic.cosine(wanted,semantic.from_blob(blobs[r[0]])))
        fused={r[0]:1/(60+k) for k,r in enumerate(rows,1)}
        for k,r in enumerate(similar,1):fused[r[0]]+=1/(60+k)
        return sorted(rows,key=lambda r:(-round(fused[r[0]],12),rows.index(r)))
    except Exception:return rows
