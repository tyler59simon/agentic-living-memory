"""Notebook excerpts in prompts (NS-2B, design sections 8.3 and 12b): injectable notebooks of both kinds join the prompt hook's
source excerpt channel with a [notebook TITLE] label; searchable, hidden and private ones never do; subagents and machine prompts
get none; SessionStart never shows a page."""
import json
from test_sources import Kb,LEDGER_QUESTION
from core import docs,docs_index,runtime,sources

QUESTION='how does the wombat failover drill promote the standby replica'
PAGE=b'# Failover drill\n\nThe wombat failover drill promotes the standby replica, then drains traffic from the primary.\n'


class Inject(Kb):
    def setUp(self):
        super().setUp();self.sync();self.hook('session-start')
    def prompt(self,text,session='s1',**extra):return self.hook('user-prompt',session,prompt=text,turn_id='t',**extra)['context']
    def lm(self,visibility='injectable',private=False,nb='drills'):
        docs.create_notebook(self.root,'demo',nb,'Drills',visibility,private)
        docs.create_page(self.root,'demo',nb,'Failover.md',PAGE,include_private=private)
    def folder(self,visibility,*extra):
        nb=self.repo/'ops';nb.mkdir(exist_ok=True);(nb/'Failover.md').write_bytes(PAGE)
        code,result=self.cli('--json','doc','folder','set',str(nb),'--visibility',visibility,*extra);self.assertEqual(code,0,result);self.sync()

    def test_an_injectable_living_memory_notebook_is_injected_with_its_label(self):
        self.lm();context=self.prompt(QUESTION)
        self.assertIn('[notebook Drills] demo/drills/Failover.md:',context);self.assertIn('promotes the standby replica',context)
        self.assertNotIn('[notebook Drills]',self.prompt(QUESTION))   # once a session, like any excerpt

    def test_an_injectable_folder_notebook_is_injected_with_its_label(self):
        self.folder('injectable');context=self.prompt(QUESTION)
        self.assertIn('[notebook ops] ops/Failover.md:',context)

    def test_searchable_hidden_and_private_notebooks_are_never_injected(self):
        for n,(visibility,private) in enumerate((('searchable',False),('hidden',False),('injectable',True))):
            with self.subTest(visibility=visibility,private=private):
                self.lm(visibility,private,f'nb{n}');self.assertNotIn('promotes the standby',self.prompt(QUESTION,session=f's{n}'))
                docs_index.db_path(self.root,'demo').unlink();import shutil;shutil.rmtree(self.root/'domains'/'demo'/'notebooks'/f'nb{n}')
        for n,extra in enumerate((('searchable',),('hidden',),('injectable','--private'))):
            with self.subTest(folder=extra):
                self.folder(*extra);self.assertNotIn('promotes the standby',self.prompt(QUESTION,session=f'f{n}'))

    def test_made_private_stops_injection_before_the_index_knows(self):
        self.lm();docs_index.refresh(self.root,'demo')
        docs.update_notebook(self.root,'demo','drills',private=True)
        self.assertNotIn('promotes the standby',self.prompt(QUESTION,session='p'))

    def test_subagents_and_machine_prompts_get_no_notebook_excerpt(self):
        self.lm();self.folder('injectable')
        self.assertNotIn('[notebook',self.prompt(QUESTION,session='sub',agent_id='a1'))
        self.assertNotIn('[notebook',self.prompt('<task-notification>'+QUESTION+'</task-notification>',session='m'))

    def test_the_hook_never_reindexes_pages(self):
        self.lm();docs_index.db_path(self.root,'demo').unlink()
        self.assertNotIn('[notebook',self.prompt(QUESTION,session='r'));self.assertFalse(docs_index.db_path(self.root,'demo').exists())

    def test_session_start_never_shows_a_page(self):
        self.lm();self.folder('injectable')
        context=self.hook('session-start',session='new')['context']
        self.assertNotIn('promotes the standby',context);self.assertNotIn('Failover',context)

    def test_the_excerpt_budget_holds_with_notebooks(self):
        self.lm();self.folder('injectable')
        context=self.prompt(QUESTION,session='b');block=context[context.index('Source excerpts'):]
        self.assertLessEqual(len(block),runtime.SOURCE_BUDGET+40);self.assertLessEqual(block.count('\n- '),runtime.SOURCE_EXCERPTS)


if __name__=='__main__':
    import unittest;unittest.main()
