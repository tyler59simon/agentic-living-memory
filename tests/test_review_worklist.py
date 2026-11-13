"""C7: the review worklist (core/worklist.py): kinds, one row per note, confirm dismissals, `review --kind`, and no usage rows for a viewer."""
import contextlib,io,json,unittest
from datetime import date,timedelta
from pathlib import Path
from test_core import StoreFixture
from test_maps import git,commit,later,LONG_AGO,GIT
from core import runtime,usage
from core.cli import main
from core.notes import capture,change,confirm,find_note
from core.worklist import worklist,KINDS,INTERIM_DAYS

TAGS=['subject/project','concern/architecture']

class Base(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project=self.base/'project';(self.project/'src').mkdir(parents=True);(self.project/'src'/'a.py').write_text('retry = 3\n',encoding='utf-8')
    def cap(self,title,body='A short fact worth keeping.',**kw):return capture(title,body,TAGS,'demo',self.root,allow_similar=True,**kw)
    def cli(self,*argv,actor=None):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),'--domain','demo',*(['--actor',actor] if actor else []),*argv])
        self.assertEqual(code,0,out.getvalue()+err.getvalue());return json.loads(out.getvalue())
    def rows(self,**kw):return {r['ref']:r for r in worklist(self.root,['demo'],**kw)['items']}
    def ref(self,made):return 'demo:'+made['id']

class Worklist(Base):
    def test_stale_lists_a_note_whose_hashed_file_changed(self):
        made=self.cap('Retry limit','The retry limit is read from the config.',files=['src/a.py'])
        self.assertEqual(self.rows(),{})
        (self.project/'src'/'a.py').write_text('retry = 5\n',encoding='utf-8')
        row=self.rows()[self.ref(made)]
        self.assertEqual((row['kinds'],row['action']),(['stale'],'update'));self.assertEqual(row['stale'],'STALE: src/a.py changed since this note')
        self.assertEqual(worklist(self.root,['demo'])['counts']['stale'],1)

    def test_due_lists_the_review_date_and_age_with_an_action_by_type(self):
        old=self.cap('Build status','Green on main.',type='status',review_by='2020-01-01')
        decided=self.cap('Money decision','Money is long cents.',type='decision',review_by='2020-01-01')
        self.cap('Fresh decision','Fresh.',type='decision')
        rows=self.rows(kinds=['due'])
        self.assertEqual(set(rows),{self.ref(old),self.ref(decided)})
        self.assertEqual((rows[self.ref(old)]['action'],rows[self.ref(decided)]['action']),('archive-plan','confirm'))
        self.assertIn('2020-01-01',rows[self.ref(old)]['reason'])

    def test_interim_waits_for_the_days_then_lists_unconfirmed_beliefs(self):
        made=self.cap('Cache miss cause','The miss rate is probably the TTL, unconfirmed.',type='bug')
        self.assertEqual(self.rows(kinds=['interim']),{})
        later_day=date.today()+timedelta(days=INTERIM_DAYS+1)
        rows={r['ref']:r for r in worklist(self.root,['demo'],kinds=['interim'],today=later_day)['items']}
        self.assertEqual(rows[self.ref(made)]['kinds'],['interim']);self.assertEqual(rows[self.ref(made)]['action'],'update')

    def test_duplicate_row_names_the_older_note_as_the_pair(self):
        first=self.cap('Retry limit setting','The retry limit is three attempts.')
        second=self.cap('Retry limit setting value','The retry limit is three attempts per request.')
        self.cap('Unrelated billing rounding','Invoices round half up.')
        rows=self.rows(kinds=['duplicate']);self.assertEqual(list(rows),[self.ref(second)])
        row=rows[self.ref(second)];self.assertEqual(row['pair']['ref'],self.ref(first));self.assertEqual(row['pair']['title'],'Retry limit setting')
        self.assertGreaterEqual(row['pair']['score'],0.6);self.assertEqual(row['action'],'merge-plan')
        self.assertEqual(worklist(self.root,['demo'],cheap=True)['counts'].get('duplicate'),None)   # cheap skips the scan
        self.assertEqual(self.rows(cheap=True),{})

    def test_a_nearly_identical_body_pairs_notes_with_different_titles_and_proposes_a_supersede(self):
        body='The release train leaves every second Tuesday and the freeze starts the Friday before, with hotfixes allowed after approval by two reviewers.'
        first=self.cap('Release cadence',body);second=self.cap('When we ship',body+' ')
        rows=self.rows(kinds=['duplicate']);self.assertEqual(rows[self.ref(second)]['pair']['ref'],self.ref(first));self.assertEqual(rows[self.ref(second)]['action'],'supersede')

    def test_digests_maps_and_other_types_are_not_paired(self):
        self.cap('Retry limit setting','The retry limit is three attempts.',type='decision')
        self.cap('Retry limit setting value','The retry limit is three attempts per request.',type='policy')
        self.assertEqual(self.rows(kinds=['duplicate']),{})

    def test_numbered_status_titles_pair_but_numbered_plans_do_not(self):
        for word in ('Caching','Billing','Search','Backups','Logging','Auth','Deploys','Hooks'):self.cap(f'{word} overview',f'About {word}.')   # enough notes for the identifier veto (TitleScorer.IDF_MIN_NOTES)
        one=self.cap('Sprint 3 status','Three closed.',type='status');two=self.cap('Sprint 4 status','Five closed.',type='status')
        self.cap('Phase 1 plan','Steps one.',type='plan');self.cap('Phase 2 plan','Steps two.',type='plan')
        rows=self.rows(kinds=['duplicate']);self.assertEqual(list(rows),[self.ref(two)]);self.assertEqual(rows[self.ref(two)]['pair']['ref'],self.ref(one))

    def test_warned_recomputes_the_content_checks_and_a_confirm_on_or_after_the_update_dismisses_it(self):
        made=self.cap('Wiring task','Pending: wire the adapter.',type='knowledge')
        self.assertEqual(self.rows()[self.ref(made)]['kinds'],['warned']);self.assertIn('unfinished state',self.rows()[self.ref(made)]['reason'])
        confirm(made['id'],'demo',self.root);self.assertEqual(self.rows(),{})
        change(made['id'],'demo','update',self.root,body='Still pending: wire the adapter later.')   # a new update is a new look
        self.assertEqual(self.rows()[self.ref(made)]['kinds'],['warned'])

    def test_confirming_either_note_dismisses_a_duplicate_pair_until_the_next_update(self):
        first=self.cap('Retry limit setting','The retry limit is three attempts.');second=self.cap('Retry limit setting value','The retry limit is three attempts per request.')
        self.assertIn(self.ref(second),self.rows(kinds=['duplicate']))
        confirm(first['id'],'demo',self.root);self.assertEqual(self.rows(kinds=['duplicate']),{})
        change(second['id'],'demo','update',self.root,body='The retry limit is four attempts per request.')
        self.assertIn(self.ref(second),self.rows(kinds=['duplicate']))

    def test_noisy_rows_come_last_and_never_propose_an_archive(self):
        quiet=self.cap('Noisy fact','Shown a lot, read never.');old=self.cap('Old status','Green.',type='status',review_by='2020-01-01')
        usage.record(self.root,'inject',[self.ref(quiet)]*3,'s1','prompt')
        rows=worklist(self.root,['demo'])['items']
        self.assertEqual([r['ref'] for r in rows],[self.ref(old),self.ref(quiet)]);noisy=rows[1]
        self.assertEqual((noisy['kinds'],noisy['action']),(['noisy'],'update'));self.assertIn('sharper title or summary',noisy['reason'])
        usage.record(self.root,'read',[self.ref(quiet)],'s1');self.assertNotIn(self.ref(quiet),self.rows())

    def test_a_note_appears_once_with_every_kind_in_order(self):
        made=self.cap('Wiring task','Pending: wire the adapter.',type='status',review_by='2020-01-01')
        usage.record(self.root,'inject',[self.ref(made)]*3,'s1','prompt')
        found=worklist(self.root,['demo']);self.assertEqual(len(found['items']),1);self.assertEqual(found['total'],1)
        self.assertEqual(found['items'][0]['kinds'],['due','warned','noisy']);self.assertEqual(found['items'][0]['action'],'archive-plan')
        self.assertEqual({k:v for k,v in found['counts'].items() if v},{'due':1,'warned':1,'noisy':1})

    def test_review_keeps_its_fields_and_filters_by_kind(self):
        old=self.cap('Old status','Green.',type='status',review_by='2020-01-01');warned=self.cap('Wiring task','Pending: wire the adapter.')
        found=self.cli('review')
        self.assertEqual((found['due'],found['stale_digests']),(2,0));self.assertEqual(found['kinds'],list(KINDS));self.assertEqual(found['counts']['due'],1);self.assertEqual(found['counts']['warned'],1)
        self.assertIn('propose plans for archive or merge; the user applies them',found['hint'])
        note={n['ref']:n for n in found['notes']}[self.ref(old)]
        for key in ('ref','title','type','updated','review_by','confirmed','revision','domain'):self.assertIn(key,note)
        only=self.cli('review','--kind','warned');self.assertEqual([n['ref'] for n in only['notes']],[self.ref(warned)]);self.assertEqual(only['due'],1)
        both=self.cli('review','--kind','warned','--kind','due');self.assertEqual(len(both['notes']),2)

    def test_a_viewer_review_writes_no_usage_rows(self):
        self.cap('Old status','Green.',type='status',review_by='2020-01-01')
        before=sorted((self.root/'.turns').glob('usage-*')) if (self.root/'.turns').exists() else []
        found=self.cli('review',actor='viewer');self.assertEqual(found['due'],1)
        after=sorted((self.root/'.turns').glob('usage-*')) if (self.root/'.turns').exists() else []
        self.assertEqual(before,after);self.assertEqual([r for r in usage.events(self.root) if r['e'] in {'read','inject','update'}],[])

class Surfaces(Base):
    def test_session_start_line_has_counts_by_kind_two_refs_and_fits_260_characters(self):
        from core.session_start import _review_line,render_context
        for i in range(3):self.cap(f'Old status number {i} with a rather long title that goes on and on and on for a while',f'Green {i}.',type='status',review_by='2020-01-01')
        self.cap('Wiring task','Pending: wire the adapter.')
        line=_review_line('demo',self.root)[0]
        self.assertIn('4 note(s) due for review (3 due, 1 warned;',line);self.assertEqual(line.count('demo:'),2);self.assertLessEqual(len(line),260)
        self.assertIn(line,render_context(str(self.base/'project'),self.root))
        self.assertEqual(_review_line('demo',self.root,[]),[])   # preloaded notes are used as given

    def test_obsidian_stale_page_lists_worklist_rows(self):
        from core.sync import build_overlay
        old=self.cap('Old status','Green.',type='status',review_by='2020-01-01');self.cap('Fine note','Nothing wrong here.')
        vault=self.base/'vault';build_overlay('demo',vault,self.root)
        page=(vault/'Dashboards'/'Stale Notes.md').read_text(encoding='utf-8')
        self.assertIn('Old status',page);self.assertIn('due: review date 2020-01-01 has passed',page);self.assertNotIn('Fine note',page)
        self.assertIn('due for review: 1',(vault/'Dashboards'/'Home.md').read_text(encoding='utf-8'))

@unittest.skipUnless(GIT,'git is not installed')
class Maps(StoreFixture):
    def test_a_map_whose_cached_currency_lists_changed_files_is_stale_without_running_git(self):
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','concern/architecture','concern/knowledge']}))
        project=self.base/'project';app=project/'app';app.mkdir(parents=True)
        git(app,'init','-q');git(app,'symbolic-ref','HEAD','refs/heads/main');app=app.resolve()
        commit(app,{'pom.xml':'<v>1</v>','src/Money.java':'long cents;'},LONG_AGO,'initial')
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=main(['--root',str(self.root),'--domain','demo','capture','--type','map','--file',str(app),'--title','app map','--summary','A money service.','--detail','**Layout:** src/Money.java is the money type.'])
        made=json.loads(out.getvalue());self.assertEqual(code,0,made)
        self.assertEqual(worklist(self.root,['demo'],kinds=['stale'])['items'],[])   # nothing cached yet
        commit(app,{'pom.xml':'<v>2</v>'},later())
        runtime.process('claude','session-start',{'cwd':str(app),'session_id':'s1'},self.root)   # computes and caches the currency
        from unittest.mock import patch
        with patch('core.maps.subprocess.run',side_effect=AssertionError('git must not run')):
            rows=worklist(self.root,['demo'],kinds=['stale'])['items']
        self.assertEqual([r['ref'] for r in rows],['demo:'+made['id']]);self.assertIn('STALE: pom.xml changed since',rows[0]['reason'])
        confirm(made['id'],'demo',self.root);self.assertEqual(worklist(self.root,['demo'],kinds=['stale'])['items'],[])   # a new revision: the cached entry no longer applies

if __name__=='__main__':unittest.main()
