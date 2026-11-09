"""The surface a desktop app uses: serve --stdio, list, domains, version and the viewer actor."""
import contextlib,io,json,os,subprocess,sys,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core.cli import main
from core.notes import change,find_note

SCRIPT=Path(__file__).resolve().parents[1]/'living_memory.py'

class AppBridge(StoreFixture):
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def serve(self,*requests):
        lines=''.join((r if isinstance(r,str) else json.dumps(r))+'\n' for r in requests)
        out=io.StringIO()
        with patch('sys.stdin',io.StringIO(lines)),contextlib.redirect_stdout(out):code=main(['--root',str(self.root),'serve','--stdio'])
        self.assertEqual(code,0)
        return [json.loads(line) for line in out.getvalue().splitlines()]
    def diary(self):
        return [json.loads(l) for p in (self.root/'.turns').glob('usage-*.jsonl') for l in p.read_text(encoding='utf-8').splitlines()]

    def test_version_lists_commands(self):
        code,result=self.cli('version')
        self.assertEqual(code,0);self.assertEqual(result['api'],1)
        self.assertTrue({'serve','list','domains','version'}<=set(result['commands']))

    def test_list_pages_filters_and_reports_revisions(self):
        a=self.note('Cache','The local cache is rebuildable.');b=self.note('Queue','The queue is durable.',type='decision')
        change(a['id'],'demo','archive',self.root)
        code,result=self.cli('--domain','demo','list')
        self.assertEqual(code,0,result);self.assertEqual([n['id'] for n in result['notes']],[b['id']])
        row=result['notes'][0]
        self.assertEqual(row['revision'],find_note(b['id'],'demo',self.root)[1]['revision'])
        self.assertTrue(row['path'].startswith('domains/demo/notes/'));self.assertEqual(row['ref'],'demo:'+b['id'])
        _,every=self.cli('--domain','demo','list','--status','any');self.assertEqual(every['total'],2)
        _,archived=self.cli('--domain','demo','list','--status','archived');self.assertEqual([n['id'] for n in archived['notes']],[a['id']])
        _,typed=self.cli('--domain','demo','list','--status','any','--type','decision');self.assertEqual([n['id'] for n in typed['notes']],[b['id']])
        _,tagged=self.cli('--domain','demo','list','--tag','concern/decision');self.assertEqual(tagged['notes'],[])
        _,page=self.cli('--domain','demo','list','--status','any','--offset','1','--limit','1')
        self.assertEqual((page['total'],len(page['notes'])),(2,1))

    def test_list_needs_a_domain_unless_all(self):
        self.note()
        code,result=self.cli('--cwd',str(self.base),'list');self.assertEqual(code,2);self.assertIn('unbound',result['error']['message'])
        code,result=self.cli('--cwd',str(self.base),'list','--all-domains');self.assertEqual((code,result['total']),(0,1))

    def test_domains_reports_counts_and_scope(self):
        a=self.note();self.note('Queue','The queue is durable.');change(a['id'],'demo','archive',self.root)
        code,result=self.cli('domains')
        self.assertEqual(code,0);demo=next(d for d in result['domains'] if d['name']=='demo')
        self.assertEqual((demo['bound'],demo['active'],demo['archived']),(True,1,1))
        self.assertEqual(result['global_domains'],['_global'])

    def test_viewer_reads_leave_no_diary_and_cannot_write(self):
        n=self.note()
        code,_=self.cli('--domain','demo','read',n['id'],'--actor','viewer');self.assertEqual(code,0)
        self.assertEqual([e for e in self.diary() if e['e']=='read'],[])
        self.cli('--domain','demo','read',n['id']);self.assertEqual(len([e for e in self.diary() if e['e']=='read']),1)
        code,result=self.cli('--domain','demo','update',n['id'],'--body','x','--actor','viewer')
        self.assertEqual(code,2);self.assertIn('Viewers are read-only',result['error']['message'])
        code,_=self.cli('--domain','demo','taxonomy','--actor','viewer');self.assertEqual(code,0)
        code,_=self.cli('--domain','demo','serve','--stdio','--actor','viewer');self.assertEqual(code,2)

    def test_serve_answers_each_request_and_survives_errors(self):
        n=self.note();revision=find_note(n['id'],'demo',self.root)[1]['revision']
        payload=json.dumps({'title':'Queue','body':'The queue is durable.','tags':['subject/project','concern/architecture']})
        replies=self.serve({'id':1,'argv':['version']},
                           'not json',
                           {'id':3,'argv':['serve','--stdio']},
                           {'id':4,'argv':['list','--domain','demo','--bogus']},
                           {'id':5,'argv':['update',n['id'],'--domain','demo','--body','Stale edit.','--expected-revision','0'*64]},
                           {'id':6,'argv':['update',n['id'],'--domain','demo','--body','The cache is rebuilt nightly.','--expected-revision',revision]},
                           {'id':7,'argv':['capture','--domain','demo'],'stdin':payload},
                           {'id':8,'argv':['list','--domain','demo','--actor','viewer']})
        self.assertEqual([r['id'] for r in replies],[1,None,3,4,5,6,7,8])
        self.assertEqual((replies[0]['exit_code'],replies[0]['result']['api']),(0,1))
        self.assertEqual([r['exit_code'] for r in replies[1:4]],[2,2,2])
        self.assertEqual((replies[4]['exit_code'],replies[4]['error']['code']),(6,'stale'))
        self.assertEqual((replies[5]['exit_code'],replies[5]['error']),(0,None))
        self.assertEqual(replies[6]['result']['status'],'created')
        self.assertEqual(replies[7]['result']['total'],2)

    def test_serve_over_a_real_pipe(self):
        self.note()
        env=dict(os.environ,PYTHONIOENCODING='utf-8')
        with subprocess.Popen([sys.executable,str(SCRIPT),'--root',str(self.root),'serve','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,text=True,encoding='utf-8',env=env) as proc:
            # Each reply arrives before the next request is sent: the app waits on one line at a time.
            for i,argv in enumerate((['version'],['list','--domain','demo','--actor','viewer'])):
                proc.stdin.write(json.dumps({'id':i,'argv':argv})+'\n');proc.stdin.flush()
                reply=json.loads(proc.stdout.readline());self.assertEqual((reply['id'],reply['exit_code']),(i,0),reply)
            proc.stdin.close();self.assertEqual(proc.wait(timeout=30),0)

if __name__=='__main__':unittest.main()
