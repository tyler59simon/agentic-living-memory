"""MAINT-1E stage A: the `maintenance` command, hook permissions, SessionStart scheduling and the summary line."""
import contextlib, io, json, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_core import StoreFixture
from core import maintenance, maintenance_agent, maintenance_state as ms, notes, runtime, usage
from core.cli import build_parser, main
from core.init_domain import init_domain

TAGS = ['subject/project', 'concern/architecture']
OLD = '2020-01-01'
EXPIRED = {'action': 'archive', 'rule': 'expired_status'}
KEEP = {'action': 'keep'}


def fake(decide, usage_micro=1000, check=None, calls=None):
    """An injected reviewer: decide(ref, candidate manifest) -> the decision fields for it. Never the real `claude`."""
    def adapter(req):
        if calls is not None:
            calls.append(req)
        out = [{'ref': ref, 'revision': c['revision'], 'reason': 'Because the evidence says so.', 'evidence_ids': c['evidence_ids'][:1], **decide(ref, c)}
               for ref, c in req['manifest']['candidates'].items()]
        return {'ok': True, 'report': {'version': 1, 'run_id': req['run']['id'], 'decisions': out}, 'usage_micro': usage_micro, 'stderr': ''}
    adapter.check = check or (lambda cfg: {'ok': True})
    return adapter


class Base(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project = self.base / 'project'
        self.project.mkdir(parents=True, exist_ok=True)
        env = patch.dict(os.environ, {'LIVING_MEMORY_NO_BACKGROUND': '', 'LIVING_MEMORY_MAINTENANCE_CHILD': ''})
        env.start()
        self.addCleanup(env.stop)

    def mk(self, title, body='A fact about the project.', **kw):
        kw.setdefault('allow_similar', True)
        return notes.capture(title, body, TAGS, 'demo', self.root, **kw)

    def status_note(self, title='Sprint status'):
        return self.mk(title, 'Currently migrating the database.', type='status', review_by=OLD)

    def ref(self, n):
        return 'demo:' + n['id']

    def get(self, n):
        return notes.find_note(n['id'], 'demo', self.root)[1]

    def cli(self, *argv, actor=None, domain='demo'):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), *(['--domain', domain] if domain else []), *(['--actor', actor] if actor else []), *argv])
        text = out.getvalue() or err.getvalue()
        return code, json.loads(text)

    def ok(self, *argv, **kw):
        code, result = self.cli(*argv, **kw)
        self.assertEqual(code, 0, result)
        return result

    def reviewer(self, adapter):
        patcher = patch.object(maintenance_agent, 'review', adapter)
        patcher.start()
        self.addCleanup(patcher.stop)

    def enable_config(self, **kw):
        return ms.set_config(self.root, ms.validate({'enabled': True, 'domains': ['demo'], **kw}))

    def day_files(self):
        return sorted(p.relative_to(self.root).as_posix() for p in (self.root / 'maintenance').rglob('*') if p.is_file() and p.name != 'worker.lock')


class Commands(Base):
    def test_every_action_through_main_and_stdio(self):
        a = self.status_note()
        self.reviewer(fake(lambda ref, c: EXPIRED))
        enabled = self.ok('maintenance', 'enable')
        self.assertEqual((enabled['enabled'], enabled['domains'], enabled['changed']), (True, ['demo'], True))
        preview = self.ok('maintenance', 'preview')
        self.assertEqual([c['ref'] for c in preview['candidates']], [self.ref(a)])
        ran = self.ok('maintenance', 'run')
        self.assertEqual((ran['status'], ran['archived'], self.get(a)['status']), ('completed', 1, 'archived'))
        status = self.ok('maintenance', 'status')
        self.assertEqual((status['last_run']['id'], status['last_run']['archived'], status['skip_reason']), (ran['id'], 1, 'interval'))
        history = self.ok('maintenance', 'history')
        self.assertEqual([r['id'] for r in history['runs']], [ran['id']])
        detail = self.ok('maintenance', 'history', '--run', ran['id'])
        self.assertEqual([(d['ref'], d['action'], d['result']) for d in detail['decisions']], [(self.ref(a), 'archive', 'applied')])
        self.assertFalse(self.ok('maintenance', 'disable')['enabled'])
        lines = ''.join(json.dumps({'id': i, 'argv': argv}) + '\n' for i, argv in enumerate([['maintenance', 'status'], ['maintenance', 'preview'], ['maintenance', 'history']]))
        out = io.StringIO()
        with patch('sys.stdin', io.StringIO(lines)), contextlib.redirect_stdout(out):
            main(['--root', str(self.root), '--domain', 'demo', 'serve', '--stdio'])
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r['exit_code'] for r in replies], [0, 0, 0])
        self.assertEqual((replies[0]['result']['enabled'], 'candidates' in replies[1]['result'], 'runs' in replies[2]['result']), (False, True, True))

    def test_subagents_and_viewers_get_the_reads_only(self):
        self.status_note()
        for actor in ('subagent', 'viewer'):
            for action in ('status', 'preview', 'history'):
                self.assertEqual(self.cli('maintenance', action, actor=actor)[0], 0, (actor, action))
            for action in ('run', 'enable', 'disable'):
                code, result = self.cli('maintenance', action, actor=actor)
                self.assertEqual(code, 2, (actor, action))
                self.assertIn('read-only', result['error']['message'])
        self.assertFalse((self.root / 'maintenance.json').exists())
        self.assertIn('maintenance', self.ok('version')['commands'])

    def test_unknown_flags_for_an_action_are_refused(self):
        for argv in (['status', '--limit', '3'], ['run', '--run', 'x'], ['enable', '--dry-run'], ['history', '--model', 'sonnet'], ['disable', '--all-domains']):
            code, result = self.cli('maintenance', *argv)
            self.assertEqual(code, 2, argv)
            self.assertIn('does not take', result['error']['message'])

    def test_scheduled_is_a_hidden_flag(self):
        self.assertNotIn('--scheduled', build_parser().subs.choices['maintenance'].format_help())
        self.assertEqual(build_parser().parse_args(['maintenance', 'run', '--scheduled']).scheduled, True)

    def test_enable_is_additive_and_keeps_accounting_and_settings(self):
        init_domain('other', [str(self.base / 'other')], self.root)
        a = self.status_note()
        self.reviewer(fake(lambda ref, c: EXPIRED))
        first = self.ok('maintenance', 'enable', '--max-notes-per-day', '7')
        self.assertEqual(first['limits']['max_notes_per_day'], 7)
        self.ok('maintenance', 'run')
        before = self.ok('maintenance', 'status')
        again = self.ok('maintenance', 'enable', domain='other')
        self.assertEqual(again['domains'], ['demo', 'other'])
        self.assertEqual((again['limits']['max_notes_per_day'], again['enabled_at'], again['accounting']['reviewed']), (7, first['enabled_at'], before['accounting']['reviewed']))
        self.assertEqual(again['accounting'], before['accounting'])
        changed = self.ok('maintenance', 'enable', '--model', 'opus', '--daily-budget-usd', '2', '--timeout-seconds', '60')
        self.assertEqual((changed['limits']['model'], changed['limits']['daily_budget_usd'], changed['limits']['timeout_seconds'], changed['limits']['max_notes_per_day']), ('opus', '2', 60, 7))
        self.assertEqual(self.ok('maintenance', 'enable', '--all-domains')['domains'], ['demo', 'other'])
        self.assertEqual(self.get(a)['status'], 'archived')

    def test_a_global_domain_must_be_named_explicitly(self):
        (self.root / 'domains' / '_global' / 'notes').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'global domain'):
            maintenance.enable(self.root, ['_global'], allow_global=False)
        self.assertFalse((self.root / 'maintenance.json').exists())
        self.assertEqual(self.ok('maintenance', 'enable', '--all-domains')['domains'], ['demo'])   # a global domain is never swept in
        named = self.ok('maintenance', 'enable', domain='_global')
        self.assertEqual(named['domains'], ['demo', '_global'])
        self.assertEqual(self.ok('maintenance', 'enable', '--model', 'sonnet')['domains'], ['demo', '_global'])   # an earlier explicit global stays valid

    def test_disable_keeps_domains_settings_and_accounting(self):
        self.status_note()
        self.reviewer(fake(lambda ref, c: KEEP))
        self.ok('maintenance', 'enable', '--max-notes-per-day', '4')
        self.ok('maintenance', 'run')
        before = self.ok('maintenance', 'status')
        off = self.ok('maintenance', 'disable')
        self.assertEqual((off['enabled'], off['domains'], off['limits'], off['accounting']), (False, ['demo'], before['limits'], before['accounting']))
        self.assertEqual(self.ok('maintenance', 'disable')['changed'], False)

    def test_a_disabled_dry_run_works_and_spends_the_allowance_while_a_live_run_is_refused(self):
        a = self.status_note()
        calls = []
        self.reviewer(fake(lambda ref, c: EXPIRED, calls=calls))
        dry = self.ok('maintenance', 'run', '--dry-run')
        self.assertEqual((dry['status'], dry['proposed'], len(calls)), ('dry_run', {'archive': 1}, 1))
        self.assertEqual((self.get(a)['status'], (self.root / 'maintenance.json').exists()), ('active', False))
        status = self.ok('maintenance', 'status')
        self.assertEqual((status['enabled'], status['accounting']['reviewed'], status['last_run']['status']), (False, 1, 'dry_run'))
        again = self.ok('maintenance', 'run', '--dry-run')   # every limit still applies: the interval is not bypassed
        self.assertEqual((again['status'], again['skip_reason']), ('skipped', 'interval'))
        live = self.ok('maintenance', 'run')
        self.assertEqual((live['status'], live['skip_reason'], len(calls)), ('skipped', 'disabled', 1))

    def test_a_disabled_dry_run_obeys_the_interval_once_enabled(self):
        self.status_note()
        self.reviewer(fake(lambda ref, c: KEEP))
        self.ok('maintenance', 'run', '--dry-run')
        self.ok('maintenance', 'enable')
        self.assertEqual(self.ok('maintenance', 'run', '--dry-run')['skip_reason'], 'interval')
        self.assertEqual(self.ok('maintenance', 'run')['skip_reason'], 'interval')

    def test_a_disabled_dry_run_needs_a_domain(self):
        self.status_note()
        self.reviewer(fake(lambda ref, c: KEEP))
        self.assertEqual(self.ok('maintenance', 'run', '--dry-run', domain=None)['skip_reason'], 'disabled')

    def test_status_shape(self):
        self.status_note()
        self.reviewer(fake(lambda ref, c: KEEP, usage_micro=420000))
        self.ok('maintenance', 'enable')
        self.ok('maintenance', 'run')
        s = self.ok('maintenance', 'status')
        self.assertEqual(set(s) - {'enabled_at', 'blocked', 'errors'}, {'ok', 'enabled', 'domains', 'policy_version', 'config_revision', 'limits', 'accounting', 'running',
                                                                      'eligible', 'next_eligible_at', 'skip_reason', 'last_run'})
        self.assertEqual(s['limits'], {'model': 'sonnet', 'max_notes_per_day': 10, 'timeout_seconds': 300, 'daily_budget_usd': '1.00', 'min_interval_seconds': 86400})
        self.assertEqual((s['accounting']['reviewed'], s['accounting']['charged_usd'], s['accounting']['reserved_usd'], s['accounting']['cost_complete']), (1, '0.42', '0.00', True))
        self.assertEqual((s['running'], s['eligible'], s['skip_reason'], s['errors'], s['blocked']), (False, False, 'interval', [], None))
        self.assertEqual({k: s['last_run'][k] for k in ('status', 'reviewed', 'updated', 'confirmed', 'archived', 'superseded', 'resolved', 'kept', 'deferred')},
                         {'status': 'completed', 'reviewed': 1, 'updated': 0, 'confirmed': 0, 'archived': 0, 'superseded': 0, 'resolved': 0, 'kept': 1, 'deferred': 0})

    def test_status_reports_a_malformed_config_and_unreadable_records_without_failing(self):
        (self.root / 'maintenance.json').write_text('{broken', encoding='utf-8')
        s = self.ok('maintenance', 'status')
        self.assertEqual((s['enabled'], s['eligible'], s['skip_reason']), (False, False, 'disabled'))
        self.assertTrue(s['errors'][0].startswith('config_invalid'))

    def test_preview_spends_nothing_and_shows_protection(self):
        a = self.status_note()
        pinned = self.mk('Pinned decision', 'Keep this one.', type='decision', pinned=True)
        calls = []
        self.reviewer(fake(lambda ref, c: EXPIRED, calls=calls))
        before = self.day_files()
        preview = self.ok('maintenance', 'preview')
        rows = {c['ref']: c for c in preview['candidates']}
        self.assertEqual(set(rows), {self.ref(a)})   # a pinned note with no review signal is not a candidate
        self.assertEqual(rows[self.ref(a)]['permitted'], ['keep', 'defer', 'confirm', 'update', 'archive'])
        self.assertEqual((rows[self.ref(a)]['protected'], rows[self.ref(a)]['kinds'], preview['too_large'], preview['dropped']), (None, ['due'], [], []))
        self.assertGreater(preview['estimated_packet_bytes'], 0)
        notes.change(pinned['id'], 'demo', 'update', self.root, review_by=OLD)
        rows = {c['ref']: c for c in self.ok('maintenance', 'preview')['candidates']}
        self.assertEqual((rows[self.ref(pinned)]['protected'], 'archive' in rows[self.ref(pinned)]['permitted']), ('pinned', False))
        self.assertEqual((calls, self.day_files()), ([], before))
        self.assertFalse((self.root / 'maintenance' / 'runs').exists())
        self.assertEqual(self.ok('maintenance', 'status')['accounting']['reviewed'], 0)

    def test_preview_before_enabling_needs_a_domain_and_names_it_in_the_error(self):
        self.status_note()
        code, result = self.cli('maintenance', 'preview', domain=None)
        self.assertEqual(code, 2)
        self.assertIn('No maintenance domains', result['error']['message'])

    def test_history_lists_runs_newest_first_and_joins_the_retirement_reason(self):
        a, b = self.status_note('Sprint status'), self.status_note('Release status')
        self.enable_config()
        t1, t2 = datetime.now(timezone.utc) - timedelta(days=30), datetime.now(timezone.utc) - timedelta(days=10)
        first = maintenance.run(self.root, adapter=fake(lambda ref, c: KEEP), now=t1)
        second = maintenance.run(self.root, adapter=fake(lambda ref, c: EXPIRED if ref == self.ref(a) else KEEP), now=t2)
        self.assertEqual((first['kept'], second['archived']), (2, 1))
        runs = self.ok('maintenance', 'history')['runs']
        self.assertEqual([r['id'] for r in runs], [second['id'], first['id']])
        self.assertEqual(len(self.ok('maintenance', 'history', '--limit', '1')['runs']), 1)
        detail = self.ok('maintenance', 'history', '--run', second['id'])
        rows = {d['ref']: d for d in detail['decisions']}
        self.assertEqual((rows[self.ref(a)]['result'], rows[self.ref(a)]['rule'], rows[self.ref(a)]['reason']), ('applied', 'expired_status', 'Because the evidence says so.'))
        self.assertEqual((rows[self.ref(b)]['result'], rows[self.ref(b)]['reason']), ('kept', 'Because the evidence says so.'))
        self.assertEqual(detail['run']['archived'], 1)
        code, result = self.cli('maintenance', 'history', '--run', '../x')
        self.assertEqual(code, 2)
        self.assertEqual(self.cli('maintenance', 'history', '--run', 'nope')[0], 2)

    def test_the_review_rows_say_what_maintenance_may_do(self):
        a, pinned = self.status_note(), self.mk('Pinned status', 'Waiting.', type='status', review_by=OLD, pinned=True)
        rows = {r['ref']: r for r in self.ok('review')['notes']}
        self.assertEqual(rows[self.ref(a)]['maintenance'], {'permitted': ['keep', 'defer', 'confirm', 'update', 'archive'], 'protected': None})
        self.assertEqual(rows[self.ref(pinned)]['maintenance'], {'permitted': ['keep', 'defer', 'confirm', 'update'], 'protected': 'pinned'})
        self.assertNotIn('Automatic maintenance', self.ok('review')['hint'])
        self.enable_config()
        self.assertIn('Automatic maintenance is on', self.ok('review')['hint'])

    def test_a_noisy_note_injected_often_and_never_used_gets_no_retirement_action(self):
        n = self.mk('Noisy fact', 'Shown a lot, read never.')
        usage.record(self.root, 'inject', [self.ref(n)] * 3, 's1', 'prompt')
        row = self.ok('maintenance', 'preview')['candidates'][0]
        self.assertEqual((row['ref'], row['kinds']), (self.ref(n), ['noisy']))
        self.assertEqual((row['permitted'], row['protected']), (['keep', 'defer', 'confirm', 'update'], 'evidence_incomplete'))
        self.assertEqual(self.ok('review')['notes'][0]['maintenance']['protected'], 'evidence_incomplete')
        self.enable_config()
        run = maintenance.run(self.root, adapter=fake(lambda ref, c: {'action': 'archive', 'rule': 'obsolete_content'}))   # a reviewer that retires on usage alone
        self.assertEqual((run['status'], run['archived'], run['deferred']), ('completed', 0, 1))
        self.assertEqual(self.get(n)['status'], 'active')
        detail = self.ok('maintenance', 'history', '--run', run['id'])['decisions'][0]
        self.assertEqual((detail['result'], detail['reason_code']), ('rejected', 'evidence_incomplete'))

    def test_the_scheduled_worker_logs_one_metadata_line_per_run(self):
        self.status_note()
        self.enable_config()
        self.reviewer(fake(lambda ref, c: EXPIRED, usage_micro=2500))
        run = self.ok('maintenance', 'run', '--scheduled')
        rows = [json.loads(line) for p in (self.root / 'logs').glob('log-*.jsonl') for line in p.read_text(encoding='utf-8').splitlines()]
        found = [r for r in rows if r['event'] == 'maintenance.run']
        self.assertEqual(len(found), 1)
        row = found[0]
        self.assertEqual((row['run_id'], row['status'], row['scheduled'], row['model'], row['cost_complete'], row['usage_micro'], row['counts']['archived']),
                         (run['id'], 'completed', True, 'sonnet', True, 2500, 1))
        self.assertNotIn('packet', json.dumps(row))
        self.assertNotIn('Currently migrating', json.dumps(rows))

    def test_the_log_shows_the_archive_row_and_no_stray_maintenance_rows(self):
        a = self.status_note()
        self.enable_config()
        self.reviewer(fake(lambda ref, c: EXPIRED))
        self.ok('maintenance', 'run')
        self.ok('maintenance', 'disable')
        rows = self.ok('log')['rows']
        self.assertEqual(sorted((r['event'], r['ref']) for r in rows), sorted([('created', self.ref(a)), ('archived', self.ref(a))]))
        self.assertEqual(self.get(a)['last_retirement']['actor'], 'maintenance')   # the log rows carry no actor; the retirement record does
        self.assertEqual([r['event'] for r in self.ok('log', '--note', self.ref(a))['rows']], ['archived', 'created'])


class Doctor(Base):
    def warnings(self):
        return [w for w in self.ok('doctor')['warnings'] if w.startswith('maintenance:')]

    def test_a_clean_store_has_no_maintenance_warning_and_no_provider_probe(self):
        with patch.object(maintenance_agent, 'provider', side_effect=AssertionError('probed')) as probe:
            self.assertEqual(self.warnings(), [])
        probe.assert_not_called()

    def test_malformed_config_and_unknown_policy_version(self):
        (self.root / 'maintenance.json').write_text('{broken', encoding='utf-8')
        self.assertIn('malformed', self.warnings()[0])
        (self.root / 'maintenance.json').write_text(json.dumps({**ms.DEFAULTS, 'policy_version': 2}), encoding='utf-8')
        self.assertIn('policy_version 2', self.warnings()[0])

    def test_unreadable_run_records_make_accounting_unavailable(self):
        self.enable_config()
        folder = self.root / 'maintenance' / 'runs' / datetime.now(timezone.utc).date().isoformat()
        folder.mkdir(parents=True)
        (folder / 'x.json').write_text('{broken', encoding='utf-8')
        with patch.object(maintenance_agent, 'provider', return_value={'ok': True, 'argv': ['claude']}):
            self.assertIn('accounting_unavailable', ' '.join(self.warnings()))

    def test_an_unfinished_run_with_no_worker_and_a_block(self):
        self.status_note()
        self.enable_config()
        record = ms.reserve(self.root, ms.config(self.root), 1)
        with patch.object(maintenance_agent, 'provider', return_value={'ok': True, 'argv': ['claude']}):
            self.assertIn(f"never finished and no worker is running ({record['id']})", self.warnings()[0])
            with ms.worker_lock(self.root):
                self.assertEqual(self.warnings(), [])   # a live worker holds the lock: not an orphan
        ms.recover_runs(self.root)
        ms.finish(self.root, ms.launch(self.root, ms.reserve(self.root, ms.config(self.root), 1, datetime.now(timezone.utc) + timedelta(days=2)), datetime.now(timezone.utc) + timedelta(days=2)),
                  5_000_000, 'completed', datetime.now(timezone.utc) + timedelta(days=2))
        with patch.object(maintenance_agent, 'provider', return_value={'ok': True, 'argv': ['claude']}):
            self.assertIn('blocked (provider_budget)', ' '.join(self.warnings()))

    def test_the_provider_is_probed_only_while_enabled(self):
        down = {'ok': False, 'reason_code': 'provider_unavailable'}
        with patch.object(maintenance_agent, 'provider', return_value=down) as probe:
            self.assertEqual(self.warnings(), [])
            probe.assert_not_called()
            self.enable_config()
            self.assertIn('provider_unavailable', self.warnings()[0])
            probe.assert_called_once()


class Hooks(Base):
    def setUp(self):
        super().setUp()
        self.popen = patch('subprocess.Popen')
        self.spawned = self.popen.start()
        self.addCleanup(self.popen.stop)
        self.marker = self.root / 'hooks' / 'maintenance.spawned'

    def start(self, session='s1', **extra):
        return runtime.process('claude', 'session-start', {'cwd': str(self.project), 'session_id': session, 'source': 'startup', **extra}, self.root)

    def workers(self):
        return [c for c in self.spawned.call_args_list if 'maintenance' in c.args[0]]

    def test_session_start_starts_one_detached_worker(self):
        self.enable_config()
        self.start()
        self.assertEqual(len(self.workers()), 1)
        call = self.workers()[0]
        script = Path(runtime.__file__).resolve().parents[1] / 'living_memory.py'
        self.assertEqual(call.args[0], [sys.executable, str(script), '--root', str(self.root), 'maintenance', 'run', '--scheduled'])
        kw = call.kwargs
        self.assertEqual((kw['stdin'], kw['stdout'], kw['stderr'], kw['cwd']), (-3, -3, -3, tempfile.gettempdir()))
        if os.name == 'nt':
            self.assertEqual(kw['creationflags'] & 0x208, 0x208)
        else:
            self.assertTrue(kw['start_new_session'])
        self.assertTrue(self.marker.exists())
        self.start('s2')
        self.assertEqual(len(self.workers()), 1)   # the cooldown

    def test_an_old_marker_does_not_stop_a_spawn_and_a_fresh_one_does(self):
        self.enable_config()
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        import time
        self.marker.write_text(str(time.time() - 700))
        self.start()
        self.assertEqual(len(self.workers()), 1)
        self.marker.write_text(str(time.time() - 5))
        self.start('s2')
        self.assertEqual(len(self.workers()), 1)

    def test_each_guard_prevents_a_spawn(self):
        self.enable_config()
        cases = {
            'subagent': dict(extra={'agent_id': 'sub-1'}),
            'headless': dict(env={'LIVING_MEMORY_HEADLESS': '1'}),
            'plan': dict(extra={'permission_mode': 'plan'}),
            'compact': dict(extra={'source': 'compact'}),
            'no_background': dict(env={'LIVING_MEMORY_NO_BACKGROUND': '1'}),
            'child': dict(env={'LIVING_MEMORY_MAINTENANCE_CHILD': '1'}),
        }
        for name, case in cases.items():
            with self.subTest(name), patch.dict(os.environ, case.get('env', {})):
                self.start(f'g-{name}', **case.get('extra', {}))
                self.assertEqual(self.workers(), [])
        self.assertEqual(self.spawned.call_count, 0)

    def test_disabled_interval_running_cooldown_and_unbound_prevent_a_spawn(self):
        self.start('d')
        self.assertEqual(self.workers(), [])   # disabled (the default)
        self.enable_config()
        with ms.worker_lock(self.root):
            self.assertEqual(maintenance.schedule(self.root), 'already_running')
            self.start('r')
        self.assertEqual(self.workers(), [])
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        import time
        self.marker.write_text(str(time.time()))
        self.assertEqual(maintenance.schedule(self.root), 'cooldown')
        self.marker.unlink()
        ms.reserve(self.root, ms.config(self.root), 1)   # an attempt just now: the interval holds
        self.assertEqual(maintenance.schedule(self.root), 'interval')
        self.start('i')
        runtime.process('claude', 'session-start', {'cwd': str(self.base / 'elsewhere'), 'session_id': 'u', 'source': 'startup'}, self.root)
        self.assertEqual(self.workers(), [])

    def test_schedule_names_each_reason_and_never_raises(self):
        self.assertEqual(maintenance.schedule(self.root), 'disabled')
        self.assertEqual(maintenance.schedule(self.root, subagent=True), 'subagent')
        self.assertEqual(maintenance.schedule(self.root, plan=True), 'plan_mode')
        self.assertEqual(maintenance.schedule(self.root, compact=True), 'compact')
        self.enable_config()
        with patch.object(ms, 'eligibility', side_effect=RuntimeError('boom')):
            self.assertEqual(maintenance.schedule(self.root), 'error')
        self.spawned.side_effect = OSError('no process')
        self.assertEqual(maintenance.schedule(self.root), 'error')
        self.assertFalse(self.marker.exists())

    def test_the_schedule_path_never_scans_probes_or_calls_the_model(self):
        self.enable_config()
        boom = AssertionError('the scheduling path must stay cheap')
        with patch.object(maintenance, 'candidates', side_effect=boom), patch.object(maintenance_agent, 'provider', side_effect=boom), \
                patch.object(maintenance_agent, 'review', side_effect=boom), patch('core.repository.inventory', side_effect=boom), \
                patch('subprocess.run', side_effect=boom), patch.object(maintenance, 'packet', side_effect=boom):
            self.assertEqual(maintenance.schedule(self.root), 'spawned')
        self.assertEqual(len(self.workers()), 1)
        self.assertFalse((self.root / 'maintenance' / 'runs').exists())   # nothing reserved: the worker decides that

    def test_the_main_agent_is_allowed_every_action_and_subagents_only_the_reads(self):
        base = f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo maintenance'
        self.assertIn('maintenance', runtime.ALLOWED_SUBCOMMANDS)
        self.assertNotIn('maintenance', runtime.MUTATING_SUBCOMMANDS)
        for action in ('status', 'preview', 'history', 'run', 'enable', 'disable'):
            payload = {'cwd': str(self.project), 'session_id': 'h', 'tool_name': 'Bash', 'tool_input': {'command': f'{base} {action}'}}
            self.assertIn('allow', runtime.process('claude', 'pre-tool-use', payload, self.root), action)
            reply = runtime.process('claude', 'pre-tool-use', {**payload, 'agent_id': 'sub-1'}, self.root)
            self.assertEqual('deny' in reply, action in ('run', 'enable', 'disable'), action)
        flagged = {'cwd': str(self.project), 'session_id': 'h', 'tool_name': 'Bash', 'tool_input': {'command': f'{base} run --dry-run'}}
        self.assertIn('deny', runtime.process('claude', 'pre-tool-use', {**flagged, 'agent_id': 'sub-1'}, self.root))
        self.assertTrue(runtime.maintenance_write(f'{base} enable --all-domains', 'posix'))
        self.assertFalse(runtime.maintenance_write(f'{base} history --run abc', 'posix'))
        self.assertTrue(runtime.maintenance_write(f'{base} --limit 3 run', 'posix'))


class Summary(Base):
    LINE = 'Memory maintenance: checked 1, updated 0, retired 1, deferred 0. maintenance history shows reasons; restore REF brings archived notes back.'

    def setUp(self):
        super().setUp()
        self.popen = patch('subprocess.Popen')
        self.popen.start()
        self.addCleanup(self.popen.stop)

    def start(self, session, **extra):
        return runtime.process('claude', 'session-start', {'cwd': str(self.project), 'session_id': session, 'source': 'startup', **extra}, self.root)['context']

    def test_nothing_is_printed_when_maintenance_was_never_enabled(self):
        self.status_note()
        self.assertNotIn('Memory maintenance', self.start('s1'))

    def test_the_summary_line_comes_once_per_run_per_session(self):
        self.status_note()
        self.enable_config()
        run = maintenance.run(self.root, adapter=fake(lambda ref, c: EXPIRED))
        self.assertEqual(run['archived'], 1)
        first = self.start('s1')
        self.assertIn(self.LINE, first)
        self.assertLess(first.index(self.LINE), first.index('Living Memory session=s1') + 400)   # in the prefix, not the degradable tail
        self.assertNotIn(self.LINE, self.start('s1'))
        self.assertIn(self.LINE, self.start('s2'))
        self.assertNotIn(self.LINE, self.start('s3', agent_id='sub-1'))   # a subagent session start stays quiet
        self.ok('maintenance', 'disable')
        self.assertNotIn('Memory maintenance', self.start('s4'))

    def test_a_dry_run_or_a_skip_prints_no_summary(self):
        self.status_note()
        maintenance.run(self.root, dry_run=True, adapter=fake(lambda ref, c: EXPIRED), allow_disabled=True, domains=['demo'])
        self.enable_config()
        self.assertNotIn('Memory maintenance', self.start('s1'))

    def test_a_blocked_store_says_why_once_per_session(self):
        self.status_note()
        self.enable_config()
        run = maintenance.run(self.root, adapter=fake(lambda ref, c: KEEP, usage_micro=5_000_000))
        self.assertEqual(run['reason_code'], None)
        blocked = 'Memory maintenance is blocked: provider_budget.'
        text = self.start('s1')
        self.assertIn(blocked, text)
        self.assertNotIn(blocked, self.start('s1'))
        self.assertIn(blocked, self.start('s2'))

    def test_a_budget_change_lifts_the_blocked_line(self):
        self.status_note()
        self.enable_config()
        maintenance.run(self.root, adapter=fake(lambda ref, c: KEEP, usage_micro=5_000_000))
        self.assertIn('Memory maintenance is blocked: provider_budget.', self.start('s1'))
        self.ok('maintenance', 'enable', '--daily-budget-usd', '2.00')
        self.assertNotIn('Memory maintenance is blocked', self.start('s2'))
        self.assertIsNone(self.ok('maintenance', 'status')['blocked'])
        with patch.object(maintenance_agent, 'provider', return_value={'ok': True, 'argv': ['claude']}):
            self.assertFalse([w for w in maintenance.doctor_warnings(self.root) if 'blocked' in w])

    def test_history_keeps_the_reason_after_a_later_retirement(self):
        from core.note_format import render
        plan = self.mk('Finished plan', render('Ship it.', actions=[], done=['Step one']), type='plan')
        self.enable_config()
        run = maintenance.run(self.root, adapter=fake(lambda ref, c: {'action': 'resolve', 'rule': 'completed_work'}))
        self.assertEqual(run['resolved'], 1)
        notes.change(plan['id'], 'demo', 'reopen', self.root)
        notes.change(plan['id'], 'demo', 'resolve', self.root)
        self.assertEqual(self.get(plan)['last_retirement']['actor'], 'agent')
        row = self.ok('maintenance', 'history', '--run', run['id'])['decisions'][0]
        self.assertEqual((row['rule'], row['reason']), ('completed_work', 'Because the evidence says so.'))

    def test_an_unavailable_provider_is_a_blocked_line(self):
        self.status_note()
        self.enable_config()
        run = maintenance.run(self.root, adapter=fake(lambda ref, c: KEEP, check=lambda cfg: {'ok': False, 'reason_code': 'provider_unavailable'}))
        self.assertEqual((run['status'], run['reason_code']), ('failed', 'provider_unavailable'))
        self.assertIn('Memory maintenance is blocked: provider_unavailable.', self.start('s1'))


if __name__ == '__main__':
    unittest.main()
