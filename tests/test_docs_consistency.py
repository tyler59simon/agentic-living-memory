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
    def test_the_maintenance_clause_is_stated_where_writes_are_described_and_the_reference_ships(self):
        from core.instructions import PROTOCOL,TOPICS
        from core.installer_lifecycle import ROOT_DOCUMENTS
        self.assertIn('maintenance the user enabled archives, resolves and supersedes within its policy and records why; restore REF undoes an archive',TOPICS['writes'][1]);self.assertIn('the harness asks the user before delete, merge and retag',PROTOCOL);self.assertNotIn('plans',PROTOCOL)
        for name in ('AGENTS.md','PROTOCOL.md'):
            self.assertIn('maintenance the user enabled'.lower(),(ROOT/name).read_text(encoding='utf-8').lower(),name)
        self.assertNotIn('There are no background engines',(ROOT/'MAINTENANCE.md').read_text(encoding='utf-8'))
        self.assertIn('NOTE_MAINTENANCE.md',ROOT_DOCUMENTS);self.assertTrue((ROOT/'NOTE_MAINTENANCE.md').is_file())

USER_DOCS=sorted([*ROOT.glob('*.md'),*ROOT.glob('adapters/**/*.md'),*ROOT.glob('templates/*.md'),*ROOT.glob('docs/**/*.md')])

def anchors(doc):
    """The GitHub anchors of a Markdown file's headings."""
    text=re.sub(r'```.*?```','',doc.read_text(encoding='utf-8'),flags=re.S)
    return {re.sub(r'[^\w\- ]','',h.strip().lower()).replace(' ','-') for h in re.findall(r'^#{1,6} (.+)$',text,flags=re.M)}

class DocLinks(unittest.TestCase):   # REL-R2
    def test_relative_links_point_at_files_that_exist(self):
        for doc in USER_DOCS:
            text=re.sub(r'```.*?```','',doc.read_text(encoding='utf-8'),flags=re.S)
            for target in re.findall(r'\]\(([^)\s]+)\)',text):
                path,_,anchor=target.partition('#')
                if re.match(r'[a-z]+:',path):continue
                with self.subTest(doc=doc.name,link=target):
                    linked=doc.parent/path if path else doc
                    self.assertTrue(linked.exists(),f'{doc.relative_to(ROOT)} links to missing {target}')
                    if anchor and linked.suffix=='.md':self.assertIn(anchor,anchors(linked),f'{doc.relative_to(ROOT)} links to a missing heading: {target}')

    def test_the_readme_indexes_every_root_doc(self):
        readme=(ROOT/'README.md').read_text(encoding='utf-8')
        for doc in ROOT.glob('*.md'):
            if doc.name in ('README.md','AGENTS.md'):continue   # AGENTS.md is the repo's own agent instructions
            with self.subTest(doc=doc.name):self.assertIn(f']({doc.name})',readme)

    def test_install_docs_do_not_pin_one_python_version(self):
        for doc in [ROOT/'README.md',ROOT/'INSTALLATION.md',ROOT/'INTEGRATION.md',*ROOT.glob('adapters/**/*.md')]:
            with self.subTest(doc=doc.name):self.assertNotIn('py -3.15',doc.read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
