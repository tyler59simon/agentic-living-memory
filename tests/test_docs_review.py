"""Regressions for the first review of notebook pages (NS-2A, Codex round 1). Each test names its finding: D attachment
staging and undo, P paths, G privacy and hook gates, R moves, B notebook-only domains in restore, N note and storage edges."""
import json,os,shutil,subprocess,sys,zipfile
from pathlib import Path
from unittest.mock import patch
from test_docs import Fixture,PAGE
from test_storage import StorageBase
from core import backup,docs,governance,hookfast,notes,repository,runtime,storage
from core.repository import digest,read_json,recover,transaction

ASSET='domains/demo/notebooks/nb1/assets/abcd1234-x.bin'


def junction(link,target):
    """A directory junction (or symbolic link) at `link`; None when this system can make neither."""
    try:
        os.symlink(target,link,target_is_directory=True);return link
    except OSError:
        if os.name=='nt' and not subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True).returncode:return link
    return None


class Attachments(Fixture):
    def test_d1_a_crash_while_placing_new_bytes_leaves_the_old_ones(self):
        self.notebook();transaction(self.root,{ASSET:b'old'},'doc_attach',None,'d-first')
        with patch('core.repository._copy_into',side_effect=OSError('crash')),patch('core.repository.recover',side_effect=OSError('crash')),self.assertRaises(OSError):
            transaction(self.root,{ASSET:b'new'},'doc_attach',None,'d-over')
        self.assertEqual((self.root/ASSET).read_bytes(),b'old')   # never missing, even mid-operation
        recover(self.root,'d-over','backward');self.assertEqual((self.root/ASSET).read_bytes(),b'old')
        recover(self.root,'d-over','forward') if read_json(self.root/'.operations/d-over/journal.json')['state']=='applying' else None

    def test_d2_an_operation_keeps_both_sides_of_its_attachments(self):
        self.notebook();transaction(self.root,{ASSET:b'old'},'doc_attach',None,'d-a')
        transaction(self.root,{ASSET:b'new'},'doc_attach',None,'d-b')
        blobs=self.root/'.operations'/'d-b'/'blobs'
        self.assertEqual({p.name for p in blobs.iterdir()},{digest(b'old'),digest(b'new')})
        self.assertTrue((self.root/'.operations'/'d-a'/'blobs'/digest(b'old')).is_file())
        governance.undo(self.root,'d-b');self.assertEqual((self.root/ASSET).read_bytes(),b'old')
        self.assertTrue((blobs/digest(b'new')).is_file())

    def test_d3_a_page_saved_after_the_delete_plan_makes_it_stale(self):
        self.notebook();self.page()
        planned=docs.delete_notebook(self.root,'demo','nb1',with_pages=True)
        self.page('later.md',b'written after the plan')
        with self.assertRaisesRegex(ValueError,'stale plan'):governance.apply(self.root,planned['plan_id'],planned['digest'])
        self.assertEqual(self.file('later.md').read_bytes(),b'written after the plan')

    def test_d4_undo_of_a_page_write_is_a_notebook_operation(self):
        self.notebook();self.page()
        written=docs.write_page(self.root,'demo','nb1','a.md',b'new\n',digest(PAGE))
        undone=self.ok('doc','undo',written['operation_id'])
        self.assertTrue(undone['operation_id'].startswith('doc-'))
        self.assertEqual(self.journal(undone['operation_id'])['meta']['notebook'],'nb1')
        self.assertTrue((self.root/'.operations'/undone['operation_id']/'doc.json').is_file())
        shutil.rmtree(self.root/'.operations'/written['operation_id'])   # retention pruned the original
        self.ok('doc','undo',undone['operation_id']);self.assertEqual(self.file('a.md').read_bytes(),b'new\n')


class Paths(Fixture):
    def snapshot_with(self,*names):
        made=self.base/'hand.zip'
        with zipfile.ZipFile(made,'w') as archive:
            for name in names:archive.writestr(name,'x')
        return made

    def test_p1_backup_members_must_be_canonical_page_names(self):
        base='domains/demo/notebooks/nb1/'
        for name in ('./p.md','a /p.md','con.md','a//p.md','sub./p.md'):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'outside the canonical set'):backup.verify(self.snapshot_with(base+name))
        with self.assertRaisesRegex(ValueError,'name the same file'):backup.verify(self.snapshot_with(base+'P.md',base+'p.md'))
        self.assertEqual(backup.verify(self.snapshot_with(base+'Sub/p.md')),[])

    def test_p2_proposals_and_journal_paths_refuse_junctions(self):
        self.notebook();self.notebook('nb2');self.page('p.md',b'one\n',nb='nb2')
        docs.propose(self.root,'demo','nb2','p.md',b'two\n',digest(b'one\n'))
        if not junction(self.file('.proposals'),self.file('.proposals','nb2')):self.skipTest('no symlink or junction')
        self.assertEqual([p['notebook'] for p in docs.proposals(self.root,['demo'])['proposals']],['nb2'])
        self.assertEqual({n['notebook']:n['proposals'] for n in docs.notebooks(self.root,['demo'])['notebooks']},{'nb1':0,'nb2':1})
        with self.assertRaisesRegex(ValueError,'Links and junctions'):repository.confined(self.root,'domains/demo/notebooks/nb1/.proposals/x.json')


class Privacy(Fixture):
    def setUp(self):
        super().setUp()
        self.notebook('secret',private=True);docs.create_page(self.root,'demo','secret','p.md',b'private words\n',include_private=True)

    def test_g1_abbreviated_options_do_not_hide_a_doc_write(self):
        alm=f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}"'
        for text in (f'{alm} doc --dom demo create demo open p.md',f'{alm} doc --dom read write demo open p.md',f'{alm} --dom demo doc write demo open p.md',f'{alm} doc --domain=demo write demo open p.md',
                     f'{alm} doc wipe demo open'):
            with self.subTest(text=text):self.assertTrue(runtime.doc_write(text,None))
        self.assertEqual(runtime._subcommand(['--dom','demo','--root=x','capture']),'capture')
        self.assertFalse(runtime.doc_write(f'{alm} doc --dom demo read demo open p.md',None))
        self.assertFalse(runtime.doc_write(f'{alm} capture doc body --tag subject/project',None))   # a word "doc" in a note is not a doc command

    def test_g2_writes_to_a_private_notebook_need_include_private(self):
        for call in (lambda:docs.write_page(self.root,'demo','secret','p.md',b'x',digest(b'private words\n')),
                     lambda:docs.create_page(self.root,'demo','secret','q.md',b'x'),
                     lambda:docs.update_notebook(self.root,'demo','secret',title='T'),
                     lambda:docs.move(self.root,'demo','secret','p.md','q.md')):
            with self.assertRaisesRegex(ValueError,'not_found'):call()
        code,text=self.fails('doc','delete','demo','secret','p.md','--expected-revision',digest(b'private words\n'));self.assertIn('not_found',text)
        self.ok('doc','delete','demo','secret','p.md','--expected-revision',digest(b'private words\n'),'--include-private')

    def test_g3_page_history_hides_operations_made_while_private(self):
        docs.update_notebook(self.root,'demo','secret',private=False,include_private=True)
        self.assertEqual(docs.history(self.root,'demo','secret','p.md')['history'],[])
        op=docs.history(self.root,'demo','secret','p.md',include_private=True)['history'][0]['operation_id']
        with self.assertRaisesRegex(ValueError,'not_found'):docs.history(self.root,'demo','secret','p.md',version=op)

    def test_g4_inspect_plan_redacts_a_private_notebook_the_plan_brings(self):
        meta=json.dumps({'version':1,'uid':'u'*32,'title':'Incoming','private':True})
        made=governance.plan(self.root,{'domains/demo/notebooks/incoming/notebook.json':meta,'domains/demo/notebooks/incoming/p.md':'INCOMING-SECRET'},'restore')
        shown=governance.inspect_plan(self.root,made['plan_id'])
        self.assertNotIn('INCOMING-SECRET',json.dumps(shown));self.assertEqual(shown['redacted'],2)

    def test_g5_undo_never_shrinks_the_privacy_registry(self):
        self.notebook('later')
        made=docs.update_notebook(self.root,'demo','later',private=True)
        governance.undo(self.root,made['operation_id'])
        self.assertFalse(read_json(self.file('notebook.json','later'))['private'])
        self.assertEqual(sorted(e['notebook'] for e in read_json(self.root/'notebook-privacy.json')['notebooks']),['later','secret'])

    def test_g6_note_history_and_plan_summaries_are_redacted(self):
        private='domains/demo/notebooks/secret/p.md'
        rows=[{'operation_id':'op','files':['domains/demo/notes/n.md',private]}]
        with patch('core.activity.note_history',return_value=rows):
            shown=governance.history(self.root,note_id='n')
        self.assertEqual((shown[0]['files'],shown[0]['redacted']),(['domains/demo/notes/n.md'],1))
        planned=docs.delete_notebook(self.root,'demo','secret',with_pages=True,include_private=True)
        summary=governance.inspect_plan(self.root,planned['plan_id'])['summary'];self.assertNotIn('notebook',summary)
        self.assertEqual(governance.inspect_plan(self.root,planned['plan_id'],include_private=True)['summary']['notebook'],'secret')

    def test_g7_hookfast_hands_store_reads_over_without_a_registry(self):
        (self.root/'notebook-privacy.json').unlink()
        state=self.root/'.turns';state.mkdir(exist_ok=True)
        import hashlib
        (state/(hashlib.sha256(b's1').hexdigest()+'.current.json')).write_text(json.dumps({'repos':{}}))
        payload={'tool_name':'Read','session_id':'s1','cwd':str(self.base),'tool_input':{'file_path':str(self.file('p.md','secret'))}}
        with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):self.assertIsNone(hookfast.quick(payload))

    def test_g8_the_hook_denies_serve_to_agents(self):
        for command in (f'alm --root {self.root} serve --stdio','python living_memory.py serve'):
            payload={'cwd':str(self.base),'session_id':'s1','tool_name':'Bash','tool_input':{'command':command}}
            with self.subTest(command=command),patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(self.root)}):
                self.assertEqual(runtime.process('claude','pre-tool-use',payload,self.root).get('deny'),runtime.SERVE_DENY)


class Moves(Fixture):
    def test_r2_proposals_follow_a_moved_section_and_a_rewritten_base(self):
        self.notebook();self.page('S/a.md',b'a\n');self.page('Old.md',b'old\n');self.page('p.md',b'see [[Old]]\n')
        new=docs.propose(self.root,'demo','nb1','S/new.md',b'brand new\n')['id']
        edit=docs.propose(self.root,'demo','nb1','p.md',b'see [[Old]] and more\n',digest(b'see [[Old]]\n'))['id']
        docs.move(self.root,'demo','nb1','S','T')
        self.assertEqual(docs.show_proposal(self.root,new)['proposal']['path'],'T/new.md')
        docs.move(self.root,'demo','nb1','Old.md','New.md',rewrite_links=True)
        shown=docs.show_proposal(self.root,edit)['proposal']
        self.assertEqual(shown['base_revision'],digest(self.file('p.md').read_bytes()));self.assertEqual(shown['body'],'see [[New]] and more\n')
        docs.accept(self.root,edit);self.assertEqual(self.file('p.md').read_bytes(),b'see [[New]] and more\n')

    def test_r3_every_moved_path_obeys_the_name_rules(self):
        self.notebook();deep='a/'+'/'.join('bcdefg')+'/h.md';self.page(deep,b'x')
        with self.assertRaisesRegex(ValueError,'levels deep'):docs.move(self.root,'demo','nb1','a','x/a')
        self.assertTrue(self.file(deep).exists())

    def test_r4_a_bare_link_to_a_shared_name_is_left_alone(self):
        self.notebook();self.page('A/Note.md',b'a\n');self.page('B/Note.md',b'b\n');self.page('p.md',b'see [[Note]]\n')
        docs.move(self.root,'demo','nb1','A/Note.md','A/Renamed.md',rewrite_links=True)
        self.assertEqual(self.file('p.md').read_bytes(),b'see [[Note]]\n')


class Domains(Fixture):
    def test_b1_a_notebook_only_domain_without_its_folder_still_works(self):
        docs.init_notebook_domain('journal',self.root);shutil.rmtree(self.root/'domains'/'journal')
        docs.create_notebook(self.root,'journal','days','Days')
        self.assertTrue((self.root/'domains/journal/notebooks/days/notebook.json').is_file())
        shutil.rmtree(self.root/'domains'/'journal')
        self.assertTrue(docs.init_notebook_domain('journal',self.root)['existing']);self.assertTrue((self.root/'domains/journal/notebooks').is_dir())

    def test_b2_restore_keeps_a_domain_converted_to_memory_since_the_snapshot(self):
        docs.init_notebook_domain('journal',self.root)
        snapshot=backup.backup(self.root)['backup']
        self.ok('bind','journal',str(self.base/'journal-folder'))
        notes.capture('Kept','A note written after the conversion.',['subject/project','concern/architecture'],'journal',self.root)
        planned=backup.restore_plan(self.root,snapshot)
        if planned.get('status')!='unchanged':governance.apply(self.root,planned['plan_id'],planned['digest'])
        self.assertNotIn('notebook_only',read_json(self.root/'bindings.json')['domains']['journal'])

    def test_n1_no_note_lands_in_a_notebook_only_domain(self):
        docs.init_notebook_domain('journal',self.root)
        with self.assertRaisesRegex(ValueError,'notebooks only'):notes.capture('Title','Body text here.',['subject/project','concern/architecture'],'journal',self.root)
        code,text=self.fails('propose','--session','s1','--title','A draft','--summary','Something worth keeping.',domain='journal')
        self.assertIn('notebooks only',text)


class Relocation(StorageBase):
    def test_n2_pages_named_like_derived_files_are_copied(self):
        self.configure()
        docs.create_notebook(self.root,'demo','nb1','One');docs.create_page(self.root,'demo','nb1','.vectors-plan.md',b'kept')
        assets=self.root/'domains/demo/notebooks/nb1/assets';assets.mkdir()
        transaction(self.root,{'domains/demo/notebooks/nb1/assets/abcd1234-x.relocation-copy':b'kept too'},'doc_attach')
        (self.root/'domains'/'demo'/'.vectors-tmp1').write_bytes(b'temp')
        plan=storage.relocation_plan(self.root,self.base/'new store')
        files=read_json(self.config.parent/'relocations'/(plan['plan_id']+'.plan.json'))['files']
        names=[f.rsplit('/',1)[-1] if isinstance(f,str) else f for f in files]
        self.assertIn('.vectors-plan.md',json.dumps(files));self.assertIn('abcd1234-x.relocation-copy',json.dumps(files))
        self.assertNotIn('.vectors-tmp1',json.dumps(files))


if __name__=='__main__':
    import unittest;unittest.main()
