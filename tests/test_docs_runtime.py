"""Notebook pages in the hook (NS-2A): which `doc` commands each actor may run, the Hivemind terminal rule, --include-private
denied to every agent, and file-reading tools kept out of private notebooks (in the full hook and in hookfast)."""
import os,sys,unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import runtime,hookfast,docs
from core.cli import DOC_WRITES


class Hook(StoreFixture):
    def setUp(self):
        super().setUp()
        docs.create_notebook(self.root,'demo','open','Open');docs.create_page(self.root,'demo','open','p.md',b'public\n')
        docs.create_notebook(self.root,'demo','secret','Secret',private=True);docs.create_page(self.root,'demo','secret','p.md',b'private\n')
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
            self.assertTrue(runtime.hivemind_governed(f'alm doc accept abc && echo done','bash'))
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
        docs.update_notebook(self.root,'demo','secret',private=False)
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


if __name__=='__main__':unittest.main()
