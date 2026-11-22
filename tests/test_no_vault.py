"""REG-3: a store with no Obsidian vault, run outside Hivemind, works from session start to session end without writing a vault."""
import contextlib, io, json, os, unittest
from unittest.mock import patch

from test_core import StoreFixture
from core import logs, runtime
from core.cli import main as cli


class NoVaultNoHivemind(StoreFixture):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(['--root', str(self.root), '--domain', 'demo', *argv])
        self.assertEqual((code, err.getvalue()), (0, ''), argv)
        return json.loads(out.getvalue())

    def test_a_session_runs_from_start_to_end_with_no_vault_and_no_hivemind(self):
        environment = {key: value for key, value in os.environ.items() if key != 'HIVEMIND_SESSION'}
        payload = {'cwd': str(self.base / 'project'), 'session_id': 'solo'}
        with patch.dict(os.environ, environment, clear=True):
            self.assertNotIn('HIVEMIND_SESSION', os.environ)
            started = runtime.process('claude', 'session-start', payload, self.root)
            self.assertIn('Living Memory session=', started['context'])
            saved = self.run_cli('capture', '--type', 'knowledge', '--concern', 'architecture', '--title', 'Cache layout', '--summary', 'The local cache is rebuildable.')
            found = self.run_cli('search', 'cache')
            self.assertIn(saved['id'], [row['id'] for row in found['results']])
            self.assertEqual(runtime.process('claude', 'session-end', payload, self.root), {})
        self.assertEqual(logs.summary(self.root)['errors_7d'], 0)
        self.assertEqual(list(self.root.glob('domains/*/sync-state.json')), [])
        self.assertEqual(json.loads((self.root / 'hooks/last-sync.json').read_text(encoding='utf-8'))['results'], [])
        self.assertEqual(list(self.base.rglob('.living-memory')), [])


if __name__ == '__main__':
    unittest.main()
