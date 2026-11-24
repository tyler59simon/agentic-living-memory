"""Hosted notebooks are retired (CON-18): the commands that served them fail at once, a store or backup that still holds their files
keeps working, and what folder notebooks need (page tasks in open and SessionStart, the privacy of their operations) stays."""
import json,os,zipfile
from pathlib import Path
from docfixture import Fixture
from core import backup,docs,governance,repository
from core.cli import SERVE_API
from core.repository import digest,read_json,transaction

REMOVED=(['doc','notebooks'],['doc','notebooks','--all-domains','--include-hidden'],['doc','notebook','create','demo','nb1','--title','T'],['doc','notebook','update','demo','nb1','--title','T'],
         ['doc','notebook','delete','demo','nb1'],['doc','list','demo','nb1'],['doc','create','demo','nb1','a.md'],['doc','write','demo','nb1','a.md','--expected-revision','x'],
         ['doc','move','demo','nb1','a.md','b.md'],['doc','delete','demo','nb1','a.md','--expected-revision','x'],['doc','attach','demo','nb1','--from-file','x'],
         ['doc','history','demo','nb1','a.md'],['doc','tasks'],['doc','tasks','--all-domains','--open'],['doc','query','--payload-file','x'],
         ['doc','read','demo','nb1','a.md'],['doc','propose','demo','nb1','a.md'],['domain','create','x','--notebook-only'])


class Commands(Fixture):
    def test_every_removed_command_fails_at_once(self):
        for argv in REMOVED:
            with self.subTest(argv=argv):
                code,_=self.cli(*argv);self.assertEqual(code,2)   # argparse: unknown command or argument
        code,result=self.cli('version');self.assertEqual(code,0)
        self.assertNotIn('domain',result['commands'])

    def test_the_folder_commands_stay(self):
        for argv in (['doc','read','--file','x.md'],['doc','propose','--file','x.md'],['doc','undo','d-1'],['doc','folder','unset','x'],['proposals']):
            with self.subTest(argv=argv):
                code,text=self.cli(*argv);self.assertNotIn('invalid choice',json.dumps(text));self.assertNotIn('unrecognized arguments',json.dumps(text))   # refused for what it names, not for its shape

    def test_search_and_ask_no_longer_take_include_private(self):
        for argv in (['search','x','--pages','--include-private'],['ask','x','--include-private'],['doctor','--include-private']):
            with self.subTest(argv=argv):self.assertEqual(self.cli(*argv)[0],2)

    def test_version_reports_api_4_and_no_hosted_docs_keys(self):
        result=self.ok('version');self.assertEqual(result['api'],4);self.assertEqual(SERVE_API,4)
        self.assertEqual(sorted(result['capabilities']['docs']),['accept_edited','folder_policy','folder_proposals','max_page_bytes','version'])

    def test_domains_list_has_no_notebook_kind_or_page_count(self):
        row=next(r for r in self.ok('domains')['domains'] if r['name']=='demo')
        self.assertNotIn('kind',row);self.assertNotIn('pages',row)


class LegacyBackups(Fixture):
    """A snapshot that holds hosted notebooks still verifies and restores, with those parts dropped."""
    def legacy(self,snapshot):
        old=self.base/'legacy.zip'
        with zipfile.ZipFile(snapshot) as src,zipfile.ZipFile(old,'w') as dst:
            for info in src.infolist():
                if info.filename!='manifest.json':dst.writestr(info,src.read(info))
            dst.writestr('manifest.json',json.dumps({'version':2,'notes':True,'notebooks':['demo/nb1'],'omitted':[{'path':'domains/demo/notebooks/nb1/assets/big.bin','reason':'x','size':1}]}))
            dst.writestr('notebook-privacy.json',json.dumps({'version':1,'notebooks':[{'uid':'u','domain':'demo','notebook':'nb1','since':'2026-01-01T00:00:00Z'}]}))
            dst.writestr('domains/demo/notebooks/nb1/notebook.json',json.dumps({'version':1,'uid':'u','title':'One'}))
            dst.writestr('domains/demo/notebooks/nb1/Sec/p.md','# page\n')
            dst.writestr('domains/demo/notebooks/nb1/assets/abcd1234-pic.png',bytes(range(256)))   # not UTF-8
        return old

    def test_a_legacy_snapshot_verifies_and_restores_without_the_hosted_parts(self):
        self.note();snapshot=backup.backup(self.root)['backup'];old=self.legacy(snapshot)
        self.assertEqual(backup.verify(old),[])
        (self.root/'domains'/'demo'/'notes'/'extra.md').write_text('stray',encoding='utf-8')   # not in the snapshot: the restore deletes it, so there is a plan
        planned=backup.restore_plan(self.root,old);self.assertTrue(governance.apply(self.root,planned['plan_id'],planned['digest'])['ok'])
        self.assertEqual(len(repository.inventory('demo',self.root)),1)
        self.assertFalse((self.root/'notebook-privacy.json').exists());self.assertFalse((self.root/'domains'/'demo'/'notebooks').exists())

    def test_a_member_outside_the_set_still_fails_verify(self):
        self.note();snapshot=backup.backup(self.root)['backup'];bad=self.base/'bad.zip'
        with zipfile.ZipFile(snapshot) as src,zipfile.ZipFile(bad,'w') as dst:
            for info in src.infolist():dst.writestr(info,src.read(info))
            dst.writestr('domains/demo/elsewhere/p.md','x')
        with self.assertRaises(ValueError):backup.verify(bad)

    def test_a_new_snapshot_holds_no_notebook_files(self):
        self.note();folder=self.root/'domains'/'demo'/'notebooks'/'nb1';folder.mkdir(parents=True);(folder/'p.md').write_text('x')   # a leftover of a hosted notebook
        made=backup.backup(self.root)
        with zipfile.ZipFile(made['backup']) as archive:names=archive.namelist()
        self.assertFalse([n for n in names if '/notebooks/' in n]);self.assertNotIn('notebook-privacy.json',names)
        self.assertEqual(json.loads(zipfile.ZipFile(made['backup']).read('manifest.json'))['version'],3)


class LegacyJournals(Fixture):
    """Operations of hosted notebooks keep their label and undo like any text write; one that moved an attachment cannot be undone."""
    def test_a_hosted_page_write_undoes_as_a_plain_operation_and_keeps_its_label(self):
        rel='domains/demo/notebooks/nb1/a.md'
        made=transaction(self.root,{rel:'one\n'},'doc_create',operation_id='doc-'+'a'*32,meta={'domain':'demo','notebook':'nb1','uid':'u'})
        self.assertEqual(made['state'],'committed')
        self.assertEqual(self.journal(made['operation_id'])['kind'],'doc_create')
        self.assertTrue(any(r['operation_id']==made['operation_id'] for r in self.ok('history')))
        self.ok('undo',made['operation_id']);self.assertFalse((self.root/rel).exists())

    def test_an_operation_that_moved_an_attachment_is_refused_clearly(self):
        rel='domains/demo/notebooks/nb1/assets/abcd1234-pic.png';operation='doc-'+'b'*32;folder=self.root/'.operations'/operation;folder.mkdir(parents=True)
        (self.root/rel).parent.mkdir(parents=True);(self.root/rel).write_bytes(b'\x00\x01')
        sha=digest(b'\x00\x01')
        journal={'schema':1,'id':operation,'kind':'doc_attach','created':'2026-01-01T00:00:00Z','state':'committed','effects':[{'path':rel,'blob':True,'before':None,'after':None,'before_hash':None,'after_hash':sha}]}
        (folder/'journal.json').write_text(repository.canonical_json(journal))
        code,text=self.fails('undo',operation);self.assertIn('hosted notebooks were retired',text)

    def test_a_plan_that_stages_an_attachment_cannot_be_applied(self):
        plan_id='c'*32;value={'schema':2,'id':plan_id,'kind':'restore','domains':[],'created':'2026-01-01T00:00:00Z','changes':{'domains/demo/notebooks/nb1/assets/x.bin':{'blob':'f'*64}},
                              'expected':{'domains/demo/notebooks/nb1/assets/x.bin':None},'summary':{'files':1}}
        value['digest']=__import__('hashlib').sha256(repository.canonical_json(value).encode()).hexdigest()
        (self.root/'.plans').mkdir(exist_ok=True);(self.root/'.plans'/(plan_id+'.json')).write_text(repository.canonical_json(value))
        with self.assertRaisesRegex(ValueError,'retired'):governance.apply(self.root,plan_id,value['digest'])


class LeftoverMirrorState(Fixture):
    """The retired Obsidian page mirror left domains/D/notebook-sync.json: nothing reads it, and the next vault sync removes it."""
    def test_sync_removes_it_and_keeps_the_copies_in_the_vault(self):
        vault=self.base/'vault';vault.mkdir();state=self.root/'domains'/'demo'/'notebook-sync.json'
        state.write_text(json.dumps({'version':1,'vault_id':'v1','pages':{},'pending':{}}))
        copy=vault/'Notebooks'/'demo'/'nb1'/'Plan.md';copy.parent.mkdir(parents=True);copy.write_text('# Plan\n')
        self.ok('sync',str(vault))
        self.assertFalse(state.exists());self.assertEqual(copy.read_text(),'# Plan\n')
        self.ok('sync',str(vault))   # nothing left to remove

    def test_a_leftover_state_never_holds_up_plan_pruning(self):
        state=self.root/'domains'/'demo'/'notebook-sync.json'
        state.write_text(json.dumps({'version':1,'pending':{'p.md':{'plan_id':'d'*32}}}))
        (self.root/'.plans').mkdir(exist_ok=True);plan=self.root/'.plans'/('d'*32+'.json');plan.write_text(json.dumps({'created':'2020-01-01T00:00:00Z'}))
        repository.prune_plans(self.root,unapplied_days=1)
        self.assertFalse(plan.exists())   # the old rule kept a plan a mirror deletion waited on


class FolderOperationPrivacy(Fixture):
    """The privacy of folder notebook operations needs no hosted registry: bookkeeping paths and the proposals of private folders."""
    def test_bookkeeping_paths_are_private_and_hosted_paths_are_not(self):
        for path in ('notebook-folders.json','domains/demo/notebook-sync.json','domains/demo/folder-proposals/nb-0123456789/0123456789ab.json','DOMAINS/DEMO/Folder-Proposals/nb-0123456789/0123456789ab.json'):
            self.assertTrue(docs.is_private_path(path),path)
        for path in ('domains/demo/notes/n.md','domains/demo/notebooks/nb1/a.md','notebook-privacy.json'):self.assertFalse(docs.is_private_path(path),path)
        self.assertFalse(hasattr(docs,'private_scope'));self.assertFalse(hasattr(docs,'PRIVACY'))

    def test_note_history_and_plan_summaries_leave_out_bookkeeping(self):
        from unittest.mock import patch
        rows=[{'operation_id':'op','files':['domains/demo/notes/n.md','domains/demo/folder-proposals/nb-0123456789/0123456789ab.json']}]
        with patch('core.activity.note_history',return_value=rows):shown=governance.history(self.root,note_id='n')
        self.assertEqual((shown[0]['files'],shown[0]['redacted']),(['domains/demo/notes/n.md'],1))
        self.assertEqual(docs.redact_summary({'skipped_unreadable':['notebook-folders.json','domains/demo/notes/n.md'],'files':2}),{'skipped_unreadable':['domains/demo/notes/n.md'],'files':2})
        made=governance.plan(self.root,{'notebook-folders.json':json.dumps({'version':1,'folders':[]})},'restore',(),{'files':1,'path':'notebook-folders.json'})
        shown=governance.inspect_plan(self.root,made['plan_id']);self.assertEqual((shown['redacted'],shown['effects'],'path' in shown['summary']),(1,[],False))
        self.assertEqual(len(governance.inspect_plan(self.root,made['plan_id'],include_private=True)['effects']),1)

    def test_batch_items_never_ask_for_private_notebooks(self):
        result=self.ok('batch',stdin=json.dumps([{'argv':['doc','read','--file','x.md','--include-private']}]))
        self.assertNotEqual(result[0]['exit_code'],0);self.assertIn('inside batch',result[0]['error'])

    def test_doc_undo_refuses_operations_that_are_not_notebook_operations(self):
        made=self.ok('capture','--title','A note','--summary','Plain note.','--subject','project','--concern','architecture')
        code,text=self.fails('doc','undo',made['operation_id']);self.assertIn('not a notebook operation',text)


if __name__=='__main__':
    import unittest;unittest.main()
