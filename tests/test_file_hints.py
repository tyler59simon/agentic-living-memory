import json,os,unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime

class FileHints(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project=self.base/'project';(self.project/'core').mkdir(parents=True)
        self.scratch=self.base/'scratch';self.scratch.mkdir()
        self.listed=self.note('Notes parser gotcha','Frontmatter keys keep their order.',files=['core/notes.py'])
        self.named=self.note('Ref resolution','Short refs are resolved in core/cli.py by _reference.')
    def payload(self,session='s1',**extra):
        return {'cwd':str(self.project),'session_id':session,'prompt':'go','scratchpad_dir':str(self.scratch),**extra}
    def edit(self,path,harness='claude',session='s1',**extra):
        p=self.payload(session,**extra)
        runtime.process(harness,'user-prompt',p,self.root)
        return runtime.process(harness,'pre-tool-use',{**p,'tool_name':'Edit','tool_input':{'file_path':path}},self.root)

    def test_hint_for_listed_file_fires_once_per_session(self):
        first=self.edit('core/notes.py')
        self.assertIn('Living Memory notes about core/notes.py',first['context']);self.assertIn('Notes parser gotcha',first['context'])
        self.assertIn('demo:'+self.listed['id'][:8],first['context'])
        self.assertNotIn('context',self.edit('core/notes.py'))
        self.assertIn('context',self.edit('core/notes.py',session='s2'))
    def test_hint_for_file_named_in_body(self):
        self.assertIn('Ref resolution',self.edit(str(self.project/'core'/'cli.py'))['context'])
    def test_no_hint_without_notes_or_on_reads(self):
        self.assertNotIn('context',self.edit('core/other.py'))
        p=self.payload()
        result=runtime.process('claude','pre-tool-use',{**p,'tool_name':'Read','tool_input':{'file_path':'core/notes.py'}},self.root)
        self.assertNotIn('context',result)
    def test_skipped_for_subagents_scratchpad_and_unsupported_hosts(self):
        self.assertNotIn('context',self.edit('core/notes.py',agent_id='sub'))
        self.assertNotIn('context',self.edit(str(self.scratch/'notes.py')))
        self.assertNotIn('context',self.edit('core/notes.py',harness='copilot',session='s3'))
        self.assertIn('context',self.edit('core/notes.py',harness='codex',session='s4'))
    def test_windows_separators_match(self):
        self.assertIn('context',self.edit(str(self.project)+'\\core\\notes.py' if os.name=='nt' else str(self.project/'core'/'notes.py')))
    def test_missing_index_fails_open(self):
        (self.root/'domains/demo/search.sqlite3').unlink()
        result=self.edit('core/notes.py')
        self.assertNotIn('context',result);self.assertNotIn('deny',result)
    def test_claude_adapter_merges_context_with_a_decision(self):
        from hooks import claude_hooks
        with patch('core.runtime.process',return_value={'context':'hint','allow':'ok'}):
            out=claude_hooks.handle('pre-tool-use',{})
        self.assertEqual(out['hookSpecificOutput'],{'hookEventName':'PreToolUse','additionalContext':'hint','permissionDecision':'allow','permissionDecisionReason':'ok'})

class FileFollowThrough(StoreFixture):
    """A note named before an edit is asked about at Stop: still true, changed, or (for a bug) fixed."""
    payload=FileHints.payload;edit=FileHints.edit
    def setUp(self):
        super().setUp()
        self.project=self.base/'project';(self.project/'core').mkdir(parents=True)
        self.scratch=self.base/'scratch';self.scratch.mkdir()
        self.listed=self.note('Notes parser gotcha','Frontmatter keys keep their order.',files=['core/notes.py'])
        self.named=self.note('Ref resolution','Short refs are resolved in core/cli.py by _reference.')
    def stop(self,session='s1',**extra):return runtime.process('claude','stop',self.payload(session,**extra),self.root)
    def cli(self,*argv):
        import contextlib,io
        from core.cli import main
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=main(['--root',str(self.root),'--domain','demo',*argv])
        return code,json.loads(out.getvalue())
    def state(self,session='s1'):return json.loads(runtime._pointer(self.root,session).read_text(encoding='utf-8'))
    def short(self,note):return 'demo:'+note['id'][:8]

    def test_hinted_then_edited_blocks_once_naming_the_note(self):
        self.edit('core/notes.py')
        self.assertEqual(self.state()['hinted'],{'core/notes.py':['demo:'+self.listed['id']]})
        reason=self.stop()['continue_reason']
        self.assertIn('You edited core/notes.py',reason);self.assertIn(self.short(self.listed)+' Notes parser gotcha',reason)
        self.assertIn('CMD confirm '+self.short(self.listed)+' --session s1',reason);self.assertIn('CMD update '+self.short(self.listed)+' --session s1 --set-detail',reason)
        self.assertNotIn('CMD resolve',reason);self.assertIn('Nothing durable: CMD capture-finalize --session s1',reason)
        self.assertLessEqual(len(reason),700)
        from core import usage
        self.assertEqual([r['k'] for r in usage.events(self.root) if r['e']=='reminder'],['follow'])
        self.assertEqual(self.state()['followed'],['demo:'+self.listed['id']])

    def test_a_second_edit_in_the_same_session_does_not_block_again(self):
        self.edit('core/notes.py');self.assertIn('continue_reason',self.stop())
        self.edit('core/notes.py');self.assertEqual(self.stop(),{})
        self.edit('core/notes.py',session='s9');self.assertIn('continue_reason',self.stop('s9'))   # another session asks again

    def test_confirming_settles_the_turn_even_when_the_note_was_confirmed_already(self):
        self.edit('core/notes.py')
        self.assertEqual(self.cli('confirm',self.short(self.listed),'--session','s1')[0],0)
        self.assertEqual(self.stop(),{})
        self.assertEqual(self.state()['saved'],['demo:'+self.listed['id']])
        # A second confirm on the same day changes nothing in the note, but is still the turn's memory decision.
        self.edit('core/notes.py',session='s2')
        code,result=self.cli('confirm',self.short(self.listed),'--session','s2')
        self.assertEqual((code,result['status']),(0,'unchanged'))
        self.assertEqual(self.stop('s2'),{})
        from core import usage
        self.assertEqual(len([r for r in usage.events(self.root) if r['e']=='confirm']),1)   # the diary counts the real confirm only

    def test_updating_a_note_the_session_saved_is_not_asked_about(self):
        self.edit('core/notes.py')
        code,_=self.cli('update',self.short(self.listed),'--session','s1','--set-detail','**Order:** keys keep their order')
        self.assertEqual(code,0);self.assertEqual(self.stop(),{})

    def test_a_note_already_saved_this_session_is_not_asked_about_in_a_later_turn(self):
        self.edit('core/notes.py');self.cli('confirm',self.short(self.listed),'--session','s1');self.stop()
        self.edit('core/notes.py');self.assertEqual(self.stop(),{})

    def test_a_bug_note_adds_the_fix_and_resolve_commands(self):
        bug=self.note('Parser crash on empty file','core/lexer.py raises IndexError when the file is empty.',type='bug')
        self.assertIn('Parser crash',self.edit('core/lexer.py')['context'])
        reason=self.stop()['continue_reason']
        self.assertIn(self.short(bug)+' Parser crash on empty file',reason)
        self.assertIn("CMD update "+self.short(bug)+" --session s1 --set-detail '**Fix:** ...' then CMD resolve "+self.short(bug),reason)

    def test_body_mentions_count_only_for_bug_and_decision_notes(self):
        self.assertIn('Ref resolution',self.edit(str(self.project/'core'/'cli.py'))['context'])   # a knowledge note names it
        self.assertEqual(self.stop(),{})                                                           # hinted, but nothing is owed
        self.assertNotIn('hinted',self.state())
        decision=self.note('Resolve refs in one place','Decision: core/cli.py _reference is the only resolver.',type='decision')
        self.edit(str(self.project/'core'/'cli.py'),session='s2')
        self.assertIn(self.short(decision),self.stop('s2')['continue_reason'])

    def test_files_edited_without_a_hint_stay_quiet(self):
        self.edit('core/other.py');self.assertEqual(self.stop(),{})
        self.assertNotIn('hinted',self.state())

    def test_unsupported_hosts_get_no_hints_and_so_no_follow_up(self):
        self.edit('core/notes.py',harness='copilot',session='s5')
        self.assertEqual(runtime.process('copilot','stop',self.payload('s5'),self.root),{})

    def test_the_hinted_and_followed_lists_are_capped(self):
        for i in range(105):self.note(f'Gotcha {i} widget{i}',f'widget{i} detail.',files=[f'core/f{i}.py'],allow_similar=True)
        for i in range(105):self.edit(f'core/f{i}.py');self.stop()
        state=self.state()
        self.assertLessEqual(len(state['hinted']),100);self.assertLessEqual(len(state['followed']),100)


if __name__=='__main__':unittest.main()
