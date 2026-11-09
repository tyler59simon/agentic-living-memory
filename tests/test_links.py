"""links: the [[Title]] and supersedes graph an app draws, and error codes that note text cannot change."""
import contextlib,io,json,unittest
from test_core import StoreFixture
from core.cli import main,error_code
from core.init_domain import init_domain
from core.notes import capture,change

TAGS=['subject/project','concern/architecture']

class Links(StoreFixture):
    def setUp(self):
        super().setUp()
        # 'child' is bound inside demo's folder, so demo is its parent domain.
        (self.base/'project'/'sub').mkdir(parents=True,exist_ok=True)
        init_domain('child',[str(self.base/'project'/'sub')],self.root)
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def add(self,title,body,domain='demo',**kw):
        return capture(title,body,TAGS,domain,self.root,**kw)['id']

    def test_links_resolve_like_title_refs(self):
        cache=self.add('Cache','The cache is rebuildable.')
        parent=self.add('Parent rule','Parent domain rule.')
        shared=self.add('Shared fact','Holds everywhere.',domain='_global')
        child=self.add('Child note','See [[cache|the cache]], [[Parent Rule#why]], [[shared fact]] and [[Nowhere]].',domain='child')
        source=self.add('Uses cache','Depends on [[CACHE]] and itself [[Uses cache]].')
        code,result=self.cli('links','--domain','child','--actor','viewer')
        self.assertEqual(code,0,result)
        edges={(e['from'],e['to'],e['kind']) for e in result['edges']}
        # The child domain sees its parent (demo) and the global domain; 'cache' is in demo, not child.
        self.assertIn(('child:'+child,'demo:'+cache,'link'),edges)
        self.assertIn(('child:'+child,'demo:'+parent,'link'),edges)
        self.assertIn(('child:'+child,'_global:'+shared,'link'),edges)
        self.assertEqual(result['unresolved'],[{'from':'child:'+child,'target':'Nowhere'}])
        self.assertEqual({n['ref'] for n in result['nodes']},{'child:'+child,'demo:'+cache,'demo:'+parent,'_global:'+shared})
        _,demo=self.cli('links','--domain','demo')
        self.assertEqual([(e['from'],e['to']) for e in demo['edges']],[('demo:'+source,'demo:'+cache)])  # no self-link

    def test_supersedes_edges_need_any_status(self):
        old=self.add('Old layout','The cache lived beside the notes.')
        new=self.add('New layout','The cache lives in its own folder.')
        change(old,'demo','supersede',self.root,superseded_by=new)
        _,active=self.cli('links','--domain','demo')
        self.assertEqual(active['edges'],[])
        _,every=self.cli('links','--domain','demo','--status','any')
        self.assertEqual(every['edges'],[{'from':'demo:'+new,'to':'demo:'+old,'kind':'supersedes'}])
        self.assertEqual({n['ref']:n['status'] for n in every['nodes']}['demo:'+old],'superseded')

    def test_active_title_wins_over_retired_and_all_domains(self):
        archived=self.add('Cache','Old cache text.')
        change(archived,'demo','archive',self.root)
        live=self.add('Cache','New cache text.',allow_similar=True)
        linker=self.add('Linker','Points at [[Cache]].',domain='child')
        _,result=self.cli('links','--all-domains','--status','any')
        self.assertIn({'from':'child:'+linker,'to':'demo:'+live,'kind':'link'},result['edges'])
        self.assertTrue({'demo','child'}<=set(result['domains']))
        code,_=self.cli('--cwd',str(self.base),'links');self.assertEqual(code,2)

    def test_error_code_ignores_note_text(self):
        self.assertEqual(error_code('possible_duplicate conflict: similar to d:1 "Stale cache bug". If it is'),'conflict')
        self.assertEqual(error_code('Unknown tags require an approved taxonomy plan: concern/stale-data'),'invalid')
        self.assertEqual(error_code("No Action Item matches 'stale'. Items: 1. x"),'invalid')
        self.assertEqual(error_code('Recovery conflicts with a user edit: domains/d/notes/BUG - stale-1.md'),'conflict')
        for stale in ('stale_revision','Stale revision: domains/d/notes/x.md','stale plan: d:1 was added','stale_filtered_index: rebuild required'):
            self.assertEqual(error_code(stale),'stale',stale)
        self.assertEqual(error_code('Undo conflicts with later edits'),'conflict')

    def test_duplicate_title_containing_stale_exits_as_conflict(self):
        self.add('Stale cache bug','The cache goes stale after a deploy.')
        code,result=self.cli('--domain','demo','capture','--title','Stale cache bugs','--summary','Different words entirely here.',
                             '--tag','subject/project','--tag','concern/architecture')
        self.assertEqual(code,5,result);self.assertEqual(result['error']['code'],'conflict')

if __name__=='__main__':unittest.main()
