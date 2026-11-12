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


class LearningFixture(StoreFixture):
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


class Learning(LearningFixture):
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

    def test_admission_needs_an_exclusion(self):
        c = self.candidate()
        c = self.call('candidate', 'update', c['id'], expected_revision=c['revision'],
                      content=dict(c['content'], do_not_use_when=[]))
        self.assertIn('--do-not-use-when', c['warnings'][0])
        report = self.call('candidate', 'evaluate', c['record']['id'])
        self.assertFalse(report['eligible']); self.assertEqual(report['errors'], [lm.NEEDS_EXCLUSIONS])
        with self.assertRaisesRegex(ValueError, 'No exclusions'):
            self.publish(c['record'])

    def test_versions_approved_without_exclusions_still_resume_and_roll_back(self):
        c = self.candidate()
        c = self.call('candidate', 'update', c['id'], expected_revision=c['revision'],
                      content=dict(c['content'], do_not_use_when=[]))['record']
        real = lm.evaluate
        def legacy(root, domain, candidate):   # admission as it was before exclusions were required
            report = real(root, domain, candidate)
            report['errors'] = [e for e in report['errors'] if e != lm.NEEDS_EXCLUSIONS]
            return dict(report, eligible=not report['errors'])
        with patch.object(lm, 'evaluate', legacy):
            p, _ = self.publish(c)
        self.assertEqual(lm.get(self.root, 'demo', 'versions', p['id']+'.1')['content']['do_not_use_when'], [])
        p = self.call('procedure', 'suspend', p['id'], expected_revision=p['revision'])['record']
        p = self.call('procedure', 'resume', p['id'], expected_revision=p['revision'])['record']
        self.assertEqual(p['state'], 'active')
        plan = self.call('procedure', 'rollback-plan', p['id'], expected_revision=p['revision'], version=1)
        governance.apply(self.root, plan['plan_id'], plan['digest'])
        self.assertEqual(lm.get(self.root, 'demo', 'procedures', p['id'])['current_version'], 2)

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
        tomb=lm.get(self.root,'demo','evidence',e['id'])
        self.assertTrue(tomb['deleted']);self.assertNotIn('excerpt',tomb)
        # An approved deletion is not an integrity problem, and the history still exports.
        self.assertEqual(lm.problems(self.root,'demo'),[])
        from core.bundle import export
        export(self.root,'demo',self.base/'after-delete.zip',include_learning=True)
        with self.assertRaisesRegex(ValueError,'requires review'):
            self.call('procedure','resume',p['id'],expected_revision=lm.get(self.root,'demo','procedures',p['id'])['revision'])

    def test_corrupt_learning_does_not_break_ordinary_facts(self):
        c=self.candidate();p,_=self.publish(c)
        self.note('Ordinary repair fact','ordinaryrepairmarker facts remain readable')
        (self.root/lm.rel('demo','procedures',p['id'])).write_text('{broken',encoding='utf-8')
        self.assertEqual([n['title'] for n in inventory('demo',self.root)],['Ordinary repair fact'])
        self.assertTrue(search_domain('ordinaryrepairmarker','demo',root=self.root))
        self.assertTrue(lm.problems(self.root,'demo'))


class Production(LearningFixture):
    """Defects found in the PR #28 review: each test pins the corrected behaviour."""
    def cli(self, *argv, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch('sys.stdin', io.StringIO(stdin)):
            code = main(['--root', str(self.root), '--domain', 'demo', *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_broken_config_hides_only_learned_runbooks(self):
        self.publish(self.candidate())
        from core.notes import capture
        capture('Manual deploy runbook', 'deploymarker steps', ['subject/project', 'concern/workflow'], 'demo', self.root, type='runbook')
        (self.root/'learning.json').write_text('{"enabled": "yes", "domains": {}}', encoding='utf-8')
        self.assertTrue(search_domain('deploymarker', 'demo', root=self.root))
        self.assertTrue(search_domain('stale index repair', 'demo', root=self.root))   # serving does not read the config

    def test_disabling_stops_collection_but_keeps_published(self):
        p, _ = self.publish(self.candidate())
        self.call('config', 'set', enabled=False)
        self.assertTrue(search_domain('stale index repair', 'demo', root=self.root))
        u = self.call('use', 'report', procedure_id=p['id'], usage='used')['record']
        self.assertEqual(self.call('feedback', 'add', use_id=u['id'], outcome='succeeded')['record']['outcome_source'], 'agent_report')
        with self.assertRaisesRegex(ValueError, 'disabled'):
            self.call('task', 'create', objective='x', family='y', acceptance_criteria=['z'])

    def test_one_malformed_record_is_reported_not_fatal(self):
        self.publish(self.candidate())
        bad = 'domains/demo/learning/candidates/deadbeefdeadbeef.json'
        (self.root/bad).write_text('{"schema":1}', encoding='utf-8')
        listed = self.call('candidate', 'list')
        self.assertEqual(len(listed['records']), 1)
        self.assertEqual([x['path'] for x in listed['problems']], [bad])
        self.assertIn(bad, [x['path'] for x in lm.problems(self.root, 'demo')])
        self.assertTrue(search_domain('stale index repair', 'demo', root=self.root))

    def test_export_keeps_suspended_procedures(self):
        from core.bundle import export
        p, _ = self.publish(self.candidate())
        self.call('procedure', 'suspend', p['id'], expected_revision=p['revision'])
        self.assertEqual(export(self.root, 'demo', self.base/'suspended.zip')['notes'], 1)

    def test_plan_only_update_is_a_preview(self):
        p, _ = self.publish(self.candidate())
        _, note = find_note(p['note_id'], 'demo', self.root)
        preview = change(note['id'], 'demo', 'update', self.root, plan_only=True, body=note['body'].replace('Rebuild index', 'Inspect first'))
        self.assertEqual((preview['status'], preview['plan_id']), ('preview', None))
        self.assertEqual(len(lm.rows(self.root, 'demo', 'candidates')), 1)

    def test_list_items_round_trip_exactly(self):
        c = self.candidate()
        content = dict(c['content'], use_when=['Index is stale; queries miss new notes', 'After a restore'])
        c = self.call('candidate', 'update', c['id'], expected_revision=c['revision'], content=content)['record']
        p, _ = self.publish(c)
        _, note = find_note(p['note_id'], 'demo', self.root)
        r = change(note['id'], 'demo', 'update', self.root, body=note['body'].replace('Rebuild a disposable', 'Rebuild the disposable'))
        staged = lm.get(self.root, 'demo', 'candidates', r['candidate_id'])['content']
        self.assertEqual(staged['use_when'], ['Index is stale; queries miss new notes', 'After a restore'])
        self.assertEqual(change(note['id'], 'demo', 'update', self.root, body=note['body'])['status'], 'unchanged')

    def test_edits_are_refused_while_disabled(self):
        p, _ = self.publish(self.candidate())
        _, note = find_note(p['note_id'], 'demo', self.root)
        self.call('config', 'set', enabled=False)
        with self.assertRaisesRegex(ValueError, 'disabled'):
            change(note['id'], 'demo', 'update', self.root, body='Other')

    def test_rollback_leaves_no_orphan_candidate(self):
        p, _ = self.publish(self.candidate())
        v1 = lm.get(self.root, 'demo', 'versions', p['id']+'.1')
        c = self.call('candidate', 'create', procedure_id=p['id'], evidence_ids=v1['evidence_ids'],
                      content=dict(v1['content'], title='Stale index repair v2'))['record']
        self.publish(c)
        p = lm.get(self.root, 'demo', 'procedures', p['id'])
        before = len(lm.rows(self.root, 'demo', 'candidates'))
        plan = self.call('procedure', 'rollback-plan', p['id'], expected_revision=p['revision'], version=1)
        self.assertEqual(len(lm.rows(self.root, 'demo', 'candidates')), before)     # nothing until approved
        governance.apply(self.root, plan['plan_id'], plan['digest'])
        self.assertEqual(lm.get(self.root, 'demo', 'procedures', p['id'])['current_version'], 3)

    def test_vault_edit_reports_the_proposal(self):
        from core.sync import sync
        p, _ = self.publish(self.candidate())
        vault = self.base/'vault'; sync('demo', vault, self.root)
        file = next(vault.rglob('*'+p['note_id'][:8]+'*.md'))
        file.write_text(file.read_text(encoding='utf-8').replace('Rebuild index', 'Inspect and rebuild index'), encoding='utf-8')
        sync('demo', vault, self.root)
        drafts = [r for r in lm.rows(self.root, 'demo', 'candidates') if r['state'] == 'draft']
        self.assertEqual(len(drafts), 1)
        entry = read_json(self.root/'domains'/'demo'/'sync-state.json')['entries'][p['note_id']]
        self.assertIn('review proposal '+drafts[0]['id'][:8], entry['conflict']['message'])

    def test_cli_flags_record_propose_and_admit(self):
        code, guide = self.cli('learn')
        self.assertEqual(code, 0); self.assertTrue(any('--do-not-use-when' in line for line in guide['guide']))
        code, rec = self.cli('learn', 'record', '--objective', 'Repair stale index', '--family', 'alm.index',
                             '--criterion', 'correct retrieval', '--result', 'pass', '--method', 'tool_check',
                             '--observer', 'search', '--excerpt', 'Expected note returned',
                             '--limitations', 'One fixture', '--outcome', 'succeeded')
        self.assertEqual(code, 0, rec); self.assertIn('--evidence', rec['hint'])
        short = rec['evidence'][0]['id'][:8]
        code, cand = self.cli('learn', 'candidate', 'create', '--title', 'Stale index repair', '--summary', 'Rebuild the index.',
                              '--use-when', 'Index is stale', '--step', 'Rebuild index', '--done-when', 'Expected notes return',
                              '--fallback', 'Inspect canonical notes', '--evidence', short)
        self.assertEqual(code, 0, cand); self.assertIn('--do-not-use-when', cand['warnings'][0])
        code, report = self.cli('learn', 'candidate', 'evaluate', cand['record']['id'][:8], '--actor', 'viewer')
        self.assertFalse(report['eligible']); self.assertIn(lm.NEEDS_EXCLUSIONS, report['errors'])
        code, cand = self.cli('learn', 'candidate', 'update', cand['record']['id'], '--expected-revision', cand['record']['revision'],
                              '--do-not-use-when', 'The note file is missing')
        self.assertEqual(code, 0, cand); self.assertNotIn('warnings', cand)
        self.assertEqual(cand['record']['content']['steps'], ['Rebuild index'])
        code, report = self.cli('learn', 'candidate', 'evaluate', cand['record']['id'][:8], '--actor', 'viewer')
        self.assertTrue(report['eligible'], report); self.assertTrue(any('by default' in w for w in report['warnings']))
        code, plan = self.cli('learn', 'candidate', 'promote-plan', cand['record']['id'], '--expected-revision', cand['record']['revision'])
        self.assertEqual(code, 0, plan)
        governance.apply(self.root, plan['plan_id'], plan['digest'])
        hit = search_domain('stale index repair', 'demo', root=self.root)[0]
        from core.runtime import hit_line
        self.assertIn('learn use report --procedure '+hit['procedure_id'][:8], hit_line(hit))
        code, used = self.cli('learn', 'use', 'report', '--procedure', hit['procedure_id'][:8], '--usage', 'used')
        self.assertEqual(code, 0, used)
        code, _ = self.cli('learn', 'config', 'set', '--disable', '--actor', 'viewer')
        self.assertEqual(code, 2)

    def test_retire_archives_and_unpublishes(self):
        p, _ = self.publish(self.candidate())
        with self.assertRaisesRegex(ValueError, 'retire-plan'):
            change(p['note_id'], 'demo', 'archive', self.root)
        plan = self.call('procedure', 'retire-plan', p['id'], expected_revision=p['revision'], reason='Superseded by tooling')
        governance.apply(self.root, plan['plan_id'], plan['digest'])
        self.assertEqual(search_domain('stale index repair', 'demo', root=self.root), [])
        _, note = find_note(p['note_id'], 'demo', self.root)
        self.assertEqual(note['status'], 'archived')
        self.assertEqual(lm.problems(self.root, 'demo'), [])

    def test_promote_copies_evidence_for_review_elsewhere(self):
        from core.init_domain import init_domain
        init_domain('other', [str(self.base/'other')], self.root)
        p, _ = self.publish(self.candidate())
        plan = self.call('procedure', 'promote-plan', p['id'], expected_revision=p['revision'], target='other')
        governance.apply(self.root, plan['plan_id'], plan['digest'])
        drafts = lm.rows(self.root, 'other', 'candidates')
        self.assertEqual(len(drafts), 1)
        self.assertTrue(lm.evaluate(self.root, 'other', drafts[0])['eligible'])
        self.assertEqual(inventory('other', self.root), [])            # nothing published until reviewed there

    def test_prune_removes_only_old_unreferenced_work(self):
        with patch.object(lm, 'now', lambda: '2020-01-01T00:00:00+00:00'):
            old = self.call('record', 'create', objective='Old', family='f', acceptance_criteria=['ok'], outcome='failed',
                            evidence=[dict(result='fail', method='tool_check', observer='x', excerpt='broke', limitations='n/a')])
            used = self.candidate()                                      # referenced by a candidate
        plan = self.call('task', 'prune-plan', days=30)
        self.assertEqual(plan['summary']['tasks'], 1)
        governance.apply(self.root, plan['plan_id'], plan['digest'])
        ids = {t['id'] for t in lm.rows(self.root, 'demo', 'tasks')}
        self.assertNotIn(old['task']['id'], ids); self.assertEqual(len(ids), 1)
        self.assertTrue(self.call('candidate', 'evaluate', used['id'])['eligible'])

    def test_hooks_allow_learning_for_main_agent_and_deny_subagent_writes(self):
        import sys
        from core import runtime
        def decide(rest, **extra):
            command = f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'
            payload = dict(session_id='s1', cwd=str(self.base/'project'), tool_name='Bash', tool_input={'command': command}, **extra)
            return runtime.process('claude', 'pre-tool-use', payload, self.root)
        self.assertIn('allow', decide("learn record --objective 'x'"))
        self.assertIn('deny', decide("learn record --objective 'x'", agent_id='sub'))
        self.assertNotIn('deny', decide('learn candidate list', agent_id='sub'))
        self.assertNotIn('allow', decide('learn config set --disable'))
        self.assertTrue(lm.is_read(None, None) and lm.is_read('candidate', 'evaluate'))
        self.assertFalse(lm.is_read('procedure', 'evaluate') or lm.is_read('procedure', 'suspend'))

    def test_imports_keep_only_their_own_closure(self):
        from core.bundle import export, import_plan
        from core.init_domain import init_domain
        self.publish(self.candidate())
        b = self.candidate()
        b = self.call('candidate', 'update', b['id'], expected_revision=b['revision'],
                      content=dict(b['content'], title='Second procedure'))['record']
        self.publish(b)
        bundle = self.base/'two.zip'; export(self.root, 'demo', bundle, include_learning=True)
        init_domain('other', [str(self.base/'other')], self.root)
        imported = import_plan(self.root, bundle, 'other')
        governance.apply(self.root, imported['plan_id'], imported['digest'])
        drafts = lm.rows(self.root, 'other', 'candidates')
        self.assertEqual(len(drafts), 2)
        for d in drafts:
            self.assertEqual(sum(1 for k in d['imported_records'] if k.startswith('versions/')), 1)
