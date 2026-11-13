"""log: the activity index over journals and changes.json, its cache, and the session ledger."""
import json,time,unittest,zipfile
from unittest.mock import patch
from test_changes import Fixture
from core import backup,runtime
from core.capture import finalize
from core.init_domain import init_domain
from core.notes import capture
from core.repository import canonical_json

TAGS=['subject/project','concern/architecture']

class Log(Fixture):
    def setUp(self):
        super().setUp();self.charters={r['op'] for r in self.log()['rows']}   # the fixture's project and system charters
    def log(self,*argv,actor='main'):
        code,result=self.cli('--actor',actor,'log',*argv);self.assertEqual(code,0,result);return result
    def rows(self,*argv):return [r for r in self.log(*argv)['rows'] if r['op'] not in self.charters]
    def events(self,*argv):return [(r['event'],r['title']) for r in self.rows(*argv)]
    def run_cli(self,*argv):
        code,result=self.cli('--domain','demo',*argv);self.assertEqual(code,0,result);return result
    def apply_undo(self,operation_id):
        code,plan=self.cli('undo',operation_id);self.assertEqual(code,0,plan);return self.apply(plan)
    def note(self,title='Cache',body='The local cache is rebuildable.',**kwargs):
        return capture(title,body,TAGS,'demo',self.root,**kwargs)

class Notes(Log):
    def test_capture_update_confirm_and_supersede_rows_newest_first(self):
        a=self.note('Cache policy','The cache is rebuildable.');b=self.note('Eviction policy','Entries expire after a day.')
        self.run_cli('update','demo:'+a['id'],'--body','The cache is rebuildable and bounded.')
        self.run_cli('confirm','demo:'+a['id'])
        self.run_cli('supersede','demo:'+b['id'],'--by','demo:'+a['id'])
        rows=self.rows()
        self.assertEqual([(r['event'],r['title']) for r in rows],[('superseded','Eviction policy'),('confirmed','Cache policy'),('updated','Cache policy'),('created','Eviction policy'),('created','Cache policy')])
        self.assertEqual(rows[-1]['ref'],'demo:'+a['id']);self.assertEqual((rows[-1]['type'],rows[-1]['domain'],rows[-1]['projects']),('knowledge','demo',[]))
        self.assertTrue(rows[0]['op'])
        self.assertEqual(sorted((r['time'] for r in rows),reverse=True),[r['time'] for r in rows])

    def test_a_confirm_alone_is_not_an_update_and_a_link_rewrite_is_hidden(self):
        a=self.note('Cache policy','Body one.');self.run_cli('confirm','demo:'+a['id'])
        self.assertEqual([r['event'] for r in self.rows('--note','demo:'+a['id'])],['confirmed','created'])
        # Archiving a note rewrites the links of notes that point at it; those notes did not change in their own right.
        self.note('Pointer note','See [Cache policy](./PROJECT%20-%20cache-policy.md).')
        before=len(self.rows());code,plan=self.cli('--domain','demo','plan','archive','demo:'+a['id']);self.assertEqual(code,0,plan);self.apply(plan)
        new=self.rows()[:len(self.rows())-before];self.assertEqual([(r['event'],r['title']) for r in new],[('archived','Cache policy')])

    def test_undo_row(self):
        self.note('Cache policy','Body one.');rows=self.rows();self.assertEqual(len(rows),1)
        self.apply_undo(rows[0]['op'])
        self.assertEqual([(r['event'],r['title']) for r in self.rows()],[('undone','Cache policy'),('created','Cache policy')])

    def test_deleted_note_stays_in_its_history(self):
        a=self.note('Cache policy','Body one.');code,plan=self.cli('--domain','demo','plan','delete','demo:'+a['id']);self.assertEqual(code,0,plan);self.apply(plan)
        self.assertEqual([r['event'] for r in self.rows('--note',a['id'])],['deleted','created'])
        self.assertEqual([r['event'] for r in self.rows('--note','demo:'+a['id'])],['deleted','created'])

class Changes(Log):
    def test_change_rows(self):
        self.create()
        code,plan=self.plan('update',{'id':1,'stage':'design'});self.assertEqual(code,0,plan);self.apply(plan)
        code,plan=self.plan('close',{'id':1});self.assertEqual(code,0,plan);self.apply(plan)
        title='Prevent duplicate payment alerts'
        rows=self.rows('--type','change');self.assertEqual([(r['event'],r['ref'],r['title'],r['projects'],r['domain']) for r in rows],
            [('closed','CHG-001',title,['paystream'],'demo'),('stage','CHG-001',title,['paystream'],'demo'),('created','CHG-001',title,['paystream'],'demo')])
        self.assertEqual((rows[0]['detail'],rows[1]['detail']),('done','explore -> design'))
        self.assertEqual([r['event'] for r in self.rows('--note','chg-1')],['closed','stage','created'])
        notes=self.rows('--type','plan');self.assertEqual([(r['event'],r['projects']) for r in notes],[('resolved',['paystream']),('created',['paystream'])])
        self.assertEqual(len(self.rows('--project','paystream')),5)

    def test_undone_change(self):
        receipt,_=self.create();self.apply_undo(receipt['operation_id'])
        self.assertEqual(sorted(r['event'] for r in self.rows('--type','change')),['created','undone'])

class Cache(Log):
    def cache(self):return json.loads((self.root/'.cache/activity.json').read_text(encoding='utf-8'))['ops']

    def test_incremental_and_rebuilt_after_deletion(self):
        self.note('Cache policy','Body one.');first=self.rows();cache=self.root/'.cache/activity.json';self.assertTrue(cache.is_file())
        with patch('core.activity._read',side_effect=AssertionError('journal reopened')):self.assertEqual(self.rows(),first)
        self.note('Eviction policy','Body two.');self.assertEqual(len(self.rows()),2)
        cache.unlink();self.assertEqual(len(self.rows()),2);self.assertTrue(cache.is_file())
        cache.write_text('{broken');self.assertEqual(len(self.rows()),2)

    def test_a_journal_that_is_not_final_is_not_cached(self):
        self.note('Cache policy','Body one.');journal=next((self.root/'.operations').glob('*/journal.json'))
        value=json.loads(journal.read_text(encoding='utf-8'));copy=self.root/'.operations'/'pending01'/'journal.json';copy.parent.mkdir()
        copy.write_text(canonical_json({**value,'id':'pending01','state':'applying'}),encoding='utf-8')
        self.assertEqual(len(self.rows()),1);self.assertNotIn('pending01',self.cache())
        copy.write_text(canonical_json({**value,'id':'pending01','state':'committed'}),encoding='utf-8')
        self.assertEqual(len(self.rows()),2);self.assertIn('pending01',self.cache())

    def test_bookkeeping_and_rolled_back_journals_leave_no_rows(self):
        self.note('Cache policy','Body one.');journal=next((self.root/'.operations').glob('*/journal.json'));value=json.loads(journal.read_text(encoding='utf-8'))
        for name,fields in (('sync01',{'kind':'overlay_sync'}),('back01',{'state':'rolled_back'})):
            folder=self.root/'.operations'/name;folder.mkdir();(folder/'journal.json').write_text(canonical_json({**value,'id':name,**fields}),encoding='utf-8')
        self.assertEqual(len(self.rows()),1);ops=self.cache();self.assertEqual((ops['sync01']['rows'],ops['back01']['rows']),([],[]))

    def test_first_run_is_slower_than_later_runs(self):
        init_domain('bulk',[str(self.base/'bulk')],self.root)
        for i in range(60):capture(f'Bulk note number {i}',f'Body about topic {i}.',TAGS,'bulk',self.root,allow_similar=True)
        started=time.perf_counter();self.assertEqual(len(self.rows('--domain','bulk','--limit','1000')),60);cold=time.perf_counter()-started
        started=time.perf_counter();self.rows();warm=time.perf_counter()-started
        self.assertLess(warm,cold)

class Filters(Log):
    def setUp(self):
        super().setUp()
        init_domain('other',[str(self.base/'other')],self.root)
        with patch('core.notes.now',return_value='2026-10-01T12:00:00.000000+00:00'),patch('core.repository.now',return_value='2026-10-01T12:00:00.000000+00:00'):self.a=self.note('Old note','Written long ago.')
        self.b=self.note('New note','Written today.')
        self.c=capture('Other domain note','Written elsewhere.',TAGS,'other',self.root)
        self.create()

    def test_since_until_note_domain_project_type_event_and_limit(self):
        self.assertEqual(self.events('--until','2026-10-01'),[('created','Old note')])
        self.assertEqual(len(self.rows('--since','2026-10-02')),len(self.rows())-1)
        self.assertEqual(self.rows('--since','2999-01-01'),[])
        self.assertEqual([r['title'] for r in self.rows('--note','demo:'+self.b['id'])],['New note'])
        self.assertEqual([r['title'] for r in self.rows('--domain','demo','--note',self.b['id'][:8])],['New note'])
        self.assertEqual([r['title'] for r in self.rows('--domain','other')],['Other domain note'])
        self.assertNotIn('Other domain note',[r['title'] for r in self.rows('--domain','demo')])
        self.assertEqual({r['ref'] for r in self.rows('--project','paystream')},{'CHG-001',self.entry()['brief']})
        self.assertEqual({r['type'] for r in self.rows('--type','plan')},{'plan'})
        self.assertEqual({r['event'] for r in self.rows('--event','created')},{'created'})
        self.assertEqual(len(self.rows('--limit','2')),2);self.assertEqual(self.log('--limit','2')['count'],len(self.log()['rows']))
        self.assertEqual(self.rows('--project','nothing'),[])

    def test_bad_arguments_are_refused(self):
        for argv in (('--since','yesterday'),('--event','exploded'),('--note','not-a-note')):
            code,_=self.cli('log',*argv);self.assertNotEqual(code,0,argv)

    def test_every_domain_by_default_and_read_only_for_every_actor(self):
        self.assertEqual({r['domain'] for r in self.rows()},{'demo','other'})
        for actor in ('viewer','subagent'):self.assertEqual(self.log(actor=actor)['count'],len(self.log()['rows']))
        self.assertIn('log',runtime.ALLOWED_SUBCOMMANDS);self.assertNotIn('log',runtime.MUTATING_SUBCOMMANDS)

class Ledger(Log):
    def setUp(self):
        super().setUp()
        self.payload={'cwd':str(self.repo),'session_id':'one','prompt':'go'}
    def hook(self,event,**extra):return runtime.process('claude',event,{**self.payload,**extra},self.root)
    def edit(self,name):return self.hook('pre-tool-use',tool_name='Edit',tool_input={'file_path':str(self.repo/name)})
    def lines(self):
        files=sorted((self.root/'ledger').glob('sessions-*.jsonl'));return [json.loads(l) for f in files for l in f.read_text(encoding='utf-8').splitlines()]
    def work(self):
        n=self.note('Saved in session one','A.');self.hook('session-start');self.hook('user-prompt');self.edit('a.py')
        state=json.loads(runtime._pointer(self.root,'one').read_text());finalize('one',state['turn'],['demo:'+n['id']],self.root)
        return n

    def test_repeated_stops_give_one_session_row(self):
        n=self.work();self.hook('stop');self.hook('stop')
        rows=self.lines();self.assertEqual(len(rows),1);row=rows[0]
        self.assertEqual(list(row),['t','s','h','d','p','n','f'])
        self.assertEqual((row['s'],row['h'],row['d'],row['p'],row['n'],row['f']),(runtime.session_hash('one'),'claude','demo',['paystream'],['demo:'+n['id']],1))
        self.assertEqual(self.rows('--event','session')[0]['notes'],['demo:'+n['id']])
        self.hook('user-prompt');self.edit('b.py');self.hook('stop');self.hook('stop')
        self.assertEqual([r['f'] for r in self.lines()],[1,2])
        sessions=self.rows('--event','session');self.assertEqual(len(sessions),1)   # the reader keeps the last row of a session
        s=sessions[0];self.assertEqual((s['files'],s['harness'],s['domain'],s['projects'],s['type'],s['ref']),(2,'claude','demo',['paystream'],'session',runtime.session_hash('one')))
        self.assertIn('claude: 1 note(s) saved, 2 file(s) changed',s['title'])
        self.assertEqual(len(self.rows('--note','demo:'+n['id'],'--event','session')),1);self.assertEqual(self.rows('--project','other'),[])

    def test_a_quiet_session_and_a_failing_ledger_leave_the_stop_alone(self):
        self.hook('user-prompt');self.hook('stop');self.assertFalse((self.root/'ledger').exists())
        self.edit('a.py')
        with patch('core.activity.record_session',side_effect=OSError('disk full')):self.hook('stop')
        self.assertFalse((self.root/'ledger').exists())

    def test_backup_holds_the_ledger_and_a_restore_never_shortens_it(self):
        self.work();self.hook('stop');path=next((self.root/'ledger').glob('sessions-*.jsonl'));rel='ledger/'+path.name
        self.assertIn(rel,backup.canonical_files(self.root))
        result=backup.backup(self.root);self.assertIn(rel,zipfile.ZipFile(result['backup']).namelist());self.assertNotIn('unreadable',result)
        with path.open('ab') as handle:handle.write(b'{"t":"2026-11-14T00:00:00+00:00","s":"later","h":"claude","d":"demo","p":[],"n":[],"f":1}\n')
        grown=path.read_bytes();plan=backup.restore_plan(self.root,result['backup']);self.assertEqual(plan['status'],'unchanged')
        self.assertEqual(path.read_bytes(),grown)
        path.unlink();plan=backup.restore_plan(self.root,result['backup']);self.assertEqual(plan['summary']['write'],1)

    def test_direct_edits_of_the_ledger_are_denied(self):
        target=str(self.root/'ledger'/'sessions-2026.jsonl')
        self.assertEqual(runtime.protected_rule(target,str(self.repo),self.root),'config')
        for tool in ('Write','Edit'):
            result=self.hook('pre-tool-use',tool_name=tool,tool_input={'file_path':target});self.assertIn('deny',result,tool)

if __name__=='__main__':unittest.main()
