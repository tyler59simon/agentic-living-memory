"""Folder notebook excerpts in prompts (NS-2B, design sections 8.3 and 12b): injectable folder notebooks join the prompt hook's source
excerpt channel with a [notebook TITLE] label; searchable, hidden and private ones never do; subagents and machine prompts get none;
SessionStart never shows a page."""
from test_sources import Kb
from core import docs_index,runtime

QUESTION='how does the wombat failover drill promote the standby replica'
PAGE=b'# Failover drill\n\nThe wombat failover drill promotes the standby replica, then drains traffic from the primary.\n'


class Inject(Kb):
    def setUp(self):
        super().setUp();self.sync();self.hook('session-start')
    def prompt(self,text,session='s1',**extra):return self.hook('user-prompt',session,prompt=text,turn_id='t',**extra)['context']
    def folder(self,visibility,*extra):
        nb=self.repo/'ops';nb.mkdir(exist_ok=True);(nb/'Failover.md').write_bytes(PAGE)
        code,result=self.cli('--json','doc','folder','set',str(nb),'--visibility',visibility,*extra);self.assertEqual(code,0,result);self.sync()

    def test_an_injectable_folder_notebook_is_injected_with_its_label_once_a_session(self):
        self.folder('injectable');context=self.prompt(QUESTION)
        self.assertIn('[notebook ops] ops/Failover.md:',context);self.assertIn('promotes the standby replica',context)
        self.assertNotIn('[notebook ops]',self.prompt(QUESTION))

    def test_searchable_hidden_and_private_notebooks_are_never_injected(self):
        for n,extra in enumerate((('searchable',),('hidden',),('injectable','--private'))):
            with self.subTest(folder=extra):
                self.folder(*extra);self.assertNotIn('promotes the standby',self.prompt(QUESTION,session=f'f{n}'))

    def test_subagents_and_machine_prompts_get_no_notebook_excerpt(self):
        self.folder('injectable')
        self.assertNotIn('[notebook',self.prompt(QUESTION,session='sub',agent_id='a1'))
        self.assertNotIn('[notebook',self.prompt('<task-notification>'+QUESTION+'</task-notification>',session='m'))

    def test_the_hook_never_builds_the_page_index(self):
        self.folder('injectable');docs_index.db_path(self.root,'demo').unlink(missing_ok=True)
        self.prompt(QUESTION,session='r');self.assertFalse(docs_index.db_path(self.root,'demo').exists())

    def test_session_start_never_shows_a_page(self):
        self.folder('injectable')
        context=self.hook('session-start',session='new')['context']
        self.assertNotIn('promotes the standby',context);self.assertNotIn('Failover',context)

    def test_the_excerpt_budget_holds_with_notebooks(self):
        self.folder('injectable')
        context=self.prompt(QUESTION,session='b');block=context[context.index('Source excerpts'):]
        self.assertLessEqual(len(block),runtime.SOURCE_BUDGET+40);self.assertLessEqual(block.count('\n- '),runtime.SOURCE_EXCERPTS)


if __name__=='__main__':
    import unittest;unittest.main()
