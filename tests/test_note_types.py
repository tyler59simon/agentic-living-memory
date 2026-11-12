import json
from pathlib import Path
from urllib.parse import quote

from test_core import StoreFixture
from core.notes import capture,change,find_note,slug
from core.repository import inventory,parse,serialize,reconcile
from core.governance import apply,undo
from core.migration import note_type_plan
from core.note_types import NOTE_TYPES
from core.search import search_domain


class NoteTypes(StoreFixture):
    def apply(self, plan):
        return apply(self.root,plan['plan_id'],plan['digest'])

    def legacy(self,title='Old note',prefix='NOTE',selected='knowledge',status='active',body='Legacy body'):
        result=self.note(title,body)
        path,note=find_note(result['id'],'demo',self.root)
        note.update(schema_version=2,prefix=prefix,type=selected,status=status)
        target=self.root/'domains/demo'/('notes' if status=='active' else 'archive')/f"{prefix} - {slug(title)}-{note['id'][:8]}.md"
        path.unlink();target.write_text(serialize(note),encoding='utf-8')
        reconcile('demo',self.root)
        return target,parse(target)

    def test_all_types_derive_prefix_without_storing_it(self):
        (self.base/'project').mkdir(exist_ok=True)
        for selected in NOTE_TYPES:
            # A codebase map names the repo folder it describes, a brief the project folder; '.' is the bound project folder.
            result=self.note(selected,'Unique content '+selected,type=selected,**({'files':['.']} if selected in ('map','brief') else {}))
            path,note=find_note(result['id'],'demo',self.root)
            self.assertTrue(path.name.startswith(selected.upper()+' - '))
            self.assertEqual(note['prefix'],selected.upper())
            self.assertEqual(note['schema_version'],3)
            self.assertNotIn('\nprefix:',path.read_text(encoding='utf-8'))

    def test_mismatched_prefix_and_duplicate_type_are_rejected(self):
        for selected,prefix in [('bug','NOTE'),('decision','BUG'),('knowledge','DECISION')]:
            with self.assertRaisesRegex(ValueError,'derived'):
                self.note(type=selected,prefix=prefix)
        note=self.note(type='bug',prefix='BUG')
        with self.assertRaisesRegex(ValueError,'different type'):
            self.note(type='decision')
        with self.assertRaisesRegex(ValueError,'Invalid note type'):
            self.note(type='unknown')
        self.assertEqual(len(inventory('demo',self.root)),1)

    def test_merged_types_are_readable_but_cannot_be_captured(self):
        for selected in ('progress','correction'):
            with self.assertRaisesRegex(ValueError,'Invalid note type'):
                self.note(selected,'New capture '+selected,type=selected)
        progress,first=self.legacy('Earlier milestone','PROGRESS','progress')
        note=parse(progress);note['schema_version']=3;progress.write_text(serialize(note),encoding='utf-8')
        correction,second=self.legacy('Earlier correction','NOTE','correction')
        self.assertEqual(parse(progress)['type'],'progress')
        self.assertEqual(parse(correction)['type'],'correction')
        proposal=note_type_plan(self.root)
        self.apply(proposal)
        status_path,status=find_note(first['id'],'demo',self.root)
        knowledge_path,knowledge=find_note(second['id'],'demo',self.root)
        self.assertEqual((status['type'],status['prefix'],status['status']),('status','STATUS','active'))
        self.assertEqual((knowledge['type'],knowledge['prefix']),('knowledge','KNOWLEDGE'))
        self.assertEqual(note_type_plan(self.root)['summary']['notes'],0)

    def test_bug_lifecycle_uses_status_and_stable_prefix(self):
        result=self.note(type='bug')
        for action,status,directory in [('resolve','resolved','archive'),('reopen','active','notes')]:
            self.apply(change(result['id'],'demo',action,self.root,plan_only=True))
            path,note=find_note(result['id'],'demo',self.root)
            self.assertEqual((note['type'],note['prefix'],note['status']),('bug','BUG',status))
            self.assertEqual(path.parent.name,directory)
        regular=self.note('Regular','A general fact')
        with self.assertRaisesRegex(ValueError,'Only active plan, bug and risk'):
            change(regular['id'],'demo','resolve',self.root,plan_only=True)

    def test_type_update_is_reviewed_and_preserves_identity(self):
        result=self.note(type='decision')
        old,note=find_note(result['id'],'demo',self.root)
        created=note['created']
        proposal=change(result['id'],'demo','update',self.root,plan_only=True,type='bug')
        self.assertTrue(old.exists())
        self.assertEqual(find_note(result['id'],'demo',self.root)[1]['type'],'decision')
        self.apply(proposal)
        path,note=find_note(result['id'],'demo',self.root)
        self.assertFalse(old.exists())
        self.assertEqual((note['type'],note['created']),('bug',created))
        self.assertTrue(path.name.startswith('BUG - '))
        self.assertEqual(search_domain('cache','demo',root=self.root,filters={'type':'bug'})[0]['id'],result['id'])

    def test_legacy_bug_resolve_and_reopen_promote_type(self):
        for prefix,status,action in [('BUG','active','resolve'),('RESOLVED','resolved','reopen')]:
            path,note=self.legacy(prefix,prefix,'knowledge',status)
            self.apply(change(note['id'],'demo',action,self.root,plan_only=True))
            target,updated=find_note(note['id'],'demo',self.root)
            self.assertEqual((updated['type'],updated['prefix']),('bug','BUG'))

    def test_migration_preserves_state_and_rewrites_local_links(self):
        target,first=self.legacy('Issue','RESOLVED','knowledge','resolved')
        rel='../archive/'+target.name
        ref,second=self.legacy('Choice','NOTE','decision',body='See [issue](<'+rel+'#details> "Details") and [encoded]('+quote(rel,safe='/')+').\n[issue-ref]: <'+rel+'> "Reference title"')
        before={n['id']:(n['created'],n['status'],n['tags']) for n in inventory('demo',self.root,retired=True)}
        proposal=note_type_plan(self.root)
        self.assertEqual(proposal['summary']['notes'],2)
        self.assertTrue(target.exists());self.assertTrue(ref.exists())
        self.apply(proposal)
        new_target,bug=find_note(first['id'],'demo',self.root)
        new_ref,decision=find_note(second['id'],'demo',self.root)
        self.assertEqual((bug['type'],bug['status'],bug['prefix']),('bug','resolved','BUG'))
        self.assertTrue(new_ref.name.startswith('DECISION - '))
        self.assertIn('../archive/'+new_target.name+'#details',decision['body'])
        self.assertIn(quote('../archive/'+new_target.name,safe='/'),decision['body'])
        self.assertIn('[issue-ref]: <../archive/'+new_target.name+'> "Reference title"',decision['body'])
        after={n['id']:(n['created'],n['status'],n['tags']) for n in inventory('demo',self.root,retired=True)}
        self.assertEqual(before,after)
        self.assertEqual(note_type_plan(self.root)['summary']['notes'],0)

    def test_migration_is_revision_checked_and_undoable(self):
        path,note=self.legacy(selected='preference')
        before=path.read_bytes()
        proposal=note_type_plan(self.root)
        self.apply(proposal)
        self.apply(undo(self.root,proposal['plan_id']))
        self.assertEqual(path.read_bytes(),before)
        proposal=note_type_plan(self.root)
        path.write_text(path.read_text(encoding='utf-8').replace('Legacy body','Modified body'),encoding='utf-8')
        with self.assertRaises(ValueError):self.apply(proposal)

    def test_single_note_rename_cannot_override_prefix(self):
        note=self.note(type='context')
        with self.assertRaisesRegex(ValueError,'derived'):
            change(note['id'],'demo','rename',self.root,plan_only=True,title='Renamed',prefix='NOTE')

    def test_legacy_deletion_does_not_retarget_links(self):
        old,note=self.legacy()
        referring=self.note('Reference','See [old]('+old.name+').')
        self.apply(change(note['id'],'demo','delete',self.root,plan_only=True))
        self.assertIn(old.name,find_note(referring['id'],'demo',self.root)[1]['body'])

    def test_overlay_capture_uses_type_instead_of_overlay_filename(self):
        import uuid
        from core.sync import sync
        original=self.note('Existing','Original note')
        _,note=find_note(original['id'],'demo',self.root)
        note.update(id=str(uuid.uuid4()),type='bug',title='From overlay',body='An issue entered in the overlay')
        vault=self.base/'vault';incoming=vault/'Domains/demo/arbitrary-name.md'
        incoming.parent.mkdir(parents=True)
        incoming.write_text(serialize(note),encoding='utf-8')
        results=sync('demo',vault,self.root)
        self.assertEqual(results['plans'],[]);self.assertEqual(len(results['applied']),1)
        path,note=find_note(note['id'],'demo',self.root)
        self.assertEqual((note['type'],note['prefix']),('bug','BUG'))
        self.assertTrue(path.name.startswith('BUG - '))
