"""Notebook pages in the Obsidian vault (NS-2B, design sections 8.4 and 12b): the byte-exact mirror of searchable and injectable
notebooks, vault edits coming back as page writes, conflicts as proposals, new pages, deletions as approved plans, the mirror
leaving the vault when a notebook turns private, and the mirror never indexed as a source."""
import json,os,shutil
from pathlib import Path
from test_docs import Fixture
from core import docs,folder_policy,notebook_sync,runtime,sources
from core.repository import digest,read_json
from core.sync import build_overlay

PAGE=b'# Plan\r\n\r\nThe quarterly zebra plan.\r\n'


class Mirror(Fixture):
    def setUp(self):
        super().setUp();self.vault=self.base/'vault'
        self.notebook();self.page('Plan.md',PAGE)
    def build(self):return build_overlay('demo',self.vault,self.root)['notebooks']
    def copy(self,rel,nb='nb1'):return self.vault/'Notebooks'/'demo'/nb/rel
    def status(self):return (self.vault/'Dashboards'/'Sync Status - demo.md').read_text(encoding='utf-8')
    def age(self,path):old=os.stat(path).st_mtime-120;os.utime(path,(old,old))   # past the quiet period

    def test_searchable_and_injectable_notebooks_are_mirrored_byte_for_byte(self):
        self.notebook('inj',visibility='injectable');self.page('I.md',b'inject\n','inj')
        self.notebook('quiet',visibility='hidden');self.page('Q.md',b'hidden\n','quiet')
        self.notebook('secret',private=True);docs.create_page(self.root,'demo','secret','S.md',b'private\n',include_private=True)
        docs.attach(self.root,'demo','nb1',self._asset(b'\x89PNG binary'),'logo.png')
        self.build()
        self.assertEqual(self.copy('Plan.md').read_bytes(),PAGE);self.assertEqual(self.copy('I.md','inj').read_bytes(),b'inject\n')
        self.assertFalse((self.vault/'Notebooks'/'demo'/'quiet').exists());self.assertFalse((self.vault/'Notebooks'/'demo'/'secret').exists())
        self.assertEqual([p.read_bytes() for p in (self.vault/'Notebooks'/'demo'/'nb1'/'assets').iterdir()],[b'\x89PNG binary'])
        self.assertEqual(self.build()['mirrored'],0)   # nothing changed: nothing rewritten

    def _asset(self,data):
        path=self.base/'logo.png';path.write_bytes(data);return path

    def test_a_vault_edit_comes_back_as_a_page_write(self):
        self.build();self.copy('Plan.md').write_bytes(b'# Plan\r\n\r\nEdited in Obsidian.\r\n');self.age(self.copy('Plan.md'))
        result=self.build();self.assertEqual(result['saved'],1)
        self.assertEqual(self.file('Plan.md').read_bytes(),b'# Plan\r\n\r\nEdited in Obsidian.\r\n')
        self.assertEqual(self.ok('search','--pages','edited obsidian')['pages'][0]['path'],'Plan.md')
        self.assertIn('Saved: nb1/Plan.md',self.status())

    def test_a_fresh_vault_edit_waits_for_the_quiet_period(self):
        self.build();self.copy('Plan.md').write_bytes(b'still typing')
        with __import__('unittest.mock').mock.patch('core.sync.QUIET_SECONDS',30):result=self.build()
        self.assertEqual(result['deferred'],1);self.assertEqual(self.file('Plan.md').read_bytes(),PAGE)

    def test_changed_on_both_sides_becomes_a_proposal_and_memory_wins_in_the_vault(self):
        self.build();self.copy('Plan.md').write_bytes(b'vault text\n');self.age(self.copy('Plan.md'))
        docs.write_page(self.root,'demo','nb1','Plan.md',b'memory text\n',digest(PAGE))
        result=self.build();self.assertEqual(result['proposed'],1)
        self.assertEqual(self.copy('Plan.md').read_bytes(),b'memory text\n');self.assertEqual(self.file('Plan.md').read_bytes(),b'memory text\n')
        staged=self.ok('proposals','--kind','page','--notebook','nb1')['proposals'];self.assertEqual(len(staged),1)
        self.assertEqual(self.ok('proposals','show',staged[0]['id'])['proposal']['body'],'vault text\n')
        self.assertIn('changed in the vault and in memory',self.status())

    def test_a_new_vault_file_becomes_a_page(self):
        self.build();new=self.copy('Ideas/New idea.md');new.parent.mkdir(parents=True);new.write_bytes(b'# New idea\n');self.age(new)
        self.assertEqual(self.build()['created'],1);self.assertEqual(self.file('Ideas/New idea.md').read_bytes(),b'# New idea\n')
        bad=self.copy('con.md');bad.write_bytes(b'x');self.age(bad);self.build()
        self.assertIn('Notebooks/demo/nb1/con.md',self.status())

    def test_a_vault_deletion_is_a_plan_ticked_on_the_status_page(self):
        self.build();self.copy('Plan.md').unlink()
        self.assertEqual(self.build()['planned'],1);self.assertTrue(self.file('Plan.md').exists())
        page=self.status();self.assertIn('- [ ] Delete page: nb1/Plan.md <!-- lm-page:',page)
        status=self.vault/'Dashboards'/'Sync Status - demo.md';status.write_text(page.replace('- [ ] Delete page','- [x] Delete page'),encoding='utf-8')
        self.assertEqual(self.build()['approved'],1);self.assertFalse(self.file('Plan.md').exists())
        self.assertNotIn('Delete page: nb1/Plan.md',self.status())

    def test_many_copies_missing_at_once_are_restored_not_deleted(self):
        for n in range(6):self.page(f'p{n}.md',f'page {n}\n'.encode())
        self.build();shutil.rmtree(self.vault/'Notebooks')
        result=self.build();self.assertEqual(result['planned'],0);self.assertTrue(self.copy('p3.md').exists())

    def test_a_memory_change_and_deletion_reach_the_vault(self):
        self.build();docs.write_page(self.root,'demo','nb1','Plan.md',b'new\n',digest(PAGE))
        self.build();self.assertEqual(self.copy('Plan.md').read_bytes(),b'new\n')
        docs.delete_page(self.root,'demo','nb1','Plan.md',digest(b'new\n'));self.build();self.assertFalse(self.copy('Plan.md').exists())

    def test_a_notebook_made_private_leaves_the_vault_and_an_edit_is_kept_as_a_proposal(self):
        self.page('Other.md',b'other\n');self.build()
        self.copy('Other.md').write_bytes(b'edited before going private\n');self.age(self.copy('Other.md'))
        docs.update_notebook(self.root,'demo','nb1',private=True)
        self.build()
        self.assertFalse((self.vault/'Notebooks'/'demo'/'nb1').exists())
        staged=docs.proposals(self.root,['demo'],'nb1',include_private=True)['proposals'];self.assertEqual(len(staged),1)
        self.assertNotIn('nb1',self.status().split('## Notebook pages',1)[-1])   # the status page does not name a private notebook

    def test_the_mirror_is_never_indexed_as_a_source(self):
        self.build();project=self.base/'project';project.mkdir(exist_ok=True)
        inner=project/'vault';shutil.move(str(self.vault),str(inner));self.vault=inner;self.build()   # a vault inside the bound folder
        (project/'.git').mkdir(exist_ok=True);(project/'README.md').write_text('# Readme\n\nzebra handbook\n')
        sources.sync(self.root,'demo')
        db=sources._readonly(self.root,'demo')
        try:paths=[p for (p,) in db.execute('SELECT path FROM file')]
        finally:db.close()
        self.assertIn('README.md',paths);self.assertFalse([p for p in paths if p.startswith('vault/Notebooks')])
        self.assertTrue(any(k.endswith(os.path.normcase('vault'+os.sep+'Notebooks')) for k in folder_policy.Policy(self.root).entries))

    def test_agents_may_not_write_the_mirror(self):
        self.build()
        self.assertEqual(runtime.protected_rule(str(self.copy('Plan.md')),str(self.base),self.root),'vault')


if __name__=='__main__':
    import unittest;unittest.main()
