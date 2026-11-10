"""Learning admission, provenance and isolation in throwaway stores."""
import contextlib
import io
import json
from unittest.mock import patch
from test_core import StoreFixture
from core import learning as lm
from core import governance, backup
from core.cli import main
from core.notes import change, find_note
from core.repository import inventory, read_json
from core.search import search_domain, rebuild_index


class Learning(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags': ['subject/project', 'concern/architecture', 'concern/workflow']}))

    def call(self, entity, action, ident=None, **payload):
        return lm.dispatch(self.root, 'demo', entity, action, ident, payload)

    def candidate(self, checked=True):
        t = self.call('task', 'create', objective='Repair stale index', family='alm.index', acceptance_criteria=['correct retrieval'])['record']
        a = self.call('attempt', 'start', task_id=t['id'])['record']
        e = self.call('evidence', 'add', attempt_id=a['id'], criterion='correct retrieval', result='pass',
                      method='tool_check' if checked else 'agent_assertion', observer='fixture', excerpt='Expected note returned', limitations='Only this fixture checked')['record']
        self.call('attempt', 'close', a['id'], expected_revision=a['revision'], outcome='succeeded')
        content = dict(title='Stale index repair', body='Rebuild a disposable index when retrieval is stale.',
                       use_when=['Index is stale'], do_not_use_when=['Canonical notes are corrupt'], steps=['Rebuild index'],
                       done_when=['Expected notes are returned'], fallback='Inspect canonical notes without deleting them')
        return self.call('candidate', 'create', content=content, evidence_ids=[e['id']], step_evidence=[[e['id']]])['record']

    def publish(self, c):
        p = self.call('candidate', 'promote-plan', c['id'], expected_revision=c['revision'])
        receipt = governance.apply(self.root, p['plan_id'], p['digest'])
        return lm.get(self.root, 'demo', 'procedures', p['summary']['procedure_id']), receipt

    def test_complete_loop_and_isolation(self):
        c = self.candidate()
        self.assertEqual(inventory('demo', self.root), [])
        self.assertEqual(search_domain('stale index repair', 'demo', root=self.root), [])
        report = self.call('candidate', 'evaluate', c['id'])
        self.assertTrue(report['eligible']); self.assertEqual(report['supporting_tasks'], 1)
        p, _ = self.publish(c)
        self.assertEqual(len(search_domain('stale index repair', 'demo', root=self.root)), 1)
        a = lm.rows(self.root, 'demo', 'attempts')[0]
        u = self.call('use', 'report', attempt_id=a['id'], procedure_id=p['id'], version=1, usage='used')['record']
        f = self.call('feedback', 'add', use_id=u['id'])['record']
        self.assertEqual((f['outcome'], f['contribution']), ('succeeded', 'unknown'))

    def test_unsupported_success_does_not_admit(self):
        c = self.candidate(checked=False)
        self.assertFalse(self.call('candidate', 'evaluate', c['id'])['eligible'])
        with self.assertRaisesRegex(ValueError, 'not eligible'):
            self.publish(c)

    def test_stale_evidence_and_idempotent_approval(self):
        c = self.candidate()
        p = self.call('candidate', 'promote-plan', c['id'], expected_revision=c['revision'])
        evidence = lm.rows(self.root, 'demo', 'evidence')[0]
        self.call('evidence', 'correct', evidence['id'], expected_revision=evidence['revision'], attempt_id=evidence['attempt_id'],
                  criterion=evidence['criterion'], result='fail', method='tool_check', observer='fixture', excerpt='Wrong note returned', limitations='Fixture only')
        with self.assertRaisesRegex(ValueError, 'stale plan'):
            governance.apply(self.root, p['plan_id'], p['digest'])
        self.assertEqual(inventory('demo', self.root), [])
        c = self.candidate(); p = self.call('candidate', 'promote-plan', c['id'], expected_revision=c['revision'])
        governance.apply(self.root, p['plan_id'], p['digest'])
        self.assertTrue(governance.apply(self.root, p['plan_id'], p['digest'])['idempotent'])

    def test_edits_keep_approved_content_and_external_changes(self):
        c = self.candidate(); p, _ = self.publish(c)
        path, note = find_note(p['note_id'], 'demo', self.root)
        result = change(note['id'], 'demo', 'update', self.root, expected_revision=note['revision'], body='Unapproved rewrite')
        self.assertEqual(result['status'], 'proposed')
        self.assertNotIn('Unapproved rewrite', path.read_text(encoding='utf-8'))
        path.write_text(path.read_text(encoding='utf-8').replace('Rebuild a disposable index', 'externalunapproved'), encoding='utf-8')
        rebuild_index('demo', self.root)
        self.assertEqual(search_domain('externalunapproved', 'demo', root=self.root), [])
        self.assertIn('Rebuild a disposable index', find_note(p['note_id'], 'demo', self.root)[1]['body'])

    def test_disabled_and_suspended(self):
        c = self.candidate(); p, _ = self.publish(c)
        self.call('procedure', 'suspend', p['id'], expected_revision=p['revision'])
        self.assertEqual(search_domain('stale index repair', 'demo', root=self.root), [])
        self.assertTrue(self.call('version', 'read', p['id']+'.1')['record'])
        self.call('config', 'set', enabled=False)
        with self.assertRaisesRegex(ValueError, 'disabled'): self.candidate()
        self.assertTrue(self.call('candidate', 'list')['records'])

    def test_backup_restore_and_rollback(self):
        c = self.candidate(); p, _ = self.publish(c)
        snap = backup.backup(self.root)
        self.assertEqual(backup.verify(snap['backup']), [])
        self.assertTrue(any('/learning/versions/' in f for f in backup.canonical_files(self.root)))
        q = self.call('procedure', 'rollback-plan', p['id'], expected_revision=p['revision'], version=1)
        governance.apply(self.root, q['plan_id'], q['digest'])
        self.assertEqual(self.call('procedure', 'read', p['id'])['record']['current_version'], 2)
        restore = backup.restore_plan(self.root, snap['backup'])
        governance.apply(self.root, restore['plan_id'], restore['digest'])
        self.assertEqual(self.call('procedure', 'read', p['id'])['record']['current_version'], 1)

    def test_idempotency_scope_limits_and_viewer(self):
        payload = dict(objective='Repair index', family='index', acceptance_criteria=['works'], idempotency_key='same-request')
        a = self.call('task', 'create', **payload)['record']
        self.assertEqual(self.call('task', 'create', **payload)['record']['id'], a['id'])
        with self.assertRaisesRegex(ValueError, 'Idempotency conflict'):
            self.call('task', 'create', **dict(payload, objective='Other task'))
        with self.assertRaises(ValueError): lm.get(self.root, 'demo', 'tasks', '../escape')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch('sys.stdin', io.StringIO('{}')):
            code = main(['--root', str(self.root), '--domain', 'demo', '--actor', 'viewer', 'learn', 'task', 'create'])
        self.assertEqual(code, 2)

    def test_evidence_correction_suspends_and_retry_families(self):
        c = self.candidate(); p, _ = self.publish(c)
        e = lm.rows(self.root, 'demo', 'evidence')[0]
        a = lm.get(self.root, 'demo', 'attempts', e['attempt_id'])
        retry = self.call('attempt', 'start', task_id=a['task_id'], retry_of=a['id'])['record']
        self.assertEqual(a['attempt_family_id'], retry['attempt_family_id'])
        self.call('evidence', 'correct', e['id'], expected_revision=e['revision'], attempt_id=a['id'], criterion=e['criterion'],
                  result='fail', method='tool_check', observer='fixture', excerpt='Regression found', limitations='Fixture only')
        self.assertEqual(self.call('procedure', 'read', p['id'])['record']['state'], 'suspended')
        self.assertEqual(search_domain('stale index repair', 'demo', root=self.root), [])

    def test_portable_export_is_untrusted_on_import(self):
        from core.bundle import export, import_plan, _read
        c=self.candidate();p,_=self.publish(c)
        plain=self.base/'plain.zip';export(self.root,'demo',plain)
        manifest,members=_read(plain)
        self.assertNotIn('learning_procedure',next(iter(members.values())).decode())
        portable=self.base/'learning.zip';export(self.root,'demo',portable,include_learning=True)
        manifest,members=_read(portable)
        self.assertEqual(manifest['schema'],2)
        self.assertTrue(any('/evidence/' in path for path in members))
        imported=import_plan(self.root,portable,'demo')
        governance.apply(self.root,imported['plan_id'],imported['digest'])
        drafts=[c for c in lm.rows(self.root,'demo','candidates') if c.get('imported_from')]
        self.assertEqual(len(drafts),1)
        self.assertFalse(lm.evaluate(self.root,'demo',drafts[0])['eligible'])
        self.assertEqual(import_plan(self.root,portable,'demo')['plan_id'],None)

    def test_disabled_config_invalidates_pending_admission(self):
        c=self.candidate()
        p=self.call('candidate','promote-plan',c['id'],expected_revision=c['revision'])
        self.call('config','set',enabled=False)
        with self.assertRaisesRegex(ValueError,'stale plan'):governance.apply(self.root,p['plan_id'],p['digest'])

    def test_crashed_admission_rolls_forward_atomically(self):
        from core import repository
        c=self.candidate();p=self.call('candidate','promote-plan',c['id'],expected_revision=c['revision'])
        original=repository._write;count=[0]
        def crash(path,data):
            count[0]+=1
            if count[0]==2:raise OSError('interrupted admission')
            return original(path,data)
        with patch.object(repository,'_write',crash):
            with self.assertRaises(OSError):governance.apply(self.root,p['plan_id'],p['digest'])
        governance.apply(self.root,p['plan_id'],p['digest'])
        self.assertEqual(len(inventory('demo',self.root)),1)
        self.assertEqual(lm.get(self.root,'demo','candidates',c['id'])['state'],'admitted')

    def test_same_title_admission_race_is_rejected(self):
        a=self.candidate();b=self.candidate()
        first=self.call('candidate','promote-plan',a['id'],expected_revision=a['revision'])
        second=self.call('candidate','promote-plan',b['id'],expected_revision=b['revision'])
        governance.apply(self.root,first['plan_id'],first['digest'])
        with self.assertRaisesRegex(ValueError,'stale plan'):
            governance.apply(self.root,second['plan_id'],second['digest'])

    def test_structured_note_edits_round_trip_without_duplicate_sections(self):
        c=self.candidate();p,_=self.publish(c)
        path,n=find_note(p['note_id'],'demo',self.root)
        edited=n['body'].replace('Rebuild index','Inspect and rebuild index')
        result=change(n['id'],'demo','update',self.root,body=edited)
        candidate=lm.get(self.root,'demo','candidates',result['candidate_id'])
        self.assertEqual(candidate['content']['steps'],['Inspect and rebuild index'])
        self.assertNotIn('Use when',candidate['content']['body'])
        self.publish(candidate)
        self.assertEqual(find_note(n['id'],'demo',self.root)[1]['body'].count('**Use when:**'),1)

    def test_file_hints_and_session_inventory_exclude_suspended(self):
        from core.search import file_notes
        c=self.candidate()
        c['content']['body']='For src/cache.py repair a stale index.'
        updated=self.call('candidate','update',c['id'],expected_revision=c['revision'],content=c['content'],
                          evidence_ids=c['evidence_ids'],step_evidence=c['step_evidence'])['record']
        p,_=self.publish(updated)
        self.assertTrue(file_notes('src/cache.py',['demo'],self.root))
        self.call('procedure','suspend',p['id'],expected_revision=p['revision'])
        self.assertEqual(file_notes('src/cache.py',['demo'],self.root),[])
        self.assertEqual(inventory('demo',self.root),[])

    def test_obsidian_edits_are_proposals(self):
        from core.sync import sync
        c=self.candidate();p,_=self.publish(c)
        vault=self.base/'vault'
        sync('demo',vault,self.root)
        file=next(vault.rglob('*'+p['note_id'][:8]+'*.md'))
        file.write_text(file.read_text(encoding='utf-8').replace('Rebuild index','Inspect and rebuild index'),encoding='utf-8')
        sync('demo',vault,self.root)
        self.assertEqual(len([r for r in lm.rows(self.root,'demo','candidates') if r['state']=='draft']),1)
        self.assertNotIn('Inspect and rebuild index',find_note(p['note_id'],'demo',self.root)[1]['body'])

    def test_evidence_limits_and_copied_origin(self):
        t=self.call('task','create',objective='Check repair',family='repair',acceptance_criteria=['works'])['record']
        a=self.call('attempt','start',task_id=t['id'],source_origin_id='source-attempt-1')['record']
        self.assertEqual(self.call('attempt','start',task_id=t['id'],source_origin_id='source-attempt-1')['record']['id'],a['id'])
        with self.assertRaisesRegex(ValueError,'excerpt'):
            self.call('evidence','add',attempt_id=a['id'],criterion='works',result='pass',method='tool_check',observer='fixture',excerpt='x'*8193,limitations='fixture only')

    def test_evidence_delete_suspends_and_is_approved(self):
        c=self.candidate();p,_=self.publish(c)
        e=lm.rows(self.root,'demo','evidence')[0]
        plan=self.call('evidence','delete-plan',e['id'],expected_revision=e['revision'])
        self.assertTrue(lm.get(self.root,'demo','evidence',e['id']))
        governance.apply(self.root,plan['plan_id'],plan['digest'])
        self.assertEqual(self.call('procedure','read',p['id'])['record']['state'],'suspended')
        with self.assertRaisesRegex(ValueError,'Missing'):lm.get(self.root,'demo','evidence',e['id'])

    def test_corrupt_learning_does_not_break_ordinary_facts(self):
        c=self.candidate();p,_=self.publish(c)
        self.note('Ordinary repair fact','ordinaryrepairmarker facts remain readable')
        (self.root/lm.rel('demo','procedures',p['id'])).write_text('{broken',encoding='utf-8')
        self.assertEqual([n['title'] for n in inventory('demo',self.root)],['Ordinary repair fact'])
        self.assertTrue(search_domain('ordinaryrepairmarker','demo',root=self.root))
        self.assertTrue(lm.problems(self.root,'demo'))
