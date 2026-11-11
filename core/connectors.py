"""Project knowledge base, layer 3: live wiki connectors (Confluence and Notion), the only code in the store that uses the network.

A `confluence` or `notion` source is registered in sources.json like any other (source-plan add) but holds no folder: `source-sync NAME`
(or `source-sync --all-remote`) pulls its pages into a derived cache, sources-cache/<name>/ (HTML for Confluence, Markdown for Notion, one
file per page, plus manifest.json: {page_id: title, version, url, path}), and the library then indexes that folder like an html-export or
a Markdown folder, with the page title and URL as the citation. Nothing else reaches the network: hooks, the SessionStart background
sync, ask and plain `source-sync` only read what the cache already holds.

The credential is never stored: `token_env` names an environment variable. For Confluence its value is a bearer token (a Data Center or
Server personal access token), or `email:api_token` for Atlassian Cloud, sent as HTTP Basic; for Notion it is the integration secret.
Safety: https only (http for 127.0.0.1 or localhost, for tests), no redirect to another host, 20 s per request, at most 3 retries on
429 and 5xx (honouring Retry-After), at most MAX_PAGES pages, MAX_PAGE_BYTES per page and BUDGET seconds per sync; an error message
never carries the token. Incremental: a page whose version (Confluence) or last_edited_time (Notion) is unchanged is not rewritten; a page
gone upstream is removed from the cache when the listing completed. The cache is derived: not canonical, not backed up, not relocated."""
from __future__ import annotations
import base64,html,json,os,re,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
from .common import atomic_write,file_lock
from .repository import now,read_json,canonical_json

REMOTE=('confluence','notion')
FIELDS={'base_url','token_env','space','root_page','page_ids','database_ids','api_path'}
ALLOWED={'confluence':{'base_url','token_env','space','root_page','page_ids','api_path'},'notion':{'base_url','token_env','page_ids','database_ids'}}
TIMEOUT=20
RETRIES=3
RETRY_CAP=30
MAX_PAGES=5000
MAX_PAGE_BYTES=2<<20
MAX_RESPONSE=32<<20
BUDGET=600
MAX_DEPTH=5
MAX_BLOCKS=5000
PAGE_SIZE=25
NOTION_URL='https://api.notion.com'
NOTION_VERSION='2022-06-28'
CONFLUENCE_API='/wiki/rest/api'
LOCAL_HOSTS=frozenset({'127.0.0.1','localhost','::1'})
EXPAND='body.export_view,version,ancestors'
ENV_NAME=re.compile(r'[A-Z][A-Z0-9_]{1,63}\Z')

class ConnectorError(ValueError):
    """A failed pull, with a message that never carries the token; status is the HTTP status, when there was one."""
    def __init__(self,message,status=None):super().__init__(message);self.status=status

def _sleep(seconds):time.sleep(seconds)   # a seam for the tests

# ---- registry fields ------------------------------------------------------------------------------------------------
def _ids(value,field,pattern,limit=200):
    if not isinstance(value,list) or not 1<=len(value)<=limit or any(not isinstance(v,str) or not re.fullmatch(pattern,v) for v in value):
        raise ValueError(f'{field} must be a list of 1 to {limit} ids')
    return list(dict.fromkeys(value))

def _base_url(value):
    if not isinstance(value,str):raise ValueError('base_url must be a URL string')
    parts=urllib.parse.urlsplit(value.strip())
    if parts.username or parts.password or parts.query or parts.fragment or '@' in parts.netloc:raise ValueError('base_url must not carry a user, password, query or fragment')
    if not parts.hostname:raise ValueError('base_url must name a host')
    if parts.scheme!='https' and not (parts.scheme=='http' and parts.hostname in LOCAL_HOSTS):raise ValueError('base_url must use https (http only for 127.0.0.1 or localhost)')
    return urllib.parse.urlunsplit((parts.scheme,parts.netloc,parts.path.rstrip('/'),'',''))

def validate(kind,payload,strict=True):
    """The connector fields of a confluence or notion source, normalized. strict (a source-plan payload) refuses a field that does not
    belong to the kind (a path, include globs, ...); the pull re-checks a stored entry without it. Raises ValueError."""
    allowed=ALLOWED[kind]
    if strict:
        extra=set(payload)-allowed-{'name','domain','kind'}
        if extra:raise ValueError(f"{', '.join(sorted(extra))} does not apply to a {kind} source (it takes {', '.join(sorted(allowed))})")
    out={}
    if kind=='notion' and payload.get('base_url') is None:out['base_url']=NOTION_URL
    elif payload.get('base_url') is None:raise ValueError('base_url is required (the site, e.g. https://example.atlassian.net)')
    else:out['base_url']=_base_url(payload['base_url'])
    name=payload.get('token_env')
    if not isinstance(name,str) or not ENV_NAME.fullmatch(name):
        raise ValueError('token_env must be the NAME of an environment variable (capital letters, digits and underscores, e.g. CONFLUENCE_TOKEN), never the token itself')
    out['token_env']=name
    if kind=='confluence':
        if payload.get('space') is not None:
            if not isinstance(payload['space'],str) or not re.fullmatch(r'~?[A-Za-z0-9_.-]{1,255}',payload['space']):raise ValueError('space must be a Confluence space key')
            out['space']=payload['space']
        if payload.get('root_page') is not None:
            if not isinstance(payload['root_page'],str) or not re.fullmatch(r'\d{1,20}',payload['root_page']):raise ValueError('root_page must be a Confluence page id (digits)')
            out['root_page']=payload['root_page']
        if payload.get('page_ids') is not None:out['page_ids']=_ids(payload['page_ids'],'page_ids',r'\d{1,20}')
        if not {'space','root_page','page_ids'}&set(out):raise ValueError('a confluence source needs a space, a root_page or page_ids')
        api=payload.get('api_path')
        if api is not None:
            if not isinstance(api,str) or not re.fullmatch(r'/[A-Za-z0-9_./-]{0,100}',api) or '..' in api:raise ValueError('api_path must be a path such as /rest/api (Data Center) or /wiki/rest/api (Cloud, the default)')
            out['api_path']=api.rstrip('/')
    else:
        if payload.get('page_ids') is not None:out['page_ids']=_ids(payload['page_ids'],'page_ids',r'[0-9a-fA-F-]{32,36}')
        if payload.get('database_ids') is not None:out['database_ids']=_ids(payload['database_ids'],'database_ids',r'[0-9a-fA-F-]{32,36}')
        if not {'page_ids','database_ids'}&set(out):raise ValueError('a notion source needs database_ids or page_ids')
    return out

# ---- cache ----------------------------------------------------------------------------------------------------------
def cache_dir(root,name):return Path(root)/'sources-cache'/name
def manifest(root,name):
    """The cache's manifest ({} when there is none or it is unreadable)."""
    try:data=read_json(cache_dir(root,name)/'manifest.json',{})
    except (OSError,ValueError):return {}
    return data if isinstance(data,dict) else {}

def prune(root,keep):
    """Remove cache folders of sources the registry no longer lists."""
    import shutil
    base=Path(root)/'sources-cache'
    for child in base.iterdir() if base.is_dir() else []:
        if child.is_dir() and not child.is_symlink() and child.name not in keep:shutil.rmtree(child,ignore_errors=True)

def summary(root,name):
    """What `sources` shows about a remote source: last successful sync, page counts, skipped pages and the last error."""
    data=manifest(root,name)
    return {'synced':data.get('synced'),'pages':len(data.get('pages') or {}),'skipped':len(data.get('skipped') or {}),'truncated':bool(data.get('truncated')),'error':data.get('error') if isinstance(data.get('error'),dict) else None}

# ---- the HTTP client ------------------------------------------------------------------------------------------------
class _SameOrigin(urllib.request.HTTPRedirectHandler):
    """A redirect keeps the host and scheme: the Authorization header must not follow it elsewhere."""
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if urllib.parse.urlsplit(newurl)[:2]!=urllib.parse.urlsplit(req.full_url)[:2]:raise ConnectorError('refusing a redirect to another host or scheme')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

class Client:
    """JSON over HTTP with the source's credential, timeouts, bounded retries and a deadline for the whole sync."""
    def __init__(self,spec,deadline):
        name=spec['token_env'];token=os.environ.get(name,'').strip()
        if not token:raise ConnectorError(f'environment variable {name} is not set; it must hold the token for source {spec["name"]}')
        if re.search(r'[\x00-\x1f\x7f]',token):raise ConnectorError(f'environment variable {name} holds an invalid token (control characters)')
        self.secrets=[s for s in (token,base64.b64encode(token.encode()).decode()) if s];self.name=name;self.deadline=deadline
        host=urllib.parse.urlsplit(spec['base_url']).hostname
        auth='Basic '+base64.b64encode(token.encode()).decode() if spec['kind']=='confluence' and ':' in token else 'Bearer '+token
        self.headers={'Authorization':auth,'Accept':'application/json','User-Agent':'living-memory-connector'}
        if spec['kind']=='notion':self.headers.update({'Notion-Version':NOTION_VERSION,'Content-Type':'application/json'})
        handlers=[_SameOrigin()]+([urllib.request.ProxyHandler({})] if host in LOCAL_HOSTS else [])
        self.opener=urllib.request.build_opener(*handlers)

    def scrub(self,text):
        for s in self.secrets:text=text.replace(s,'<token>')
        return text

    def get(self,url,body=None):
        """The decoded JSON of one request; 429 and 5xx (and a network error) are retried up to RETRIES times."""
        attempt=0;where=urllib.parse.urlsplit(url);place=f'{where.netloc}{where.path}'
        while True:
            remaining=self.deadline-time.monotonic()
            if remaining<=0:raise ConnectorError('the time budget for this sync ran out; what was fetched is kept, run source-sync again to continue')
            req=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),headers=self.headers,method='GET' if body is None else 'POST')
            delay=min(RETRY_CAP,2**attempt)
            try:
                with self.opener.open(req,timeout=min(TIMEOUT,remaining)) as reply:data=reply.read(MAX_RESPONSE+1)
                if len(data)>MAX_RESPONSE:raise ConnectorError(f'response from {place} is larger than {MAX_RESPONSE>>20} MB')
                try:return json.loads(data)
                except ValueError:raise ConnectorError(f'{place} did not return JSON; check base_url')
            except urllib.error.HTTPError as exc:
                status=exc.code
                if status in (429,500,502,503,504) and attempt<RETRIES:
                    try:delay=min(RETRY_CAP,max(0.0,float(exc.headers.get('Retry-After'))))
                    except (TypeError,ValueError):pass
                else:
                    if status in (401,403):raise ConnectorError(f'authentication failed (HTTP {status}) at {place}; check that ${self.name} holds a valid token with access to the source',status)
                    if status==404:raise ConnectorError(f'not found (HTTP 404): {place}',status)
                    raise ConnectorError(f'HTTP {status} from {place}'+(f' after {RETRIES} retries' if status in (429,500,502,503,504) else ''),status)
            except (urllib.error.URLError,OSError) as exc:
                if attempt>=RETRIES:raise ConnectorError(self.scrub(f'cannot reach {where.netloc} after {RETRIES} retries: {getattr(exc,"reason",exc)}'))
            if delay>=self.deadline-time.monotonic():raise ConnectorError('the time budget for this sync ran out while waiting to retry')
            attempt+=1;_sleep(delay)

# ---- Confluence -----------------------------------------------------------------------------------------------------
def _confluence_page(spec,r,top=''):
    links=r.get('_links') or {};version=r.get('version') or {};body=((r.get('body') or {}).get('export_view') or {}).get('value')
    api=spec.get('api_path',CONFLUENCE_API);site=links.get('base') or top or spec['base_url']+(api[:-len('/rest/api')] if api.endswith('/rest/api') else '')
    web=links.get('webui') or ''
    url=(site.rstrip('/')+web) if web.startswith('/') and str(site).startswith(('http://','https://')) else ''
    return {'id':str(r['id']),'title':str(r.get('title') or r['id']),'version':str(version.get('number','')),'modified':version.get('when'),'url':url,
            'ancestors':[str(a.get('title','')) for a in r.get('ancestors') or [] if isinstance(a,dict)],'body':body if isinstance(body,str) else ''}

def _confluence_list(client,spec,first):
    """Pages of a content listing (space or descendants), following _links.next by start offset."""
    start=0
    while True:
        data=client.get(f'{first}&limit={PAGE_SIZE}&start={start}');results=data.get('results') or []
        for r in results:
            if isinstance(r,dict) and 'id' in r:yield _confluence_page(spec,r,(data.get('_links') or {}).get('base',''))
        if not results or not (data.get('_links') or {}).get('next'):return
        start+=len(results)

def _confluence(client,spec):
    api=spec['base_url']+spec.get('api_path',CONFLUENCE_API);missing=spec.setdefault('missing',[])
    def one(pid):
        try:return _confluence_page(spec,client.get(f'{api}/content/{pid}?expand={EXPAND}'))
        except ConnectorError as exc:
            if exc.status==404:missing.append(pid);return None
            raise
    if spec.get('space'):yield from _confluence_list(client,spec,f"{api}/content?spaceKey={urllib.parse.quote(spec['space'])}&type=page&expand={EXPAND}")
    if spec.get('root_page'):
        page=one(spec['root_page'])
        if page:yield page
        yield from _confluence_list(client,spec,f"{api}/content/{spec['root_page']}/descendant/page?expand={EXPAND}")
    for pid in spec.get('page_ids') or []:
        page=one(pid)
        if page:yield page

# ---- Notion ---------------------------------------------------------------------------------------------------------
def _rich(items):
    out=''
    for t in items or []:
        text=t.get('plain_text') or '' if isinstance(t,dict) else ''
        if not text:continue
        a=t.get('annotations') or {}
        if a.get('code'):text='`'+text+'`'
        else:
            if a.get('bold'):text='**'+text+'**'
            if a.get('italic'):text='*'+text+'*'
            if a.get('strikethrough'):text='~~'+text+'~~'
        out+=f"[{text}]({t['href']})" if t.get('href') else text
    return out

def _plain(items):return ''.join(t.get('plain_text') or '' for t in items or [] if isinstance(t,dict))

def _table(rows,data):
    cells=[[_rich(c).replace('|','\\|').replace('\n',' ') for c in (r.get('table_row') or {}).get('cells') or []] for r in rows if r.get('type')=='table_row']
    if not cells:return []
    width=max(len(r) for r in cells);cells=[r+['']*(width-len(r)) for r in cells]
    header,rest=(cells[0],cells[1:]) if data.get('has_column_header') else ([' ']*width,cells)
    return ['| '+' | '.join(header)+' |','|'+' --- |'*width]+['| '+' | '.join(r)+' |' for r in rest]

LISTS=frozenset({'bulleted_list_item','numbered_list_item','to_do','toggle'})
def notion_markdown(blocks,indent=''):
    """Markdown for Notion blocks (children already fetched under each block's 'children'): paragraphs, headings, lists, to-dos, code,
    quotes, callouts, toggles (as bullets), tables, dividers, links to sub-pages and embeds; unsupported blocks keep their text."""
    out=[];number=0;previous_list=False
    for b in blocks:
        if not isinstance(b,dict):continue
        t=b.get('type') or '';d=b.get(t) if isinstance(b.get(t),dict) else {};kids=b.get('children') or [];text=_rich(d.get('rich_text'));number=number+1 if t=='numbered_list_item' else 0
        child=indent;lines=[]
        if t=='bulleted_list_item':lines=[f'{indent}- {text}'];child=indent+'  '
        elif t=='numbered_list_item':lines=[f'{indent}{number}. {text}'];child=indent+'   '
        elif t=='to_do':lines=[f"{indent}- [{'x' if d.get('checked') else ' '}] {text}"];child=indent+'  '
        elif t=='toggle':lines=[f'{indent}- {text}'];child=indent+'  '
        elif t in {'heading_1','heading_2','heading_3'}:lines=[f"{indent}{'#'*int(t[-1])} {text}"]
        elif t=='code':
            language=d.get('language') or '';lines=[f"{indent}```{'' if language=='plain text' else language}"]+[indent+l for l in _plain(d.get('rich_text')).split('\n')]+[indent+'```']
        elif t=='quote':lines=[f'{indent}> {text}'];child=indent+'> '
        elif t=='callout':icon=(d.get('icon') or {}).get('emoji') or '';lines=[f"{indent}> {icon+' ' if icon else ''}{text}"];child=indent+'> '
        elif t=='divider':lines=[indent+'---']
        elif t=='table':lines=[indent+l for l in _table(kids,d)];kids=[]
        elif t in {'child_page','child_database'}:lines=[f"{indent}- (sub-page) {d.get('title') or ''}"];kids=[]
        elif t in {'bookmark','embed','link_preview'} and d.get('url'):lines=[f"{indent}[{d['url']}]({d['url']})"]
        elif t=='equation':lines=[f"{indent}$$ {d.get('expression') or ''} $$"]
        elif t in {'image','file','pdf','video','audio'} and _rich(d.get('caption')):lines=[f"{indent}[{t}: {_rich(d.get('caption'))}]"]
        elif text:lines=[indent+text]
        is_list=t in LISTS
        if lines:
            if out and not (is_list and previous_list):out.append('')
            out+=lines;previous_list=is_list
        if kids:
            inner=notion_markdown(kids,child)
            if inner:
                if out and not (lines and (is_list or t in {'quote','callout'})):out.append('')
                out+=inner.split('\n');previous_list=is_list
    return '\n'.join(out)

def _notion_title(page):
    for p in (page.get('properties') or {}).values():
        if isinstance(p,dict) and p.get('type')=='title':return _plain(p.get('title')).strip()
    return ''

def _children(client,base,block_id,depth,count):
    """The blocks under one block or page, following next_cursor and recursing into children up to MAX_DEPTH levels and MAX_BLOCKS blocks."""
    blocks=[];cursor=None
    while True:
        data=client.get(f"{base}/v1/blocks/{block_id}/children?page_size=100"+(f'&start_cursor={urllib.parse.quote(cursor)}' if cursor else ''))
        for b in data.get('results') or []:
            if not isinstance(b,dict):continue
            count[0]+=1
            if count[0]>MAX_BLOCKS:
                blocks.append({'type':'paragraph','paragraph':{'rich_text':[{'plain_text':f'[page cut here: more than {MAX_BLOCKS} blocks]'}]}});return blocks
            if b.get('has_children') and depth<MAX_DEPTH and b.get('type') not in {'child_page','child_database'}:b['children']=_children(client,base,b['id'],depth+1,count)
            blocks.append(b)
        if not data.get('has_more') or not data.get('next_cursor'):return blocks
        cursor=data['next_cursor']

def _notion_page(client,base,p):
    pid=str(p['id']);return {'id':pid,'title':_notion_title(p) or pid,'version':str(p.get('last_edited_time') or ''),'modified':p.get('last_edited_time'),'url':str(p.get('url') or ''),'ancestors':[],
                             'load':lambda:notion_markdown(_children(client,base,pid,0,[0]))}

def _notion(client,spec):
    base=spec['base_url'];missing=spec.setdefault('missing',[])
    for db in spec.get('database_ids') or []:
        cursor=None
        while True:
            data=client.get(f'{base}/v1/databases/{db}/query',dict({'page_size':100},**({'start_cursor':cursor} if cursor else {})))
            for p in data.get('results') or []:
                if isinstance(p,dict) and p.get('object','page')=='page' and 'id' in p and not (p.get('archived') or p.get('in_trash')):yield _notion_page(client,base,p)
            if not data.get('has_more') or not data.get('next_cursor'):break
            cursor=data['next_cursor']
    for pid in spec.get('page_ids') or []:
        try:p=client.get(f'{base}/v1/pages/{pid}')
        except ConnectorError as exc:
            if exc.status==404:missing.append(pid);continue
            raise
        if isinstance(p,dict) and 'id' in p and not (p.get('archived') or p.get('in_trash')):yield _notion_page(client,base,p)

# ---- the pull -------------------------------------------------------------------------------------------------------
def _file(page,kind):
    slug=re.sub(r'[^a-z0-9]+','-',page['title'].lower()).strip('-')[:40] or 'page'
    return f"{slug}-{re.sub(r'[^A-Za-z0-9_-]','',page['id'])[:64]}"+('.html' if kind=='confluence' else '.md')

def _render(page,kind,content):
    if kind=='notion':return f"# {page['title']}\n\n{content}\n"
    return f"<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>{html.escape(page['title'])}</title></head><body>\n{content}\n</body></html>\n"

def pull(root,spec,full=False):
    """Fetch one remote source into its cache and return what changed: {name, fetched, unchanged, removed, skipped, missing, truncated, pages, error}.
    Only pages whose version differs are fetched in full and rewritten (full: all of them); with a complete listing, cached pages that are
    gone upstream are removed. A failure keeps what was fetched, is recorded in the manifest (shown by `sources`) and is returned as `error`."""
    from .sources import redact_text
    name=spec['name'];kind=spec['kind'];folder=cache_dir(root,name)
    result={'name':name,'fetched':0,'unchanged':0,'removed':0,'skipped':0,'missing':[],'truncated':False,'redacted':0,'error':None}
    try:
        with file_lock(Path(root)/'hooks'/f'sources-pull-{name}.lock',timeout=0):
            old=manifest(root,name);pages=dict(old.get('pages') or {});skipped={};seen=set();complete=False;folder.mkdir(parents=True,exist_ok=True)
            try:
                fields=validate(kind,spec,strict=False);work=dict(spec,**fields);client=Client(work,time.monotonic()+BUDGET)
                for page in (_confluence if kind=='confluence' else _notion)(client,work):
                    if len(seen)>=MAX_PAGES:result['truncated']=True;break
                    pid=page['id'];seen.add(pid);rel=_file(page,kind);prev=pages.get(pid)
                    if prev and not full and prev.get('version')==page['version'] and prev.get('path')==rel and (folder/rel).is_file():
                        pages[pid]=dict(prev,title=page['title'],url=page['url'],modified=page['modified']);result['unchanged']+=1;continue
                    text,redacted=redact_text(_render(page,kind,page['body'] if 'body' in page else page['load']()))
                    if prev and prev.get('path')!=rel:(folder/prev['path']).unlink(missing_ok=True)
                    if len(text.encode())>MAX_PAGE_BYTES:
                        skipped[pid]={'title':page['title'],'reason':f'larger than {MAX_PAGE_BYTES>>20} MB'};result['skipped']+=1
                        if prev:(folder/prev['path']).unlink(missing_ok=True);pages.pop(pid,None)
                        continue
                    atomic_write(folder/rel,text);result['redacted']+=redacted;result['fetched']+=1
                    pages[pid]={'title':page['title'],'version':page['version'],'modified':page['modified'],'url':page['url'],'path':rel,'ancestors':page['ancestors']}
                else:complete=True
                result['missing']=work.get('missing',[])
            except ConnectorError as exc:result['error']=str(exc)
            except (OSError,ValueError) as exc:result['error']=f'{type(exc).__name__}: {exc}'[:300]
            if complete and not result['truncated']:   # the whole listing was seen: what it no longer has is gone upstream
                for pid in [p for p in pages if p not in seen]:
                    (folder/pages[pid]['path']).unlink(missing_ok=True);del pages[pid];result['removed']+=1
            failed=result['error']
            atomic_write(folder/'manifest.json',canonical_json({'version':1,'source':name,'kind':kind,'synced':old.get('synced') if failed else now(),'attempted':now(),
                         'error':{'at':now(),'message':failed} if failed else None,'truncated':result['truncated'],'pages':dict(sorted(pages.items())),'skipped':skipped}))
            result['pages']=len(pages)
    except TimeoutError:result['error']='another sync of this source is running'
    return result
