"""The starter instruction files in templates/ ship with the package and take the managed block cleanly."""
import unittest
from pathlib import Path

from core.installer_lifecycle import ROOT_DOCUMENTS
from core.instructions import BLOCK, PLACEHOLDER, instruction_block, merge_block
from core.repository import digest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ('templates/CLAUDE.md', 'templates/AGENTS.md', 'templates/GEMINI.md')


class Templates(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text(encoding='utf-8')

    def test_the_templates_have_the_same_sections(self):
        claude, *others = (self.read(rel) for rel in TEMPLATES)
        self.assertEqual(claude.splitlines()[0], '# CLAUDE.md')
        for rel, text in zip(TEMPLATES[1:], others):
            with self.subTest(rel):
                self.assertEqual(text.splitlines()[0], '# ' + Path(rel).name)
                self.assertEqual(claude.splitlines()[1:], text.splitlines()[1:])
        for marker in ('behavior:begin', 'behavior:end', 'writing-style:begin', 'writing-style:end'):
            self.assertIn(f'<!-- {marker} -->', claude)

    def test_setup_puts_the_block_under_the_first_heading(self):
        for rel in TEMPLATES:
            with self.subTest(rel):
                text = self.read(rel)
                self.assertEqual(BLOCK.findall(text), [])
                lines = text.splitlines()
                self.assertEqual(lines[2:4], ['## Agentic Living Memory', PLACEHOLDER])
                block = instruction_block('claude')
                merged = merge_block(text, block, None, digest)
                self.assertEqual(merged, text.replace(PLACEHOLDER, block, 1))
                self.assertNotIn(PLACEHOLDER, merged)
                # Uninstall keeps the heading, and a later install goes back under it.
                removed = merge_block(merged, block, digest(block.encode()), digest, uninstall=True)
                self.assertEqual(BLOCK.findall(removed), [])
                again = merge_block(removed, block, None, digest)
                self.assertEqual(again.index(block), merged.index(block))

    def test_without_the_heading_the_block_goes_last(self):
        text = '# CLAUDE.md\n\n## Mine\nKeep this.\n'
        block = instruction_block('claude')
        self.assertEqual(merge_block(text, block, None, digest), text + '\n' + block + '\n')
        heading_last = '# CLAUDE.md\n\n## Agentic Living Memory'
        self.assertEqual(merge_block(heading_last, block, None, digest), heading_last + '\n' + block + '\n')

    def test_the_package_ships_the_templates_and_writing_guide(self):
        for rel in TEMPLATES + ('WRITING.md',):
            self.assertIn(rel, ROOT_DOCUMENTS)
            self.assertTrue((ROOT / rel).is_file(), rel)


if __name__ == '__main__':
    unittest.main()
