"""Folder notebook pages in the hook: which `doc` commands each actor may run, the Hivemind terminal rule, the private-notebook flag
denied to every agent (CON-18 HOOK-INCLUDE: only option words of a memory command count), and the edit guard on folder pages."""
import json,os,sys,unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime,hookfast
from core.cli import DOC_WRITES

FLAG='--include'+'-private'   # the hook of the session that runs the tests would refuse this text in a shell command


class Hook(StoreFixture):
    def setUp(self):
        super().setUp()
        self.nb=self.base/'wiki';self.nb.mkdir();(self.nb/'p.md').write_text('public\n')
        (self.root/'notebook-folders.json').write_text(json.dumps({'version':1,'folders':[{'path':self.nb.resolve().as_posix(),'domain':'demo','visibility':'searchable','private':False}]}))
        (self.base/'elsewhere').mkdir()
    def command(self,rest):return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'
    def hook(self,tool,tool_input,cwd=None,**extra):
        payload={'cwd':str(cwd or self.base/'project'),'session_id':'s1','tool_name':tool,'tool_input':tool_input,**extra}
        with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):return runtime.process('claude','pre-tool-use',payload,self.root)
    def shell(self,rest,**extra):return self.hook('Bash',{'command':self.command(rest)},**extra)
    def page(self):return str(self.nb/'p.md')

    def test_the_write_lists_agree(self):
        self.assertEqual(DOC_WRITES,runtime.DOC_WRITE_ACTIONS);self.assertNotIn('propose',DOC_WRITES);self.assertEqual(runtime.DOC_AGENT_ACTIONS,{'read','propose'})
        self.assertNotIn('domain',runtime.MUTATING_SUBCOMMANDS);self.assertNotIn('domain',runtime.HIVEMIND_GOVERNED)

    def test_doc_write_detector(self):
        for rest,writes in ((f'doc read --file "{self.page()}"',False),(f'doc --json read --file x.md',False),(f'doc propose --file x.md --base-revision r',False),
                            ('doc folder set /x',True),('doc undo d-1',True),('doc write demo open p.md --expected-revision x',True),('doc list demo open',True),('doc notebooks',True),
                            ('doc read write a.md',False)):
            with self.subTest(rest=rest):self.assertEqual(runtime.doc_write(self.command(rest),None),writes)

    def test_abbreviated_options_do_not_hide_a_doc_write(self):
        alm=f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}"'
        for text in (f'{alm} doc --dom demo undo d-1',f'{alm} --dom demo doc folder set /x',f'{alm} doc --domain=demo undo d-1',f'{alm} doc wipe demo open'):
            with self.subTest(text=text):self.assertTrue(runtime.doc_write(text,None))
        self.assertEqual(runtime._subcommand(['--dom','demo','--root=x','capture']),'capture')
        self.assertFalse(runtime.doc_write(f'{alm} doc --dom demo read --file x.md',None))
        self.assertFalse(runtime.doc_write(f'{alm} capture doc body --tag subject/project',None))   # a word "doc" in a note is not a doc command

    def test_subagents_read_and_propose_only(self):
        self.assertIn('deny',self.shell('doc undo d-1',agent_id='a1'))
        self.assertIn('deny',self.shell('doc folder set /x',agent_id='a1'))
        self.assertNotIn('deny',self.shell(f'doc propose --file "{self.page()}" --base-revision x',agent_id='a1'))
        self.assertNotIn('deny',self.shell(f'doc read --file "{self.page()}"',agent_id='a1'))

    def test_main_writes_keep_the_prompt(self):
        self.assertIn('allow',self.shell(f'doc read --file "{self.page()}"'))
        self.assertIn('allow',self.shell(f'doc propose --file "{self.page()}" --base-revision x'))
        self.assertNotIn('allow',str(self.shell('doc undo d-1')))
        self.assertNotIn('allow',str(self.shell('doc folder set /x')))

    def test_hivemind_terminal(self):
        with patch.dict(os.environ,{'HIVEMIND_SESSION':'hm-1'}):
            self.assertEqual(self.shell('doc undo d-1')['deny'],runtime.HIVEMIND_DENY)
            self.assertEqual(self.shell('doc folder set /x')['deny'],runtime.HIVEMIND_DENY)
            self.assertTrue(runtime.hivemind_governed('alm doc undo d-1 && echo done','bash'))
            self.assertTrue(runtime.hivemind_governed('alm proposals accept abcdef123456 && echo done','bash'))
            self.assertNotIn('deny',self.shell(f'doc propose --file "{self.page()}" --base-revision x'))

    def test_the_flag_is_denied_to_agents_everywhere(self):
        for rest in (f'doc read --file x.md {FLAG}','history --include-p',f'diff op {FLAG}',f'proposals --kind page {FLAG}'):
            with self.subTest(rest=rest):self.assertEqual(self.shell(rest)['deny'],runtime.PRIVATE_DENY)
        self.assertEqual(self.shell(f'doc read --file "{self.page()}" {FLAG}',cwd=self.base/'elsewhere')['deny'],runtime.PRIVATE_DENY)
        self.assertEqual(self.hook('Bash',{'command':f'alm doc read --file x.md {FLAG}; echo'})['deny'],runtime.PRIVATE_DENY)

    def test_serve_is_denied_to_agents(self):
        for command in (f'alm --root {self.root} serve --stdio','python living_memory.py serve'):
            with self.subTest(command=command):self.assertEqual(self.hook('Bash',{'command':command}).get('deny'),runtime.SERVE_DENY)
        self.assertFalse(runtime.serve_command('python living_memory.py capture --title serve --summary "How serve starts"',None))
        self.assertTrue(runtime.serve_command('python living_memory.py --root "C:/Living Memory" serve --stdio',None))
        self.assertTrue(runtime.serve_command('python living_memory.py --root "C:/Living Memory serve',None))   # unbalanced: fail closed
        self.assertFalse(runtime.serve_command('python living_memory.py --root "C:/Living Memory" capture --title serve',None))

    def test_the_hook_never_reads_a_page_for_the_private_flag(self):
        self.assertNotIn('deny',self.hook('Read',{'file_path':self.page()}))   # the store's file-reading rule left with hosted notebooks
        self.assertNotIn('PRIVATE_READ_DENY',dir(runtime))

    def test_hookfast_hands_store_reads_to_the_full_hook(self):
        state=self.root/'.turns';state.mkdir(exist_ok=True)
        import hashlib
        (state/(hashlib.sha256(b's1').hexdigest()+'.current.json')).write_text(json.dumps({'repos':{}}))
        payload=lambda tool,args,cwd:{'tool_name':tool,'session_id':'s1','cwd':str(cwd),'tool_input':args}
        with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):
            self.assertIsNone(hookfast.quick(payload('Read',{'file_path':str(self.root/'domains'/'demo'/'notes'/'x.md')},self.base/'elsewhere')))
            self.assertIsNone(hookfast.quick(payload('Grep',{'pattern':'x'},self.root)))
            self.assertEqual(hookfast.quick(payload('Read',{'file_path':str(self.base/'elsewhere'/'x.txt')},self.base/'elsewhere')),{})

    def test_protected_config_files(self):
        self.assertEqual(runtime.protected_rule(str(self.root/'notebook-folders.json'),str(self.base),self.root),'config')

    def test_the_leftover_vault_mirror_folder_stays_off_limits(self):
        vault=self.base/'vault';(vault/'.living-memory').mkdir(parents=True);(vault/'.living-memory'/'vault.json').write_text('{"id":"v1"}')
        (self.root/'domains'/'demo'/'sync-state.json').write_text(json.dumps({'vault':str(vault)}))
        self.assertEqual(runtime.protected_rule(str(vault/'Notebooks'/'demo'/'nb1'/'Plan.md'),str(self.base),self.root),'vault')


class IncludePrivateText(unittest.TestCase):
    """include_private reads the option words of a memory command only: quoted values and other programs' options never count."""
    def refused(self,text,grammar=None):return runtime.include_private(text,grammar)

    def test_a_real_flag_and_an_unambiguous_abbreviation_on_a_memory_command_are_refused(self):
        for text in (f'alm search x {FLAG}',f'alm search x {FLAG}=1','alm search x --include-priv','alm search x --incl',f'alm doc read --file a.md {FLAG}',
                     f'echo hi && alm search x {FLAG}',f'alm capture --summary "$(alm search x {FLAG})"',f'bash -c "alm search x {FLAG}"',f'cmd /c alm search x {FLAG}',
                     f'py -3.15 C:\\x\\living_memory.py search x {FLAG}',f'python3 /x/living_memory.py search x --include',f'"{sys.executable}" "{runtime.SCRIPT}" search x {FLAG}',
                     f'alm search \'unclosed {FLAG}'):
            for grammar in ('posix','powershell',None):
                with self.subTest(text=text,grammar=grammar):self.assertTrue(self.refused(text,grammar))

    def test_grep_with_an_include_glob_passes_even_beside_a_memory_command(self):
        for text in ('grep -rn foo --include=*.py .','alm search x | grep -rn --include=*.py y','alm search x; grep --in foo .','find . -name x | xargs grep --include="*.md" y'):
            for grammar in ('posix','powershell',None):
                with self.subTest(text=text,grammar=grammar):self.assertFalse(self.refused(text,grammar))

    def test_the_flag_text_inside_a_quoted_value_passes(self):
        for text in (f"alm capture --title t --summary '{FLAG}'","alm capture --title t --summary '--in'",f'alm capture --summary "refuses {FLAG} and --in for agents | $(date)"',
                     f"alm capture --summary 'The hook refuses {FLAG}' --detail '**Rule:** never pass {FLAG}'",f'alm capture --summary="the flag {FLAG}"',
                     f'alm search "{FLAG}"',f'echo {FLAG}'):
            for grammar in ('posix','powershell',None):
                with self.subTest(text=text,grammar=grammar):self.assertFalse(self.refused(text,grammar))


class FolderGuard(StoreFixture):
    """An agent may not edit a page of a folder notebook directly (CON-9): it is told to read it and stage the edit with doc propose --file."""
    def setUp(self):
        super().setUp()
        self.nb=self.base/'wiki';self.nb.mkdir();(self.nb/'a.md').write_text('one');(self.nb/'a.markdown').write_text('one');(self.nb/'a.txt').write_text('one')
        (self.base/'elsewhere').mkdir();(self.base/'elsewhere'/'a.md').write_text('one')
        self.register()
    def register(self,**more):
        entry={'path':self.nb.resolve().as_posix(),'domain':'demo','visibility':'searchable','private':False,**more}
        (self.root/'notebook-folders.json').write_text(json.dumps({'version':1,'folders':[entry]}))
    def hook(self,tool,tool_input,cwd=None,**extra):
        payload={'cwd':str(cwd or self.base/'project'),'session_id':'s1','tool_name':tool,'tool_input':tool_input,**extra}
        with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):return runtime.process('claude','pre-tool-use',payload,self.root)

    def test_edit_tools_are_denied_on_markdown_pages_of_a_folder_notebook(self):
        page=str(self.nb/'a.md')
        for tool,args in (('Edit',{'file_path':page}),('Write',{'file_path':page}),('MultiEdit',{'file_path':page,'edits':[]}),('NotebookEdit',{'notebook_path':page}),
                          ('Edit',{'file_path':str(self.nb/'a.markdown')}),('apply_patch',{'input':f'*** Begin Patch\n*** Update File: {page}\n@@\n-one\n+two\n*** End Patch'}),
                          ('Write',{'file_path':str(self.nb/'new'/'page.md')})):
            with self.subTest(tool=tool,args=args):
                deny=self.hook(tool,args)['deny']
                self.assertIn('doc read --file',deny);self.assertIn('doc propose --file',deny);self.assertIn('--base-revision',deny);self.assertIn('Inbox',deny)
                self.assertIn(self.nb.resolve().as_posix(),deny)

    def test_every_session_is_covered_bound_or_not_and_subagents_too(self):
        page=str(self.nb/'a.md')
        for extra in ({'agent_id':'a1'},{'cwd':self.base/'elsewhere'}):
            with self.subTest(extra=extra):self.assertIn('deny',self.hook('Write',{'file_path':page},**extra))
        with patch.dict(os.environ,{'HIVEMIND_SESSION':'hm-1'}):self.assertIn('doc propose --file',self.hook('Write',{'file_path':page})['deny'])
        with patch.object(runtime,'FOLDER_GUARD','off'):self.assertNotIn('deny',self.hook('Write',{'file_path':page}))
        self.assertEqual(runtime.FOLDER_GUARD,'all')

    def test_other_files_other_folders_and_copies_are_allowed(self):
        for tool,path in (('Edit',self.nb/'a.txt'),('Write',self.nb/'notes.json'),('Edit',self.base/'elsewhere'/'a.md'),('Write',self.base/'wiki-copy'/'a.md'),('Read',self.nb/'a.md'),('Grep',self.nb/'a.md')):
            with self.subTest(tool=tool,path=path):self.assertNotIn('deny',self.hook(tool,{'file_path':str(path)}))
        self.assertNotIn('deny',self.hook('Bash',{'command':f'echo x > "{self.nb/"a.md"}"'}))   # shell writes are not covered
        self.assertIsNone(runtime.folder_rule(str(self.nb/'a.txt'),str(self.base),self.root))
        self.assertEqual(runtime.folder_rule(str(self.nb/'a.md'),str(self.base),self.root)['domain'],'demo')
        self.assertEqual(runtime.folder_rule('wiki/a.md',str(self.base),self.root)['domain'],'demo')   # relative to the working folder

    def test_a_removed_folder_is_no_notebook_and_a_private_one_says_to_tell_the_user(self):
        page=str(self.nb/'a.md')
        self.register(removed=True);self.assertNotIn('deny',self.hook('Write',{'file_path':page}))
        self.register(visibility='hidden');self.assertIn('doc propose --file',self.hook('Write',{'file_path':page})['deny'])
        self.register(private=True);deny=self.hook('Write',{'file_path':page})['deny']
        self.assertIn('private folder notebook',deny);self.assertIn('Tell the user',deny);self.assertNotIn('doc propose',deny)

    def test_apply_patch_crlf_move_to_dot_folders_and_private_sidecar_subfolder(self):
        page=str(self.nb/'a.md');moved=str(self.nb/'b.md')
        crlf_patch=f'*** Begin Patch\r\n*** Update File: {page}\r\n@@\r\n-old\r\n+new\r\n*** End Patch\r\n'
        self.assertIn('deny',self.hook('apply_patch',{'input':crlf_patch}))
        move_patch=f'*** Begin Patch\n*** Update File: {self.base/"elsewhere"/"x.txt"}\n*** Move to: {moved}\n@@\n*** End Patch\n'
        self.assertIn('deny',self.hook('apply_patch',{'input':move_patch}))
        self.assertNotIn('deny',self.hook('Write',{'file_path':str(self.nb/'.obsidian'/'readme.md')}))
        sub=self.nb/'secret'
        (sub/'.hivemind').mkdir(parents=True,exist_ok=True)
        (sub/'.hivemind'/'notebook.json').write_text('{"private":true}',encoding='utf-8')
        deny=self.hook('Write',{'file_path':str(sub/'note.md')})['deny']
        self.assertIn('private folder notebook',deny);self.assertNotIn('doc propose',deny)

    def test_the_registry_is_cached_by_its_mtime_and_a_damaged_one_denies_nothing(self):
        page=str(self.nb/'a.md');self.assertIn('deny',self.hook('Write',{'file_path':page}))
        (self.root/'notebook-folders.json').write_text('{"version":1,"folders":[]}');os.utime(self.root/'notebook-folders.json',(1,2))
        self.assertNotIn('deny',self.hook('Write',{'file_path':page}))
        (self.root/'notebook-folders.json').write_text('{damaged');os.utime(self.root/'notebook-folders.json',(1,3))
        self.assertNotIn('deny',self.hook('Write',{'file_path':page}))


if __name__=='__main__':unittest.main()
