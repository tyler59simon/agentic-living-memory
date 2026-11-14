"""recap: the activity window, maintenance runs, open work and review counts, the CLI and the Obsidian Recap page."""
import contextlib,io,json,unittest
from datetime import datetime,timedelta
from unittest.mock import patch
from core import notes,runtime,sync,recap as recap_mod
from core.cli import main
from core.note_format import render
from test_maintenance import Base,OLD,TAGS

class Recap(Base):
    def setUp(self):
        super().setUp()
        self.cache=self.mk('Cache policy','The cache is rebuildable.')
        self.old=self.mk('Sprint status','Currently migrating.',type='status',review_by=OLD)
        self.mk('Rollout plan',render('Roll out v2.',actions=['Migrate users','Delete v1'],done=['Plan it']),type='plan')
        notes.change(self.cache['id'],'demo','update',self.root,body='The cache is rebuildable and bounded.')
        notes.change(self.cache['id'],'demo','update',self.root,body='The cache is rebuildable and bounded by size.')
        self.run=self.go((self.old,'archive',{'rule':'expired_status','reason':'The sprint ended.'}))
        self.mk('Late status','Still current.',type='status',review_by=OLD)   # due for review after the run
        self.at=self.now+timedelta(hours=1)
    def recap(self,**kw):return recap_mod.recap(self.root,['demo'],now=self.at,**kw)
    def cli(self,*argv,actor='main'):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:code=main(['--root',str(self.root),'--actor',actor,'--domain','demo',*argv])
            except SystemExit as exc:code=exc.code
        return code,json.loads(out.getvalue() or err.getvalue())

    def test_counts_groups_and_collapsed_repeats(self):
        r=self.recap();self.assertTrue(r['ok']);self.assertEqual(r['domains'],['demo'])
        self.assertEqual((r['counts']['created'],r['counts']['updated'],r['counts']['archived']),(4,2,1))
        self.assertEqual([(c['title'],c['count'],c['actor']) for c in r['changes']['updated']],[('Cache policy',2,'command')])
        arch=r['changes']['archived'][0];self.assertEqual((arch['title'],arch['actor']),('Sprint status','maintenance'));self.assertIn('expired_status',arch['summary'])
        self.assertNotIn('confirmed',r['changes'])
    def test_maintenance_section(self):
        m=self.recap()['maintenance'];self.assertEqual((m['runs'],m['retired']),(1,1));self.assertEqual(m['charged_usd'],'0.001')
        n=m['retired_notes'][0];self.assertEqual((n['title'],n['action'],n['rule']),('Sprint status','archive','expired_status'));self.assertTrue(n['reason'])
    def test_maintenance_counts_only_runs_that_reviewed_this_scope(self):
        from core import maintenance
        real=maintenance._recent(self.root);other={**real[0],'id':'other-run','charged_micro':5000000,
            'selected':[{**s,'ref':'other:'+s['ref'].partition(':')[2]} for s in real[0]['selected']],
            'decisions':{'other:'+r.partition(':')[2]:d for r,d in real[0]['decisions'].items()}}
        with patch('core.maintenance._recent',return_value=[other,*real]):m=self.recap()['maintenance']
        self.assertEqual((m['runs'],m['retired'],m['charged_usd']),(1,1,'0.001'));self.assertEqual(m['reviewed'],len(real[0]['selected']))
        with patch('core.maintenance._recent',return_value=[other]):m=self.recap()['maintenance']
        self.assertEqual((m['runs'],m['reviewed'],m['retired']),(0,0,0))
    def test_open_work_and_review(self):
        r=self.recap();self.assertEqual((r['open_work']['notes'],r['open_work']['items']),(1,2))
        self.assertEqual((r['open_work']['top'][0]['title'],r['open_work']['top'][0]['open']),('Rollout plan',2))
        self.assertGreaterEqual(r['review']['due'],1);self.assertGreaterEqual(sum(r['review']['by_kind'].values()),1)
    def test_markdown_sections(self):
        text=self.recap()['markdown'];self.assertTrue(text.startswith('# Recap: '))
        for h in ('## Summary','## Created (4)','## Updated (1)','## Archived (1)','## Maintenance','## Sessions','## Open work','## Due for review'):self.assertIn(h,text)
        self.assertIn('- Cache policy (command): ',text);self.assertIn('(2 times)',text);self.assertNotIn('—',text)
    def test_the_window_excludes_older_rows(self):
        r=recap_mod.recap(self.root,['demo'],days=7,now=self.at+timedelta(days=30))
        self.assertEqual((r['counts'],r['maintenance']['runs']),({},0));self.assertIn('No maintenance runs.',r['markdown'])
    def test_group_is_capped_in_the_markdown(self):
        for i in range(17):self.mk(f'Extra note {i}',f'Extra fact {i}.')
        r=self.recap();self.assertEqual(len(r['changes']['created']),21);self.assertIn('- and 6 more',r['markdown'])
    def test_project_filter(self):
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':[*TAGS,'project/alpha']}))
        notes.capture('Alpha work','Alpha fact.',TAGS+['project/alpha'],'demo',self.root,allow_similar=True)
        r=self.recap(project='alpha');self.assertEqual([c['title'] for c in r['changes']['created']],['Alpha work']);self.assertEqual(r['maintenance']['runs'],1)
    def test_cli_for_every_actor_and_bounds(self):
        for actor in ('main','viewer','subagent'):
            code,r=self.cli('recap',actor=actor);self.assertEqual(code,0,r);self.assertEqual(r['days'],7);self.assertEqual(r['counts']['created'],4)
        code,r=self.cli('recap','--days','1','--all-domains');self.assertEqual((code,r['domains']),(0,['demo']))
        self.assertNotEqual(self.cli('recap','--days','91')[0],0)
    def test_allow_lists(self):
        from core import cli
        self.assertIn('recap',runtime.ALLOWED_SUBCOMMANDS);self.assertIn('recap',cli.READ_ONLY);self.assertIn('recap',cli.VIEWER)

class Overlay(Base):
    def setUp(self):
        super().setUp();self.vault=self.base/'vault';self.mk('Cache policy','The cache is rebuildable.')
    def page(self):return (self.vault/'Dashboards/Recap.md').read_text(encoding='utf-8')
    def test_page_and_home_link_and_one_computation_per_day(self):
        sync.build_overlay('demo',self.vault,self.root)
        self.assertIn('# Recap: ',self.page());self.assertIn('Cache policy',self.page())
        self.assertIn('[[Dashboards/Recap|Recap]]',(self.vault/'Dashboards/Home.md').read_text(encoding='utf-8'))
        with patch('core.recap.recap',side_effect=AssertionError('recomputed')) as spy:
            sync.build_overlay('demo',self.vault,self.root);self.assertEqual(spy.call_count,0)
        self.assertEqual(json.loads((self.root/'.cache/recap-demo.json').read_text(encoding='utf-8'))['day'],datetime.now().date().isoformat())
    def test_a_new_day_recomputes(self):
        sync.build_overlay('demo',self.vault,self.root)
        (self.root/'.cache/recap-demo.json').write_text(json.dumps({'day':'2000-01-01','markdown':'# stale\n'}),encoding='utf-8')
        sync.build_overlay('demo',self.vault,self.root);self.assertIn('# Recap: ',self.page())
    def test_a_failing_recap_gives_the_fallback_page(self):
        with patch('core.recap.recap',side_effect=RuntimeError('boom')):sync.build_overlay('demo',self.vault,self.root)
        self.assertIn('Recap unavailable',self.page());self.assertFalse((self.root/'.cache/recap-demo.json').exists())

if __name__=='__main__':unittest.main()
