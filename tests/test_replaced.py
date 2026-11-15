"""UX-C9: possible replacements. Capture queues a pair, the maintenance reviewer sees it as a `replaced` candidate and either supersedes the
older note, records the pair as distinct, or defers; a backfill queues the pairs already in the store."""
import unittest
from datetime import datetime,timedelta,timezone
from unittest import mock
from core import maintenance,maintenance_state as ms,notes
from core.repository import inventory
from test_maintenance import Base,TAGS,OLD

OLD_TITLE,OLD_BODY='Final ten-type taxonomy','The note taxonomy is final at ten types: knowledge, decision, preference, context, status, bug, plan, risk, research and policy.'
NEW_TITLE,NEW_BODY='Final taxonomy of thirteen note types','The note taxonomy has thirteen types: the ten earlier types plus map, runbook and brief. The ten-type taxonomy is no longer final.'

class Pairs(Base):
    def old_new(self,old_type='decision',new_type='decision',**old_kw):
        old=self.mk(OLD_TITLE,OLD_BODY,type=old_type,**old_kw);new=self.mk(NEW_TITLE,NEW_BODY,type=new_type)
        return old,new
    def queue(self):return [(p['new'],p['old']) for p in ms.queue(self.root)['pairs']]
    def pair(self,old,new):return (self.ref(new),self.ref(old))
    def replaced(self,old,*specs):
        return self.go((old,'supersede',{'successor':self.ref(specs[0]),'rule':'replaced_by_successor','evidence':['own','successor']}))

class CaptureQueue(Pairs):
    def test_an_owed_same_domain_top_hit_is_queued_once_and_the_capture_result_is_unchanged(self):
        old,new=self.old_new()
        self.assertEqual(self.queue(),[self.pair(old,new)])
        # capture's own result: the same keys with maintenance off (nothing is added for the queue)
        ms.set_config(self.root,ms.validate({'enabled':False,'domains':['demo']}))
        plain=self.mk('Another final taxonomy of thirteen note types','The note taxonomy has thirteen types, as decided later.',type='decision')
        ms.set_config(self.root,self.cfg)
        queued=self.mk('Yet another final taxonomy of thirteen note types','The note taxonomy has thirteen types, as decided again.',type='decision')
        self.assertEqual(set(plain)-{'id','path','operation_id'},set(queued)-{'id','path','operation_id'})
        self.assertEqual(ms.queue_add(self.root,[{'new':self.ref(new),'old':self.ref(old)}]),0)   # deduplicated
    def test_nothing_is_queued_for_an_unrelated_subject_a_disabled_store_or_another_type_group(self):
        self.mk(OLD_TITLE,OLD_BODY,type='decision');self.mk('Invoice rounding','Invoices round half up to cents.',type='decision')
        self.assertEqual(self.queue(),[])
        ms.set_config(self.root,ms.validate({'enabled':False,'domains':['demo']}))
        self.mk(NEW_TITLE,NEW_BODY,type='decision');self.assertEqual(self.queue(),[])
        ms.set_config(self.root,self.cfg)
        self.mk('Final taxonomy of thirteen note types explained',NEW_BODY,type='knowledge');self.assertEqual(self.queue(),[])
    def test_a_distinct_pair_or_a_cross_domain_hit_is_not_owed(self):
        old,new=self.old_new();n=self.get(new);o=self.get(old)
        top={'id':old['id'],'domain':'demo','owed_bar':True}
        self.assertEqual(notes.owed_pair(n,[top],'demo',self.root)['id'],old['id'])
        self.assertIsNone(notes.owed_pair(n,[{**top,'domain':'other'}],'demo',self.root))
        self.assertIsNone(notes.owed_pair(n,[{**top,'owed_bar':False}],'demo',self.root))
        notes.change(new['id'],'demo','update',self.root,distinct_from=notes.distinct_with(n,'demo',old['id'],'Different subjects entirely.','user'))
        self.assertIsNone(notes.owed_pair(self.get(new),[top],'demo',self.root))
    def test_a_failing_queue_never_fails_the_capture(self):
        self.mk(OLD_TITLE,OLD_BODY,type='decision')
        with mock.patch.object(ms,'queue_add',side_effect=OSError('disk full')):
            made=self.mk(NEW_TITLE,NEW_BODY,type='decision')
        self.assertEqual(made['status'],'created');self.assertEqual(self.queue(),[])

class Candidates(Pairs):
    def test_a_queued_pair_is_a_replaced_candidate_ranked_after_due_rows(self):
        old,new=self.old_new();due=self.mk('Money decision','Money is long cents.',type='decision',review_by=OLD)
        found=self.cands()
        self.assertEqual([c['ref'] for c in found],[self.ref(due),self.ref(old)])
        c=found[1];self.assertEqual((c['kinds'],c['pair'],c['level'],c['pair_note']['id']),(['replaced'],new['id'],10,new['id']))
        self.assertEqual(c['permitted'],['keep','defer','distinct','supersede'])
    def test_a_retired_or_distinct_pair_is_not_a_candidate_and_is_pruned(self):
        old,new=self.old_new()
        notes.change(old['id'],'demo','supersede',self.root,superseded_by=new['id'])
        self.assertEqual([c['ref'] for c in self.cands()],[])
        maintenance.prune_queue(self.root,self.cfg)
        self.assertNotIn(self.pair(old,new),self.queue())
    def test_a_pair_marked_distinct_by_the_user_leaves_the_queue(self):
        old,new=self.old_new()
        notes.change(new['id'],'demo','update',self.root,distinct_from=notes.distinct_with(self.get(new),'demo',old['id'],'Different subjects entirely.','user'))
        self.assertEqual(self.cands(),[]);maintenance.prune_queue(self.root,self.cfg);self.assertEqual(self.queue(),[])
    def test_a_note_another_row_already_reviews_is_left_for_a_later_batch(self):
        old,new=self.old_new()
        notes.change(old['id'],'demo','update',self.root,review_by=OLD)
        found=self.cands();self.assertEqual([(c['ref'],c['kinds']) for c in found],[(self.ref(old),['due'])])
        self.assertEqual(self.queue(),[self.pair(old,new)])
    def test_the_budget_is_shared_with_the_other_kinds(self):
        self.cfg=ms.validate({**self.cfg,'max_notes_per_day':2});ms.set_config(self.root,self.cfg)
        due=self.mk('Money decision','Money is long cents.',type='decision',review_by=OLD);old,new=self.old_new()
        res=self.go()
        self.assertEqual((res['status'],res['reviewed']),('completed',2));self.assertEqual(ms.state(self.root,self.now)['reviewed'],2)
        self.assertEqual(self.go()['skip_reason'],'daily_note_limit')
        self.assertEqual(self.queue(),[self.pair(old,new)])

class Decisions(Pairs):
    def test_supersede_retires_the_older_decision_and_the_pair_leaves_the_queue(self):
        old,new=self.old_new()
        res=self.replaced(old,new)
        self.assertEqual((res['status'],res['superseded']),('completed',1))
        n=self.get(old);self.assertEqual((n['status'],n['superseded_by']),('superseded',new['id']));self.assertEqual(n['last_retirement']['rule'],'replaced_by_successor')
        self.assertEqual(n['last_retirement']['actor'],'maintenance');self.assertEqual(self.queue(),[])
    def refused(self,code,**kw):
        old,new=self.old_new(**kw)
        self.assertIn(self.pair(old,new),self.queue())
        res=self.replaced(old,new)
        self.assertEqual((self.one(res)['result'],self.one(res)['reason_code']),('rejected',code));self.assertEqual(self.get(old)['status'],'active')
        self.assertIn(self.pair(old,new),self.queue())   # a refusal is not a decision: the pair stays
    def test_supersede_is_refused_for_a_policy_older_note(self):self.refused('protected_type',old_type='policy')
    def test_supersede_is_refused_for_a_pinned_older_note(self):self.refused('pinned',pinned=True)
    def offered(self,**kw):
        old,new=self.old_new(**kw);cand=next(c for c in self.cands() if c['ref']==self.ref(old))
        self.assertEqual(cand['permitted'],['keep','defer','distinct'])
        self.go((old,'distinct',{'reason':'Both are still true for different reasons.','evidence':['own','successor']}))
        self.assertTrue(notes.distinct(self.get(old),self.get(new)));self.assertEqual(self.queue(),[])
    def test_a_policy_older_note_is_offered_distinct_but_not_supersede(self):self.offered(old_type='policy')
    def test_a_pinned_older_note_is_offered_distinct_but_not_supersede(self):self.offered(pinned=True)
    def test_distinct_writes_the_record_on_the_newer_note_and_the_pair_never_returns(self):
        old,new=self.old_new();before=self.get(old)
        res=self.go((old,'distinct',{'reason':'The newer note adds map, runbook and brief; both are still true.','evidence':['own','successor']}))
        self.assertEqual((res['status'],res['distinct']),('completed',1))
        record=self.get(new)['distinct_from'][0]
        self.assertEqual((record['ref'],record['by']),(self.ref(old),'maintenance'));self.assertIn('both are still true',record['why'])
        self.assertEqual(self.get(old)['revision'],before['revision']);self.assertEqual(self.queue(),[])
        self.assertTrue(notes.distinct(self.get(old),self.get(new)))
        self.assertEqual(self.cands(),[])
        # the backfill and a fresh capture both skip it
        ms.queue_backfill(self.root,False,0);self.assertEqual(maintenance.backfill_related(self.root,self.cfg,self.now),0);self.assertEqual(self.queue(),[])
    def test_distinct_needs_evidence_and_a_reason_of_at_least_eight_characters(self):
        old,new=self.old_new();run,manifest,_=self.prepare()
        short=self.d(manifest,old,'distinct',reason='Short')
        res=self.apply(run,manifest,[short]);self.assertEqual((self.one(res)['result'],self.one(res)['reason_code']),('rejected','invalid_report'))
        self.assertNotIn('distinct_from',self.get(new));self.assertEqual(self.queue(),[self.pair(old,new)])
    def test_defer_keeps_the_pair_held_for_a_week_and_keep_drops_it(self):
        old,new=self.old_new()
        self.go((old,'defer',{}));self.assertEqual(self.queue(),[self.pair(old,new)]);self.assertEqual(self.cands(),[])
        self.now+=timedelta(days=8);self.assertEqual([c['ref'] for c in self.cands()],[self.ref(old)])
        self.go((old,'keep',{}));self.assertEqual(self.queue(),[]);self.assertEqual(self.get(old)['status'],'active')
    def test_an_edit_after_the_packet_skips_the_decision_and_keeps_the_pair(self):
        old,new=self.old_new();run,manifest,_=self.prepare()
        notes.change(new['id'],'demo','update',self.root,body=NEW_BODY+' It was reviewed again.')
        res=self.apply(run,manifest,[self.d(manifest,old,'supersede',successor=self.ref(new),rule='replaced_by_successor',evidence=['own','successor'])])
        self.assertEqual((self.one(res)['result'],self.one(res)['reason_code']),('skipped','stale_revision'));self.assertEqual(self.get(old)['status'],'active')
        self.assertEqual(self.queue(),[self.pair(old,new)])
    def test_the_packet_carries_both_notes_in_full_with_dates(self):
        old,new=self.old_new();_,manifest,text=self.prepare()
        import json
        block=json.loads(text)['candidates'][0]
        self.assertEqual((block['review_kinds'],block['permitted_actions']),(['replaced'],['keep','defer','distinct','supersede']))
        self.assertEqual(block['body'],self.get(old)['body']);self.assertTrue(block['created'] and block['updated'])
        succ=block['successors'][0];self.assertEqual((succ['ref'],succ['body']),(self.ref(new),self.get(new)['body']));self.assertTrue(succ['created'] and succ['updated'])

class Backfill(Pairs):
    def test_the_first_run_queues_the_pairs_already_in_the_store_once(self):
        ms.set_config(self.root,ms.validate({'enabled':False,'domains':['demo']}))
        old,new=self.old_new();self.assertEqual(self.queue(),[])
        ms.set_config(self.root,self.cfg)
        self.assertEqual(maintenance.backfill_related(self.root,self.cfg,self.now),1)
        self.assertEqual(self.queue(),[self.pair(old,new)]);self.assertTrue(ms.queue(self.root)['backfill']['done'])
        self.assertIsNone(maintenance.backfill_related(self.root,self.cfg,self.now))
    def test_a_run_does_the_backfill_and_reviews_the_pair(self):
        ms.set_config(self.root,ms.validate({'enabled':False,'domains':['demo']}))
        old,new=self.old_new();ms.set_config(self.root,self.cfg)
        res=self.replaced(old,new);self.assertEqual(res['superseded'],1);self.assertTrue(ms.queue(self.root)['backfill']['done'])
    def test_a_short_budget_saves_its_place_and_resumes(self):
        ms.set_config(self.root,ms.validate({'enabled':False,'domains':['demo']}))
        old,new=self.old_new();ms.set_config(self.root,self.cfg)
        maintenance.backfill_related(self.root,self.cfg,self.now,seconds=0)
        mid=ms.queue(self.root)['backfill'];self.assertEqual((mid['done'],mid['next']),(False,1))
        maintenance.backfill_related(self.root,self.cfg,self.now);self.assertTrue(ms.queue(self.root)['backfill']['done']);self.assertEqual(self.queue(),[self.pair(old,new)])

class Contract(Pairs):
    def test_the_report_contract_and_prompt_know_the_distinct_action(self):
        from core import maintenance_agent as agent
        self.assertIn('distinct',agent.SCHEMA['properties']['decisions']['items']['properties']['action']['enum'])
        self.assertIn('review_kinds is ["replaced"]',agent.POLICY);self.assertIn('- distinct:',agent.POLICY)
        old,new=self.old_new();run,manifest,_=self.prepare()
        report=agent.validate_report({'version':1,'run_id':run['id'],'decisions':[{'key':'C1','action':'distinct','reason':'Two parts of one plan.','evidence_ids':['E1']}]},run['id'],manifest)
        self.assertEqual(report['decisions'][0]['action'],'distinct')
    def test_a_damaged_queue_file_reads_as_empty_and_is_rewritten_by_the_next_add(self):
        (self.root/ms.QUEUE).parent.mkdir(exist_ok=True);(self.root/ms.QUEUE).write_text('{not json',encoding='utf-8')
        self.assertEqual(ms.queue(self.root)['pairs'],[])
        self.assertEqual(ms.queue_add(self.root,[{'new':'demo:a','old':'demo:b'}]),1);self.assertEqual(len(ms.queue(self.root)['pairs']),1)

if __name__=='__main__':unittest.main()
