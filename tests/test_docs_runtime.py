"""Notebook pages in the hook (NS-2A): which `doc` commands each actor may run, the Hivemind terminal rule, --include-private
denied to every agent, and file-reading tools kept out of private notebooks (in the full hook and in hookfast)."""
import json,os,sys,unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime,hookfast,docs
from core.cli import DOC_WRITES


class Hook(StoreFixture):
    def setUp(self):
        super().setUp()
        docs.create_notebook(self.root,'demo','open','Open');docs.create_page(self.root,'demo','open','p.md',b'public\n')
        docs.create_notebook(self.root,'demo','secret','Secret',private=True);docs.create_page(self.root,'demo','secret','p.md',b'private\n',include_private=True)
        (self.base/'elsewhere').mkdir()
    def command(self,rest):return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'
    def hook(self,tool,tool_input,cwd=None,**extra):
        payload={'cwd':str(cwd or self.base/'project'),'session_id':'s1','tool_name':tool,'tool_input':tool_input,**extra}
        with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):return runtime.process('claude','pre-tool-use',payload,self.root)
    def shell(self,rest,**extra):return self.hook('Bash',{'command':self.command(rest)},**extra)

    def test_the_write_lists_agree(self):
        self.assertEqual(DOC_WRITES,runtime.DOC_WRITE_ACTIONS);self.assertNotIn('propose',DOC_WRITES)
        self.assertIn('domain',runtime.MUTATING_SUBCOMMANDS);self.assertIn('domain',runtime.HIVEMIND_GOVERNED)

    def test_doc_write_detector(self):
        for rest,writes in (('doc read demo open p.md',False),('doc --json list demo open',False),('doc notebooks',False),('doc propose demo open p.md',False),
                            ('doc write demo open p.md --expected-revision x',True),('doc --domain demo notebook create demo nb --title T',True),
                            ('doc move demo open a.md b.md',True),('doc attach demo open --from-file x',True),('doc accept abc',True),('doc undo d-1',True),
                            ('doc read demo write a.md',False)):
            with self.subTest(rest=rest):self.assertEqual(runtime.doc_write(self.command(rest),None),writes)

    def test_subagents_read_and_propose_only(self):
        self.assertIn('deny',self.shell('doc write demo open p.md --expected-revision x',agent_id='a1'))
        self.assertIn('deny',self.shell('domain create nb --notebook-only',agent_id='a1'))
        self.assertNotIn('deny',self.shell('doc propose demo open p.md --base-revision x',agent_id='a1'))
        self.assertNotIn('deny',self.shell('doc read demo open p.md',agent_id='a1'))

    def test_main_writes_keep_the_prompt(self):
        self.assertIn('allow',self.shell('doc read demo open p.md'))
        self.assertIn('allow',self.shell('doc propose demo open p.md --base-revision x'))
        self.assertNotIn('allow',str(self.shell('doc write demo open p.md --expected-revision x')))
        self.assertNotIn('allow',str(self.shell('doc attach demo open --from-file x')))

    def test_hivemind_terminal(self):
        with patch.dict(os.environ,{'HIVEMIND_SESSION':'hm-1'}):
            self.assertEqual(self.shell('doc write demo open p.md --expected-revision x')['deny'],runtime.HIVEMIND_DENY)
            self.assertEqual(self.shell('domain create nb --notebook-only')['deny'],runtime.HIVEMIND_DENY)
            self.assertTrue(runtime.hivemind_governed(f'alm doc undo d-1 && echo done','bash'))
            self.assertTrue(runtime.hivemind_governed(f'alm proposals accept abcdef123456 && echo done','bash'))
            self.assertNotIn('deny',self.shell('doc propose demo open p.md --base-revision x'))

    def test_include_private_is_denied_to_agents_everywhere(self):
        for rest in ('doc notebooks --include-private','history --include-p','diff op --include-private'):
            with self.subTest(rest=rest):self.assertEqual(self.shell(rest)['deny'],runtime.PRIVATE_DENY)
        self.assertEqual(self.shell('doc read demo secret p.md --include-private',cwd=self.base/'elsewhere')['deny'],runtime.PRIVATE_DENY)
        self.assertEqual(self.hook('Bash',{'command':'alm doc read demo secret p.md --include-private; echo'})['deny'],runtime.PRIVATE_DENY)

    def test_reading_tools_stay_out_of_private_notebooks(self):
        secret=self.root/'domains'/'demo'/'notebooks'/'secret'
        for cwd in (self.base/'project',self.base/'elsewhere'):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.hook('Read',{'file_path':str(secret/'p.md')},cwd=cwd).get('deny'),runtime.PRIVATE_READ_DENY)
                self.assertEqual(self.hook('Grep',{'pattern':'private','path':str(self.root)},cwd=cwd).get('deny'),runtime.PRIVATE_READ_DENY)
                self.assertEqual(self.hook('Glob',{'pattern':str(self.root/'domains').replace('\\','/')+'/**/*.md'},cwd=cwd).get('deny'),runtime.PRIVATE_READ_DENY)
                self.assertNotIn('deny',self.hook('Read',{'file_path':str(self.root/'domains'/'demo'/'notebooks'/'open'/'p.md')},cwd=cwd))
        self.assertEqual(self.hook('Grep',{'pattern':'x'},cwd=self.root).get('deny'),runtime.PRIVATE_READ_DENY)
        self.assertNotIn('deny',self.hook('Grep',{'pattern':'x','path':str(self.base/'project')}))
        docs.update_notebook(self.root,'demo','secret',private=False,include_private=True)
        self.assertNotIn('deny',self.hook('Read',{'file_path':str(secret/'p.md')}))

    def test_a_damaged_notebook_json_counts_as_private(self):
        meta=self.root/'domains'/'demo'/'notebooks'/'open'/'notebook.json';meta.write_text('{broken')
        self.assertEqual(self.hook('Read',{'file_path':str(meta.parent/'p.md')}).get('deny'),runtime.PRIVATE_READ_DENY)

    def test_hookfast_hands_store_reads_to_the_full_hook(self):
        state=self.root/'.turns';state.mkdir(exist_ok=True)
        import hashlib,json
        (state/(hashlib.sha256(b's1').hexdigest()+'.current.json')).write_text(json.dumps({'repos':{}}))
        payload=lambda tool,args,cwd:{'tool_name':tool,'session_id':'s1','cwd':str(cwd),'tool_input':args}
        with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):
            self.assertIsNone(hookfast.quick(payload('Read',{'file_path':str(self.root/'domains'/'demo'/'notebooks'/'secret'/'p.md')},self.base/'elsewhere')))
            self.assertIsNone(hookfast.quick(payload('Grep',{'pattern':'x'},self.root)))
            self.assertIsNone(hookfast.quick(payload('Glob',{'pattern':str(self.root)+'/**'},self.base/'elsewhere')))
            self.assertEqual(hookfast.quick(payload('Read',{'file_path':str(self.base/'elsewhere'/'x.txt')},self.base/'elsewhere')),{})

    def test_protected_config_files(self):
        for name in ('notebook-privacy.json','notebook-folders.json'):
            with self.subTest(name=name):self.assertEqual(runtime.protected_rule(str(self.root/name),str(self.base),self.root),'config')


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
