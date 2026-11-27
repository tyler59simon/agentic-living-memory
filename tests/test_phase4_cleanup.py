"""Regression tests for Phase 4 cleanup, deduplication, module splits, and style (REL-D1 to REL-D4)."""
from __future__ import annotations
import ast
import io
import json
import tempfile
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _top_level_semicolons(path: Path) -> int:
    src = path.read_text(encoding='utf-8')
    depth = 0
    line_start_depth = {}
    count = 0
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING):
            if tok.start[0] not in line_start_depth:
                line_start_depth[tok.start[0]] = depth
        if tok.type == tokenize.OP:
            if tok.string in '([{':
                depth += 1
            elif tok.string in ')]}':
                depth = max(0, depth - 1)
            elif tok.string == ';' and depth == 0 and line_start_depth.get(tok.start[0], 0) == 0:
                count += 1
    return count


class Phase4CleanupTests(unittest.TestCase):
    def test_no_utf8_bom_in_core_and_hooks(self):
        with_bom = []
        for folder in (ROOT / 'core', ROOT / 'hooks'):
            for py in sorted(folder.glob('*.py')):
                if py.read_bytes().startswith(b'\xef\xbb\xbf'):
                    with_bom.append(py.relative_to(ROOT).as_posix())
        self.assertEqual(with_bom, [])

    def test_dead_copilot_setup_removed_and_root_documents_exist(self):
        from core import installer_lifecycle
        self.assertFalse((ROOT / 'integrations' / 'copilot' / 'setup.py').exists())
        missing = [name for name in installer_lifecycle.ROOT_DOCUMENTS if not (ROOT / name).exists()]
        self.assertEqual(missing, [])

    def test_extracted_modules_and_runtime_reexports(self):
        from core import background, prompt_context, runtime, shell_guard, signals, web_assets
        self.assertTrue(callable(background.spawn_detached))
        self.assertTrue(callable(background.session_gate))
        self.assertTrue(callable(background.in_cooldown))
        self.assertTrue(callable(background.touch_cooldown))
        self.assertTrue(callable(web_assets.script_json))
        self.assertTrue(callable(web_assets.out_path))
        self.assertIn('data:image/svg+xml;base64,', web_assets.ALM_LOGO_SVG)
        for name in ('memory_subcommand', 'readonly_shell', 'writing_shell_text', 'hivemind_governed', 'auto_allowed'):
            self.assertIs(getattr(runtime, name), getattr(shell_guard, name))
        for name in ('signals_of', 'signal_of', 'durable_signal', 'settle_signals', 'signal_nudge'):
            self.assertIs(getattr(runtime, name), getattr(signals, name))
        for name in ('context', 'scope_domains', 'parent_domains', 'child_domains', 'scoped_file_hits', 'hit_line'):
            self.assertIs(getattr(runtime, name), getattr(prompt_context, name))

    def test_cli_command_handlers_table(self):
        from core import cli
        self.assertIsInstance(cli._COMMAND_HANDLERS, dict)
        for cmd in ('bind', 'config', 'capture', 'propose', 'search', 'read', 'update', 'review', 'open', 'views', 'wiki', 'maintenance', 'semantic', 'batch', 'doc', 'undo', 'recover', 'diff'):
            self.assertIn(cmd, cli._COMMAND_HANDLERS)
            self.assertTrue(callable(cli._COMMAND_HANDLERS[cmd]))

    def test_repository_journal_state_and_load_tags(self):
        from core.repository import journal_state
        from core.tag_taxonomy import load_tags
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            j = p / 'journal.json'
            with self.assertRaises(FileNotFoundError):
                journal_state(j)
            j.write_text(json.dumps({'state': 'committed'}), encoding='utf-8')
            self.assertEqual(journal_state(j), 'committed')
            j.write_text('{bad json', encoding='utf-8')
            with self.assertRaises(ValueError):
                journal_state(j)
            (p / 'tag_taxonomy.json').write_text(json.dumps({'tags': ['subject/core', 'concern/rule']}), encoding='utf-8')
            self.assertEqual(load_tags(p), ['subject/core', 'concern/rule'])

    def test_unpacked_modules_have_no_top_level_semicolons(self):
        targets = [
            ROOT / 'core' / 'background.py',
            ROOT / 'core' / 'web_assets.py',
            ROOT / 'core' / 'shell_guard.py',
            ROOT / 'core' / 'signals.py',
            ROOT / 'core' / 'prompt_context.py',
            ROOT / 'core' / 'runtime.py',
            ROOT / 'core' / 'cli.py',
            *sorted((ROOT / 'hooks').glob('*.py')),
        ]
        packed = {}
        for py in targets:
            ast.parse(py.read_text(encoding='utf-8'))
            n = _top_level_semicolons(py)
            if n:
                packed[py.relative_to(ROOT).as_posix()] = n
        self.assertEqual(packed, {})


if __name__ == '__main__':
    unittest.main()
