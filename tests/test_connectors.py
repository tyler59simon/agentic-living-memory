"""Project knowledge base, layer 3: the Confluence and Notion connectors against a local http.server (127.0.0.1, ephemeral port; no real network):
registry validation, pagination, auth, retries, caps, the incremental cache, indexing with page title and URL citations, and the guarantee that
only an explicit `source-sync NAME` (or --all-remote) reaches the network: never a hook, never the background sync, never auto-allowed."""
import base64,contextlib,http.server,io,itertools,json,os,re,sys,threading,unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from test_core import StoreFixture
from core import backup,connectors,runtime,sources,storage
from core.cli import main

TOKEN='wikitoken0123456789'
SERVER_CAP=2   # the server returns at most this many items per request, whatever limit or page_size is asked for
IDS=itertools.count(1000)

def nid(n):return f'{n:032x}'
def rt(text,**annotations):
    href=annotations.pop('href',None);return {'plain_text':text,'annotations':annotations,'href':href}
def block(kind,text='',children=None,**data):
    """A Notion block as the API returns it; `kids` (not part of the API) holds the blocks the server serves for its children."""
    b={'id':nid(next(IDS)),'type':kind,kind:dict(({'rich_text':[rt(text)]} if text else {}),**data)}
    if children:b['kids']=children
    return b

class Fake:
    """What the fixture server knows, and what it saw."""
    def __init__(self):
        self.token=TOKEN;self.log=[];self.faults=[];self.pages={};self.databases={};self.notion_pages={};self.blocks={};self.redirect=None
    def requests(self,prefix=''):return [r for r in self.log if r[1].startswith(prefix)]

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,code,body=None,headers=None):
        data=json.dumps(body if body is not None else {}).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(data)
    def do_GET(self):self.handle_request('GET')
    def do_POST(self):self.handle_request('POST')
    def handle_request(self,method):
        fake=self.server.fake;url=urlsplit(self.path);q={k:v[0] for k,v in parse_qs(url.query).items()};length=int(self.headers.get('Content-Length') or 0);body=json.loads(self.rfile.read(length)) if length else None
        auth=self.headers.get('Authorization');fake.log.append((method,url.path,q,auth,self.headers.get('Notion-Version'),body))
        if auth not in ('Bearer '+fake.token,'Basic '+base64.b64encode(fake.token.encode()).decode()):return self.reply(401,{'message':f'invalid credentials {auth}'})
        for fault in list(fake.faults):
            if url.path.startswith(fault[0]):fake.faults.remove(fault);return self.reply(fault[1],{'message':'try later'},fault[2])
        if fake.redirect and url.path==fake.redirect[0]:self.send_response(302);self.send_header('Location',fake.redirect[1]);self.send_header('Content-Length','0');self.end_headers();return
        base=f'http://127.0.0.1:{self.server.server_port}'
        if url.path.startswith('/wiki/rest/api/content'):return self.confluence(fake,url.path,q,base)
        if url.path.startswith('/v1/'):return self.notion(fake,url.path,q,body)
        self.reply(404,{'message':'no such route'})
    def page(self,fake,p,base):
        return {'id':p['id'],'type':'page','title':p['title'],'version':{'number':p['version'],'when':f"2026-01-0{p['version']}T10:00:00Z"},'ancestors':[{'title':'Handbook'}],
                'body':{'export_view':{'value':p['html']}},'_links':{'webui':f"/spaces/{p.get('space','ENG')}/pages/{p['id']}/{p['title'].replace(' ','+')}"}}
    def confluence(self,fake,path,q,base):
        links={'base':base+'/wiki'}
        single=re.fullmatch(r'/wiki/rest/api/content/(\d+)',path);below=re.fullmatch(r'/wiki/rest/api/content/(\d+)/descendant/page',path)
        if single:
            p=fake.pages.get(single[1])
            if not p:return self.reply(404,{'message':'gone'})
            item=self.page(fake,p,base);item['_links']['base']=base+'/wiki';return self.reply(200,item)
        if below:
            def under(p):
                while p:
                    if p.get('parent')==below[1]:return True
                    p=fake.pages.get(p.get('parent'))
                return False
            chosen=[p for p in fake.pages.values() if under(p)]
        else:chosen=[p for p in fake.pages.values() if p.get('space','ENG')==q.get('spaceKey') and p.get('status','current')=='current']
        start=int(q.get('start',0));limit=min(int(q.get('limit',25)),SERVER_CAP);part=chosen[start:start+limit]
        if start+limit<len(chosen):links['next']='/wiki/rest/api/content?next'
        self.reply(200,{'results':[self.page(fake,p,base) for p in part],'start':start,'limit':limit,'size':len(part),'_links':links})
    def notion(self,fake,path,q,body):
        query=re.fullmatch(r'/v1/databases/(\w+)/query',path);children=re.fullmatch(r'/v1/blocks/(\w+)/children',path);one=re.fullmatch(r'/v1/pages/(\w+)',path)
        def page(pid):
            p=fake.notion_pages[pid];return {'object':'page','id':pid,'last_edited_time':p['edited'],'url':p['url'],'archived':p.get('archived',False),'properties':{'Name':{'type':'title','title':[rt(p['title'])]},'Tags':{'type':'multi_select'}}}
        if query:
            ids=fake.databases.get(query[1])
            if ids is None:return self.reply(404,{'message':'no such database'})
            start=int((body or {}).get('start_cursor') or 0);part=ids[start:start+SERVER_CAP]
            return self.reply(200,{'object':'list','results':[page(i) for i in part],'has_more':start+SERVER_CAP<len(ids),'next_cursor':str(start+SERVER_CAP) if start+SERVER_CAP<len(ids) else None})
        if one:return self.reply(200,page(one[1])) if one[1] in fake.notion_pages else self.reply(404,{'message':'no such page'})
        if children:
            blocks=fake.blocks.get(children[1],[]);start=int(q.get('start_cursor') or 0);part=blocks[start:start+SERVER_CAP]
            return self.reply(200,{'object':'list','results':[{k:v for k,v in b.items() if k!='kids'}|{'has_children':b['id'] in fake.blocks} for b in part],'has_more':start+SERVER_CAP<len(blocks),'next_cursor':str(start+SERVER_CAP) if start+SERVER_CAP<len(blocks) else None})
        self.reply(404,{'message':'no such route'})

class Wiki(StoreFixture):
    """A bound domain, the fixture server and a token in the environment; helpers to register and sync sources."""
    def setUp(self):
        super().setUp();self.project=self.base/'project';self.project.mkdir()
        self.fake=Fake();self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.fake=self.fake
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(lambda:(self.server.shutdown(),self.server.server_close(),self.thread.join()))
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        env=patch.dict(os.environ,{'WIKI_TOKEN':TOKEN});env.start();self.addCleanup(env.stop)
        self.sleeps=[];sleep=patch.object(connectors,'_sleep',self.sleeps.append);sleep.start();self.addCleanup(sleep.stop)
        self.fake.pages={'100':{'id':'100','title':'Refund Policy','version':1,'html':'<h1>Refund Policy</h1><p>Customers get a pelican refund within 45 days.</p><table><tr><td>EUR</td><td>same day</td></tr></table>'},
                         '101':{'id':'101','title':'On-call Rota','version':1,'html':'<h2>Rota</h2><p>The wombat rotation changes every Monday.</p>'},
                         '102':{'id':'102','title':'Deploy Guide','version':1,'html':'<p>Deploys use the narwhal pipeline.</p>'},
                         '103':{'id':'103','title':'Old Page','version':1,'html':'<p>The quokka page will be deleted.</p>'},
                         '104':{'id':'104','title':'Child Page','version':1,'html':'<p>Child of the deploy guide.</p>','parent':'102','space':'OTHER'},
                         '105':{'id':'105','title':'Grandchild','version':1,'html':'<p>Deeper than the child.</p>','parent':'104','space':'OTHER'}}
    def cli(self,*argv,stdin=None,domain='demo'):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',io.StringIO(stdin or '')):
            try:code=main(['--root',str(self.root),*(['--domain',domain] if domain else []),*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue();return code,(json.loads(text) if text.strip().startswith('{') else text)
    def register(self,**kw):
        value={k:v for k,v in {'name':'wiki','kind':'confluence','base_url':self.url,'token_env':'WIKI_TOKEN','space':'ENG',**kw}.items() if v is not None}
        code,plan=self.cli('source-plan','add',stdin=json.dumps(value));self.assertEqual(code,0,plan)
        code,done=self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest']);self.assertEqual(code,0,done);return plan
    def sync(self,*argv):return self.cli('source-sync',*(argv or ('wiki',)))
    def cache(self,name='wiki'):return connectors.cache_dir(self.root,name)
    def manifest(self,name='wiki'):return json.loads((self.cache(name)/'manifest.json').read_text(encoding='utf-8'))

class Registry(Wiki):
    def test_a_confluence_and_a_notion_source_hold_the_variable_name_and_never_the_token(self):
        self.register()
        code,plan=self.cli('source-plan','add',stdin=json.dumps({'name':'notes','kind':'notion','token_env':'NOTION_TOKEN','database_ids':[nid(1)],'page_ids':[nid(2)]}));self.assertEqual(code,0,plan)
        self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest'])
        text=(self.root/'sources.json').read_text(encoding='utf-8');self.assertNotIn(TOKEN,text);entries={e['name']:e for e in json.loads(text)['sources']}
        self.assertEqual((entries['wiki']['kind'],entries['wiki']['token_env'],entries['wiki']['space'],entries['wiki']['base_url']),('confluence','WIKI_TOKEN','ENG',self.url))
        self.assertEqual((entries['notes']['base_url'],entries['notes']['database_ids'],entries['notes']['page_ids']),('https://api.notion.com',[nid(1)],[nid(2)]));self.assertNotIn('path',entries['wiki'])
        rows={r['name']:r for r in self.cli('sources')[1]['sources']};self.assertEqual((rows['wiki']['kind'],rows['wiki']['state'],rows['wiki']['remote']['pages']),('confluence','not synced',0))
    def test_validation(self):
        ok={'name':'wiki','kind':'confluence','base_url':'https://example.atlassian.net','token_env':'WIKI_TOKEN','space':'ENG'}
        cases=((dict(ok,base_url='http://example.com'),'must use https'),(dict(ok,base_url='ftp://example.com'),'must use https'),(dict(ok,base_url='https://user:pw@example.com'),'must not carry'),
               (dict(ok,base_url='https://example.com/?q=1'),'must not carry'),(dict(ok,base_url='example.com'),'must name a host'),(dict(ok,base_url=None),'base_url is required'),
               (dict(ok,token_env=None),'NAME of an environment variable'),(dict(ok,token_env='lowercase_name'),'NAME of an environment variable'),(dict(ok,token_env='ATATT3xFfGF0abcdefghijklmnop'),'NAME of an environment variable'),
               (dict(ok,token_env='me@example.com:abcdefgh'),'NAME of an environment variable'),(dict(ok,token_env='sk-abcdefghijklmnopqrstuvwxyz0123'),'credential'),(dict(ok,token_env='AKIAIOSFODNN7EXAMPLE'),'credential'),
               (dict(ok,space='ENG',page_ids=['AKIAIOSFODNN7EXAMPLE']),'credential'),(dict(ok,space=None),'needs a space'),(dict(ok,space='bad space'),'space must be'),(dict(ok,root_page='abc'),'root_page must be'),
               (dict(ok,page_ids='100'),'page_ids must be a list'),(dict(ok,database_ids=[nid(1)]),'does not apply to a confluence'),(dict(ok,path=str(self.project)),'does not apply to a confluence'),
               (dict(ok,include=['*.md']),'does not apply to a confluence'),(dict(ok,token='x'),'Unknown payload fields'),(dict(ok,api_path='rest/api'),'api_path must be'),(dict(ok,api_path='/a/../b'),'api_path must be'),
               ({'name':'n','kind':'notion','token_env':'NOTION_TOKEN'},'needs database_ids or page_ids'),({'name':'n','kind':'notion','token_env':'NOTION_TOKEN','database_ids':['not-an-id']},'database_ids must be'),
               ({'name':'n','kind':'notion','token_env':'NOTION_TOKEN','database_ids':[nid(1)],'space':'ENG'},'does not apply to a notion'),
               ({'name':'f','kind':'folder','path':str(self.project),'base_url':'https://example.com'},'apply to confluence and notion sources only'))
        for payload,message in cases:
            with self.subTest(message=message,payload={k:v for k,v in payload.items() if 'token' not in k}):
                code,error=self.cli('source-plan','add',stdin=json.dumps(payload));self.assertEqual(code,2,error);self.assertIn(message,str(error));self.assertNotIn('sk-abcdefgh',str(error))
        for good in (ok,dict(ok,base_url='http://localhost:8090/wiki/'),dict(ok,base_url='http://127.0.0.1:1'),dict(ok,space=None,root_page='123'),dict(ok,space=None,page_ids=['1','2']),dict(ok,api_path='/rest/api')):
            with self.subTest(good=good['base_url']):self.assertEqual(self.cli('source-plan','add',stdin=json.dumps({k:v for k,v in good.items() if v is not None}))[0],0)
    def test_a_removed_source_loses_its_cache_at_the_next_full_sync(self):
        self.register();self.assertEqual(self.sync()[0],0);self.assertTrue(self.cache().is_dir())
        code,plan=self.cli('source-plan','remove',stdin='{"name":"wiki"}');self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest'])
        self.assertEqual(self.cli('source-sync')[0],0);self.assertFalse(self.cache().exists())

class Confluence(Wiki):
    def setUp(self):super().setUp();self.register()
    def test_pull_paginates_writes_a_cache_and_indexes_it_with_title_and_url_citations(self):
        code,result=self.sync();self.assertEqual(code,0,result);self.assertEqual(result['pulled'][0]['fetched'],4)
        listing=self.fake.requests('/wiki/rest/api/content');self.assertEqual(len(listing),2);self.assertEqual([r[2]['start'] for r in listing],['0','2'])   # four pages, two per request
        first=listing[0];self.assertEqual((first[2]['spaceKey'],first[2]['type'],first[2]['expand'],first[2]['limit']),('ENG','page','body.export_view,version,ancestors',str(connectors.PAGE_SIZE)))
        self.assertEqual({r[3] for r in self.fake.log},{'Bearer '+TOKEN})
        manifest=self.manifest();self.assertEqual(set(manifest['pages']),{'100','101','102','103'});page=manifest['pages']['100']
        self.assertEqual((page['title'],page['version'],page['url'],page['path'],page['ancestors']),('Refund Policy','1',f'{self.url}/wiki/spaces/ENG/pages/100/Refund+Policy','refund-policy-100.html',['Handbook']))
        self.assertEqual(sorted(p.name for p in self.cache().iterdir()),['deploy-guide-102.html','manifest.json','old-page-103.html','on-call-rota-101.html','refund-policy-100.html'])
        self.assertIn('pelican refund',(self.cache()/'refund-policy-100.html').read_text(encoding='utf-8'))
        hit=self.cli('ask','pelican refund days')[1]['sources'][0]
        self.assertEqual((hit['source'],hit['path'],hit['title'],hit['url']),('wiki','refund-policy-100.html','Refund Policy',f'{self.url}/wiki/spaces/ENG/pages/100/Refund+Policy'))
        self.assertEqual(hit['citation'],f"Refund Policy <{hit['url']}>");self.assertEqual(hit['file'],hit['url']);self.assertIn('45 days',hit['snippet'])
        self.assertEqual(self.cli('ask','wombat rotation monday')[1]['sources'][0]['title'],'On-call Rota')
        self.assertEqual(self.cli('search','--sources','narwhal pipeline')[1]['results'][0]['url'],f'{self.url}/wiki/spaces/ENG/pages/102/Deploy+Guide')
        row=next(r for r in self.cli('sources')[1]['sources'] if r['name']=='wiki');self.assertEqual((row['state'],row['files'],row['remote']['pages'],row['remote']['error']),('indexed',4,4,None));self.assertTrue(row['remote']['synced'])
    def test_basic_auth_when_the_variable_holds_email_and_token(self):
        self.fake.token='me@example.com:'+TOKEN
        with patch.dict(os.environ,{'WIKI_TOKEN':self.fake.token}):self.assertEqual(self.sync()[0],0)
        self.assertEqual({r[3] for r in self.fake.log},{'Basic '+base64.b64encode(self.fake.token.encode()).decode()})
    def test_a_second_sync_skips_unchanged_pages_and_a_new_version_and_a_deletion_are_followed(self):
        self.sync();stamps={p.name:p.stat().st_mtime_ns for p in self.cache().glob('*.html')}
        code,again=self.sync();self.assertEqual((again['pulled'][0]['fetched'],again['pulled'][0]['unchanged'],again['pulled'][0]['removed']),(0,4,0));self.assertEqual({p.name:p.stat().st_mtime_ns for p in self.cache().glob('*.html')},stamps)
        self.fake.pages['101'].update(version=2,html='<h2>Rota</h2><p>The wombat rotation now changes every Friday.</p>');self.fake.pages['103']['status']='trashed';self.fake.pages['102'].update(title='Deployment Guide',version=2)
        code,third=self.sync();self.assertEqual((third['pulled'][0]['fetched'],third['pulled'][0]['unchanged'],third['pulled'][0]['removed']),(2,1,1))
        self.assertEqual(sorted(p.name for p in self.cache().glob('*.html')),['deployment-guide-102.html','on-call-rota-101.html','refund-policy-100.html'])   # a renamed page replaces its file
        self.assertNotIn('103',self.manifest()['pages']);self.assertEqual(self.manifest()['pages']['101']['version'],'2')
        self.assertEqual(self.cli('ask','quokka page deleted')[1]['sources'],[]);self.assertIn('Friday',self.cli('ask','wombat rotation friday')[1]['sources'][0]['snippet'])
        self.assertEqual([r['title'] for r in self.cli('ask','narwhal pipeline')[1]['sources']],['Deployment Guide'])
        self.assertEqual(sources.state(self.root,'demo')['files'],3)
    def test_rebuild_of_the_source_fetches_every_page_again(self):
        self.sync();code,again=self.sync('wiki','--rebuild');self.assertEqual((code,again['pulled'][0]['fetched'],again['pulled'][0]['unchanged']),(0,4,0))
    def test_429_honours_retry_after_and_a_500_is_retried_with_backoff(self):
        self.fake.faults+=[('/wiki/rest/api/content',429,{'Retry-After':'2'}),('/wiki/rest/api/content',500,{}),('/wiki/rest/api/content',503,{'Retry-After':'soon'})]
        code,result=self.sync();self.assertEqual(code,0,result);self.assertEqual(result['pulled'][0]['fetched'],4);self.assertEqual(self.sleeps,[2.0,2,4])   # Retry-After, then 2**attempt
    def test_retries_stop_after_three_and_the_error_is_recorded_without_losing_the_cache(self):
        self.sync();self.fake.pages['101'].update(version=2);self.fake.faults+=[('/wiki/rest/api/content',500,{})]*5
        code,error=self.sync();self.assertEqual(code,2);self.assertIn('HTTP 500',str(error));self.assertIn('after 3 retries',str(error));self.assertEqual(len(self.sleeps),3);self.assertNotIn(TOKEN,str(error))
        self.assertEqual(len(self.fake.requests('/wiki/rest/api/content')),2+4);self.assertEqual(len(list(self.cache().glob('*.html'))),4)   # nothing was removed
        self.assertIn('HTTP 500',self.manifest()['error']['message']);self.assertTrue(self.manifest()['synced'])
        listed=self.cli('sources')[1];row=next(r for r in listed['sources'] if r['name']=='wiki');self.assertIn('HTTP 500',row['remote']['error']['message']);self.assertTrue(any('last remote sync failed' in w for w in listed['warnings']))
        self.assertTrue(any(w.startswith('sources: wiki: last remote sync failed') for w in self.cli('doctor')[1]['warnings']))
        self.assertEqual(self.cli('ask','pelican refund days')[1]['sources'][0]['title'],'Refund Policy')   # still answers from the cache
    def test_a_wrong_token_is_a_clear_error_that_never_shows_it(self):
        for secret in ('wrongtoken9876543210','me@example.com:wrongapitoken'):
            with self.subTest(secret=secret),patch.dict(os.environ,{'WIKI_TOKEN':secret}):
                code,error=self.sync();self.assertEqual(code,2);self.assertIn('authentication failed (HTTP 401)',str(error));self.assertIn('$WIKI_TOKEN',str(error))
                for leak in (secret,base64.b64encode(secret.encode()).decode(),'wrongapitoken'):
                    self.assertNotIn(leak,str(error));self.assertNotIn(leak,(self.cache()/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(self.sleeps,[])   # a 401 is not retried
    def test_a_missing_or_malformed_variable_is_reported_by_name(self):
        with patch.dict(os.environ,{'WIKI_TOKEN':''}):code,error=self.sync();self.assertEqual(code,2);self.assertIn('WIKI_TOKEN is not set',str(error))
        with patch.dict(os.environ,{'WIKI_TOKEN':'bad\ntoken'}):code,error=self.sync();self.assertIn('invalid token',str(error));self.assertNotIn('badtoken',str(error))
        self.assertEqual(self.fake.log,[])
    def test_the_page_count_the_page_size_and_the_time_budget_are_capped(self):
        with patch.object(connectors,'MAX_PAGES',3):code,result=self.sync()
        pulled=result['pulled'][0];self.assertEqual((code,pulled['fetched'],pulled['truncated']),(0,3,True));self.assertTrue(self.manifest()['truncated'])
        self.assertTrue(any('only the first' in w for w in self.cli('sources')[1]['warnings']))
        self.assertEqual(self.sync()[1]['pulled'][0]['truncated'],False)   # without the cap the rest arrives
        self.fake.pages['100'].update(version=2,html='<p>'+'x'*200+'</p>')
        with patch.object(connectors,'MAX_PAGE_BYTES',150):code,result=self.sync()
        self.assertEqual((result['pulled'][0]['skipped'],result['pulled'][0]['removed']),(1,0));self.assertFalse((self.cache()/'refund-policy-100.html').exists());self.assertIn('100',self.manifest()['skipped'])
        with patch.object(connectors,'BUDGET',0):code,error=self.sync();self.assertEqual(code,2);self.assertIn('time budget',str(error))
    def test_a_truncated_listing_never_deletes_cached_pages(self):
        self.sync();self.fake.pages['103']['status']='trashed'
        with patch.object(connectors,'MAX_PAGES',2):self.sync()
        self.assertIn('103',self.manifest()['pages']);self.assertEqual(self.sync()[1]['pulled'][0]['removed'],1)
    def test_a_redirect_to_another_host_is_refused(self):
        self.fake.redirect=('/wiki/rest/api/content','http://localhost:%d/elsewhere'%self.server.server_port)
        code,error=self.sync();self.assertEqual(code,2);self.assertIn('refusing a redirect',str(error));self.assertEqual(len(self.fake.log),1)
    def test_secrets_in_a_page_are_redacted_in_the_cache(self):
        self.fake.pages['101']['html']='<p>Key is AKIAIOSFODNN7EXAMPLE for the wombat vault.</p>';self.sync()
        text=(self.cache()/'on-call-rota-101.html').read_text(encoding='utf-8');self.assertNotIn('AKIAIOSFODNN7EXAMPLE',text);self.assertIn('redacted',text)
    def test_the_cache_is_derived_data_outside_backups_and_relocation(self):
        self.sync();self.assertFalse([k for k in storage._manifest(self.root) if 'sources-cache' in k]);self.assertFalse([f for f in backup.canonical_files(self.root) if 'sources-cache' in f])
        self.assertIn('sources.json',backup.canonical_files(self.root))
    def test_ingest_scan_lists_the_cached_pages_as_wiki_files(self):
        self.sync();found=self.cli('ingest-scan')[1];self.assertEqual({i['kind'] for i in found['items']},{'wiki'});self.assertTrue(all(i['path'].startswith('wiki:') for i in found['items']))

class ConfluenceModes(Wiki):
    def test_a_root_page_pulls_it_and_its_descendants_and_page_ids_pull_single_pages(self):
        self.register(space=None,root_page='102');code,result=self.sync();self.assertEqual((code,result['pulled'][0]['fetched']),(0,3),result)
        self.assertEqual(set(self.manifest()['pages']),{'102','104','105'});self.assertTrue(self.fake.requests('/wiki/rest/api/content/102/descendant/page'))
    def test_page_ids_and_a_page_gone_upstream_is_not_an_error(self):
        self.register(space=None,page_ids=['100','999']);code,result=self.sync();self.assertEqual((code,result['pulled'][0]['fetched'],result['pulled'][0]['missing']),(0,1,['999']),result)
        self.assertEqual(set(self.manifest()['pages']),{'100'})
    def test_the_api_path_of_a_server_install_is_configurable(self):
        self.register(api_path='/rest/api');code,error=self.sync();self.assertEqual(code,2);self.assertIn('/rest/api/content',str(error))   # the fixture only serves /wiki/rest/api: a 404 names the path

def notion_fixture(fake):
    deep=block('bulleted_list_item','level seven')
    for word in ('six','five','four','three','two','one'):deep=block('bulleted_list_item',f'level {word}',[deep])   # seven levels: the client follows six
    rows=[block('table_row',cells=[[rt('Currency')],[rt('Settles')]]),block('table_row',cells=[[rt('EUR')],[rt('same | day')]])]
    top=[block('heading_1','Release runbook'),dict(block('paragraph'),paragraph={'rich_text':[rt('Run the '),rt('migration',bold=True),rt(' first, see '),rt('docs',href='https://example.com/docs')]}),
         block('bulleted_list_item','Freeze deploys',[block('bulleted_list_item','tell the marmot team')]),block('numbered_list_item','Snapshot the database'),block('numbered_list_item','Run the script'),
         block('to_do','Update the status page',checked=True),block('code','print("hello")',language='python'),block('quote','Measure twice'),block('callout','Never skip the backup',icon={'emoji':'!'}),
         block('toggle','More detail',[block('paragraph','Hidden axolotl detail')]),block('table',children=rows,has_column_header=True,table_width=2),block('divider'),block('child_page',title='Postmortems'),deep]
    def register(blocks):
        for b in blocks:
            if b.get('kids'):fake.blocks[b['id']]=b['kids'];register(b['kids'])
    register(top);fake.blocks[nid(11)]=top
    fake.blocks[nid(12)]=[block('paragraph','Quarterly planning notes about the ibex roadmap.')]
    fake.notion_pages={nid(11):{'title':'Release Runbook','edited':'2026-02-01T10:00:00.000Z','url':'https://notion.example/Release-Runbook-11'},nid(12):{'title':'Planning','edited':'2026-02-02T10:00:00.000Z','url':'https://notion.example/Planning-12'},
                       nid(13):{'title':'Old','edited':'2026-02-03T10:00:00.000Z','url':'https://notion.example/Old-13'},nid(14):{'title':'Gone','edited':'2026-02-03T10:00:00.000Z','url':'x','archived':True}}
    fake.blocks[nid(13)]=[block('paragraph','Obsolete okapi page.')]
    fake.databases={nid(1):[nid(11),nid(12),nid(13),nid(14)]}

class Notion(Wiki):
    def setUp(self):
        super().setUp();notion_fixture(self.fake);self.register(name='notes',kind='notion',space=None,database_ids=[nid(1)])
    def sync(self,*argv):return super().sync(*(argv or ('notes',)))
    def test_database_query_and_block_children_paginate_and_become_markdown(self):
        code,result=self.sync();self.assertEqual((code,result['pulled'][0]['fetched']),(0,3),result)   # the archived page is not pulled
        self.assertEqual(len(self.fake.requests('/v1/databases')),2);self.assertEqual({r[4] for r in self.fake.log},{'2022-06-28'});self.assertEqual({r[3] for r in self.fake.log},{'Bearer '+TOKEN})
        self.assertEqual([r[5] for r in self.fake.requests('/v1/databases')],[{'page_size':100},{'page_size':100,'start_cursor':'2'}])
        top=[r for r in self.fake.requests(f'/v1/blocks/{nid(11)}/children')];self.assertGreater(len(top),3);self.assertEqual(top[0][2]['page_size'],'100')
        text=(self.cache('notes')/f'release-runbook-{nid(11)}.md').read_text(encoding='utf-8')
        for expected in ('# Release Runbook\n','# Release runbook','Run the **migration** first, see [docs](https://example.com/docs)','- Freeze deploys\n  - tell the marmot team','1. Snapshot the database\n2. Run the script',
                         '- [x] Update the status page','```python\nprint("hello")\n```','> Measure twice','> ! Never skip the backup','- More detail\n  Hidden axolotl detail',
                         '| Currency | Settles |\n| --- | --- |\n| EUR | same \\| day |','---','(sub-page) Postmortems'):
            self.assertIn(expected,text)
        self.assertIn('level six',text);self.assertNotIn('level seven',text)   # six levels are followed
        manifest=self.manifest('notes');self.assertEqual(set(manifest['pages']),{nid(11),nid(12),nid(13)});self.assertEqual(manifest['pages'][nid(12)]['url'],'https://notion.example/Planning-12');self.assertEqual(manifest['pages'][nid(12)]['version'],'2026-02-02T10:00:00.000Z')
        hit=self.cli('ask','ibex roadmap quarterly')[1]['sources'][0];self.assertEqual((hit['source'],hit['title'],hit['url'],hit['citation']),('notes','Planning','https://notion.example/Planning-12','Planning <https://notion.example/Planning-12>'))
        self.assertEqual(self.cli('ask','axolotl detail')[1]['sources'][0]['title'],'Release Runbook')
    def test_incremental_by_last_edited_time_and_removal(self):
        self.sync();fetched=len(self.fake.requests('/v1/blocks'));code,again=self.sync();self.assertEqual((again['pulled'][0]['fetched'],again['pulled'][0]['unchanged']),(0,3));self.assertEqual(len(self.fake.requests('/v1/blocks')),fetched)   # the blocks were not fetched again
        self.fake.notion_pages[nid(12)]['edited']='2026-03-01T10:00:00.000Z';self.fake.blocks[nid(12)]=[block('paragraph','Planning now mentions the lemur roadmap.')];self.fake.databases[nid(1)].remove(nid(13))
        code,third=self.sync();self.assertEqual((third['pulled'][0]['fetched'],third['pulled'][0]['unchanged'],third['pulled'][0]['removed']),(1,1,1))
        self.assertEqual(self.cli('ask','lemur roadmap')[1]['sources'][0]['title'],'Planning');self.assertEqual(self.cli('ask','obsolete okapi')[1]['sources'],[]);self.assertFalse(list(self.cache('notes').glob(f'*{nid(13)}*')))
    def test_a_page_id_source_and_a_missing_page(self):
        self.register(name='pages',kind='notion',space=None,database_ids=None,page_ids=[nid(12),nid(99)])
        code,result=self.sync('pages');self.assertEqual((code,result['pulled'][0]['fetched'],result['pulled'][0]['missing']),(0,1,[nid(99)]),result)
        self.assertEqual(self.fake.requests(f'/v1/pages/{nid(12)}')[0][0],'GET')
    def test_an_unknown_database_is_a_clear_error(self):
        self.register(name='bad',kind='notion',space=None,database_ids=[nid(77)]);code,error=self.sync('bad');self.assertEqual(code,2);self.assertIn('not found (HTTP 404)',str(error));self.assertNotIn(TOKEN,str(error))
    def test_the_block_count_is_capped(self):
        with patch.object(connectors,'MAX_BLOCKS',3):code,result=self.sync()
        self.assertIn('page cut here',(self.cache('notes')/f'release-runbook-{nid(11)}.md').read_text(encoding='utf-8'))

class Markdown(unittest.TestCase):
    def test_a_small_page_exactly(self):
        blocks=[block('heading_2','Steps'),block('numbered_list_item','one'),block('numbered_list_item','two',[block('bulleted_list_item','inner')]),block('paragraph','after'),block('code','a\nb',language='plain text')]
        for b in blocks:
            if b.get('kids'):b['children']=b.pop('kids')
        self.assertEqual(connectors.notion_markdown(blocks),'## Steps\n\n1. one\n2. two\n   - inner\n\nafter\n\n```\na\nb\n```')
    def test_text_annotations_and_unsupported_blocks(self):
        b=[{'type':'paragraph','paragraph':{'rich_text':[rt('a',code=True),rt(' b',italic=True,strikethrough=True)]}},{'type':'unsupported','unsupported':{}},{'type':'bookmark','bookmark':{'url':'https://x.example','caption':[]}},{'type':'equation','equation':{'expression':'E=mc^2'}}]
        self.assertEqual(connectors.notion_markdown(b),'`a`~~* b*~~\n\n[https://x.example](https://x.example)\n\n$$ E=mc^2 $$')

class NoNetwork(Wiki):
    """Only an explicit `source-sync NAME` (or --all-remote) reaches the network."""
    def setUp(self):super().setUp();self.register();self.sync();self.fake.log.clear()
    def command(self,rest):return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'
    def decision(self,rest,**extra):
        return runtime.process('claude','pre-tool-use',{'cwd':str(self.project),'session_id':'allow','tool_name':'Bash','tool_input':{'command':self.command(rest)},**extra},self.root)
    def test_session_start_and_its_background_sync_never_touch_the_network(self):
        with patch.dict(os.environ,{'LIVING_MEMORY_NO_BACKGROUND':''}),patch.object(sources.subprocess,'Popen') as spawned,patch.object(connectors,'pull',side_effect=AssertionError('network pull from a hook')):
            sources.db_path(self.root,'demo').unlink();context=runtime.process('claude','session-start',{'cwd':str(self.project),'session_id':'s1'},self.root)['context']
            self.assertEqual(spawned.call_count,1);argv=spawned.call_args.args[0];self.assertEqual(argv[-1],'source-sync');self.assertNotIn('wiki',argv);self.assertFalse([a for a in argv if a.startswith('--a')]);self.assertIn('Knowledge base',context)
            self.assertEqual(sources.sync(self.root,'demo')['sources'][0]['name'],'wiki')   # what the spawned process runs: the cache is indexed, nothing is pulled
        self.assertEqual(self.fake.log,[]);self.assertEqual(self.cli('ask','pelican refund days')[1]['sources'][0]['title'],'Refund Policy')
    def test_plain_source_sync_and_rebuild_only_reindex_the_cache(self):
        with patch.object(connectors,'pull',side_effect=AssertionError('network pull')):
            for argv in (('source-sync',),('source-sync','--rebuild')):self.assertEqual(self.cli(*argv)[0],0)
        self.assertEqual(self.fake.log,[]);self.cache().joinpath('refund-policy-100.html').write_text('<p>changed pelican</p>',encoding='utf-8');self.assertTrue(sources.stale(self.root,'demo'))
    def test_a_source_never_pulled_does_not_break_the_startup_check(self):
        self.register(name='fresh',space='OTHER');self.assertTrue(sources.stale(self.root,'demo'));self.assertEqual(self.cli('source-sync')[0],0);self.assertEqual(self.fake.log,[])
    def test_only_a_named_remote_source_or_all_remote_pulls(self):
        self.assertEqual(self.sync('wiki')[0],0);self.assertTrue(self.fake.log);self.fake.log.clear()
        self.assertEqual(self.cli('source-sync','--all-remote')[0],0);self.assertTrue(self.fake.log)
    def test_agents_do_not_get_auto_allow_for_a_remote_sync(self):
        for rest in ('source-sync wiki','source-sync wiki --rebuild','source-sync --all-remote','source-sync --all','source-sync --a','source-sync --rebuild --all-remote'):
            with self.subTest(rest=rest):reply=self.decision(rest);self.assertNotIn('allow',reply);self.assertNotIn('deny',reply)   # the host's normal prompt
        for rest in ("ask 'x'",'sources','source-sync','source-sync --rebuild','search --sources retry','source-sync ghost'):
            with self.subTest(rest=rest):self.assertIn('allow',self.decision(rest))
    def test_subagents_are_denied_a_remote_sync_in_the_hook_and_in_the_cli(self):
        for rest in ('source-sync wiki','source-sync --all-remote'):
            with self.subTest(rest=rest):reply=self.decision(rest,agent_id='sub1');self.assertIn('deny',reply);self.assertIn('network',reply['deny'])
        self.assertIn('allow',self.decision('source-sync',agent_id='sub1'))
        for actor in ('subagent','viewer'):
            code,error=self.cli('--actor',actor,'source-sync','wiki');self.assertEqual(code,2);self.assertIn('reaches the network',str(error));self.assertEqual(self.cli('--actor',actor,'source-sync')[0],0)
        self.assertEqual(self.fake.log,[])
    def test_the_source_plan_keeps_the_hosts_prompt(self):
        self.assertNotIn('allow',self.decision('source-plan add'))

if __name__=='__main__':unittest.main()
