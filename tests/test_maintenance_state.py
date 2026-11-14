import json,shutil,threading,unittest
from datetime import datetime,timedelta,timezone
from core import maintenance_state as ms
from core import repository as repo
from core.runtime import protected_rule
from test_core import StoreFixture

T0=datetime(2026,3,10,12,0,tzinfo=timezone.utc)
H=timedelta(hours=1)
DAY=timedelta(days=1)

class Base(StoreFixture):
    def setUp(self):
        super().setUp()
        self.cfg=ms.validate({'enabled':True,'domains':['demo']})
        ms.set_config(self.root,self.cfg)
    def reserve(self,at=T0,n=3,cfg=None):return ms.reserve(self.root,cfg or self.cfg,n,at)
    def code(self,at=T0):return ms.eligibility(self.root,at)['skip_reason']
    def runs(self):return sorted((self.root/'maintenance/runs').glob('*/*.json'))
    def change(self,**kw):
        self.cfg=dict(self.cfg,**kw);ms.set_config(self.root,self.cfg);return ms.config(self.root)

class Config(Base):
    def test_defaults_are_disabled(self):
        (self.root/'maintenance.json').unlink()
        self.assertFalse(ms.config(self.root)['enabled']);self.assertEqual(self.code(),'disabled')
    def test_rejected_values(self):
        for bad in ({'max_notes_per_day':-1},{'max_notes_per_day':True},{'timeout_seconds':0},{'min_interval_seconds':1.5},{'daily_budget_usd':'NaN'},
                    {'daily_budget_usd':'1e999'},{'daily_budget_usd':'-1'},{'daily_budget_usd':1.0},{'daily_budget_usd':'0'},{'model':'Sonnet!'},{'enabled':1},{'surprise':1}):
            with self.subTest(bad=bad),self.assertRaises(ValueError):ms.validate(bad)
        with self.assertRaises(ValueError):ms.set_config(self.root,{'domains':['nope']})
    def test_global_domain_needs_explicit_opt_in(self):
        (self.root/'domains/_global').mkdir(parents=True)
        with self.assertRaises(ValueError):ms.set_config(self.root,{'domains':['_global']})
        self.assertEqual(ms.set_config(self.root,{'domains':['_global']},allow_global=True)['config']['domains'],['_global'])
    def test_malformed_file_raises(self):
        (self.root/'maintenance.json').write_text('{nope')
        with self.assertRaises(ValueError):ms.config(self.root)
    def test_unknown_policy_version(self):
        self.change(policy_version=2);self.assertEqual(self.code(),'policy_unknown')
    def test_revision_is_stable(self):
        r=ms.config_revision(ms.config(self.root));ms.set_config(self.root,ms.config(self.root))
        self.assertEqual(ms.config_revision(ms.config(self.root)),r)
        with self.assertRaises(ValueError):ms.set_config(self.root,dict(self.cfg,model='haiku'),expected_revision='0'*64)
    def test_money(self):
        self.assertEqual(ms.parse_usd('1.00'),1_000_000);self.assertEqual(ms.format_usd(420000),'0.42');self.assertEqual(ms.format_usd(12300),'0.0123')
        self.assertEqual(ms.format_usd(3000000),'3.00');self.assertEqual(ms.micro_from_provider(0.25),250000);self.assertIsNone(ms.micro_from_provider(float('nan')))

class Allowances(Base):
    def test_ten_notes_count_as_ten(self):
        cfg=self.change(min_interval_seconds=1)
        run=self.reserve(n=10,cfg=cfg);ms.launch(self.root,run,T0);ms.finish(self.root,run,0,'completed',T0)
        self.assertEqual(self.code(T0+H),'daily_note_limit')
    def test_reserve_is_capped_by_remaining_notes(self):
        self.assertEqual(self.reserve(n=50)['reserved_notes'],10)
    def test_interval_includes_dry_runs(self):
        run=self.reserve();ms.launch(self.root,run,T0);ms.finish(self.root,run,1000,'dry_run',T0)
        self.assertEqual(self.code(T0+H),'interval')
        with self.assertRaises(ms.MaintenanceSkip) as cm:self.reserve(T0+H)
        self.assertEqual(cm.exception.reason,'interval')
        self.assertTrue(ms.eligibility(self.root,T0+DAY+timedelta(seconds=1))['eligible'])
    def test_utc_midnight_resets_counters_not_interval(self):
        late=datetime(2026,3,10,23,0,tzinfo=timezone.utc);after=datetime(2026,3,11,0,30,tzinfo=timezone.utc)
        run=self.reserve(late);ms.launch(self.root,run,late);ms.finish(self.root,run,500000,'completed',late)
        self.assertEqual(self.code(after),'interval')
        acc=ms.state(self.root,after);self.assertEqual((acc['reviewed'],acc['charged_micro'],acc['day']),(0,0,'2026-03-11'))
        self.change(min_interval_seconds=3600);self.assertTrue(ms.eligibility(self.root,after)['eligible'])
    def test_backward_clock(self):
        run=self.reserve();ms.launch(self.root,run,T0);back=T0-2*DAY
        self.assertEqual(self.code(back),'interval')
        acc=ms.state(self.root,back);self.assertEqual((acc['reviewed'],acc['last_attempt_at']),(3,run['attempted_at']))
        ms.finish(self.root,run,10,'completed',back)
        self.assertEqual(ms.state(self.root,back)['last_attempt_at'],run['attempted_at'])
    def test_daily_budget(self):
        cfg=self.change(min_interval_seconds=1);run=self.reserve(cfg=cfg);ms.launch(self.root,run,T0)
        self.assertEqual(run['reserved_micro'],1_000_000);self.assertEqual(self.code(T0+H),'daily_budget')
    def test_disable_and_reenable_keep_accounting(self):
        run=self.reserve();ms.launch(self.root,run,T0);ms.finish(self.root,run,250000,'completed',T0)
        before=ms.state(self.root,T0+H)
        self.change(enabled=False);self.assertEqual(self.code(T0+H),'disabled')
        self.change(enabled=True);self.assertEqual(ms.state(self.root,T0+H),before);self.assertEqual(self.code(T0+H),'interval')

class Damage(Base):
    def test_cache_deletion_changes_nothing(self):
        run=self.reserve();ms.launch(self.root,run,T0);before=ms.state(self.root,T0+H)
        shutil.rmtree(self.root/'.cache',ignore_errors=True)
        self.assertEqual(ms.state(self.root,T0+H),before)
    def test_state_deletion_rebuilds_without_new_allowance(self):
        run=self.reserve();ms.launch(self.root,run,T0);ms.finish(self.root,run,300000,'completed',T0)
        before=ms.state(self.root,T0+H);(self.root/ms.STATE).unlink()
        after=ms.state(self.root,T0+H)
        for k in ('reviewed','charged_micro','reserved_micro','last_attempt_at'):self.assertEqual(after[k],before[k],k)
        self.assertEqual(self.code(T0+H),'interval')
        (self.root/ms.STATE).write_text('{garbage');self.assertEqual(ms.state(self.root,T0+H)['reviewed'],3)
    def test_state_deletion_across_midnight_keeps_interval(self):
        late=datetime(2026,3,10,23,0,tzinfo=timezone.utc);run=self.reserve(late);ms.launch(self.root,run,late);ms.finish(self.root,run,1,'completed',late)
        (self.root/ms.STATE).unlink();self.assertEqual(self.code(datetime(2026,3,11,0,30,tzinfo=timezone.utc)),'interval')
    def test_corrupt_run_record_blocks(self):
        run=self.reserve();ms.launch(self.root,run,T0);self.runs()[0].write_text('{"bad":')
        self.assertEqual(self.code(T0+H),'accounting_unavailable')
        with self.assertRaises(ms.MaintenanceSkip) as cm:self.reserve(T0+H)
        self.assertEqual(cm.exception.reason,'accounting_unavailable')
    def test_runs_directory_damage(self):
        (self.root/'maintenance').mkdir(exist_ok=True);(self.root/'maintenance/runs').write_text('x')
        self.assertEqual(self.code(),'accounting_unavailable')
    def test_crash_before_launch_releases_money_keeps_interval(self):
        self.reserve();self.assertEqual(len(ms.recover_runs(self.root,T0+H)),1)
        acc=ms.state(self.root,T0+H);self.assertEqual((acc['reserved_notes'],acc['reserved_micro'],acc['charged_micro'],acc['reviewed']),(0,0,0,0))
        self.assertEqual(self.code(T0+H),'interval');self.assertEqual(ms.recover_runs(self.root,T0+H),[])
        (self.root/ms.STATE).unlink();self.assertEqual(ms.state(self.root,T0+H)['reserved_notes'],0)
    def test_failed_release_holds_until_config_changes(self):
        run=self.reserve();ms.release(self.root,run,'adapter_missing',T0)
        self.assertEqual(self.code(T0+H),'interval')
        self.change(model='haiku');self.assertTrue(ms.eligibility(self.root,T0+H)['eligible'])
        with self.assertRaises(ValueError):ms.release(self.root,run,'again',T0)
    def test_crash_after_launch_charges_full_reservation(self):
        run=self.reserve();ms.launch(self.root,run,T0);ms.recover_runs(self.root,T0+H)
        acc=ms.state(self.root,T0+H);self.assertEqual((acc['charged_micro'],acc['reserved_micro'],acc['reviewed']),(1_000_000,0,3));self.assertFalse(acc['cost_complete'])
        self.assertEqual(json.loads(self.runs()[0].read_text())['state'],'interrupted')
        (self.root/ms.STATE).unlink();self.assertFalse(ms.state(self.root,T0+H)['cost_complete'])
    def test_unknown_usage_charges_reservation(self):
        run=self.reserve();ms.launch(self.root,run,T0);done=ms.finish(self.root,run,None,'failed',T0,'timeout')
        self.assertEqual((done['charged_micro'],done['cost_complete']),(1_000_000,False))
        with self.assertRaises(ValueError):ms.finish(self.root,run,1,'completed',T0)
    def test_provider_overrun_blocks(self):
        run=self.reserve();ms.launch(self.root,run,T0);done=ms.finish(self.root,run,1_500_000,'completed',T0)
        self.assertEqual(done['charged_micro'],1_500_000);self.assertEqual(self.code(T0+2*DAY),'provider_budget')
        (self.root/ms.STATE).unlink();self.assertEqual(self.code(T0+DAY),'provider_budget')
        self.change(model='haiku');self.assertTrue(ms.eligibility(self.root,T0+2*DAY)['eligible'])

class Concurrency(Base):
    def test_two_reserves_make_one_run(self):
        out=[];gate=threading.Barrier(2)
        def go():
            gate.wait()
            try:out.append(ms.reserve(self.root,self.cfg,3,T0))
            except ms.MaintenanceSkip as exc:out.append(exc.reason)
        ts=[threading.Thread(target=go) for _ in range(2)];[t.start() for t in ts];[t.join() for t in ts]
        self.assertEqual(len(self.runs()),1);self.assertEqual(sorted(type(o).__name__ for o in out),['dict','str']);self.assertIn('interval',out)
    def test_worker_lock(self):
        self.assertFalse(ms.running(self.root))
        with ms.worker_lock(self.root):
            self.assertTrue(ms.running(self.root))
            with self.assertRaises(ms.MaintenanceSkip) as cm,ms.worker_lock(self.root):pass
            self.assertEqual(cm.exception.reason,'already_running')
        self.assertFalse(ms.running(self.root))
    def test_decision_and_note_replay_idempotently(self):
        run=ms.launch(self.root,self.reserve(),T0)
        changes,expected,op,new=ms.decision_changes(self.root,run,'demo:abc',{'action':'keep','result':'kept','reason_code':None})
        self.assertRegex(op,r'^m-[0-9a-f]{40}$');self.assertEqual(op,ms.operation_id(run['id'],'demo:abc','keep'))
        changes['domains/demo/notes/x.md']='note\n';expected['domains/demo/notes/x.md']=None
        first=repo.transaction(self.root,changes,'maintenance_note',expected=expected,operation_id=op)
        again=repo.transaction(self.root,changes,'maintenance_note',expected=expected,operation_id=op)
        self.assertTrue(first['ok'] and again['ok'])
        self.assertEqual(json.loads(self.runs()[0].read_text())['decisions']['demo:abc']['operation_id'],op)
        self.assertTrue(repo.BOOKKEEPING_KINDS.isdisjoint({'maintenance_run','maintenance_config'}))

class Retry(Base):
    def test_hold_and_clear(self):
        ms.set_retry(self.root,'demo:a','r1','f1','kept',T0);st=ms.state(self.root,T0)
        self.assertTrue(ms.retry_hold(st,'demo:a','r1','f1',T0+6*DAY))
        self.assertIsNone(ms.retry_hold(st,'demo:a','r1','f1',T0+8*DAY))
        self.assertIsNone(ms.retry_hold(st,'demo:a','r2','f1',T0+H));self.assertIsNone(ms.retry_hold(st,'demo:a','r1','f2',T0+H))

class Protection(Base):
    def test_protected_paths(self):
        for p in ('maintenance.json','maintenance/state.json','maintenance/runs/2026-03-10/x.json'):
            self.assertEqual(protected_rule(str(self.root/p),str(self.base),self.root),'config',p)

if __name__=='__main__':unittest.main()
