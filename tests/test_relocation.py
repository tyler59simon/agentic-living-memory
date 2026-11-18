import json,os,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core.governance import apply,plan
from core.repository import read_json,canonical_json,digest
from integrations import setup

class Relocation(StoreFixture):
    """Moving from CODEX_HOME/agentic-living-memory to the harness-neutral install folder."""
    def setUp(self):
        super().setUp()
        self.codex=Path(os.environ['CODEX_HOME']);self.codex.mkdir(parents=True,exist_ok=True)
        self.claude=Path(os.environ['CLAUDE_HOME'])
        self.legacy=self.codex/'agentic-living-memory'
    def apply(self,home,proposed):return apply(home,proposed['plan_id'],proposed['digest'])
    def from_legacy(self,harness,home):
        """Wire a harness the way an older release did: from the copy inside CODEX_HOME."""
        with patch.object(setup,'ROOT',self.legacy):self.apply(home,setup.setup_plan(harness,home))
    def ours(self,config,harness):
        return [h for groups in setup._events(config,harness).values() for g in groups for h in g.get('hooks',[]) if setup._ours(h,harness)]
    def commands(self,handlers):return [' '.join([str(h.get('command',''))]+[str(a) for a in h.get('args',[])]) for h in handlers]

    def test_claude_handlers_move_to_the_new_copy(self):
        self.from_legacy('claude',self.claude)
        settings=read_json(self.claude/'settings.json');settings['hooks']['Stop'].append({'hooks':[{'type':'command','command':'other'}]})
        (self.claude/'settings.json').write_text(json.dumps(settings))
        self.apply(self.claude,setup.setup_plan('claude',self.claude))   # no "Modified managed handlers preserved"
        settings=read_json(self.claude/'settings.json');handlers=self.ours(settings,'claude')
        self.assertEqual(len(handlers),7)
        self.assertTrue(all(str(setup._script('claude')) in c and str(self.legacy) not in c for c in self.commands(handlers)))
        self.assertIn({'hooks':[{'type':'command','command':'other'}]},settings['hooks']['Stop'])
    def test_codex_handlers_from_the_old_installer_are_replaced(self):
        self.from_legacy('codex',self.codex)
        # The old install.py kept only the instruction hash.
        state=read_json(self.codex/'.living-memory-ownership.json');(self.codex/'.living-memory-ownership.json').write_text(canonical_json({'schema':2,'instruction_hash':state['instruction_hash']}))
        self.apply(self.codex,setup.setup_plan('codex',self.codex))
        config=read_json(self.codex/'hooks.json');handlers=self.ours(config,'codex')
        self.assertEqual(len(handlers),6);self.assertTrue(all(str(self.legacy) not in c for c in self.commands(handlers)))
        self.assertEqual((self.codex/'AGENTS.md').read_text(encoding='utf-8').count('agentic-living-memory:begin'),1)

    def wired_from(self,source,harness='claude'):
        with patch.object(setup,'ROOT',source):self.apply(self.claude,setup.setup_plan(harness,self.claude))
    def test_handlers_wired_from_the_source_checkout_are_managed_by_the_installed_copy(self):
        source,app=self.base/'checkout',self.base/'app';app.mkdir();(app/'.core-ownership-v1.json').write_text(json.dumps({'source':str(source)}))
        self.wired_from(source)
        with patch.object(setup,'ROOT',app):
            self.apply(self.claude,setup.setup_plan('claude',self.claude))   # no "Modified managed handlers preserved"
            handlers=self.ours(read_json(self.claude/'settings.json'),'claude')
            self.assertEqual(len(handlers),7);self.assertTrue(all(str(app) in c and str(source) not in c for c in self.commands(handlers)))
            self.apply(self.claude,setup.setup_plan('claude',self.claude,uninstall=True))   # uninstall is not blocked either
        self.assertEqual(self.ours(read_json(self.claude/'settings.json'),'claude'),[])
    def test_handlers_wired_from_the_default_install_folder_are_managed_from_a_checkout(self):
        import install
        app=self.base/'app';self.wired_from(app)
        with patch.object(install,'INSTALL_DIR',app):self.apply(self.claude,setup.setup_plan('claude',self.claude))
        self.assertTrue(all(str(setup._script('claude')) in c for c in self.commands(self.ours(read_json(self.claude/'settings.json'),'claude'))))
    def test_a_real_edit_of_a_managed_handler_names_the_file_and_the_repair(self):
        self.wired_from(setup.ROOT)
        settings=read_json(self.claude/'settings.json');settings['hooks']['Stop'][0]['hooks'][0]['timeout']=999
        (self.claude/'settings.json').write_text(json.dumps(settings))
        with self.assertRaises(ValueError) as caught:setup.setup_plan('claude',self.claude)
        self.assertIn('Modified managed handlers preserved',str(caught.exception));self.assertIn(str(self.claude/'settings.json'),str(caught.exception));self.assertIn('claude --hooks',str(caught.exception))

    def edit_handler(self):
        self.wired_from(setup.ROOT)
        settings=read_json(self.claude/'settings.json');settings['hooks']['Stop'][0]['hooks'][0]['timeout']=999
        (self.claude/'settings.json').write_text(json.dumps(settings))
    def test_adopt_shows_the_handler_diff_and_applies_through_the_plan(self):
        self.edit_handler()
        with self.assertRaisesRegex(ValueError,'--adopt'):setup.setup_plan('claude',self.claude)
        before=(self.claude/'settings.json').read_text()
        proposed=setup.setup_plan('claude',self.claude,adopt=True);diff=proposed['summary']['adopt']['settings.json']
        self.assertIn('--- edited settings.json',diff);self.assertIn('+++ managed settings.json',diff);self.assertIn('-          "timeout": 999',diff);self.assertIn('+          "timeout": 15',diff)
        self.assertEqual((self.claude/'settings.json').read_text(),before)   # a plan changes nothing
        with self.assertRaisesRegex(ValueError,'digest'):apply(self.claude,proposed['plan_id'],'0'*64)
        self.apply(self.claude,proposed)
        self.assertNotIn('999',(self.claude/'settings.json').read_text());self.assertNotIn('adopt',setup.setup_plan('claude',self.claude)['summary'])
    def test_adopt_shows_the_instruction_block_diff_and_keeps_text_around_it(self):
        self.wired_from(setup.ROOT);path=self.claude/'CLAUDE.md';text=path.read_text(encoding='utf-8');begin='agentic-living-memory:begin -->'
        path.write_text('Mine before.'+chr(10)+text.replace(begin,begin+chr(10)+'My own rule.',1),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'--adopt'):setup.setup_plan('claude',self.claude)
        proposed=setup.setup_plan('claude',self.claude,adopt=True);self.assertIn('-My own rule.',proposed['summary']['adopt']['CLAUDE.md'])
        self.apply(self.claude,proposed);after=path.read_text(encoding='utf-8')
        self.assertNotIn('My own rule.',after);self.assertTrue(after.startswith('Mine before.'));self.assertEqual(after.count('agentic-living-memory:begin'),1)
    def test_adopt_is_a_plan_flag_only(self):
        with self.assertRaises(SystemExit):setup.main(['claude','--home',str(self.claude),'--apply-plan','a'*32,'--expected-plan-sha256','b'*64,'--adopt'])

    def test_retire_refuses_while_referenced_then_removes_owned_files(self):
        import install
        home=self.base/'local'/'agentic-living-memory'
        with patch.multiple(install,CODEX_HOME=self.codex,LEGACY_DIR=self.legacy,INSTALL_HOME=home,INSTALL_DIR=home/'app'):
            self.apply(self.codex,plan(self.codex,install._package_changes(self.legacy,False),'legacy_install'))
            (self.legacy/'.gitignore').write_text('*.pyc\n')   # a file the old install never owned
            with self.assertRaisesRegex(ValueError,'before retiring'):install.plan_retire()
            self.apply(home,install.plan_install())
            self.from_legacy('claude',self.claude)
            with self.assertRaisesRegex(ValueError,'still in use by: claude'):install.plan_retire()
            self.apply(self.claude,setup.setup_plan('claude',self.claude))
            proposed=install.plan_retire()
            self.assertEqual(proposed['summary']['not_owned_left_in_place'],['.gitignore'])
            self.apply(self.codex,proposed)
        self.assertFalse((self.legacy/'living_memory.py').exists());self.assertFalse((self.legacy/'.core-ownership-v1.json').exists())
        self.assertTrue((self.legacy/'.gitignore').exists());self.assertTrue((home/'app/living_memory.py').exists())
    def test_retire_checks_custom_homes_when_given(self):
        import install
        home=self.base/'local'/'agentic-living-memory';custom=self.base/'custom-claude'
        with patch.multiple(install,CODEX_HOME=self.codex,LEGACY_DIR=self.legacy,INSTALL_HOME=home,INSTALL_DIR=home/'app'):
            self.apply(self.codex,plan(self.codex,install._package_changes(self.legacy,False),'legacy_install'))
            self.apply(home,install.plan_install())
            self.from_legacy('claude',custom)
            proposed=install.plan_retire()   # the default homes are clean
            self.assertNotIn(str(custom/'settings.json'),proposed['summary']['checked'])
            with self.assertRaisesRegex(ValueError,'still in use by: claude'):install.plan_retire([('claude',str(custom))])
    def test_retire_without_legacy_copy(self):
        import install
        with patch.multiple(install,LEGACY_DIR=self.legacy):
            with self.assertRaisesRegex(ValueError,'No legacy package'):install.plan_retire()

if __name__=='__main__':unittest.main()
