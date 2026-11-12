"""Project knowledge base, layer 1: extraction and chunking per kind, the source registry and its plans, the incremental
index, ask / search --sources / sources, auto-allow, the SessionStart line with its background sync, and prompt excerpts."""
import contextlib,io,json,os,shutil,sqlite3,sys,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from test_semantic import write_model
from core import backup,runtime,semantic,sources,storage
from core.cli import main
from core.runtime import protected_rule

FIXTURE=Path(__file__).parent/'fixtures'/'kb_repo'
LEDGER_QUESTION='how does the exponential backoff retry for failed payment captures work'

class Kb(StoreFixture):
    """A bound project holding a git repo (kb/) copied from the fixture, plus the files a git checkout would also have."""
    def setUp(self):
        super().setUp()
        self.project=self.base/'project';self.repo=self.project/'kb'
        shutil.copytree(FIXTURE,self.repo);(self.repo/'.git').mkdir()
        (self.repo/'.gitignore').write_text('secret_notes.txt\ngenerated/\n')
        for rel in ('node_modules/leftpad/index.js','vendor/lib/util.py','target/out.md','bin/run.sh','build/x.md'):
            path=self.repo/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('vendored-marker-capybara function zzz() {}\n')
        (self.repo/'docs'/'big.txt').write_text('large-file-marker-ocelot\n'+'x'*(sources.MAX_BYTES+10))
    def cli(self,*argv,stdin=None,domain='demo'):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',io.StringIO(stdin or '')):
            try:code=main(['--root',str(self.root),*(['--domain',domain] if domain else []),*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip().startswith('{') else text)
    def sync(self,**kw):return sources.sync(self.root,'demo',**kw)
    def db(self):return sqlite3.connect(sources.db_path(self.root,'demo'))
    def indexed(self):
        with contextlib.closing(self.db()) as db:return {p for (p,) in db.execute('SELECT path FROM file WHERE skipped IS NULL')}
    def hook(self,event,session='s1',**extra):
        return runtime.process('claude',event,{'cwd':str(self.project),'session_id':session,**extra},self.root)
    def register(self,**kw):
        value={'name':'wiki','kind':'html-export','path':str(self.repo/'docs'/'export'),**kw}
        code,plan=self.cli('source-plan','add',stdin=json.dumps(value));self.assertEqual(code,0,plan)
        code,done=self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest']);self.assertEqual(code,0,done);return plan

class Chunking(unittest.TestCase):
    def chunks(self,rel,kind='folder'):return sources.chunk_file(rel,(FIXTURE/rel).read_text(encoding='utf-8'),kind)
    def test_markdown_chunks_at_headings_and_ignores_fenced_code(self):
        found=self.chunks('README.md');symbols=[c['symbol'] for c in found]
        self.assertIn('Billing Service > Retry policy',symbols);self.assertIn('Billing Service > Deployment',symbols)
        self.assertFalse([s for s in symbols if 'this comment' in s])   # a # line in a fenced block is not a heading
        retry=next(c for c in found if c['symbol'].endswith('Retry policy'));self.assertIn('first retry waits 30 seconds',retry['text'])
    def test_line_ranges_cite_the_lines_the_text_came_from(self):
        for rel in ('README.md','docs/adr/0003-use-event-sourcing.md','src/billing/ledger.py','src/main/java/com/acme/billing/InvoiceService.java','legacy/PAYROLL.cbl','sql/schema.sql','docs/oncall.txt'):
            lines=(FIXTURE/rel).read_text(encoding='utf-8').split('\n')
            for c in self.chunks(rel):
                with self.subTest(rel=rel,at=c['line_start']):self.assertEqual(c['text'],'\n'.join(l.rstrip() for l in lines[c['line_start']-1:c['line_end']]).strip('\n'))
    def test_html_export_keeps_headings_and_table_rows_and_drops_chrome(self):
        found=self.chunks('docs/export/Billing-FAQ.html','html-export');text='\n'.join(c['text'] for c in found)
        self.assertEqual([c['symbol'] for c in found],['Billing FAQ > Refund window','Billing FAQ > Supported currencies'])
        for kept in ('# Billing FAQ','## Refund window','within 45 days','EUR | same day','USD | next business day'):self.assertIn(kept,text)
        for dropped in ('Sidebar-link-pelican','zebra-tracker','footer-marker-walrus','Wiki Site Header','color: red','Spaces'):self.assertNotIn(dropped,text)
        self.assertNotIn('Wiki »',text);self.assertEqual({c['kind'] for c in found},{'html'})
    def test_a_git_wiki_page_without_a_heading_is_named_by_its_file(self):
        found=sources.chunk_file('Retry-Policy.md','Payments retry with backoff.\n','git-wiki');self.assertEqual(found[0]['symbol'],'Retry Policy')
        self.assertEqual(sources.chunk_file('Retry-Policy.md','Payments retry.\n','folder')[0]['symbol'],'')
        self.assertEqual(sources.chunk_file('Home.md','# Welcome\nhello\n','git-wiki')[0]['symbol'],'Welcome')
    def test_code_is_cut_at_definitions_with_symbol_paths(self):
        self.assertIn('Ledger.fold; reconcile_ledger',[c['symbol'] for c in self.chunks('src/billing/ledger.py')])
        self.assertIn('InvoiceService.applyLoyaltyDiscount',[c['symbol'] for c in self.chunks('src/main/java/com/acme/billing/InvoiceService.java')])
        cobol=' '.join(c['symbol'] for c in self.chunks('legacy/PAYROLL.cbl'));self.assertIn('PROCEDURE DIVISION',cobol);self.assertIn('COMPUTE-OVERTIME',cobol);self.assertIn('IDENTIFICATION DIVISION',cobol)
        self.assertIn('CREATE TABLE ledger_events',self.chunks('sql/schema.sql')[0]['symbol'])
    def test_other_languages(self):
        cases={'a.go':'package a\n\ntype Server struct {\n\tPort int\n}\n\nfunc (s *Server) Listen() error {\n\treturn nil\n}\n\nfunc main() {\n}\n',
               'a.rs':'pub struct Cache {\n}\n\nimpl Cache {\n    pub fn new() -> Cache {\n        Cache {}\n    }\n}\n\nfn helper() {}\n',
               'a.ts':'export class Store {\n  private items = [];\n  add(item: string): void {\n    this.items.push(item);\n  }\n}\n\nexport function build(): Store {\n  return new Store();\n}\n',
               'a.kt':'class Repo {\n    fun load(id: Int): String {\n        return ""\n    }\n}\n',
               'a.py':'import os\n\n@cached\ndef top(a):\n    def inner():\n        pass\n    return a\n\nclass K:\n    def method(self):\n        pass\n'}
        found={rel:' '.join(c['symbol'] for c in sources.chunk_file(rel,text)) for rel,text in cases.items()}
        for rel,names in {'a.go':('Server','Server.Listen','main'),'a.rs':('Cache','Cache.new','helper'),'a.ts':('Store','Store.add','build'),'a.kt':('Repo','Repo.load'),'a.py':('top','top.inner','K.method')}.items():
            for name in names:
                with self.subTest(rel=rel,name=name):self.assertIn(name,found[rel])
    def test_decorators_and_javadoc_stay_with_their_definition(self):
        py=sources.chunk_file('a.py','x = 1\n\n\n'+'y = 2\n'*60+'\n@cached\ndef top(a):\n    return a\n')
        self.assertTrue(any(c['text'].startswith('@cached\ndef top') or '@cached\ndef top' in c['text'] and c['symbol']=='top' for c in py))
    def test_chunks_are_about_1200_characters_and_overlap(self):
        lines=[f'line {i:03d} '+'word '*10 for i in range(120)];found=sources.chunk_file('notes.txt','\n'.join(lines))
        self.assertGreaterEqual(len(found),6)
        for before,after in zip(found,found[1:]):
            self.assertLessEqual(len(before['text']),sources.TARGET);self.assertLessEqual(after['line_start'],before['line_end'])   # overlap
            self.assertGreater(after['line_end'],before['line_end'])
        self.assertEqual((found[0]['line_start'],found[-1]['line_end']),(1,120))
    def test_one_very_long_line_is_split(self):
        found=sources.chunk_file('a.txt','x'*3000);self.assertEqual(len(found),3);self.assertTrue(all(len(c['text'])<=sources.TARGET for c in found))
    def test_credentials_are_redacted_without_moving_lines(self):
        text='a = 1\nkey = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\nMIIEdef\n-----END RSA PRIVATE KEY-----"\nb = 2\ntoken = "AKIAIOSFODNN7EXAMPLE"\n'
        clean,count=sources.redact_text(text);self.assertEqual(count,2);self.assertEqual(clean.count('\n'),text.count('\n'))
        self.assertNotIn('MIIE',clean);self.assertNotIn('AKIAIOSFODNN7EXAMPLE',clean);self.assertIn("<redacted type='private key'/>",clean)
    def test_gitignore_patterns(self):
        rules=sources.gitignore.__globals__['_compile']
        def check(lines,rel,is_dir=False):return sources.ignored([r for r in map(rules,lines) if r],rel,is_dir)
        self.assertTrue(check(['*.log'],'a/b/x.log'));self.assertFalse(check(['*.log'],'a/b/x.txt'))
        self.assertTrue(check(['/dist'],'dist',True));self.assertFalse(check(['/dist'],'src/dist',True))
        self.assertTrue(check(['build/'],'src/build',True));self.assertFalse(check(['build/'],'src/build'))   # a file named build is not the folder
        self.assertTrue(check(['docs/**/*.tmp'],'docs/a/b/x.tmp'));self.assertTrue(check(['docs/**/*.tmp'],'docs/x.tmp'))
        self.assertFalse(check(['*.log','!keep.log'],'keep.log'));self.assertTrue(check(['*.log','!keep.log'],'other.log'))
        self.assertTrue(check(['# comment','secret?.txt'],'secret1.txt'));self.assertTrue(check(['[ab].txt'],'a.txt'));self.assertFalse(check(['[!ab].txt'],'a.txt'))

class Index(Kb):
    def test_sync_indexes_docs_and_code_and_skips_the_rest(self):
        result=self.sync();self.assertTrue(result['ok']);self.assertTrue(result['rebuilt'])
        found=self.indexed()
        for rel in ('README.md','docs/adr/0003-use-event-sourcing.md','docs/oncall.txt','src/billing/ledger.py','src/main/java/com/acme/billing/InvoiceService.java','legacy/PAYROLL.cbl','sql/schema.sql','config/settings.py'):self.assertIn(rel,found)
        self.assertNotIn('docs/export/Billing-FAQ.html',found)   # html belongs to an html-export source
        for rel in ('secret_notes.txt','generated/out.md','node_modules/leftpad/index.js','vendor/lib/util.py','target/out.md','bin/run.sh','build/x.md','.gitignore'):self.assertNotIn(rel,found)
        with contextlib.closing(self.db()) as db:skipped=dict(db.execute('SELECT path,skipped FROM file WHERE skipped IS NOT NULL'))
        self.assertEqual(skipped,{'docs/blob.txt':'binary','docs/big.txt':'large'})
        for marker in ('quokka-gitignored-marker','generated-marker-narwhal','vendored-marker-capybara','large-file-marker-ocelot','lynx'):self.assertEqual(sources.search(self.root,'demo',marker),[],marker)
    def test_credentials_never_reach_the_index(self):
        self.sync()
        with contextlib.closing(self.db()) as db:
            everything=' '.join(' '.join(map(str,row)) for row in db.execute('SELECT symbol,path,names,text FROM chunk_fts'))
        for secret in ('AKIAIOSFODNN7EXAMPLE','hunter2hunter2'):self.assertNotIn(secret,everything)
        self.assertIn("<redacted type='AWS access key'/>",everything);self.assertEqual(sources.search(self.root,'demo','hunter2hunter2'),[])
        self.assertEqual(self.sync()['sources'][0]['unchanged'],len(self.indexed())+2)
    def test_unchanged_sync_reads_and_chunks_nothing(self):
        first=self.sync()['sources'][0];self.assertEqual((first['added'],first['changed'],first['removed']),(first['files']+2,0,0))
        with patch.object(sources,'chunk_file',side_effect=AssertionError('chunked')),patch('pathlib.Path.read_bytes',side_effect=AssertionError('read')):
            again=self.sync()
        row=again['sources'][0];self.assertEqual((row['added'],row['changed'],row['removed'],row['unchanged']),(0,0,0,first['files']+2));self.assertFalse(again['rebuilt'])
    def test_modified_file_is_rechunked_and_found_by_its_new_text(self):
        self.sync();time.sleep(0.02);(self.repo/'docs'/'oncall.txt').write_text('Escalate to the pager rota named platypus-escalation.\n')
        calls=[];real=sources.chunk_file
        def spy(rel,*a,**k):calls.append(rel);return real(rel,*a,**k)
        with patch.object(sources,'chunk_file',spy):row=self.sync()['sources'][0]
        self.assertEqual(calls,['docs/oncall.txt']);self.assertEqual((row['changed'],row['added'],row['removed']),(1,0,0))
        self.assertEqual(sources.search(self.root,'demo','platypus escalation')[0]['path'],'docs/oncall.txt');self.assertEqual(sources.search(self.root,'demo','rotation monday'),[])
    def test_a_touched_file_with_the_same_content_is_not_rechunked(self):
        self.sync();os.utime(self.repo/'README.md',(time.time()+5,time.time()+5))
        with patch.object(sources,'chunk_file',side_effect=AssertionError('chunked')):row=self.sync()['sources'][0]
        self.assertEqual((row['changed'],row['unchanged']),(0,row['files']+2))
    def test_deleted_and_added_files(self):
        self.sync();(self.repo/'docs'/'oncall.txt').unlink();(self.repo/'docs'/'new.md').write_text('# Fresh\nA new page about marmot-migration.\n')
        row=self.sync()['sources'][0];self.assertEqual((row['added'],row['removed']),(1,1))
        self.assertNotIn('docs/oncall.txt',self.indexed());self.assertEqual(sources.search(self.root,'demo','marmot migration')[0]['path'],'docs/new.md')
        with contextlib.closing(self.db()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chunk').fetchone(),db.execute('SELECT count(*) FROM chunk_fts').fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM vocab WHERE stem='marmot'").fetchone()[0],1)
    def test_vocabulary_follows_the_chunks(self):
        self.sync();(self.repo/'docs'/'oncall.txt').unlink();self.sync();total,df=sources.vocabulary(self.root,'demo',['dunning','pager'])
        self.assertGreater(total,5);self.assertEqual(df.get('pager',0),0);self.assertGreaterEqual(df['dunn'],1)   # stems, as search.vocabulary keeps them
    def test_rebuild_and_a_version_change_start_over(self):
        first=self.sync();before=self.indexed()
        with contextlib.closing(self.db()) as db:db.execute("UPDATE meta SET value='0' WHERE key='version'");db.commit()
        self.assertEqual(sources.state(self.root,'demo'),None)   # an index from another version reads as absent
        again=self.sync();self.assertTrue(again['rebuilt']);self.assertEqual(again['chunks'],first['chunks']);self.assertEqual(self.indexed(),before)
        forced=self.sync(rebuild=True);self.assertTrue(forced['rebuilt']);self.assertEqual(forced['sources'][0]['added'],first['sources'][0]['added'])
        self.assertEqual([p.name for p in sources.db_path(self.root,'demo').parent.glob('.sources-*')],[])
    def test_rebuild_of_one_source_leaves_the_others(self):
        self.register();self.sync();code,result=self.cli('source-sync','wiki','--rebuild');self.assertEqual(code,0,result)
        self.assertEqual([s['name'] for s in result['sources']],['wiki']);self.assertFalse(result['rebuilt']);self.assertIn('README.md',self.indexed())
        code,error=self.cli('source-sync','nope');self.assertEqual(code,2);self.assertIn('Unknown source',str(error))
    def test_a_removed_source_leaves_the_index(self):
        self.register();self.sync();self.assertIn('Billing-FAQ.html',self.indexed())
        code,plan=self.cli('source-plan','remove',stdin='{"name":"wiki"}');self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest'])
        self.sync();self.assertNotIn('Billing-FAQ.html',self.indexed());self.assertEqual([s['name'] for s in self.cli('sources')[1]['sources']],['kb'])
    def test_a_git_wiki_source_indexes_pages_and_html(self):
        wiki=self.base/'wiki';wiki.mkdir();(wiki/'Dunning-Process.md').write_text('Overdue invoices are chased every seven days by the collections bot.\n');(wiki/'Faq.html').write_text('<html><body><h1>Faq</h1><p>Wallaby support hours are nine to five.</p></body></html>')
        (wiki/'notes.py').write_text('x = 1\n');self.register(name='gitwiki',kind='git-wiki',path=str(wiki));self.sync()
        hit=sources.search(self.root,'demo','collections bot overdue')[0];self.assertEqual((hit['source'],hit['path'],hit['symbol']),('gitwiki','Dunning-Process.md','Dunning Process'))
        self.assertEqual(sources.search(self.root,'demo','wallaby support hours')[0]['path'],'Faq.html');self.assertFalse([h for h in sources.search(self.root,'demo','notes') if h['path']=='notes.py'])
    def test_a_second_sync_waits_or_reports_busy(self):
        with sources._lock(self.root,'demo',0):
            self.assertTrue(sources.syncing(self.root,'demo'));self.assertEqual(sources.sync(self.root,'demo',wait=0)['status'],'busy')
        self.assertFalse(sources.syncing(self.root,'demo'));self.assertTrue(self.sync()['ok'])
    def test_stale_follows_file_times(self):
        self.assertTrue(sources.stale(self.root,'demo'));self.sync();self.assertFalse(sources.stale(self.root,'demo'))
        time.sleep(0.02);(self.repo/'src'/'billing'/'ledger.py').write_text('# changed\n');self.assertTrue(sources.stale(self.root,'demo'))
        self.sync();self.assertFalse(sources.stale(self.root,'demo'));(self.repo/'docs'/'oncall.txt').unlink();self.assertTrue(sources.stale(self.root,'demo'))   # a delete moves a folder's time
    def test_stale_notices_a_new_source_and_ignores_vendored_changes(self):
        self.sync();time.sleep(0.02);(self.repo/'node_modules'/'leftpad'/'index.js').write_text('changed');(self.repo/'generated'/'out.md').write_text('changed');self.assertFalse(sources.stale(self.root,'demo'))
        self.register();self.assertTrue(sources.stale(self.root,'demo'))
    def test_a_sync_of_one_source_does_not_hide_the_changes_of_another(self):
        self.register();self.sync();self.assertFalse(sources.stale(self.root,'demo'))
        time.sleep(0.02);(self.repo/'docs'/'oncall.txt').write_text('The pager rotation is weekly.\n')
        self.sync(name='wiki');self.assertTrue(sources.stale(self.root,'demo'))   # kb was not refreshed, whatever the domain-wide time says
        self.sync();self.assertFalse(sources.stale(self.root,'demo'));self.assertEqual(sources.search(self.root,'demo','pager rotation weekly')[0]['path'],'docs/oncall.txt')
    def test_a_gitignore_edited_in_place_drops_the_files_it_now_names(self):
        self.sync();self.assertIn('src/billing/ledger.py',self.indexed());self.assertFalse(sources.stale(self.root,'demo'))
        time.sleep(0.02);ignore=self.repo/'.gitignore';ignore.write_text(ignore.read_text()+'src/billing/ledger.py\n')   # no folder's time moves
        self.assertTrue(sources.stale(self.root,'demo'));self.sync();self.assertNotIn('src/billing/ledger.py',self.indexed());self.assertFalse(sources.stale(self.root,'demo'))
        time.sleep(0.02);ignore.write_text(ignore.read_text().replace('src/billing/ledger.py\n',''))
        self.assertTrue(sources.stale(self.root,'demo'));self.sync();self.assertIn('src/billing/ledger.py',self.indexed())
    def test_removing_the_last_source_makes_the_index_stale_and_the_sync_prunes_it(self):
        self.register();self.sync();self.assertIn('Billing-FAQ.html',self.indexed())
        code,plan=self.cli('source-plan','remove',stdin='{"name":"wiki"}');self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest'])
        code,plan=self.cli('source-plan','implicit',stdin='{"enabled": false}');self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest'])
        self.assertEqual(sources.resolve(self.root,'demo'),[]);self.assertTrue(sources.stale(self.root,'demo'))
        self.sync();self.assertEqual(self.indexed(),set());self.assertEqual(sources.search(self.root,'demo','retry policy'),[]);self.assertFalse(sources.stale(self.root,'demo'))
    def test_include_and_exclude_globs(self):
        self.register(name='code',kind='folder',path=str(self.repo),include=['*.py'],exclude=['config/**'])
        code,plan=self.cli('source-plan','implicit',stdin='{"enabled": false}');self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest']);self.sync()
        found=self.indexed();self.assertEqual(found,{'src/billing/ledger.py'})
    def test_semantic_vectors_cover_only_changed_chunks(self):
        source=self.base/'tiny';hashes=write_model(source)
        with patch.dict(semantic.MODELS,{'tiny':{'repo':'none/tiny','revision':'0','min_similarity':0.45,'files':hashes}},clear=True):
            self.addCleanup(semantic._unload);semantic.install(self.root,'tiny',source)
            first=self.sync();self.assertEqual(first['vectors']['pending'],0);self.assertEqual(first['vectors']['embedded'],first['chunks'])
            self.assertEqual(self.sync()['vectors']['embedded'],0)
            time.sleep(0.02);(self.repo/'docs'/'oncall.txt').write_text('The cache layout is a note.\n');self.assertEqual(self.sync()['vectors']['embedded'],1)
            self.assertTrue(sources.search(self.root,'demo','cache layout note')[0]['path']=='docs/oncall.txt')
            semantic.remove(self.root);self.assertIsNone(self.sync()['vectors']);
            with contextlib.closing(self.db()) as db:self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='vec'").fetchone())

class Limits(Kb):
    def test_a_tree_over_the_file_limit_is_indexed_up_to_it_and_says_so(self):
        with patch.object(sources,'MAX_FILES',3):result=self.sync()
        self.assertEqual(result['sources'][0]['truncated'],1);self.assertEqual(len(self.indexed()),3)
    def test_the_memory_store_is_not_project_source(self):
        self.note('Cache note','store-marker-gecko');self.register(name='everything',kind='folder',path=str(self.base))
        spec=next(s for s in sources.resolve(self.root,'demo') if s['name']=='everything');self.assertEqual(spec['exclude'],['/store'])
        self.sync()
        with contextlib.closing(self.db()) as db:paths={p for (p,) in db.execute("SELECT path FROM file WHERE source='everything'")}
        self.assertIn('project/kb/README.md',paths);self.assertFalse([p for p in paths if p.startswith('store/')])
    def test_a_source_that_is_or_lies_in_the_store_is_refused_and_never_indexed(self):
        self.note('Cache note','store-marker-gecko')
        for path in (self.root,self.root/'domains'):
            with self.subTest(path=path):
                code,error=self.cli('source-plan','add',stdin=json.dumps({'name':'mem','kind':'folder','path':str(path)}));self.assertEqual(code,2,error);self.assertIn('memory store',str(error))
        from core.repository import canonical_json   # registered before the check existed
        entries=[{'name':n,'domain':'demo','kind':'folder','path':p.as_posix(),'include':[],'exclude':[]} for n,p in (('mem',self.root),('inner',self.root/'domains'))]
        (self.root/'sources.json').write_bytes(canonical_json({'version':1,'sources':entries}).encode())
        self.assertEqual([s['name'] for s in sources.resolve(self.root,'demo')],['kb']);self.sync()
        with contextlib.closing(self.db()) as db:self.assertEqual({n for (n,) in db.execute('SELECT name FROM source')},{'kb'})
        found=[{'name':'store','domain':'demo','kind':'folder','path':self.root.as_posix(),'include':[],'exclude':[],'implicit':True}]
        with patch.object(sources,'implicit_sources',return_value=found):self.assertEqual(sources.resolve(self.root,'demo'),[]) # a repo that is the store
    def test_benchmark_case_reports_the_numbers(self):
        import importlib.util
        found=importlib.util.spec_from_file_location('benchmark',Path(__file__).resolve().parents[1]/'scripts'/'benchmark.py');bench=importlib.util.module_from_spec(found);found.loader.exec_module(bench)
        with contextlib.redirect_stdout(io.StringIO()):value=bench.sources_scale(90,6)
        self.assertEqual((value['indexed_files'],value['idle_unchanged']),(90,90));self.assertTrue(value['met'],value)

class Registry(Kb):
    def test_add_inspect_apply_and_listing(self):
        plan=self.register(include=['*.html'],exclude=['drafts/**']);self.assertEqual(plan['kind'],'source_add')
        data=json.loads((self.root/'sources.json').read_text(encoding='utf-8'));entry=data['sources'][0]
        self.assertEqual((entry['name'],entry['domain'],entry['kind'],entry['include'],entry['exclude']),('wiki','demo','html-export',['*.html'],['drafts/**']))
        self.assertEqual(entry['path'],(self.repo/'docs'/'export').as_posix());self.assertTrue(entry['added'])
        code,listed=self.cli('sources');rows={r['name']:r for r in listed['sources']}
        self.assertEqual(set(rows),{'kb','wiki'});self.assertTrue(rows['kb']['implicit']);self.assertFalse(rows['wiki']['implicit']);self.assertEqual(rows['wiki']['state'],'not synced')
        self.assertEqual(listed['warnings'],[])
    def test_plan_effects_and_undo(self):
        code,plan=self.cli('source-plan','add',stdin=json.dumps({'name':'wiki','kind':'html-export','path':str(self.repo/'docs'/'export')}))
        _,view=self.cli('inspect-plan',plan['plan_id']);self.assertEqual([e['path'] for e in view['effects']],['sources.json']);self.assertFalse((self.root/'sources.json').exists())
        code,done=self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest']);self.assertEqual(code,0,done)
        code,undo=self.cli('undo',done['operation_id']);code,done=self.cli('apply',undo['plan_id'],'--expected-plan-sha256',undo['digest']);self.assertEqual(code,0,done)
        self.assertFalse((self.root/'sources.json').exists())
    def test_validation(self):
        folder=str(self.repo/'docs'/'export');ok={'name':'wiki','kind':'html-export','path':folder}
        for payload,message in ((dict(ok,kind='ftp'),'kind must be'),(dict(ok,name='Bad Name'),'name must match'),(dict(ok,path='relative/dir'),'absolute'),(dict(ok,path=str(self.repo/'nope')),'not a folder'),
                                (dict(ok,extra=1),'Unknown payload fields'),(dict(ok,include='*.md'),'list of at most 20'),(dict(ok,domain='ghost'),'not a bound domain'),
                                (dict(ok,name='sk-abcdefghijklmnopqrstuvwxyz0123'),'credential')):
            with self.subTest(message=message):
                code,error=self.cli('source-plan','add',stdin=json.dumps(payload));self.assertEqual(code,2,error);self.assertIn(message,str(error))
        code,error=self.cli('source-plan','add',stdin=json.dumps(dict(ok,name='w')));self.assertEqual(code,2,error);self.assertIn('at least 2',str(error))   # `w:Page.md` is not a named path: a letter and a colon is a drive
        self.assertEqual(self.cli('source-plan','add',stdin=json.dumps(dict(ok,name='wk')))[0],0)
        self.register();code,error=self.cli('source-plan','add',stdin=json.dumps(ok));self.assertEqual(code,2);self.assertIn('already exists',str(error))
        code,error=self.cli('source-plan','remove',stdin='{"name":"ghost"}');self.assertEqual(code,2);self.assertIn('Unknown source',str(error))
        code,error=self.cli('source-plan','add',stdin=json.dumps(dict(ok,name='other',include=['api_key = "abcd1234efgh5678"'])));self.assertEqual(code,2);self.assertIn('credential',str(error))
    def test_implicit_default_source_and_the_flag(self):
        specs=sources.resolve(self.root,'demo');self.assertEqual([(s['name'],s['kind'],s['implicit']) for s in specs],[('kb','folder',True)])
        self.assertEqual(specs[0]['path'],self.repo.resolve().as_posix())
        code,plan=self.cli('source-plan','implicit',stdin='{"enabled": false}');self.assertEqual((code,plan['kind']),(0,'source_implicit'))
        self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest']);self.assertEqual(sources.resolve(self.root,'demo'),[])
        self.assertEqual(json.loads((self.root/'sources.json').read_text())['implicit'],{'demo':False});self.assertEqual(self.cli('sources')[1]['implicit_disabled'],['demo'])
        code,plan=self.cli('source-plan','implicit',stdin='{"enabled": true}');self.cli('apply',plan['plan_id'],'--expected-plan-sha256',plan['digest'])
        self.assertEqual([s['name'] for s in sources.resolve(self.root,'demo')],['kb'])
    def test_a_bound_repo_and_nested_repos_are_found_but_not_vendored_ones(self):
        (self.project/'.git').mkdir();self.assertEqual([s['name'] for s in sources.resolve(self.root,'demo')],['project'])   # the bound folder itself is a repo
        shutil.rmtree(self.project/'.git');(self.project/'a'/'b'/'deep'/'.git').mkdir(parents=True);(self.project/'node_modules'/'pkg'/'.git').mkdir(parents=True)
        (self.project/'a'/'b'/'deep'/'c'/'.git').mkdir(parents=True)   # inside a repo: the walk of its parent covers it
        self.assertEqual(sorted(s['name'] for s in sources.resolve(self.root,'demo')),['deep','kb'])
    def test_protected_backed_up_and_not_in_a_snapshot_when_derived(self):
        self.register();self.assertEqual(protected_rule(str(self.root/'sources.json'),str(self.project),self.root),'config')
        self.sync();files=backup.canonical_files(self.root);self.assertIn('sources.json',files);self.assertFalse([f for f in files if 'sqlite3' in f])
        self.assertFalse([k for k in storage._manifest(self.root) if 'sources.sqlite3' in k])
        result=backup.backup(self.root);self.assertNotIn('warning',result)
    def test_doctor_reports_problems(self):
        self.register();self.assertEqual([w for w in self.cli('doctor')[1]['warnings'] if w.startswith('sources')],[])
        shutil.rmtree(self.repo/'docs'/'export');self.assertTrue(any(w.startswith('sources: wiki: folder does not exist') for w in self.cli('doctor')[1]['warnings']))
        (self.root/'sources.json').write_text('{broken');self.assertTrue(any(w.startswith('sources: Invalid or unreadable') for w in self.cli('doctor')[1]['warnings']))
        self.assertEqual([s['name'] for s in sources.resolve(self.root,'demo')],['kb'])   # a broken registry leaves the implicit sources

class Commands(Kb):
    def setUp(self):
        super().setUp();self.register();self.sync()
    def test_ask_puts_notes_first_then_cited_source_excerpts(self):
        self.note('Retry backoff policy','Payment captures retry with exponential backoff.')
        code,result=self.cli('ask','retry','backoff','payment');self.assertEqual(code,0,result)
        self.assertLess(list(result).index('notes'),list(result).index('sources'));self.assertEqual(result['notes'][0]['title'],'Retry backoff policy');self.assertEqual(result['notes'][0]['label'],'note')
        top=result['sources'][0];self.assertEqual((top['label'],top['type'],top['source'],top['path']),('source excerpt','source','kb','README.md'))
        self.assertEqual(top['citation'],f"README.md:{top['line_start']}-{top['line_end']}");self.assertEqual(top['symbol'],'Billing Service > Retry policy')
        lines=(self.repo/'README.md').read_text().split('\n')[top['line_start']-1:top['line_end']]
        self.assertIn('exponential backoff',' '.join(lines));self.assertLessEqual(len(top['snippet']),sources.SNIPPET+10);self.assertTrue(top['file'].endswith('/kb/README.md'))
        self.assertGreater(result['knowledge_base']['files'],5);self.assertFalse(result['knowledge_base']['syncing'])
    def test_ask_cites_html_exports_and_code(self):
        refund=self.cli('ask','refund window days')[1]['sources'][0];self.assertEqual((refund['source'],refund['path']),('wiki','Billing-FAQ.html'));self.assertIn('45 days',refund['snippet'])
        overtime=self.cli('ask','overtime hours rate')[1]['sources'][0];self.assertEqual(overtime['path'],'legacy/PAYROLL.cbl');self.assertIn('COMPUTE-OVERTIME',overtime['symbol'])
        discount=self.cli('ask','applyLoyaltyDiscount')[1]['sources'][0];self.assertEqual(discount['symbol'],'InvoiceService.applyLoyaltyDiscount')
    def test_ask_without_an_index_or_a_match_says_so(self):
        found=self.cli('ask','zeppelin hangar rotunda')[1];self.assertEqual(found['sources'],[]);self.assertIn('No source excerpt matched',found['knowledge_base']['hint'])
        sources.db_path(self.root,'demo').unlink();found=self.cli('ask','retry backoff')[1];self.assertEqual(found['sources'],[]);self.assertIn('source-sync',found['knowledge_base']['hint'])
        code,error=self.cli('ask','   ');self.assertEqual(code,2);self.assertIn('needs a question',str(error));self.assertEqual(self.cli('ask','x',domain=None)[0],2)
    def test_search_sources_returns_sources_only(self):
        self.note('Retry backoff policy','Payment captures retry with exponential backoff.')
        code,result=self.cli('search','--sources','retry backoff');self.assertEqual((code,result['mode']),(0,'sources'));self.assertEqual(result['results'][0]['path'],'README.md');self.assertNotIn('notes',result)
        self.assertEqual({r['type'] for r in result['results']},{'source'});self.assertEqual(self.cli('search','--sources','--file','x','q')[0],2)
        self.assertEqual(self.cli('search','retry backoff')[1]['results'][0]['title'],'Retry backoff policy')   # the plain search is unchanged
    def test_limit_bounds_both_sections(self):
        for i in range(4):self.note(f'Payment retry note {i}',f'Payment retry detail number {i}.')
        found=self.cli('ask','payment retry','--limit','2')[1];self.assertLessEqual(len(found['notes']),2);self.assertLessEqual(len(found['sources']),2)
    def test_several_questions_are_each_answered_on_their_own(self):
        self.note('Retry backoff policy','Payment captures retry with exponential backoff.')
        quiz='1. How does the retry backoff for payment captures work?\n2. What is the refund window days?\n3. How is the overtime hours rate computed?'
        code,result=self.cli('ask',quiz);self.assertEqual(code,0,result)
        each=result['questions'];self.assertEqual([q['question'] for q in each],['How does the retry backoff for payment captures work?','What is the refund window days?','How is the overtime hours rate computed?'])
        self.assertTrue(all({'question','notes','sources','relevant'}<=set(q) for q in each))
        self.assertEqual(each[0]['notes'][0]['title'],'Retry backoff policy');self.assertEqual(each[0]['sources'][0]['path'],'README.md')
        self.assertEqual(each[1]['sources'][0]['path'],'Billing-FAQ.html');self.assertEqual(each[2]['sources'][0]['path'],'legacy/PAYROLL.cbl')
        self.assertTrue(all(len(q['notes'])<=3 and len(q['sources'])<=3 for q in each))
        self.assertIn('notes',result);self.assertIn('sources',result);self.assertEqual(result['query'],quiz)   # the flat answer to the whole text stays
        sentences=self.cli('ask','How does the retry backoff work? What is the refund window days?')[1];self.assertEqual(len(sentences['questions']),2)
    def test_one_question_keeps_its_shape(self):
        for argv in (('ask','how does the retry backoff work?'),('ask','retry backoff'),('search','how does the retry backoff work?'),('search','--sources','retry backoff')):
            with self.subTest(argv=argv):self.assertNotIn('questions',self.cli(*argv)[1])
    def test_search_answers_several_questions_too_and_the_number_is_bounded(self):
        self.note('Retry backoff policy','Payment captures retry with exponential backoff.')
        quiz='1. How does the retry backoff work?\n2. What is the refund window days?\n3. How is the overtime hours rate computed?'
        found=self.cli('search',quiz)[1];self.assertEqual(len(found['questions']),3);self.assertEqual(found['questions'][0]['results'][0]['title'],'Retry backoff policy')
        self.assertTrue(all({'question','results','relevant'}<=set(q) for q in found['questions']));self.assertIn('results',found)
        only=self.cli('search','--sources',quiz)[1];self.assertEqual(only['questions'][1]['results'][0]['path'],'Billing-FAQ.html')
        many=self.cli('ask','\n'.join(f'{i}. What is the refund window days {i}?' for i in range(15)))[1];self.assertEqual(len(many['questions']),10)
    def test_test_files_rank_below_the_code_they_exercise_unless_the_question_is_about_tests(self):
        (self.repo/'src'/'billing'/'tariff.py').write_text('def compute_wombat_tariff(order):\n    """Compute the wombat tariff for an order."""\n    return order.total * 0.2\n')
        (self.repo/'tests').mkdir();(self.repo/'tests'/'test_tariff.py').write_text('"""wombat tariff computation tests"""\n'+''.join(f'def test_wombat_tariff_computation_{i}():\n    # wombat tariff computation case {i}\n    assert compute_wombat_tariff(order) == {i}\n' for i in range(2)))
        self.sync()
        paths=lambda q:[h['path'] for h in sources.search(self.root,'demo',q,5)]
        with patch.object(sources,'TEST_PENALTY',0):self.assertEqual(paths('how does the wombat tariff computation work')[0],'tests/test_tariff.py')   # the test file out-matches the code
        self.assertEqual(paths('how does the wombat tariff computation work')[0],'src/billing/tariff.py')
        self.assertIn('tests/test_tariff.py',paths('how does the wombat tariff computation work'))                       # demoted, not hidden
        self.assertEqual(paths('wombat tariff computation tests')[0],'tests/test_tariff.py')
        self.assertEqual(paths('wombat tariff computation spec')[0],'tests/test_tariff.py')
    def test_what_counts_as_a_test_file(self):
        for path in ('tests/a.py','test/a.py','src/__tests__/a.js','spec/a.rb','pkg/test_a.py','pkg/a_test.go','web/a.spec.ts','web/a.test.js','src/main/AppTest.java','Tests/A.cs'):
            with self.subTest(path=path):self.assertTrue(sources.TEST_PATH.search(path))
        for path in ('src/a.py','docs/testing-guide.md','src/contest.py','src/attest.py','latest/a.py','README.md'):
            with self.subTest(path=path):self.assertFalse(sources.TEST_PATH.search(path))
    def test_sources_listing_counts_and_last_sync(self):
        listed=self.cli('sources')[1];rows={r['name']:r for r in listed['sources']}
        self.assertGreater(rows['kb']['files'],5);self.assertEqual(rows['wiki']['files'],1);self.assertTrue(rows['kb']['synced']);self.assertEqual(rows['kb']['state'],'indexed')
        self.assertEqual(listed['index']['files'],rows['kb']['files']+1);self.assertGreater(listed['index']['chunks'],listed['index']['files'])
    def test_ask_sources_and_source_sync_are_read_only_for_subagents_and_viewers(self):
        for actor in ('subagent','viewer'):
            for argv in (('ask','retry backoff'),('sources',),('search','--sources','retry'),('source-sync',)):
                with self.subTest(actor=actor,argv=argv):self.assertEqual(self.cli('--actor',actor,*argv)[0],0)
            self.assertEqual(self.cli('--actor',actor,'source-plan','implicit',stdin='{"enabled": false}')[0],2)

class AutoAllow(Kb):
    def command(self,rest):return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'
    def decision(self,rest,tool='Bash',**extra):
        payload={'cwd':str(self.project),'session_id':'allow','tool_name':tool,'tool_input':{'command':self.command(rest)},**extra}
        return runtime.process('claude','pre-tool-use',payload,self.root)
    def test_ask_sources_and_source_sync_are_auto_allowed(self):
        for rest in ("ask 'how does retry work'",'sources',"search --sources 'retry'",'source-sync','source-sync --rebuild',"ask 'x' --limit 3"):
            with self.subTest(rest=rest):self.assertIn('allow',self.decision(rest))
    def test_subagents_may_run_them_but_not_the_plan(self):
        for rest in ("ask 'x'",'sources','source-sync'):
            with self.subTest(rest=rest):
                reply=self.decision(rest,agent_id='sub1');self.assertIn('allow',reply);self.assertNotIn('deny',reply)
    def test_source_plan_keeps_the_hosts_prompt_and_chained_commands_are_not_allowed(self):
        self.assertNotIn('allow',self.decision('source-plan add'));self.assertNotIn('allow',self.decision("ask 'x'; rm -rf ~"))
        self.assertNotIn('allow',self.decision("ask 'x' | cat"))
    def test_sources_json_is_protected_from_direct_edits(self):
        reply=runtime.process('claude','pre-tool-use',{'cwd':str(self.project),'session_id':'p','tool_name':'Write','tool_input':{'file_path':str(self.root/'sources.json'),'content':'{}'}},self.root)
        self.assertIn('deny',reply)

class Hooks(Kb):
    def setUp(self):
        super().setUp()
        enabled=patch.dict(os.environ,{'LIVING_MEMORY_NO_BACKGROUND':''});enabled.start();self.addCleanup(enabled.stop)
        self.popen=patch.object(sources.subprocess,'Popen');self.spawned=self.popen.start();self.addCleanup(self.popen.stop)
    def start(self,session='s1'):return self.hook('session-start',session)
    def test_session_start_starts_one_detached_sync_and_says_so(self):
        text=self.start()['context'];self.assertEqual(self.spawned.call_count,1)
        self.assertIn('Knowledge base: the first index is being built',text);self.assertIn("CMD ask '...'",text)
        argv=self.spawned.call_args.args[0];self.assertEqual(argv[-1],'source-sync');self.assertIn('--domain',argv);self.assertEqual(argv[argv.index('--domain')+1],'demo');self.assertEqual(argv[argv.index('--root')+1],str(self.root))
        kw=self.spawned.call_args.kwargs;self.assertEqual((kw['stdin'],kw['stdout'],kw['stderr']),(sources.subprocess.DEVNULL,)*3)
        if os.name=='nt':self.assertEqual(kw['creationflags']&0x208,0x208)
        else:self.assertTrue(kw['start_new_session'])
        self.start('s2');self.assertEqual(self.spawned.call_count,1)   # the cooldown: a sync was just started
    def test_the_line_names_the_files_and_the_date_and_a_fresh_index_spawns_nothing(self):
        self.sync();text=self.start()['context'];count=sources.state(self.root,'demo')['files']
        self.assertIn(f'Knowledge base: {count} files indexed (synced {sources.state(self.root,"demo")["synced"][:10]}); for questions about the project, CMD ask',text)
        self.assertEqual(self.spawned.call_count,0)
    def test_a_change_since_the_last_sync_spawns_again(self):
        self.sync();time.sleep(0.02);(self.repo/'README.md').write_text('# changed\n');self.start();self.assertEqual(self.spawned.call_count,1)
        self.assertIn('files indexed',self.start('s2')['context'])
    def test_no_sources_means_no_line_and_no_spawn(self):
        shutil.rmtree(self.repo);text=self.start()['context'];self.assertNotIn('Knowledge base',text);self.assertEqual(self.spawned.call_count,0)
    def test_the_check_stays_within_its_time_cap_and_a_failing_spawn_is_harmless(self):
        self.spawned.side_effect=OSError('no process');text=self.start()['context'];self.assertIn('Knowledge base',text);self.assertIn('Living Memory session=',text)
        self.sync();started=time.perf_counter();sources.stale(self.root,'demo');self.assertLess(time.perf_counter()-started,sources.STALE_SECONDS*3)
    def test_the_env_switch_turns_the_background_sync_off(self):
        with patch.dict(os.environ,{'LIVING_MEMORY_NO_BACKGROUND':'1'}):self.start()
        self.assertEqual(self.spawned.call_count,0)

class Prompts(Kb):
    def setUp(self):
        super().setUp();self.register();self.sync();self.hook('session-start')
    def prompt(self,text,session='s1',**extra):return self.hook('user-prompt',session,prompt=text,turn_id='t',**extra)
    def diary(self):
        from core.usage import events
        return [r for r in events(self.root) if r.get('k')=='source']
    def test_a_question_the_notes_cannot_answer_gets_cited_excerpts(self):
        context=self.prompt(LEDGER_QUESTION)['context']
        self.assertIn('Source excerpts from the project knowledge base',context);self.assertIn('README.md:',context);self.assertIn('exponential backoff',context);self.assertIn("CMD ask 'question'",context)
        block=context[context.index('Source excerpts'):];self.assertLessEqual(len(block),runtime.SOURCE_BUDGET+40);self.assertLessEqual(block.count('\n- '),runtime.SOURCE_EXCERPTS)
        self.assertEqual(len(self.diary()),1);self.assertTrue(self.diary()[0]['ref'].startswith('source:kb:README.md:'))
    def test_budget_and_count_hold_with_many_matches(self):
        for i in range(3):(self.repo/'docs'/f'retry{i}.md').write_text(f'# Retry {i}\n'+'Failed payment captures use exponential backoff with retry waits. '*20)
        self.sync();context=self.prompt(LEDGER_QUESTION,session='s2')['context'];block=context[context.index('Source excerpts'):]
        self.assertLessEqual(len(block),runtime.SOURCE_BUDGET+40);self.assertEqual(block.count('\n- '),runtime.SOURCE_EXCERPTS)
    def test_an_excerpt_is_not_repeated_in_the_same_session(self):
        self.prompt(LEDGER_QUESTION);again=self.prompt(LEDGER_QUESTION)['context'];self.assertNotIn('Source excerpts',again)
        self.assertIn('Source excerpts',self.prompt(LEDGER_QUESTION,session='other')['context'])
    def test_notes_that_answer_the_prompt_keep_the_excerpts_out(self):
        for i in range(2):self.note(f'Payment capture retry rule {i}',f'Failed payment captures retry with exponential backoff, rule {i}.',allow_similar=True)
        context=self.prompt(LEDGER_QUESTION,session='s3')['context'];self.assertIn('Payment capture retry rule',context);self.assertNotIn('Source excerpts',context)
        self.assertEqual(self.diary(),[])
    def test_one_note_leaves_room_for_excerpts(self):
        self.note('Payment capture retry rule','Failed payment captures retry with exponential backoff.')
        context=self.prompt(LEDGER_QUESTION,session='s4')['context'];self.assertIn('Payment capture retry rule',context);self.assertIn('Source excerpts',context)
    def test_unrelated_prompts_and_single_words_inject_nothing(self):
        for text in ('what is the weather like in lisbon tomorrow','continue','please summarize','tell me a joke about penguins','thanks, that looks good','service','how do I rebase my branch onto main','write a haiku about autumn leaves'):
            with self.subTest(text=text):self.assertNotIn('Source excerpts',self.prompt(text,session='neg-'+text[:6])['context'])
        self.assertEqual(self.diary(),[])
    def test_prompts_without_a_domain_index_or_with_a_machine_tag_skip_the_lookup(self):
        self.assertNotIn('Source excerpts',self.prompt('<task-notification>'+LEDGER_QUESTION+'</task-notification>',session='m')['context'])
        sources.db_path(self.root,'demo').unlink();self.assertNotIn('Source excerpts',self.prompt(LEDGER_QUESTION,session='n')['context'])
        with patch.object(sources,'search',side_effect=RuntimeError('boom')):self.assertIn('Living Memory session=',self.prompt('retry exponential backoff payment',session='o')['context'])
    def test_a_subagent_prompt_is_left_alone(self):
        self.assertNotIn('Source excerpts',self.prompt(LEDGER_QUESTION,session='sub',agent_id='a1')['context'])
    def test_a_long_task_brief_finds_the_excerpt_its_assignment_needs(self):
        boiler='Team rules for this sprint. Never push and never touch other branches. Report what changed, what you verified, what is left. '
        brief='\n\n'.join(['# Team brief',boiler*4,'## Your assignment',LEDGER_QUESTION+', then summarise it.','## Checklist',boiler*4])
        self.assertGreater(len(brief),runtime.LONG_PROMPT);context=self.prompt(brief,session='long')['context']
        self.assertIn('Source excerpts',context);self.assertIn('README.md:4-7',context);self.assertIn('exponential backoff',context);self.assertNotIn('event-sourcing',context)
    def test_a_list_prompts_items_each_reach_source_retrieval(self):
        boiler='Team rules for this sprint. Never push and never touch other branches. Report what changed, what you verified, what is left. '
        items=['1. do the blue green pipeline smoke tests page the pager rotation on monday?','2. does the dunning glossary cover payroll batches, schema migrations and currencies refunded within the window?','3. '+LEDGER_QUESTION+'?']
        prompt='\n\n'.join([boiler*5,*items]);self.assertGreater(len(prompt),runtime.LONG_PROMPT);self.assertEqual(len(runtime.list_items(prompt)),3)
        context=self.prompt(prompt,session='list')['context']
        self.assertIn('Source excerpts',context);self.assertIn('README.md:4-7',context);self.assertIn('exponential backoff',context)
    def test_trace_holds_refs_and_numbers_only(self):
        detail={};text,keys=runtime.prompt_sources(LEDGER_QUESTION,self.root,'demo',{},detail)
        self.assertEqual(set(detail),{'index','queries','candidates','injected'});self.assertEqual(detail['injected'],keys);self.assertNotIn('exponential',json.dumps(detail))

class Speed(Kb):
    def test_a_few_hundred_files_sync_and_answer_quickly(self):
        for i in range(300):
            path=self.repo/'gen'/f'd{i%10}'/f'f{i}.md';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(f'# Page {i}\nGenerated filler text number {i} about topic{i%7}.\n'*4)
        started=time.perf_counter();first=self.sync();built=time.perf_counter()-started;started=time.perf_counter();again=self.sync();idle=time.perf_counter()-started
        self.assertGreaterEqual(first['files'],300);self.assertEqual(again['sources'][0]['unchanged'],again['sources'][0]['files']+2);self.assertLess(built,30);self.assertLess(idle,5)
        runs=[];
        for i in range(10):
            started=time.perf_counter();sources.search(self.root,'demo','exponential backoff retry payment');runs.append(time.perf_counter()-started)
        self.assertLess(sorted(runs)[-1],0.3)

if __name__=='__main__':unittest.main()
