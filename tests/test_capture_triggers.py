"""Capture triggers: decision documents edited during a turn, decision and directive statements, and the
once-per-session rules that keep them from nagging. See core/triggers.py and runtime.stop_pending."""
import contextlib,io,json,os,shutil,subprocess,time,unittest
from test_core import StoreFixture
from core import runtime,usage
from core.cli import main
from core.common import resolve_domain,load_bindings
from core.triggers import decision_docs,glob_regex,matches

class Glob(unittest.TestCase):
    def test_double_star_spans_folders_and_single_star_does_not(self):
        self.assertTrue(matches('docs/adr/ADR-004-money.md',['docs/**/*.md']))
        self.assertTrue(matches('docs/x.md',['docs/**/*.md']))                     # ** may match no folder at all
        self.assertFalse(matches('docs/x.txt',['docs/**/*.md']))
        self.assertTrue(matches('a/b/adr/0001.md',['**/adr/**']))
        self.assertFalse(matches('a/b/adrs.md',['**/adr/**']))
        self.assertTrue(matches('CHANGELOG.md',['CHANGELOG*']));self.assertTrue(matches('CHANGELOG',['CHANGELOG*']))
        self.assertTrue(matches('b.py',['*.py']));self.assertFalse(glob_regex('*.py').fullmatch('a/b.py'))   # * stays in a segment
        self.assertTrue(matches('A/ADR/X.MD',['a/adr/*.md']))                         # case-insensitive, like the file systems it serves

    def test_a_pattern_also_matches_the_tail_of_a_longer_path(self):
        self.assertTrue(matches('myrepo/docs/guide/intro.md',['docs/**/*.md']))
        self.assertTrue(matches('myrepo/CHANGELOG.md',['CHANGELOG*']))

    def test_defaults_include_exclude_and_override(self):
        paths=['docs/adr/ADR-004-money.md','CHANGELOG.md','board/ticket-9.md','tickets/T-1.md','handoff/2026-11-10.md','docs/standup/mon.md','src/app.py','README.md','decisions/use-uuid.md']
        self.assertEqual(decision_docs(paths,{}),['docs/adr/ADR-004-money.md','CHANGELOG.md','board/ticket-9.md','tickets/T-1.md','handoff/2026-11-10.md','decisions/use-uuid.md'])
        custom={'decision_docs':{'include':['notes/**','README*'],'exclude':['notes/private/**']}}
        self.assertEqual(decision_docs(['notes/a.md','notes/private/b.md','README.md','docs/adr/x.md'],custom),['notes/a.md','README.md'])
        self.assertEqual(decision_docs(['docs/adr/x.md','docs/standup/y.md'],{'decision_docs':{'exclude':[]}}),['docs/adr/x.md','docs/standup/y.md'])   # one list replaced, the other default
        self.assertEqual(decision_docs(['docs/adr/x.md'],{'decision_docs':'broken'}),['docs/adr/x.md'])


class DocFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project=self.base/'project';(self.project/'docs'/'adr').mkdir(parents=True);(self.project/'docs'/'standup').mkdir(parents=True)
        self.cwd=str(self.project);self.payload={'cwd':self.cwd,'session_id':'s1','prompt':'record the money decision'}
        self.doc=self.project/'docs'/'adr'/'ADR-004-money.md'
        self.doc.write_text('# ADR-004: Money representation\n\n## Decision\nAll amounts are BigDecimal.\n',encoding='utf-8')
    def prompt(self,text='work on it',**extra):
        self.payload={**self.payload,'prompt':text,**extra};return runtime.process('claude','user-prompt',self.payload,self.root)
    def tool(self,name,args,**extra):return runtime.process('claude','pre-tool-use',{**self.payload,'tool_name':name,'tool_input':args,**extra},self.root)
    def stop(self,**extra):return runtime.process('claude','stop',{**self.payload,**extra},self.root)
    def state(self):return json.loads(runtime._pointer(self.root,'s1').read_text(encoding='utf-8'))
    def edit_doc(self,path=None):self.tool('Write',{'file_path':str(path or self.doc)})
    def reminders(self):return [r['k'] for r in usage.events(self.root) if r['e']=='reminder' and not r['k'].startswith('nudge-')]

class DocEdits(DocFixture):
    def test_writing_a_decision_document_blocks_stop_once_and_names_the_doc_and_a_candidate_note(self):
        money=self.note('Money representation','Amounts are stored as long cents.')   # no path in it: found by the doc's heading
        self.prompt();self.edit_doc()
        self.assertEqual(self.state()['docs'],['docs/adr/ADR-004-money.md'])
        reason=self.stop()['continue_reason']
        self.assertIn('You changed docs/adr/ADR-004-money.md',reason)
        self.assertIn("--link 'docs/adr/ADR-004-money.md'",reason);self.assertIn('--type decision',reason)
        self.assertIn('demo:'+money['id'][:8]+' Money representation',reason);self.assertIn('CMD update demo:'+money['id'][:8]+' --session s1 --set-detail',reason)
        self.assertIn('Nothing durable: CMD capture-finalize --session s1',reason)
        self.assertLessEqual(len(reason),700)
        self.assertEqual(self.reminders(),['doc'])
        state=self.state();self.assertNotIn('docs',state);self.assertEqual(state['docs_reminded'],['docs/adr/ADR-004-money.md'])
        self.assertEqual(self.stop(),{})                                              # once per reply

    def test_a_note_listing_the_doc_is_a_candidate_first(self):
        listed=self.note('ADR index','Where decisions live.',files=['docs/adr/ADR-004-money.md'])
        self.prompt();self.edit_doc()
        self.assertIn('demo:'+listed['id'][:8]+' ADR index',self.stop()['continue_reason'])

    def test_a_path_reminds_at_most_once_per_session(self):
        self.prompt();self.edit_doc();self.assertIn('continue_reason',self.stop())
        self.prompt('again');self.edit_doc()
        self.assertEqual(self.stop(),{})                                              # same doc, same session
        other=self.project/'docs'/'adr'/'ADR-005-ids.md';other.write_text('# ADR-005: Identifiers\n',encoding='utf-8')
        self.prompt('another');self.edit_doc(other)
        self.assertIn('ADR-005-ids.md',self.stop()['continue_reason'])
        self.payload['session_id']='s2'
        self.prompt('new session');self.edit_doc()
        self.assertIn('ADR-004-money.md',self.stop()['continue_reason'])              # a new session asks again

    def test_a_receipt_quiets_it_and_so_does_the_capture_that_answers_it(self):
        self.prompt();self.edit_doc()
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=main(['--root',str(self.root),'--domain','demo','capture','--session','s1','--type','decision','--tag','subject/project','--tag','concern/decision',
                       '--title','Money is BigDecimal','--summary','Amounts are BigDecimal.','--link','docs/adr/ADR-004-money.md'])
        self.assertEqual(code,0,out.getvalue())
        self.assertEqual(self.stop(),{})
        self.assertEqual(self.reminders(),[])

    def cli(self,*argv):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=main(['--root',str(self.root),'--domain','demo',*argv])
        self.assertEqual(code,0,out.getvalue());return json.loads(out.getvalue())

    def capture(self,title,*extra):
        return self.cli('capture','--session','s1','--type','decision','--tag','subject/project','--tag','concern/decision','--title',title,'--summary',title+'.',*extra)

    def test_an_unrelated_receipt_does_not_silence_the_doc_reminder(self):
        self.prompt();self.edit_doc()
        self.capture('Retries use jittered backoff')   # a note about something else
        reason=self.stop()['continue_reason']
        self.assertIn('You changed docs/adr/ADR-004-money.md',reason);self.assertEqual(self.reminders(),['doc'])

    def test_a_note_that_lists_or_digests_the_doc_covers_it(self):
        self.prompt();self.edit_doc();self.capture('Money is BigDecimal','--file','docs/adr/ADR-004-money.md')   # files
        self.assertEqual(self.stop(),{})
        other=self.project/'docs'/'adr'/'ADR-005-ids.md';other.write_text('# ADR-005: Identifiers\n',encoding='utf-8')
        self.prompt('digest it');self.edit_doc(other)
        self.cli('capture','--session','s1','--type','knowledge','--tag','subject/project','--tag','concern/architecture','--title','Ids ADR digest','--summary','Digest.','--source','docs/adr/ADR-005-ids.md')
        self.assertEqual(self.stop(),{})                                                # sources

    def test_updating_a_note_that_links_the_doc_covers_it_but_a_link_to_another_doc_does_not(self):
        old=self.note('Money notes','Amounts.',allow_similar=True)
        self.prompt();self.edit_doc()
        self.cli('update','demo:'+old['id'],'--session','s1','--add-link','docs/adr/ADR-040-other.md')
        self.assertIn('You changed docs/adr/ADR-004-money.md',self.stop()['continue_reason'])
        other=self.project/'docs'/'adr'/'ADR-005-ids.md';other.write_text('# ADR-005: Identifiers\n',encoding='utf-8')
        self.prompt('again');self.edit_doc(other)
        self.cli('update','demo:'+old['id'],'--session','s1','--add-link','docs/adr/ADR-005-ids.md')
        self.assertEqual(self.stop(),{})

    def test_nothing_durable_still_dismisses_the_doc(self):
        self.prompt();self.edit_doc();self.cli('capture-finalize','--session','s1')
        self.assertEqual(self.stop(),{})

    def test_docs_beyond_the_cap_are_asked_about_at_the_next_stop(self):
        extra=[self.project/'docs'/'adr'/n for n in ('ADR-006-x.md','ADR-007-y.md')]
        for p in extra:p.write_text('# '+p.stem+'\n',encoding='utf-8')
        self.prompt()
        for p in (self.doc,*extra):self.edit_doc(p)
        self.assertEqual(self.stop()['continue_reason'].count('You changed'),2)
        self.assertEqual(self.state()['docs_left'],['docs/adr/ADR-007-y.md'])
        self.prompt('and now?')
        self.assertIn('You changed docs/adr/ADR-007-y.md',self.stop()['continue_reason'])
        self.prompt('and now?');self.assertEqual(self.stop(),{})

    def test_excluded_unmatched_and_non_writes_do_not_remind(self):
        standup=self.project/'docs'/'standup'/'mon.md';standup.write_text('x',encoding='utf-8')
        self.prompt();self.edit_doc(standup);self.assertEqual(self.stop(),{})
        self.prompt();self.tool('Edit',{'file_path':str(self.project/'src.py')});self.assertEqual(self.stop(),{})
        self.prompt();self.tool('Read',{'file_path':str(self.doc)});self.assertEqual(self.stop(),{})
        self.prompt();self.tool('Write',{'file_path':str(self.doc)},agent_id='sub');self.assertEqual(self.stop(),{})   # subagents are read-only
        self.prompt();self.tool('Write',{'file_path':str(self.doc)},permission_mode='plan');self.assertEqual(self.stop(),{})

    def test_the_bindings_override_replaces_the_default_list(self):
        path=self.root/'bindings.json';row=json.loads(path.read_text(encoding='utf-8-sig'))
        path.write_text(json.dumps({**row,'decision_docs':{'include':['memos/**']}}),encoding='utf-8')
        self.assertEqual(resolve_domain(self.cwd,load_bindings(self.root)),'demo')    # the top-level key is not a domain
        (self.project/'memos').mkdir();memo=self.project/'memos'/'cfo.md';memo.write_text('# Memo\n',encoding='utf-8')
        self.prompt();self.edit_doc();self.assertEqual(self.stop(),{})                # ADRs are no longer listed
        self.prompt();self.edit_doc(memo);self.assertIn('memos/cfo.md',self.stop()['continue_reason'])

    def test_the_text_stays_short_with_two_docs_and_long_titles(self):
        for i in range(3):self.note('A very long note title about money representation and rounding rules number %d in the ledger'%i,'Money amounts.',files=['docs/adr/ADR-004-money.md','docs/adr/ADR-006-x.md'],allow_similar=True)
        second=self.project/'docs'/'adr'/'ADR-006-x.md';third=self.project/'docs'/'adr'/'ADR-007-y.md'
        for p in (second,third):p.write_text('# '+p.stem+'\n',encoding='utf-8')
        self.payload['prompt']='remember that we use BigDecimal'
        runtime.process('claude','user-prompt',self.payload,self.root)
        for p in (self.doc,second,third):self.edit_doc(p)
        reason=self.stop()['continue_reason']
        self.assertLessEqual(len(reason),700)
        self.assertEqual(reason.count('You changed'),1)                              # the rest did not fit; they stay unasked
        self.assertIn('asked you to remember something',reason)
        self.assertLessEqual(len(self.state().get('docs_reminded',[])),2)

@unittest.skipUnless(shutil.which('git'),'needs git')
class ShellFallback(DocFixture):
    def setUp(self):
        super().setUp()
        subprocess.run(['git','init','-q',str(self.project)],check=True)
        for name in ('user.email','user.name'):subprocess.run(['git','-C',str(self.project),'config',name,'t'],check=True)
        subprocess.run(['git','-C',str(self.project),'add','-A'],check=True);subprocess.run(['git','-C',str(self.project),'commit','-qm','init'],check=True)
        self.old=self.project/'docs'/'adr'/'ADR-001-old.md';self.old.write_text('# ADR-001\n',encoding='utf-8')
        os.utime(self.old,(time.time()-3600,time.time()-3600))                        # changed before the turn: not this turn's
    def test_relative_shell_edits_are_found_through_git_status_and_mtime(self):
        self.prompt();time.sleep(0.05)
        (self.project/'docs'/'adr'/'ADR-005-ids.md').write_text('# ADR-005: Identifiers\n',encoding='utf-8')
        self.tool('Bash',{'command':'echo "# ADR-005" > docs/adr/ADR-005-ids.md'})
        state=self.state();self.assertTrue(state['shell']);self.assertNotIn('docs',state)   # no absolute path for the hook to see
        reason=self.stop()['continue_reason']
        self.assertIn('You changed docs/adr/ADR-005-ids.md',reason);self.assertNotIn('ADR-001-old',reason)
        self.assertEqual(self.reminders(),['doc'])
        self.prompt('later');self.tool('Bash',{'command':'touch docs/adr/ADR-005-ids.md'})
        self.assertEqual(self.stop(),{})                                              # once per path per session

    def test_a_doc_written_and_committed_through_the_shell_in_one_turn_is_found_by_head(self):
        self.prompt()
        before=subprocess.run(['git','-C',str(self.project),'rev-parse','HEAD'],capture_output=True,text=True).stdout.strip()
        self.assertEqual(list(self.state()['heads'].values()),[before])
        new=self.project/'docs'/'adr'/'ADR-009-queues.md';new.write_text('# ADR-009: Queues\n',encoding='utf-8')
        os.utime(new,(time.time()-3600,time.time()-3600))                             # older than the turn: mtime cannot find it
        self.tool('Bash',{'command':'git add docs && git commit -m "adr 9"'})
        subprocess.run(['git','-C',str(self.project),'add','docs/adr/ADR-009-queues.md'],check=True);subprocess.run(['git','-C',str(self.project),'commit','-qm','adr 9'],check=True)
        self.assertIn('You changed docs/adr/ADR-009-queues.md',self.stop()['continue_reason'])

    def test_a_commit_that_touches_no_decision_document_is_quiet(self):
        self.prompt();(self.project/'src.py').write_text('x=1\n',encoding='utf-8')
        self.tool('Bash',{'command':'git add src.py && git commit -m code'})
        subprocess.run(['git','-C',str(self.project),'add','src.py'],check=True);subprocess.run(['git','-C',str(self.project),'commit','-qm','code'],check=True)
        self.assertEqual(self.stop(),{})

    def test_a_shell_turn_that_touches_no_decision_document_is_quiet(self):
        self.prompt();time.sleep(0.05);(self.project/'src.py').write_text('x=1\n',encoding='utf-8')
        self.tool('Bash',{'command':'echo x=1 > src.py'});self.assertEqual(self.stop(),{})

    def test_git_failure_never_raises(self):
        from unittest.mock import patch
        self.prompt();self.tool('Bash',{'command':'touch docs/adr/x.md'})
        with patch('core.triggers.subprocess.run',side_effect=subprocess.TimeoutExpired('git',2)):self.assertEqual(self.stop(),{})


class SupersedeKeepsPointers(DocFixture):
    def capture(self,*extra):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=main(['--root',str(self.root),'--domain','demo','capture','--type','decision','--tag','subject/project','--tag','concern/decision',*extra])
        self.assertEqual(code,0,out.getvalue());return json.loads(out.getvalue())

    def test_the_successor_carries_links_and_files_of_the_note_it_replaces(self):
        old=self.capture('--title','Money uses cents','--summary','Cents.','--link','docs/adr/ADR-001.md','--link','docs/adr/ADR-002.md','--file','src/money.py')
        new=self.capture('--title','Money uses BigDecimal','--summary','BigDecimal.','--link','docs/adr/ADR-002.md','--link','docs/adr/ADR-003.md','--file','src/ledger.py','--supersedes','demo:'+old['id'])
        self.assertEqual(new['carried_links'],['docs/adr/ADR-001.md']);self.assertEqual(new['carried_files'],['src/money.py'])
        from core.notes import find_note
        from core.note_format import sections
        note=find_note(new['id'],'demo',self.root)[1]
        self.assertEqual(sections(note['body'])['links'].split('\n'),['- docs/adr/ADR-002.md','- docs/adr/ADR-003.md','- docs/adr/ADR-001.md'])   # the new note's own first, no duplicates
        self.assertEqual(note['files'],['src/ledger.py','src/money.py'])

    def test_drop_links_keeps_only_the_new_notes_own(self):
        old=self.capture('--title','Money uses cents','--summary','Cents.','--link','docs/adr/ADR-001.md','--file','src/money.py')
        new=self.capture('--title','Money uses BigDecimal','--summary','BigDecimal.','--supersedes','demo:'+old['id'],'--drop-links')
        self.assertEqual(new['carried_links'],[])
        from core.notes import find_note
        note=find_note(new['id'],'demo',self.root)[1]
        self.assertNotIn('ADR-001',note['body']);self.assertFalse(note.get('files'))

    def supersede(self,old,new,*extra):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=main(['--root',str(self.root),'--domain','demo','supersede','demo:'+old['id'],'--by','demo:'+new['id'],*extra])
        self.assertEqual(code,0,out.getvalue());return json.loads(out.getvalue())

    def test_the_standalone_supersede_carries_links_and_files_to_the_successor_too(self):
        old=self.capture('--title','Money uses cents','--summary','Cents.','--link','docs/adr/ADR-001.md','--link','docs/adr/ADR-002.md','--file','src/money.py')
        new=self.capture('--title','Money uses BigDecimal','--summary','BigDecimal.','--link','docs/adr/ADR-002.md','--link','docs/adr/ADR-003.md','--file','src/ledger.py')
        result=self.supersede(old,new)
        self.assertEqual(result['carried_links'],['docs/adr/ADR-001.md']);self.assertEqual(result['carried_files'],['src/money.py'])
        from core.notes import find_note
        from core.note_format import sections
        note=find_note(new['id'],'demo',self.root)[1]
        self.assertEqual(sections(note['body'])['links'].split('\n'),['- docs/adr/ADR-002.md','- docs/adr/ADR-003.md','- docs/adr/ADR-001.md'])
        self.assertEqual(note['files'],['src/ledger.py','src/money.py']);self.assertEqual(note['supersedes'],[old['id']])
        self.assertEqual(find_note(old['id'],'demo',self.root)[1]['status'],'superseded')

    def test_supersede_drop_links_keeps_only_the_successors_own(self):
        old=self.capture('--title','Money uses cents','--summary','Cents.','--link','docs/adr/ADR-001.md','--file','src/money.py')
        new=self.capture('--title','Money uses BigDecimal','--summary','BigDecimal.','--link','docs/adr/ADR-003.md')
        result=self.supersede(old,new,'--drop-links')
        self.assertEqual((result['carried_links'],result['carried_files']),([],[]))
        from core.notes import find_note
        note=find_note(new['id'],'demo',self.root)[1]
        self.assertNotIn('ADR-001',note['body']);self.assertFalse(note.get('files'));self.assertEqual(note['supersedes'],[old['id']])

    def test_a_supersede_with_nothing_to_carry_reports_empty_lists(self):
        old=self.capture('--title','Money uses cents','--summary','Cents.');new=self.capture('--title','Money uses BigDecimal','--summary','BigDecimal.')
        result=self.supersede(old,new);self.assertEqual((result['carried_links'],result['carried_files']),([],[]))

    def test_a_capture_without_supersedes_reports_no_carry(self):
        self.assertNotIn('carried_links',self.capture('--title','Plain','--summary','Plain note.'))

    def test_a_superseding_capture_retried_with_its_idempotency_key_is_existing_not_a_conflict(self):
        old=self.capture('--title','Money uses cents','--summary','Cents.','--link','docs/adr/ADR-001.md','--file','src/money.py')
        args=('--title','Money uses BigDecimal','--summary','BigDecimal.','--supersedes','demo:'+old['id'],'--idempotency-key','money-1')
        first=self.capture(*args);self.assertEqual(first['carried_links'],['docs/adr/ADR-001.md'])
        again=self.capture(*args);self.assertEqual((again['status'],again['id']),('existing',first['id']))

    def test_a_capture_superseding_by_title_retried_with_its_idempotency_key_is_existing(self):
        self.capture('--title','Money uses cents','--summary','Cents.')
        args=('--title','Money uses BigDecimal','--summary','BigDecimal.','--supersedes','Money uses cents','--idempotency-key','money-2')
        first=self.capture(*args)
        again=self.capture(*args);self.assertEqual((again['status'],again['id']),('existing',first['id']))

    def test_the_standalone_supersede_rebases_relative_links_to_the_successors_folder(self):
        from core.notes import find_note
        from core.note_format import sections
        old=self.capture('--title','Money uses cents','--summary','Cents.','--link','[Prior](../archive/prior.md)','--link','[[Other note]]','--link','https://example.com/a.md')
        new=self.capture('--title','Money uses BigDecimal','--summary','BigDecimal.')
        self.supersede(old,new);links=sections(find_note(new['id'],'demo',self.root)[1]['body'])['links']
        self.assertEqual(links.split('\n'),['- [Prior](../archive/prior.md)','- [[Other note]]','- https://example.com/a.md'])

    def test_a_capture_superseding_an_archived_note_rebases_its_relative_links_too(self):
        from core.notes import find_note
        from core.note_format import sections
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=main(['--root',str(self.root),'--domain','demo','capture','--type','plan','--tag','subject/project','--tag','concern/decision','--title','Money plan','--summary','Cents.','--link','[Spec](spec.md)','--link','[Notes](docs/notes.md)'])
        self.assertEqual(code,0,out.getvalue());old=json.loads(out.getvalue());out=io.StringIO()
        with contextlib.redirect_stdout(out):self.assertEqual(main(['--root',str(self.root),'--domain','demo','resolve','demo:'+old['id']]),0)
        self.assertEqual(find_note(old['id'],'demo',self.root)[1]['status'],'resolved');self.assertIn('../notes/spec.md',find_note(old['id'],'demo',self.root)[1]['body'])   # relative to archive/ now
        new=self.capture('--title','Money uses BigDecimal','--summary','BigDecimal.','--supersedes','demo:'+old['id'])
        links=sections(find_note(new['id'],'demo',self.root)[1]['body'])['links'].split('\n')
        self.assertEqual(links,['- [Spec](spec.md)','- [Notes](docs/notes.md)'])


class Statements(DocFixture):
    """Decision and directive statements hold the turn open, and a statement is raised once, not in every brief."""
    MEMO='> Directive CFO-2025-03: effective immediately, all monetary amounts must be BigDecimal with scale 4, replacing the earlier long-cents guidance.'
    HEADER=('Never push and never touch other branches. Regulators require a written record, and the compliance office has approved a change in writing.\n'
            'Implement the endpoint; the API must stay idempotent.')

    def test_a_blockquoted_memo_is_a_directive(self):
        self.assertEqual(runtime.durable_signal(self.MEMO),'directive')
        self.assertEqual(runtime.durable_signal('Here is the memo:\n'+self.MEMO+'\nPlease apply it.'),'directive')
        self.assertEqual(runtime.durable_signal('<pasted_content id="1">Policy: all deploys need two approvals.</pasted_content>\nsummarize'),'directive')

    def test_quoted_text_never_yields_the_user_kinds_and_weak_hits_do_not_count(self):
        for prompt in ['> remember that we use tabs\nok','> from now on use tabs\nwhat did they mean?','> No, the cache lives in AppData\nthoughts?',
                       '> Pushing is not allowed.\nok','> The API must be idempotent.\nok','<pasted_content>From now on never deploy on Fridays</pasted_content>\nwhat is this?',
                       '```\nDirective: effective immediately, policy decided\n```\nwhat does this log mean?','<system-reminder>effective immediately, decision: x</system-reminder>']:
            with self.subTest(prompt=prompt):self.assertIsNone(runtime.durable_signal(prompt))
        self.assertEqual(runtime.durable_signal('> All amounts must be BigDecimal, replacing long cents.'),'directive')   # two weaker hits

    def test_a_task_description_is_not_a_decision(self):
        for prompt in ['Implement the endpoint; the API must stay idempotent.','The regulators approved the plan, so the module must be reviewed.','Add a field; it must be optional.']:
            with self.subTest(prompt=prompt):self.assertIsNone(runtime.durable_signal(prompt))

    def test_an_instruction_to_the_agent_is_not_a_team_decision(self):
        for prompt in ['You must not modify any files; answer from memory.','You must never touch the tests.','You are not allowed to push.',
                       'You are allowed to edit docs only.','You must always answer in English.']:
            with self.subTest(prompt=prompt):self.assertNotEqual(runtime.durable_signal(prompt),'decision')
        self.assertIsNone(runtime.durable_signal('You must not modify any files; answer from memory.'))
        for prompt in ['From now on the team must never deploy on Fridays.','We are not allowed to push to main.','The team must always review ADRs.']:
            with self.subTest(prompt=prompt):self.assertIn(runtime.durable_signal(prompt),{'decision','preference'})
        self.assertEqual(runtime.durable_signal('The team must stay on Python 3.10.'),'decision')

    def test_the_decision_wording_the_team_uses(self):
        for prompt in ['We decided to store money as BigDecimal.','We have agreed on UUIDs for all ids.','The decision is to drop the legacy API.','Decision: UUIDs everywhere.',
                       'Effective today, deploys need two approvals.','Pushing to main is not allowed.','We must never push to main.',"Let's go with option B."]:
            with self.subTest(prompt=prompt):self.assertEqual(runtime.durable_signal(prompt),'decision')
        self.assertEqual(runtime.durable_signal('We must stay on Python 3.10.'),'decision')

    def test_a_blocking_kind_wins_over_a_non_blocking_one_in_the_same_prompt(self):
        for prompt,kind in [('I prefer short runbooks. We decided to standardize on the SDK.','decision'),('No, we decided to use X.','decision'),
                            ('From now on use tabs. Remember that CI runs on Linux.','remember'),('No, the cache lives in AppData. Always use tabs.','correction'),
                            ('I prefer short runbooks.','preference'),('I prefer short runbooks.\n> Policy: all deploys need two approvals.','directive'),
                            ('No, the cache lives in AppData.\n> Policy: all deploys need two approvals.','directive')]:
            with self.subTest(prompt=prompt):self.assertEqual(runtime.durable_signal(prompt),kind)
        self.assertEqual(runtime.signal_of('I prefer short runbooks. We decided to standardize on the SDK.'),('decision','We decided to standardize on the SDK.'))

    def test_the_nudge_names_every_kind_and_suggests_the_primary_one(self):
        context=self.prompt('I prefer short runbooks. We decided to standardize on the SDK.')['context']
        self.assertIn('the user made a decision and stated a preference in this prompt',context);self.assertIn('--type decision --concern decision',context)
        self.assertEqual(len([line for line in context.splitlines() if 'Living Memory: the user' in line]),1)
        self.assertEqual(runtime.signal_nudge('preference','s',['preference']).count('stated a preference'),1)

    def test_a_decision_statement_blocks_and_names_what_the_user_did(self):
        self.prompt('We decided to store money as BigDecimal.')
        reason=self.stop()['continue_reason']
        self.assertIn('The user made a decision',reason);self.assertIn('--type decision',reason);self.assertIn('capture-finalize',reason)
        self.assertEqual(self.reminders(),['decision'])

    def test_a_directive_memo_blocks_and_asks_for_the_source_link(self):
        self.prompt(self.MEMO+'\nApply it to the ledger.')
        reason=self.stop()['continue_reason']
        self.assertIn('shared a directive',reason);self.assertIn('--link SOURCE',reason);self.assertEqual(self.reminders(),['directive'])

    def test_a_standing_header_raises_once_then_is_deduped(self):
        first=self.prompt(self.HEADER)['context']
        self.assertIn('stated a preference',first)                                    # "Never push ..." is a preference the first time
        self.assertEqual(self.stop(),{})                                              # preferences never block; the turn settles the statement
        self.payload['session_id']='s2'
        self.assertNotIn('Living Memory: the user',self.prompt(self.HEADER)['context'])
        self.assertIsNone(self.state_for('s2').get('signal'))
        seen=json.loads((self.root/'domains'/'demo'/'.signals-seen.json').read_text(encoding='utf-8'))
        self.assertEqual(len(seen['seen']),1);self.assertNotIn('push',json.dumps(seen))   # salted hashes only, never text

    def state_for(self,session):return json.loads(runtime._pointer(self.root,session).read_text(encoding='utf-8'))

    def test_a_deduped_header_does_not_hide_a_real_decision_in_the_same_brief(self):
        self.prompt(self.HEADER);self.stop()
        self.payload['session_id']='s2'
        context=self.prompt('Never push and never touch other branches.\nWe decided to use BigDecimal for money.')['context']
        self.assertIn('made a decision',context)

    def test_a_blocking_statement_is_settled_by_its_reminder_and_not_raised_again(self):
        self.prompt('We decided to store money as BigDecimal.');self.assertIn('continue_reason',self.stop())
        self.prompt('We decided to store money as BigDecimal.')
        self.assertEqual(self.stop(),{})
        self.assertNotIn('signal',self.state())

    def test_a_captured_statement_is_settled_too(self):
        self.prompt('We decided to store money as BigDecimal.')
        runtime_cli=['--root',str(self.root),'--domain','demo','capture-finalize','--session','s1']
        with contextlib.redirect_stdout(io.StringIO()):main(runtime_cli)
        self.assertEqual(self.stop(),{})
        self.prompt('We decided to store money as BigDecimal.');self.assertEqual(self.stop(),{})

    def test_the_seen_list_is_capped_oldest_first(self):
        from core.runtime import signals_seen,settle_signal,MAX_SIGNALS_SEEN
        raised,hashed=signals_seen(self.root,'demo');hashes=[hashed(f'statement number {i}') for i in range(MAX_SIGNALS_SEEN+5)]
        for h in hashes:settle_signal(self.root,'demo',h)
        raised,_=signals_seen(self.root,'demo')
        self.assertFalse(raised('statement number 0'));self.assertTrue(raised(f'statement number {MAX_SIGNALS_SEEN+4}'))
        self.assertEqual(len(json.loads((self.root/'domains'/'demo'/'.signals-seen.json').read_text(encoding='utf-8'))['seen']),MAX_SIGNALS_SEEN)

    def test_the_sidecar_is_not_canonical_and_stays_out_of_validate_and_backups(self):
        from core.validate import validate_domain
        self.prompt(self.HEADER);self.stop()
        self.assertTrue((self.root/'domains'/'demo'/'.signals-seen.json').is_file())
        self.assertEqual(validate_domain('demo',self.root),[])
        from core.backup import MEMBER
        self.assertIsNone(MEMBER.fullmatch('domains/demo/.signals-seen.json'))


if __name__=='__main__':unittest.main()
