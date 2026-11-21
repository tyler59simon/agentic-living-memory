"""The starter instruction files in templates/ ship with the package and take the managed block cleanly."""
import unittest
from pathlib import Path

from core.installer_lifecycle import ROOT_DOCUMENTS
from core.instructions import BLOCK, instruction_block, merge_block
from core.repository import digest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ('templates/CLAUDE.md', 'templates/AGENTS.md')


class Templates(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text(encoding='utf-8')

    def test_both_templates_have_the_same_sections(self):
        claude, agents = (self.read(rel) for rel in TEMPLATES)
        self.assertEqual(claude.splitlines()[0], '# CLAUDE.md')
        self.assertEqual(agents.splitlines()[0], '# AGENTS.md')
        self.assertEqual(claude.splitlines()[1:], agents.splitlines()[1:])
        for marker in ('behavior:begin', 'behavior:end', 'writing-style:begin', 'writing-style:end'):
            self.assertIn(f'<!-- {marker} -->', claude)

    def test_setup_appends_the_block_under_the_last_heading(self):
        for rel in TEMPLATES:
            with self.subTest(rel):
                text = self.read(rel)
                self.assertEqual(BLOCK.findall(text), [])
                self.assertTrue(text.rstrip().endswith('## Agentic Living Memory'))
                block = instruction_block('claude')
                merged = merge_block(text, block, None, digest)
                self.assertEqual(merged, text + '\n' + block + '\n')

    def test_the_package_ships_the_templates_and_writing_guide(self):
        for rel in TEMPLATES + ('WRITING.md',):
            self.assertIn(rel, ROOT_DOCUMENTS)
            self.assertTrue((ROOT / rel).is_file(), rel)


if __name__ == '__main__':
    unittest.main()
