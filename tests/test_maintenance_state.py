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

class CheckpointLoss(Base):
    def test_rebuild_reads_runs_older_than_yesterday(self):
        cfg=self.change(min_interval_seconds=3*86400);run=self.reserve(cfg=cfg);ms.launch(self.root,run,T0);ms.finish(self.root,run,10,'completed',T0)
        (self.root/ms.STATE).unlink();self.assertEqual(self.code(T0+2*DAY+H),'interval')
    def test_old_provider_block_survives_rebuild(self):
        run=self.reserve();ms.launch(self.root,run,T0);ms.finish(self.root,run,1_500_000,'completed',T0)
        (self.root/ms.STATE).unlink();self.assertEqual(self.code(T0+5*DAY),'provider_budget')
    def test_block_after_midnight_reaches_checkpoint(self):
        late=datetime(2026,3,10,23,50,tzinfo=timezone.utc);after=datetime(2026,3,11,0,10,tzinfo=timezone.utc)
        run=self.reserve(late);ms.launch(self.root,run,late);ms.finish(self.root,run,1_500_000,'completed',after)
        self.assertEqual(ms.state(self.root,after)['blocked']['reason'],'provider_budget')
        self.assertEqual(self.code(after+5*DAY),'provider_budget')
    def test_rebuild_keeps_pre_launch_failure(self):
        run=self.reserve();ms.release(self.root,run,'adapter_missing',T0);(self.root/ms.STATE).unlink()
        self.change(model='haiku');self.assertTrue(ms.eligibility(self.root,T0+H)['eligible'])
    def test_failure_after_launch_never_allows_early_retry(self):
        run=self.reserve();ms.launch(self.root,run,T0);ms.finish(self.root,run,None,'failed',T0,'timeout')
        self.change(model='haiku');self.assertEqual(self.code(T0+H),'interval')
    def test_an_older_released_run_does_not_allow_early_retry(self):
        # A worker died holding yesterday's reservation; a newer run launched today; recovery then releases the old one.
        cfg=self.change(min_interval_seconds=7200);d1=datetime(2026,3,10,23,0,tzinfo=timezone.utc);d2=d1+2*H
        self.reserve(d1,cfg=cfg);new=self.reserve(d2,cfg=cfg);ms.launch(self.root,new,d2);ms.finish(self.root,new,10,'completed',d2)
        self.assertEqual(len(ms.recover_runs(self.root,d2+H)),1)
        self.change(model='haiku');self.assertEqual(self.code(d2+H+H/2),'interval')
    def test_releasing_an_older_run_keeps_the_latest_failure(self):
        cfg=self.change(min_interval_seconds=7200);d1=datetime(2026,3,10,23,0,tzinfo=timezone.utc);d2=d1+2*H
        self.reserve(d1,cfg=cfg);new=self.reserve(d2,cfg=cfg);ms.release(self.root,new,'adapter_missing',d2)
        ms.recover_runs(self.root,d2+H/2)
        self.change(model='haiku');self.assertTrue(ms.eligibility(self.root,d2+H)['eligible'])
    def test_release_refuses_a_launched_run(self):
        run=self.reserve();ms.launch(self.root,run,T0)
        with self.assertRaises(ValueError):ms.release(self.root,run,'adapter_missing',T0)
        ms.finish(self.root,run,None,'failed',T0,'timeout');self.assertEqual(ms.state(self.root,T0+H)['charged_micro'],1_000_000)
    def test_recovery_completes_a_cut_off_transition(self):
        from unittest import mock
        run=self.reserve();ms.launch(self.root,run,T0);real=repo.atomic_write
        def flaky(path,*a,**k):
            if str(path).replace('\\','/').endswith(ms.STATE):raise OSError('disk gone')
            return real(path,*a,**k)
        with mock.patch('core.repository.atomic_write',flaky),self.assertRaises(OSError):ms.finish(self.root,run,10,'completed',T0)
        ms.recover_runs(self.root,T0+H)
        acc=json.loads((self.root/ms.STATE).read_text());self.assertEqual((acc['reserved_micro'],acc['charged_micro']),(0,10))
    def test_domain_names_are_plain(self):
        (self.root/'domains/_global').mkdir(parents=True)
        for bad in ('./_global','../demo','demo/..',str(self.root/'domains/demo')):
            with self.subTest(bad=bad),self.assertRaises(ValueError):ms.set_config(self.root,{'domains':[bad]})

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

class RestoreMerge(Base):
    """ms.merge_restore on its own: what a snapshot restore writes for the maintenance files."""
    def files(self):
        out={'maintenance.json':(self.root/'maintenance.json').read_text(encoding='utf-8')}
        for rel in ('maintenance/state.json',*[p.relative_to(self.root).as_posix() for p in self.runs()]):
            if (self.root/rel).is_file():out[rel]=(self.root/rel).read_text(encoding='utf-8')
        return out
    def finished(self,at=T0,n=3):
        run=self.reserve(at,n);ms.launch(self.root,run,at);return ms.finish(self.root,run,250000,'completed',at)
    def test_config_is_the_current_one_but_disabled(self):
        snap=self.files();self.change(model='haiku',max_notes_per_day=4)
        cfg=json.loads(ms.merge_restore(self.files(),snap)['maintenance.json'])
        self.assertEqual((cfg['enabled'],cfg['model'],cfg['max_notes_per_day']),(False,'haiku',4))
    def test_config_comes_from_the_snapshot_when_the_store_has_none(self):
        cfg=json.loads(ms.merge_restore({},self.files())['maintenance.json'])
        self.assertEqual((cfg['enabled'],cfg['domains']),(False,['demo']))
    def test_a_malformed_current_config_falls_back_to_the_snapshot_and_nothing_enables(self):
        self.assertFalse(json.loads(ms.merge_restore({'maintenance.json':'{nope'},self.files())['maintenance.json'])['enabled'])
        self.assertNotIn('maintenance.json',ms.merge_restore({'maintenance.json':'{nope'},{'maintenance.json':'{nope'}))
    def test_an_older_snapshot_never_lowers_the_days_figures(self):
        self.finished(T0,n=2);snap=self.files()
        run=self.reserve(T0+3*H,3,self.change(min_interval_seconds=1));ms.launch(self.root,run,T0+3*H);ms.finish(self.root,run,100000,'completed',T0+3*H)
        ms.set_retry(self.root,'demo:a','r1','f1','kept',T0+4*H);current=self.files()
        merged=json.loads(ms.merge_restore(current,snap)['maintenance/state.json']);now=json.loads(current['maintenance/state.json'])
        for k in ('reviewed','charged_micro','reserved_notes','reserved_micro','last_attempt_at','last_run'):self.assertEqual(merged[k],now[k],k)
        self.assertIn('demo:a',merged['retry'])
    def test_higher_snapshot_counters_survive_and_a_block_or_hold_is_kept_from_either_side(self):
        self.finished(T0,n=2)
        current=self.files();state=json.loads(current['maintenance/state.json'])
        hold={'demo:z':{'revision':'r','fingerprint':'f','retry_after':'2026-04-01T00:00:00+00:00','result':'kept'}}
        snap=dict(current,**{'maintenance/state.json':json.dumps(dict(state,reviewed=9,charged_micro=900000,blocked={'reason':'provider_budget','config_revision':'x'},retry=hold))})
        merged=json.loads(ms.merge_restore(current,snap)['maintenance/state.json'])
        self.assertEqual((merged['reviewed'],merged['charged_micro']),(9,900000));self.assertEqual(merged['blocked']['reason'],'provider_budget');self.assertIn('demo:z',merged['retry'])
    def test_a_later_retry_hold_wins_and_the_later_day_leads(self):
        self.finished(T0,n=2);ms.set_retry(self.root,'demo:a','r1','f1','kept',T0);current=self.files();state=json.loads(current['maintenance/state.json'])
        old=dict(state,day='2026-03-09',reviewed=10,last_attempt_at='2026-03-09T01:00:00+00:00',retry={'demo:a':dict(state['retry']['demo:a'],retry_after='2030-01-01T00:00:00+00:00')})
        merged=json.loads(ms.merge_restore(current,{'maintenance/state.json':json.dumps(old)})['maintenance/state.json'])
        self.assertEqual((merged['day'],merged['reviewed']),(state['day'],state['reviewed']));self.assertEqual(merged['retry']['demo:a']['retry_after'],'2030-01-01T00:00:00+00:00')
        self.assertEqual(merged['last_attempt_at'],state['last_attempt_at'])
    def test_run_records_are_add_only_and_a_terminal_record_wins(self):
        run=self.reserve(T0,3);rel=f"maintenance/runs/{run['day']}/{run['id']}.json";reserved=self.files()
        ms.launch(self.root,run,T0);ms.finish(self.root,run,100,'completed',T0);final=self.files()
        self.assertEqual(json.loads(ms.merge_restore(final,reserved)[rel])['state'],'completed')    # current terminal, snapshot not
        self.assertEqual(json.loads(ms.merge_restore(reserved,final)[rel])['state'],'completed')    # current not terminal, snapshot is
        self.assertIn(rel,ms.merge_restore({k:v for k,v in final.items() if k!=rel},final))         # added when missing
        self.assertNotIn(rel,ms.merge_restore(final,{'maintenance.json':final['maintenance.json']}))  # a current-only run is never touched
    def test_invalid_snapshot_records_are_ignored(self):
        bad={'maintenance/runs/2026-03-10/x.json':'{"id":"x"}','maintenance/runs/2026-03-10/y.json':'nope','maintenance/state.json':'[1]'}
        self.assertEqual(ms.merge_restore({},bad),{})
    def test_active_runs_lists_only_unfinished_records(self):
        self.assertEqual(ms.active_runs(self.root),[]);run=self.reserve();self.assertEqual(ms.active_runs(self.root),[run['id']])
        ms.launch(self.root,run,T0);ms.finish(self.root,run,0,'completed',T0);self.assertEqual(ms.active_runs(self.root),[])

if __name__=='__main__':unittest.main()
