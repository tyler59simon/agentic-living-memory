"""Project briefs: the `brief` note type (one per project folder, keyed like a map), its injection and SessionStart
pointer, and the two Stop reminders that keep it current (core/briefs.py, runtime.brief_pending)."""
import contextlib,io,json,unittest
from test_core import StoreFixture
from core import runtime,usage,session_start
from core.cli import main
from core.common import load_bindings
from core.maps import find_map
from core.notes import find_note,capture
from core.note_types import review_due
from core.briefs import find_brief,find_user_brief,project_dir,project_key,USER_BRIEF_BUDGET

class BriefFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','concern/architecture','concern/decision','concern/knowledge']}))
        self.project=self.base/'project';(self.project/'docs'/'adr').mkdir(parents=True);(self.project/'app'/'.git').mkdir(parents=True)
        self.doc=self.project/'docs'/'adr'/'ADR-004-money.md';self.doc.write_text('# ADR-004: Money\n\n## Decision\nBigDecimal.\n',encoding='utf-8')
        self.cwd=str(self.project);self.payload={'cwd':self.cwd,'session_id':'s1','prompt':'work on the ledger'}
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def capture_brief(self,folder='.',title='project project brief',*extra):
        return self.cli('--domain','demo','capture','--type','brief','--file',folder,'--title',title,'--summary','A demo ledger rebuild, phase 2 of 3.',
                        '--detail','**Goals and scope:** Replace the legacy ledger.','--detail','**Key decisions:** Money is BigDecimal (docs/adr/ADR-004-money.md).',*extra)
    def hook(self,event,**extra):return runtime.process('claude',event,{**self.payload,**extra},self.root)
    def prompt(self,text='work on the ledger',**extra):return self.hook('user-prompt',prompt=text,**extra)
    def tool(self,name,args,**extra):return self.hook('pre-tool-use',tool_name=name,tool_input=args,**extra)
    def stop(self,**extra):return self.hook('stop',**extra)
    def state(self):return json.loads(runtime._pointer(self.root,'s1').read_text(encoding='utf-8'))
    def edit_doc(self):self.tool('Write',{'file_path':str(self.doc)})
    def edit_code(self):self.tool('Edit',{'file_path':str(self.project/'app'/'main.py')})
    def reminders(self):return [r['k'] for r in usage.events(self.root) if r['e']=='reminder' and not r['k'].startswith('nudge-')]
    def fill_notes(self,count=3):
        for i,title in enumerate(['Cache rebuild','Retry budget','Queue depth','Index rotation'][:count]):self.note(title,f'Fact number {i} about the service.',allow_similar=True)

class BriefType(BriefFixture):
    def test_capture_defaults_key_review_and_conflict(self):
        code,result=self.capture_brief()
        self.assertEqual(code,0,result)
        note=find_note(result['id'],'demo',self.root)[1]
        self.assertEqual((note['type'],note['prefix'],note['files']),('brief','BRIEF',['.']))
        self.assertIn('subject/project',note['tags']);self.assertIn('concern/knowledge',note['tags'])
        self.assertTrue(note['review_by']);self.assertFalse(review_due(note))
        code,again=self.capture_brief('.','Other brief title','--allow-similar')
        self.assertNotEqual(code,0);self.assertIn('brief_exists conflict',json.dumps(again));self.assertIn('project brief for',json.dumps(again))
        code,replaced=self.capture_brief('.','Newer brief','--supersedes','demo:'+result['id'])
        self.assertEqual(code,0,replaced)
        self.assertEqual(find_brief(self.cwd,self.root,load_bindings(self.root))['id'],replaced['id'])
        code,missing=self.cli('--domain','demo','capture','--type','brief','--title','Loose brief','--summary','No folder named.')
        self.assertNotEqual(code,0);self.assertIn('brief notes need --file DIR',json.dumps(missing))

    def test_another_folder_gets_its_own_brief_and_maps_are_unaffected(self):
        self.assertEqual(self.capture_brief()[0],0)
        code,second=self.capture_brief('app','app project brief')
        self.assertEqual(code,0,second)
        code,mapped=self.cli('--domain','demo','capture','--type','map','--file','app','--title','app codebase map','--summary','The app.','--detail','**Layout:** one folder.')
        self.assertEqual(code,0,mapped)                                               # a map and a brief may share a folder
        bindings=load_bindings(self.root);app=(self.project/'app').resolve()
        self.assertEqual(find_map(app,self.root,bindings)['id'],mapped['id'])
        self.assertEqual(find_brief(app,self.root,bindings)['id'],second['id'])       # the deepest folder wins
        self.assertEqual(find_brief(self.project/'docs',self.root,bindings)['title'],'project project brief')
        code,again=self.cli('--domain','demo','capture','--type','map','--file','app','--title','app map again','--summary','x','--detail','**Layout:** y','--allow-similar')
        self.assertIn('map_exists conflict',json.dumps(again))                        # the map message is unchanged
        # app is a git repo inside the bound folder: like a map, its brief is keyed to the repo, not the parent binding.
        self.assertEqual(project_dir(app,bindings),app);self.assertEqual(project_dir(self.project/'docs',bindings),self.project.resolve())
        self.assertEqual(project_key(self.project.resolve(),bindings),'.')
        self.assertEqual(project_key(app,bindings),app.as_posix())

    def test_parent_binding_with_several_repos_keys_one_brief_per_repo(self):
        # A domain bound to a folder of several repos (one sandbox for many projects) asks for each repo's own brief.
        other=self.project/'tools';(other/'.git').mkdir(parents=True);bindings=load_bindings(self.root)
        self.assertEqual(project_dir(other/'src',bindings),other.resolve())
        self.assertEqual(project_dir(self.project/'app'/'src',bindings),(self.project/'app').resolve())
        self.assertNotEqual(project_dir(other,bindings),project_dir(self.project/'app',bindings))

    def test_retyping_a_note_into_a_second_brief_is_refused(self):
        self.assertEqual(self.capture_brief()[0],0)
        other=self.cli('--domain','demo','capture','--type','knowledge','--file','.','--title','Ledger overview','--summary','Overview.')[1]
        code,error=self.cli('--domain','demo','update','demo:'+other['id'],'--type','brief')
        self.assertNotEqual(code,0);self.assertIn('brief_exists conflict',json.dumps(error))

class BriefInjection(BriefFixture):
    def test_the_first_prompt_injects_the_brief_once_per_revision_and_a_new_session_again(self):
        code,result=self.capture_brief();ref='demo:'+result['id']
        self.hook('session-start')
        text=self.prompt()['context']
        self.assertIn('Project brief — ',text);self.assertIn('Replace the legacy ledger',text);self.assertIn(result['id'][:8],text)
        self.assertLess(len(text),2500+300)
        self.assertEqual(self.state()['shown'][ref],find_brief(self.cwd,self.root,load_bindings(self.root))['revision'][:12])
        self.assertEqual([r['k'] for r in usage.events(self.root) if r['e']=='inject' and r['ref']==ref],['prompt-brief'])
        self.assertNotIn('Project brief — ',self.prompt('again about the ledger')['context'])
        code,_=self.cli('--domain','demo','update',ref,'--set-detail','**Current phase, status and blockers:** Phase 3, blocked on sign-off.')
        self.assertEqual(code,0)
        self.assertIn('blocked on sign-off',self.prompt('and again')['context'])
        self.payload['session_id']='s2'
        self.hook('session-start');self.assertIn('Project brief — ',self.prompt()['context'])

    def test_it_comes_before_the_map_and_stays_out_of_the_hit_list(self):
        self.capture_brief()
        self.cli('--domain','demo','capture','--type','map','--file','app','--title','app codebase map','--summary','The app.','--detail','**Layout:** one folder.')
        self.hook('session-start')
        text=self.prompt('ledger work in app',cwd=str(self.project/'app'))['context']
        self.assertEqual(text.count('Untrusted historical memory'),1)
        self.assertLess(text.index('Project brief — '),text.index('Codebase map for app'))

    def test_the_body_is_bounded(self):
        self.cli('--domain','demo','capture','--type','brief','--file','.','--title','big brief','--summary','Big.','--detail','**Glossary:** '+'word '*1200)
        self.hook('session-start')
        self.assertLess(len(self.prompt()['context']),2500+200)

    def test_no_brief_no_injection_and_other_projects_are_not_mixed_in(self):
        self.hook('session-start');self.assertNotIn('Project brief',self.prompt()['context'])
        self.capture_brief('app','app project brief')                                  # a brief for another folder
        self.hook('session-start');self.assertNotIn('Project brief',self.prompt()['context'])

class BriefSessionStart(BriefFixture):
    def test_a_pointer_line_names_the_brief_and_its_date(self):
        code,result=self.capture_brief()
        context=self.hook('session-start')['context']
        self.assertRegex(context,r'Project brief: demo:%s project project brief \(updated \d{4}-\d\d-\d\d\)'%result['id'][:8])

    def test_the_pointer_is_dropped_only_after_the_index_and_open_work_lines(self):
        self.fill_notes(4);code,result=self.capture_brief()
        trace={};full,_,_=session_start.build_context(self.cwd,self.root,session_start.BUDGET,trace=trace)
        self.assertIn('Project brief: ',full);self.assertTrue(trace['degrade']['brief']);listed=trace['degrade']['listed']
        sizes=[]
        for budget in range(len(full),150,-20):
            trace={};text,_,_=session_start.build_context(self.cwd,self.root,budget,trace=trace)
            sizes.append((trace['degrade']['listed'],trace['degrade']['opened'],trace['degrade']['brief']))
        self.assertTrue(any(l==0 and brief for l,o,brief in sizes))                  # index lines went first, the pointer stayed
        self.assertTrue(any(not brief for l,o,brief in sizes))                       # then it went too
        for l,o,brief in sizes:
            if not brief:self.assertEqual(l,0)                                       # never dropped while an index line remained
        self.assertGreater(listed,0)

    def test_the_pointer_fits_the_budget_with_a_full_index(self):
        self.fill_notes(4);self.capture_brief()
        self.assertLessEqual(len(self.hook('session-start')['context']),session_start.HARD_CAP)
        text,_,_=session_start.build_context(self.cwd,self.root,session_start.BUDGET)
        self.assertLessEqual(len(text),session_start.BUDGET)

class BriefReminders(BriefFixture):
    def test_unbriefed_needs_three_notes_a_doc_or_a_blocking_signal(self):
        self.fill_notes(2)
        self.prompt();self.edit_code();self.assertEqual(self.stop(),{})                # two notes, files only: quiet
        self.prompt('new turn');self.assertEqual(self.stop(),{})                       # nothing happened
        self.fill_notes(3)
        self.prompt('third note exists');self.assertEqual(self.stop(),{})              # three notes but no change this turn: quiet
        self.prompt('edit');self.edit_code()
        reason=self.stop()['continue_reason']
        self.assertIn('project has no project brief',reason)
        self.assertIn("CMD capture --session s1 --type brief --file '.'",reason)
        self.assertIn("--title 'project project brief'",reason);self.assertIn('**Goals and scope:**',reason);self.assertLessEqual(len(reason),700)
        self.assertEqual(self.reminders(),['unbriefed']);self.assertEqual(self.state()['briefed'],['unbriefed'])
        self.prompt('more');self.edit_code();self.assertEqual(self.stop(),{})          # once per session
        self.payload['session_id']='s2'
        self.prompt('other session');self.edit_code();self.assertIn('has no project brief',self.stop()['continue_reason'])

    def test_a_directive_or_decision_without_a_brief_asks_for_one_in_the_same_reminder(self):
        self.prompt('We decided to store money as BigDecimal.');self.edit_code()
        reason=self.stop()['continue_reason']
        self.assertIn('The user made a decision',reason);self.assertIn('Also: project has no project brief',reason)
        self.assertIn('--type brief',reason);self.assertLessEqual(len(reason),700);self.assertEqual(self.reminders(),['decision'])
        self.assertEqual(self.state()['briefed'],['unbriefed'])
        self.assertIn('Write what you know now and mark unknowns as unknown',reason)

    def test_a_read_only_session_is_not_asked_for_a_brief(self):
        self.prompt('We decided to store money as BigDecimal.')
        reason=self.stop()['continue_reason']
        self.assertIn('The user made a decision',reason);self.assertNotIn('project brief',reason)   # the statement still holds the turn
        self.assertEqual(self.state().get('briefed',[]),[])
        self.fill_notes(3);self.prompt('now change the code');self.edit_code()   # a later turn that edits: the brief is asked for then
        self.assertIn('has no project brief',self.stop()['continue_reason'])

    def test_a_decision_document_with_no_brief_joins_the_doc_reminder(self):
        self.prompt();self.edit_doc()
        reason=self.stop()['continue_reason']
        self.assertIn('You changed docs/adr/ADR-004-money.md',reason);self.assertIn('Also: ',reason);self.assertIn('--type brief',reason)
        self.assertLessEqual(len(reason),700);self.assertEqual(reason.count('Living Memory:'),1);self.assertEqual(reason.count('Nothing durable'),1)
        self.assertEqual(self.reminders(),['doc'])

    def test_a_brief_makes_the_unbriefed_reminder_go_away(self):
        self.capture_brief()
        self.prompt();self.edit_code();self.assertEqual(self.stop(),{})
        self.fill_notes(3);self.prompt('again');self.edit_code();self.assertEqual(self.stop(),{})

    def test_a_decision_document_edit_asks_for_a_brief_update_next_to_the_doc(self):
        code,result=self.capture_brief();short='demo:'+result['id'][:8]
        self.prompt();self.edit_doc()
        reason=self.stop()['continue_reason']
        self.assertIn('You changed docs/adr/ADR-004-money.md',reason);self.assertIn('Also: a decision was made or recorded',reason)
        self.assertIn(f"CMD update {short} --session s1 --set-detail '**Key decisions:** ...'",reason)
        self.assertLessEqual(len(reason),700);self.assertEqual(self.reminders(),['doc'])
        self.assertEqual(self.state()['briefed'],['brief'])
        self.assertLess(reason.index('You changed'),reason.index('project brief'))

    def test_a_decision_capture_asks_even_though_it_has_a_receipt_and_an_updated_brief_does_not(self):
        code,result=self.capture_brief();ref='demo:'+result['id']
        self.prompt('record the money decision')
        code,_=self.cli('--domain','demo','capture','--session','s1','--type','decision','--tag','subject/project','--tag','concern/decision',
                        '--title','Money is BigDecimal','--summary','Amounts are BigDecimal.','--link','docs/adr/ADR-004-money.md')
        self.assertEqual(code,0)
        reason=self.stop()['continue_reason']
        self.assertTrue(reason.startswith('Living Memory: a decision was made or recorded'),reason);self.assertEqual(self.reminders(),['brief'])
        self.prompt('another decision')
        self.cli('--domain','demo','capture','--session','s1','--type','decision','--tag','subject/project','--tag','concern/decision','--title','Ids are UUIDs',
                 '--summary','Identifiers are UUIDs.','--link','docs/adr/ADR-005-ids.md')
        self.assertEqual(self.stop(),{})                                               # once per session
        self.payload['session_id']='s2'
        self.prompt('third decision')
        self.cli('--domain','demo','capture','--session','s2','--type','decision','--tag','subject/project','--tag','concern/decision','--title','Rounding is half-even',
                 '--summary','Rounding is half-even.','--link','docs/adr/ADR-006-round.md')
        self.cli('--domain','demo','update',ref,'--session','s2','--set-detail','**Key decisions:** Money is BigDecimal; rounding half-even.')
        self.assertEqual(self.stop(),{})                                               # the brief was updated this turn

    def test_a_directive_asks_for_a_brief_update_and_a_plain_edit_does_not(self):
        self.capture_brief()
        self.prompt();self.edit_code();self.assertEqual(self.stop(),{})
        self.prompt('> Directive CFO-2025-03: effective immediately, all monetary amounts must be BigDecimal with scale 4, replacing the earlier long-cents guidance.\nApply it.')
        reason=self.stop()['continue_reason']
        self.assertIn('shared a directive',reason);self.assertIn('CMD update demo:',reason);self.assertIn('Key decisions',reason);self.assertLessEqual(len(reason),700)
        self.assertEqual(self.reminders(),['directive'])

    def test_planning_mode_and_subagents_never_arm_a_brief_reminder(self):
        self.fill_notes(3)
        self.prompt();self.tool('Edit',{'file_path':str(self.project/'app'/'main.py')},permission_mode='plan')
        self.assertEqual(self.stop(permission_mode='plan'),{})
        self.assertNotIn('briefed',self.state())

class UserBrief(BriefFixture):
    """The user brief: a `brief` with no files in a global domain, one per store, shown at SessionStart and named in preference hints."""
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','subject/user','concern/architecture','concern/decision','concern/knowledge','concern/preference']}))
    def capture_user(self,title='User brief',*extra):
        return self.cli('--domain','demo','capture','--global','--type','brief','--title',title,'--summary','A terse developer on Windows who reviews diffs closely.',
                        '--detail','**Working style:** Short answers first.','--detail','**Tools and machine:** Windows 11, PowerShell, Python 3.15.',*extra)
    def preference(self,title,*extra):
        return self.cli('--domain','demo','capture','--global','--type','preference','--title',title,'--summary',f'{title}, always.',*extra)

    def test_capture_without_files_is_accepted_in_global_and_one_per_store(self):
        code,made=self.capture_user();self.assertEqual(code,0,made)
        note=find_note(made['id'],'_global',self.root)[1]
        self.assertEqual((note['type'],note.get('files')),('brief',None));self.assertIn('subject/user',note['tags'])
        self.assertEqual(find_user_brief(self.root)['id'],made['id'])
        code,again=self.capture_user('Other user brief','--allow-similar')
        self.assertNotEqual(code,0);self.assertIn('brief_exists conflict',json.dumps(again));self.assertIn('is the user brief',json.dumps(again))
        code,replaced=self.capture_user('Newer user brief','--supersedes','_global:'+made['id'])
        self.assertEqual(code,0,replaced);self.assertEqual(find_user_brief(self.root)['id'],replaced['id'])
        other=self.cli('--domain','demo','capture','--global','--type','knowledge','--title','Machine notes','--summary','Notes.')[1]
        code,error=self.cli('--domain','demo','update','_global:'+other['id'],'--type','brief')
        self.assertNotEqual(code,0);self.assertIn('brief_exists conflict',json.dumps(error))      # retyping a note into a second user brief is refused too

    def test_a_project_brief_without_files_is_still_refused_and_the_user_brief_is_not_a_project_brief(self):
        code,error=self.cli('--domain','demo','capture','--type','brief','--title','Loose brief','--summary','No folder named.')
        self.assertNotEqual(code,0);self.assertIn('brief notes need --file DIR',json.dumps(error))
        self.capture_user();self.assertIsNone(find_brief(self.cwd,self.root,load_bindings(self.root)))
        self.hook('session-start');self.assertNotIn('Project brief',self.prompt()['context'])
        self.assertEqual(self.capture_brief()[0],0)                                                   # a project brief and the user brief coexist
        self.assertEqual(find_brief(self.cwd,self.root,load_bindings(self.root))['title'],'project project brief')
        self.assertEqual(find_user_brief(self.root)['title'],'User brief')

    def test_session_start_shows_it_within_its_budget_and_marks_it_shown(self):
        self.fill_notes(4)
        code,made=self.cli('--domain','demo','capture','--global','--type','brief','--title','User brief','--summary','A terse developer.',
                           '--detail','**Working style:** '+'Short answers first. '*60,'--detail','**Current focus:** The user brief.')
        trace={};text,shown,_=session_start.build_context(self.cwd,self.root,trace=trace)
        block=text[text.index('User brief _global:'):].split('\n- [')[0].split('\nOther notes')[0]
        self.assertLessEqual(len(block),USER_BRIEF_BUDGET);self.assertIn('Short answers first.',block)
        self.assertEqual(trace['degrade']['user_brief'],2);self.assertIn('_global:'+made['id'],shown)
        self.assertEqual(text.count(made['id'][:8]),1)                                                # one block, not also an index line
        self.assertLessEqual(len(text),session_start.BUDGET)

    def test_it_degrades_to_its_summary_then_a_pointer_before_guidance_snippets_go(self):
        self.fill_notes(4)
        self.cli('--domain','demo','capture','--type','decision','--title','Money is BigDecimal','--summary','Store money as BigDecimal.')
        code,made=self.capture_user();seen={}
        for budget in range(session_start.BUDGET,150,-10):
            trace={};text,shown,_=session_start.build_context(self.cwd,self.root,budget,trace=trace)
            seen.setdefault(trace['degrade']['user_brief'],(trace['degrade']['titles_only'],text,shown))
        self.assertEqual(set(seen),{2,1,0})
        self.assertIn('Short answers first.',seen[2][1])
        self.assertNotIn('Short answers first.',seen[1][1]);self.assertIn('A terse developer on Windows',seen[1][1])
        self.assertNotIn('A terse developer on Windows',seen[0][1]);self.assertIn('User brief: _global:'+made['id'][:8],seen[0][1])
        for level in (1,0):self.assertFalse(seen[level][0])                                           # guidance snippets were still whole when the brief shrank
        self.assertIn('_global:'+made['id'],seen[2][2]);self.assertNotIn('_global:'+made['id'],seen[1][2])   # only the whole block counts as shown

    def test_the_hint_names_the_brief_or_offers_to_create_it(self):
        code,first=self.preference('Prefer tabs');self.assertEqual(code,0)
        self.assertNotIn('user_brief',first)                                                          # one preference is not enough to ask for a brief
        code,second=self.preference('Short answers first')
        self.assertIn('no user brief yet (2 global preferences)',second['user_brief'])
        self.assertIn("capture --global --type brief --title 'User brief'",second['user_brief'])
        for label in ('Working style','Preferences','Tools and machine','Current focus'):self.assertIn(f'**{label}:**',second['user_brief'])
        plain=self.cli('--domain','demo','capture','--type','knowledge','--title','Plain fact','--summary','Nothing about the user.')[1]
        self.assertNotIn('user_brief',plain)
        code,made=self.capture_user();self.assertNotIn('user_brief',made)                             # the brief itself is not hinted
        ref='_global:'+made['id'][:8]
        code,third=self.preference('Run tests first')
        self.assertIn(f'user brief {ref}: update it if this changes how you work with the user',third['user_brief'])
        self.assertIn(f"update {ref} --set-detail '**Label:** ...'",third['user_brief'])
        code,changed=self.cli('--domain','demo','update','_global:'+third['id'],'--body','Run the whole test suite first.')
        self.assertIn(ref,changed['user_brief'])
        code,edited=self.cli('--domain','demo','update',ref,'--set-detail','**Current focus:** Writing the user brief.')
        self.assertEqual(code,0);self.assertNotIn('user_brief',edited)
        tagged=self.cli('--domain','demo','capture','--type','context','--tag','subject/user','--title','Works from a laptop','--summary','The user works from a laptop.')[1]
        self.assertIn(ref,tagged.get('user_brief',''))                                                # a project note tagged subject/user counts too

if __name__=='__main__':unittest.main()
