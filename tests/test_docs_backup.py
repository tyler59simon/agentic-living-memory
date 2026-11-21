"""Notebooks in snapshots, restore and relocation (NS-2A): pages and attachments are backed up and restored byte for byte, a
snapshot that does not cover a notebook never touches it, automatic snapshots keep to an attachment budget, note counts
count notes only, and relocation copies notebooks and leaves out every derived index."""
import json,os,zipfile
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from test_storage import StorageBase
from core import backup,docs,storage
from core.governance import apply,inspect_plan
from core.repository import digest,read_json
from core.init_domain import init_domain

PAGE=b'\xef\xbb\xbfLine\r\nno newline at end'
PICTURE=bytes(range(256))*16


class Snapshots(StoreFixture):
    def setUp(self):
        super().setUp()
        docs.create_notebook(self.root,'demo','nb1','One');docs.create_page(self.root,'demo','nb1','Sec/p.md',PAGE)
        self.picture=self.base/'pic.png';self.picture.write_bytes(PICTURE)
        self.asset=docs.attach(self.root,'demo','nb1',self.picture)['path']
        self.nb=self.root/'domains'/'demo'/'notebooks'/'nb1'
    def restore(self,snapshot):
        planned=backup.restore_plan(self.root,snapshot)
        if planned.get('status')=='unchanged':return planned
        return apply(self.root,planned['plan_id'],planned['digest'])

    def test_snapshot_holds_notebooks_with_a_manifest(self):
        self.note()
        made=backup.backup(self.root)
        self.assertEqual((made['notes'],made['notebooks'],made['pages']),(1,1,1))
        with zipfile.ZipFile(made['backup']) as archive:
            manifest=json.loads(archive.read('manifest.json'))
            self.assertEqual((manifest['version'],manifest['notebooks'],manifest['omitted']),(2,['demo/nb1'],[]))
            info=archive.getinfo(f'domains/demo/notebooks/nb1/{self.asset}')
            self.assertEqual(info.compress_type,zipfile.ZIP_STORED)
            self.assertEqual(archive.read('domains/demo/notebooks/nb1/Sec/p.md'),PAGE)
        self.assertEqual(backup._note_count(Path(made['backup'])),1)
        self.assertEqual(backup.verify(made['backup']),[])

    def test_restore_brings_pages_and_attachments_back(self):
        snapshot=backup.backup(self.root)['backup']
        (self.nb/'Sec'/'p.md').write_bytes(b'mangled');(self.nb/self.asset).unlink()
        docs.create_page(self.root,'demo','nb1','later.md',b'after the snapshot')
        planned=backup.restore_plan(self.root,snapshot)
        shown=inspect_plan(self.root,planned['plan_id'])
        binary=[e for e in shown['effects'] if e.get('binary')];self.assertEqual(len(binary),1);self.assertEqual(binary[0]['sha256'],digest(PICTURE))
        apply(self.root,planned['plan_id'],planned['digest'])
        self.assertEqual((self.nb/'Sec'/'p.md').read_bytes(),PAGE);self.assertEqual((self.nb/self.asset).read_bytes(),PICTURE)
        self.assertFalse((self.nb/'later.md').exists())

    def test_a_version_1_snapshot_leaves_notebooks_alone(self):
        snapshot=backup.backup(self.root)['backup'];old=self.base/'old.zip'
        with zipfile.ZipFile(snapshot) as src,zipfile.ZipFile(old,'w') as dst:
            for info in src.infolist():
                if info.filename!='manifest.json' and '/notebooks/' not in info.filename:dst.writestr(info,src.read(info))
        before={p:p.read_bytes() for p in self.nb.rglob('*') if p.is_file()}
        self.restore(old)
        self.assertEqual({p:p.read_bytes() for p in self.nb.rglob('*') if p.is_file()},before)

    def test_a_notebook_made_after_the_snapshot_is_kept(self):
        snapshot=backup.backup(self.root)['backup']
        docs.create_notebook(self.root,'demo','nb2','Two');docs.create_page(self.root,'demo','nb2','x.md',b'kept')
        self.restore(snapshot)
        self.assertEqual((self.root/'domains/demo/notebooks/nb2/x.md').read_bytes(),b'kept')

    def test_automatic_snapshots_keep_to_the_attachment_budget(self):
        made=backup.backup(self.root,budget=10)
        self.assertEqual([o['path'] for o in made['omitted']],[f'domains/demo/notebooks/nb1/{self.asset}']);self.assertIn('left out',made['warning'])
        (self.nb/'Sec'/'p.md').write_bytes(b'changed')
        self.restore(made['backup'])
        self.assertEqual((self.nb/'Sec'/'p.md').read_bytes(),PAGE);self.assertEqual((self.nb/self.asset).read_bytes(),PICTURE)
        with patch.object(backup,'ASSET_BUDGET',10),patch.object(backup,'INTERVAL_HOURS',0):
            self.assertTrue(backup.maybe_backup(self.root)['omitted'])

    def test_verify_routes_members_by_path(self):
        made=self.base/'hand.zip'
        with zipfile.ZipFile(made,'w') as archive:
            archive.writestr('domains/demo/notebooks/nb1/p.md',b'\xff not utf-8')
            archive.writestr('domains/demo/notebooks/nb1/notebook.json','{bad')
            archive.writestr('domains/demo/notebooks/nb1/assets/abcd1234-x.bin',b'\x00\xff anything')
            archive.writestr('domains/demo/notebooks/nb1/free text.md','no frontmatter is fine for a page')
        problems={p['member']:p['problem'] for p in backup.verify(made)}
        self.assertEqual(problems,{'domains/demo/notebooks/nb1/p.md':'not UTF-8','domains/demo/notebooks/nb1/notebook.json':'invalid JSON'})
        with zipfile.ZipFile(made,'a') as archive:archive.writestr('domains/demo/notebooks/nb1/other/x.txt','stray')
        with self.assertRaisesRegex(ValueError,'outside the canonical set'):backup.verify(made)

    def test_restore_keeps_a_notebook_only_domain_the_snapshot_lacks(self):
        snapshot=backup.backup(self.root)['backup']
        docs.init_notebook_domain('journal',self.root);docs.create_notebook(self.root,'journal','days','Days');docs.create_page(self.root,'journal','days','d.md',b'x')
        (self.nb/'Sec'/'p.md').write_bytes(b'changed')
        self.restore(snapshot)
        self.assertTrue(read_json(self.root/'bindings.json')['domains']['journal']['notebook_only'])
        self.assertEqual((self.root/'domains/journal/notebooks/days/d.md').read_bytes(),b'x')

    def test_privacy_registry_only_grows_on_restore(self):
        snapshot=backup.backup(self.root)['backup']
        docs.create_notebook(self.root,'demo','secret','S',private=True)
        self.restore(snapshot)
        self.assertEqual([e['notebook'] for e in read_json(self.root/'notebook-privacy.json')['notebooks']],['secret'])


class Relocation(StorageBase):
    def test_notebooks_move_and_derived_indexes_stay_behind(self):
        self.configure();self.note()
        docs.create_notebook(self.root,'demo','nb1','One');docs.create_page(self.root,'demo','nb1','p.md',PAGE)
        docs.init_notebook_domain('journal',self.root);docs.create_notebook(self.root,'journal','days','Days');docs.create_page(self.root,'journal','days','d.md',b'x')
        for name in ('docs.sqlite3','docs.sqlite3-wal','sources.sqlite3-wal','vectors.sqlite3-shm'):(self.root/'domains'/'demo'/name).write_bytes(b'derived')
        plan=storage.relocation_plan(self.root,self.base/'new shared store')
        self.assertEqual(plan['notes'],1)
        files=read_json(self.config.parent/'relocations'/(plan['plan_id']+'.plan.json'))['files']
        self.assertFalse([f for f in files if f.rsplit('/',1)[-1] in ('docs.sqlite3','docs.sqlite3-wal','sources.sqlite3-wal','vectors.sqlite3-shm')])
        self.apply(plan)
        target=Path(plan['destination'])
        self.assertEqual((target/'domains/demo/notebooks/nb1/p.md').read_bytes(),PAGE)
        self.assertEqual((target/'domains/journal/notebooks/days/d.md').read_bytes(),b'x')
        self.assertFalse((target/'domains/journal/notes').exists())


if __name__=='__main__':
    import unittest;unittest.main()
