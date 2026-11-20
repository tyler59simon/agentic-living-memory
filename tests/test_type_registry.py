"""Custom note types: the note_types.json registry, `type` (a direct, journaled write), `types`, and how notes of a custom type are
captured, laid out, resolved, retired, undone and loaded when their type is no longer registered."""
import contextlib,io,json,unittest
from datetime import date,timedelta
from unittest.mock import patch
from test_core import StoreFixture
from core.cli import main
from core.notes import find_note
from core.repository import inventory
from core import backup,clock
from core.runtime import protocol
from core.sync import _template

INCIDENT={'name':'incident','description':'A production incident: what broke, its impact and the follow-up.','summary':'What broke, for whom, and when it was resolved.',
          'sections':['Timeline','Impact','Follow-up'],'actions':True,'review_days':60,'concern':'concern/bug'}

class Fixture(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','subject/user','concern/architecture','concern/decision','concern/knowledge','concern/bug']}))
    def cli(self,*argv,stdin=None):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',io.StringIO(stdin or '')):
            try:code=main(['--root',str(self.root),'--json',*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip()[:1] in '{[' else text)
    def write(self,action,**payload):return self.cli('type',action,stdin=json.dumps(payload))
    def create(self,**kw):
        code,r=self.write('create',**{**INCIDENT,**kw});self.assertEqual(code,0,r);return r
    def capture(self,*extra,title='Checkout outage',summary='Checkout returned 500s for 20 minutes.',type_='incident'):
        return self.cli('--domain','demo','capture','--type',type_,'--title',title,'--summary',summary,*extra)
    def registry(self):return json.loads((self.root/'note_types.json').read_text(encoding='utf-8'))

class Registry(Fixture):
    def test_create_and_list(self):
        written=self.create()
        self.assertTrue(written['operation_id'].startswith('d-'))
        entry=self.registry()['types'][0]
        self.assertEqual((entry['name'],entry['status'],entry['sections'],entry['actions'],entry['review_days']),('incident','active',['Timeline','Impact','Follow-up'],True,60))
        code,listed=self.cli('types');self.assertEqual(code,0,listed)
        names=[t['name'] for t in listed['types']]
        self.assertEqual(names[:3],['knowledge','decision','preference']);self.assertEqual(names[-1],'incident');self.assertEqual(len(names),14)
        custom=listed['types'][-1];self.assertFalse(custom['builtin']);self.assertEqual(custom['concern'],'concern/bug')
        bug=next(t for t in listed['types'] if t['name']=='bug');self.assertTrue(bug['builtin']);self.assertEqual(bug['sections'],['Cause','Fix','How to check'])

    def test_validation(self):
        bad=[{'name':'Incident'},{'name':'in'},{'name':'in-cident'},{'name':'bug'},{'name':'correction'},{'name':'resolved'},
             {'sections':['Details']},{'sections':['Action items']},{'sections':['Impact','impact']},{'sections':[f'S{i}' for i in range(9)]},
             {'review_days':3},{'review_days':True},{'concern':'concern/missing'},{'concern':'subject/project'},{'actions':'yes'},{'extra':1},{'description':''}]
        for change in bad:
            with self.subTest(change=change):
                code,r=self.write('create',**{**INCIDENT,**change});self.assertNotEqual(code,0,r)
        payload=dict(INCIDENT);payload.pop('description')
        self.assertNotEqual(self.write('create',**payload)[0],0)
        self.create();code,r=self.write('create',**INCIDENT);self.assertNotEqual(code,0);self.assertIn('already exists',json.dumps(r))

    def test_subagents_and_viewers_cannot_write(self):
        for actor in ('subagent','viewer'):
            with self.subTest(actor=actor):
                code,r=self.cli('--actor',actor,'type','create',stdin=json.dumps(INCIDENT));self.assertNotEqual(code,0,r)
        self.assertFalse((self.root/'note_types.json').exists())

    def test_backup_and_version_include_types(self):
        self.assertIn('note_types.json',backup.TOP_LEVEL)
        code,v=self.cli('version');self.assertIn('types',v['commands']);self.assertIn('type',v['commands']);self.assertEqual(v['capabilities']['note_types'],1)

class Notes(Fixture):
    def test_capture_uses_the_definition(self):
        self.create()
        code,r=self.capture('--section','Impact','All checkouts failed.','--section','Timeline','14:02 deploy, 14:22 rollback.');self.assertEqual(code,0,r)
        self.assertIn('concern/bug',r['inferred_tags']);self.assertIn('subject/project',r['inferred_tags'])
        path,note=find_note(r['id'],'demo',self.root)
        self.assertTrue(path.name.startswith('INCIDENT - '));self.assertEqual(note['type'],'incident')
        self.assertLess(note['body'].index('## Timeline'),note['body'].index('## Impact'))
        self.assertEqual(note['review_by'],(clock.today()+timedelta(days=60)).isoformat())

    def test_action_items_and_resolve_follow_actions(self):
        self.create();self.create(name='postmortem',actions=False,review_days=None,concern=None)
        code,r=self.capture('--action','Add a canary');self.assertEqual(code,0,r)
        code,done=self.cli('--domain','demo','resolve',r['id']);self.assertEqual(code,0,done)
        self.assertEqual(find_note(r['id'],'demo',self.root)[1]['status'],'resolved')
        code,refused=self.capture('--action','Write it up','--concern','knowledge',title='Checkout postmortem',type_='postmortem');self.assertNotEqual(code,0,refused)
        code,plain=self.capture('--concern','knowledge',title='Checkout postmortem',type_='postmortem');self.assertEqual(code,0,plain)
        self.assertNotIn('review_by',find_note(plain['id'],'demo',self.root)[1])
        self.assertNotEqual(self.cli('--domain','demo','resolve',plain['id'])[0],0)

    def test_actions_cannot_be_turned_off_while_notes_use_them(self):
        self.create();self.assertEqual(self.capture('--action','Add a canary')[0],0)
        code,r=self.write('update',name='incident',actions=False);self.assertNotEqual(code,0,r);self.assertIn('action items',json.dumps(r))
        self.assertTrue(self.registry()['types'][0]['actions'])

    def test_retired_type_keeps_its_notes(self):
        self.create();code,r=self.capture();self.assertEqual(code,0,r)
        self.assertEqual(self.write('retire',name='incident')[0],0)
        code,refused=self.capture(title='Search outage',summary='Search was down.');self.assertNotEqual(code,0,refused);self.assertIn('retired',json.dumps(refused))
        code,updated=self.cli('--domain','demo','update',r['id'],'--section','Follow-up','Add a canary.');self.assertEqual(code,0,updated)
        code,listed=self.cli('types');self.assertNotIn('incident',[t['name'] for t in listed['types']])
        code,listed=self.cli('types','--all');self.assertEqual(listed['types'][-1]['status'],'retired')
        self.assertEqual(self.write('update',name='incident',status='active')[0],0)
        self.assertEqual(self.capture(title='Search outage',summary='Search was down.')[0],0)

    def test_retype_needs_an_active_type(self):
        self.create();code,r=self.cli('--domain','demo','capture','--title','Cache','--summary','The cache is rebuildable.','--concern','architecture');self.assertEqual(code,0,r)
        code,changed=self.cli('--domain','demo','update',r['id'],'--type','incident');self.assertEqual(code,0,changed)
        self.assertTrue(find_note(r['id'],'demo',self.root)[0].name.startswith('INCIDENT - '))
        self.assertNotEqual(self.cli('--domain','demo','update',r['id'],'--type','unknown')[0],0)

    def test_unknown_type_filter_names_the_store_types(self):
        self.create();code,r=self.cli('--domain','demo','list','--type','incidnet');self.assertNotEqual(code,0)
        self.assertIn('incident',json.dumps(r))
        self.assertEqual(self.cli('--domain','demo','list','--type','incident')[0],0)

class Undo(Fixture):
    def test_undo_create_refused_while_a_note_uses_the_type(self):
        written=self.create();code,r=self.capture();self.assertEqual(code,0,r)
        code,refused=self.cli('undo',written['operation_id']);self.assertNotEqual(code,0,refused);self.assertIn('incident',json.dumps(refused))
        self.assertTrue((self.root/'note_types.json').exists())

    def test_undo_create_without_notes(self):
        written=self.create()
        code,r=self.cli('undo',written['operation_id']);self.assertEqual(code,0,r)
        self.assertFalse((self.root/'note_types.json').exists())

class Unregistered(Fixture):
    def test_note_of_an_unregistered_type_still_loads(self):
        self.create();code,r=self.capture();self.assertEqual(code,0,r)
        (self.root/'note_types.json').unlink()
        self.assertIn(r['id'],[n['id'] for n in inventory('demo',self.root)])
        code,hits=self.cli('--domain','demo','search','checkout outage');self.assertEqual(code,0,hits);self.assertIn(r['id'],json.dumps(hits))
        code,doctor=self.cli('--domain','demo','doctor');self.assertIn('type incident, which is not registered',json.dumps(doctor))
        code,listed=self.cli('types');self.assertTrue(any('incident' in w for w in listed['warnings']))

class Surfaces(Fixture):
    def test_obsidian_template(self):
        self.create();text=_template('incident',self.root)
        for part in ('type: "incident"','concern/bug','What broke, for whom','## Timeline','## Impact','## Follow-up','## Action items'):self.assertIn(part,text)
        self.assertNotIn('## Details',text)

    def test_session_start_names_active_custom_types(self):
        self.assertNotIn('Custom note types',protocol(self.root,'demo','claude'))
        self.create();text=protocol(self.root,'demo','claude')
        self.assertIn('Custom note types (--type NAME): incident (Timeline, Impact, Follow-up, action items): A production incident',text)
        self.write('retire',name='incident');self.assertNotIn('Custom note types',protocol(self.root,'demo','claude'))

class ReviewFixes(Fixture):
    """Regressions for the Codex review of the first custom types commit (2026-11-20)."""
    def test_headings_the_renderer_treats_as_equal_are_one_heading(self):
        code,r=self.write('create',**{**INCIDENT,'sections':['Follow-up','Follow up']});self.assertNotEqual(code,0,r);self.assertIn('twice',json.dumps(r))

    def test_a_plain_action_section_keeps_actions_on(self):
        from core.notes import capture
        self.create()
        capture('Checkout outage','Checkout returned 500s.\n\n## Action items\n- add a canary',['subject/project','concern/bug'],'demo',self.root,type='incident')
        code,r=self.write('update',name='incident',actions=False);self.assertNotEqual(code,0,r)

    def test_template_uses_the_custom_subject(self):
        self.create(subject='subject/user');self.assertIn('subject/user',_template('incident',self.root))

    def test_a_new_note_needs_an_active_type(self):
        from core.notes import build_note
        self.create();self.write('retire',name='incident')
        with self.assertRaisesRegex(ValueError,'retired'):
            build_note('Checkout outage','Checkout returned 500s.',['subject/project','concern/bug'],'demo',self.root,'incident')

    def test_maintenance_treats_an_action_type_as_work_tracking(self):
        from core.maintenance import permitted
        self.create();code,r=self.capture();self.assertEqual(code,0,r)
        note=find_note(r['id'],'demo',self.root)[1]
        self.assertNotIn('supersede',permitted(note,['due'],True,True,self.root))
        code,k=self.cli('--domain','demo','capture','--title','Cache','--summary','The cache is rebuildable.','--concern','architecture')
        self.assertIn('supersede',permitted(find_note(k['id'],'demo',self.root)[1],['due'],True,True,self.root))

    def test_undo_is_judged_on_the_store_it_leaves(self):
        from core.type_registry import check_undo
        self.create();code,r=self.capture();self.assertEqual(code,0,r)
        rel=find_note(r['id'],'demo',self.root)[0].relative_to(self.root.resolve()).as_posix()
        with self.assertRaises(ValueError):check_undo(self.root,{'note_types.json':None})
        check_undo(self.root,{'note_types.json':None,rel:None})   # the undo removes the type and its only note together

    def test_undo_cannot_bring_back_action_items_the_type_no_longer_takes(self):
        self.create();code,r=self.capture('--action','Add a canary');self.assertEqual(code,0,r)
        code,edited=self.cli('--domain','demo','update',r['id'],'--drop-section','Action items');self.assertEqual(code,0,edited)
        self.assertEqual(self.write('update',name='incident',actions=False)[0],0)
        code,refused=self.cli('undo',edited['operation_id']);self.assertNotEqual(code,0,refused);self.assertIn('action items',json.dumps(refused))

    def test_restore_keeps_a_type_that_notes_left_after_it_use(self):
        from core.type_registry import restore_registry
        self.create(name='postmortem',actions=False,review_days=None,concern=None)
        older=(self.root/'note_types.json').read_text(encoding='utf-8')
        self.create();code,r=self.capture();self.assertEqual(code,0,r)
        rel=find_note(r['id'],'demo',self.root)[0].relative_to(self.root.resolve()).as_posix()
        kept=json.loads(restore_registry(self.root,{'note_types.json':older}))
        self.assertEqual(sorted(t['name'] for t in kept['types']),['incident','postmortem'])
        self.assertEqual(restore_registry(self.root,{'note_types.json':older,rel:None}),older)   # the restore also removes the note

class ReReviewFixes(Fixture):
    """Regressions for the Codex re-review of the review fixes (2026-11-20)."""
    def test_apply_rechecks_custom_types(self):
        from core.governance import plan,apply
        self.create();code,r=self.capture('--action','Add a canary');self.assertEqual(code,0,r)
        path=find_note(r['id'],'demo',self.root)[0];rel=path.relative_to(self.root.resolve()).as_posix();old=path.read_text(encoding='utf-8')
        self.assertEqual(self.cli('--domain','demo','update',r['id'],'--drop-section','Action items')[0],0)
        made=plan(self.root,{rel:old},'restore',['demo'])   # brings the action items back
        self.assertEqual(self.write('update',name='incident',actions=False)[0],0)
        with self.assertRaisesRegex(ValueError,'stale plan'):apply(self.root,made['plan_id'],made['digest'])

    def test_a_damaged_live_registry_does_not_block_a_restore(self):
        from core.type_registry import restore_registry
        self.create();healthy=(self.root/'note_types.json').read_text(encoding='utf-8')
        (self.root/'note_types.json').write_text('{broken',encoding='utf-8')
        self.assertEqual(restore_registry(self.root,{'note_types.json':healthy}),healthy)

    def test_a_registry_with_a_bom_restores(self):
        from core.type_registry import restore_registry
        self.create();text=(self.root/'note_types.json').read_text(encoding='utf-8')
        self.assertEqual(restore_registry(self.root,{'note_types.json':'\ufeff'+text}),'\ufeff'+text)

    def test_nested_note_paths_count(self):
        from core.type_registry import NOTE_FILE
        self.assertTrue(NOTE_FILE.fullmatch('domains/demo/notes/sub/INCIDENT - x-12345678.md'))
        self.assertTrue(NOTE_FILE.fullmatch('domains/demo/archive/INCIDENT - x-12345678.md'))

    def test_review_uses_the_store_root_for_maintenance(self):
        from core import worklist
        seen=[]
        real=__import__('core.maintenance',fromlist=['permitted']).permitted
        def spy(note,kinds=(),pair_same=False,has_successor=False,root=None):seen.append(root);return real(note,kinds,pair_same,has_successor,root)
        with patch('core.maintenance.permitted',spy):worklist._maintenance({'type':'knowledge','body':'x'},['due'],False,False,self.root)
        self.assertEqual(seen,[self.root])

if __name__=='__main__':unittest.main()
