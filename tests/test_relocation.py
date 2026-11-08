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
