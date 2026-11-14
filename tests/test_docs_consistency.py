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

if __name__=='__main__':unittest.main()
