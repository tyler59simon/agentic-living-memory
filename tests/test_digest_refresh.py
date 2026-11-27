"""core/digest_refresh.py and the digest_refresh setting: off by default, validated, scheduled at most once a day from SessionStart, never
from a headless or subagent session. No agent process is started: the spawn is patched."""
import json,os,unittest,unittest.mock
from test_core import StoreFixture
from core import digest_refresh,settings

ON={'digest_refresh':{'daily_budget_usd':'1.00','model':'haiku'}}

class Setting(unittest.TestCase):
    def test_a_budget_and_model_are_stored_in_one_shape(self):
        self.assertEqual(settings.validate('digest_refresh',{'daily_budget_usd':1}),{'daily_budget_usd':'1.00','model':'haiku'})
        self.assertEqual(settings.validate('digest_refresh',{'daily_budget_usd':'2.5','model':'sonnet'}),{'daily_budget_usd':'2.50','model':'sonnet'})
        self.assertIsNone(settings.validate('digest_refresh',None))
    def test_bad_values_are_refused(self):
        for value in ({},{'daily_budget_usd':0},{'daily_budget_usd':'51'},{'daily_budget_usd':True},{'daily_budget_usd':'x'},
                      {'daily_budget_usd':1,'model':'a b'},{'daily_budget_usd':1,'extra':1},'1.00'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'invalid_value'):settings.validate('digest_refresh',value)
    def test_it_is_off_unless_set(self):
        self.assertIsNone(digest_refresh.setting({}));self.assertIsNone(digest_refresh.setting({'digest_refresh':{'daily_budget_usd':'0'}}))
        self.assertEqual(digest_refresh.setting(ON),{'daily_budget_usd':1.0,'model':'haiku'})

class Schedule(StoreFixture):
    def setUp(self):
        super().setUp()
        patcher=unittest.mock.patch.dict(os.environ,{},clear=False)
        patcher.start();self.addCleanup(patcher.stop)
        for name in ('LIVING_MEMORY_NO_BACKGROUND','LIVING_MEMORY_MAINTENANCE_CHILD'):os.environ.pop(name,None)
        self.spawned=[]
        spawn=unittest.mock.patch.object(digest_refresh,'_spawn',lambda root,config:self.spawned.append(config))
        spawn.start();self.addCleanup(spawn.stop)
        quiet=unittest.mock.patch('core.runtime.headless',lambda:False)
        quiet.start();self.addCleanup(quiet.stop)
    def test_off_by_default(self):
        self.assertEqual(digest_refresh.schedule(self.root,{}),'disabled');self.assertEqual(self.spawned,[])
    def test_it_starts_once_a_day(self):
        self.assertEqual(digest_refresh.schedule(self.root,ON),'spawned')
        self.assertEqual(digest_refresh.schedule(self.root,ON),'cooldown')
        self.assertEqual(self.spawned,[{'daily_budget_usd':1.0,'model':'haiku'}])
        marker=self.root/'hooks'/digest_refresh.MARKER
        marker.write_text(json.dumps({'at':json.loads(marker.read_text(encoding='utf-8'))['at']-digest_refresh.EVERY}),encoding='utf-8')
        self.assertEqual(digest_refresh.schedule(self.root,ON),'spawned');self.assertEqual(len(self.spawned),2)
    def test_headless_subagent_plan_and_compact_sessions_never_start_it(self):
        self.assertEqual(digest_refresh.schedule(self.root,ON,subagent=True),'subagent')
        self.assertEqual(digest_refresh.schedule(self.root,ON,plan=True),'plan_mode')
        self.assertEqual(digest_refresh.schedule(self.root,ON,compact=True),'compact')
        with unittest.mock.patch('core.runtime.headless',lambda:True):self.assertEqual(digest_refresh.schedule(self.root,ON),'headless')
        with unittest.mock.patch.dict(os.environ,{'LIVING_MEMORY_NO_BACKGROUND':'1'}):self.assertEqual(digest_refresh.schedule(self.root,ON),'background_disabled')
        self.assertEqual(self.spawned,[])
    def test_the_worker_refreshes_stale_digests_only_within_the_budget(self):
        argv=digest_refresh.argv(self.root,{'daily_budget_usd':1.5,'model':'haiku'})
        self.assertTrue(argv[1].endswith('ingest_run.py'));self.assertTrue(os.path.isfile(argv[1]))
        for flag in ('--every-domain','--stale-only'):self.assertIn(flag,argv)
        self.assertEqual(argv[argv.index('--budget-usd')+1],'1.50');self.assertEqual(argv[argv.index('--model')+1],'haiku')
    def test_config_set_stores_it_and_null_turns_it_off(self):
        out=settings.set_(self.root,'digest_refresh','{"daily_budget_usd": "1", "model": "haiku"}')
        self.assertEqual(out['value'],{'daily_budget_usd':'1.00','model':'haiku'});self.assertFalse(out['default'])
        self.assertEqual(settings.get(self.root,'digest_refresh')['settings']['digest_refresh']['value'],{'daily_budget_usd':'1.00','model':'haiku'})
        self.assertTrue(settings.set_(self.root,'digest_refresh','null')['default'])

if __name__=='__main__':unittest.main()
