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

if __name__=='__main__':unittest.main()
