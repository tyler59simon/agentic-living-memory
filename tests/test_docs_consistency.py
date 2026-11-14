"""The docs that list the note types name every type of core.note_types.NOTE_TYPES."""
import re,unittest
from pathlib import Path
from core.note_types import NOTE_TYPES

ROOT=Path(__file__).resolve().parent.parent

class NoteTypeLists(unittest.TestCase):
    def test_each_doc_lists_every_note_type(self):
        for name in ('README.md','ARCHITECTURE.md','SCRIPT_REFERENCE.md'):
            with self.subTest(doc=name):
                lines=[l for l in (ROOT/name).read_text(encoding='utf-8').splitlines() if 'knowledge, decision, preference' in l]
                self.assertEqual(len(lines),1,'expected one line listing the note types')
                listed=set(re.findall(r'[a-z]+',lines[0].split('knowledge, decision',1)[1].split('.',1)[0]))|{'knowledge','decision'}
                self.assertFalse(set(NOTE_TYPES)-listed,'missing: '+', '.join(sorted(set(NOTE_TYPES)-listed)))

class MaintenanceDocs(unittest.TestCase):
    def test_the_archive_exception_is_stated_wherever_archive_needs_a_plan_and_the_reference_ships(self):
        from core.instructions import PROTOCOL
        from core.installer_lifecycle import ROOT_DOCUMENTS
        self.assertIn('maintenance the user enabled archives, resolves and supersedes within its policy and records why; restore REF undoes an archive',PROTOCOL)
        for name in ('AGENTS.md','PROTOCOL.md'):
            self.assertIn('maintenance the user enabled'.lower(),(ROOT/name).read_text(encoding='utf-8').lower(),name)
        self.assertNotIn('There are no background engines',(ROOT/'MAINTENANCE.md').read_text(encoding='utf-8'))
        self.assertIn('NOTE_MAINTENANCE.md',ROOT_DOCUMENTS);self.assertTrue((ROOT/'NOTE_MAINTENANCE.md').is_file())

if __name__=='__main__':unittest.main()
