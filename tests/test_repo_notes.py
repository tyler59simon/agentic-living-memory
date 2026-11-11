"""Repo briefing: one-line summaries of a repo's decisions, bugs, runbooks and policies after its map or its brief
(core/maps.repo_notes, runtime.prompt_maps and prompt_briefs)."""
import contextlib, io, json, unittest

from test_core import StoreFixture
from core import runtime, usage
from core.cli import main
from core.common import load_bindings
from core.maps import _rows, find_map, repo_notes
from core.notes import capture, short_ref

TAGS = ['subject/project', 'concern/decision']


class RepoNotes(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root / 'tag_taxonomy.json').write_text(json.dumps({'tags': ['subject/project', 'concern/architecture', 'concern/decision', 'concern/knowledge']}))
        self.project = self.base / 'project'
        for repo in ('suite', 'hive'):
            (self.project / repo / '.git').mkdir(parents=True)
        self.suite = (self.project / 'suite').resolve()
        self.payload = {'cwd': str(self.suite), 'session_id': 's1'}

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--root', str(self.root), *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def save(self, title, summary, kind='decision', files=None, **extra):
        made = capture(title, summary, TAGS, 'demo', self.root, type=kind, allow_similar=True, **({'files': files} if files else {}), **extra)
        return 'demo:' + made['id']

    def capture_map(self, *extra):
        code, result = self.cli('--domain', 'demo', 'capture', '--type', 'map', '--file', str(self.suite), '--title', 'suite codebase map',
                                '--summary', 'A suite of tools.', '--detail', '**Layout:** src/ holds the code.', *extra)
        self.assertEqual(code, 0, result)
        return 'demo:' + result['id']

    def hit(self):
        return find_map(self.suite, self.root, load_bindings(self.root))

    def lines(self, **kwargs):
        shown = kwargs.pop('shown', {})
        text, refs = repo_notes(self.hit(), self.suite, self.root, load_bindings(self.root), shown, **kwargs)
        return text.splitlines(), refs

    def hook(self, event, **extra):
        return runtime.process('claude', event, {**self.payload, **extra}, self.root)

    def prompt(self, text):
        return self.hook('user-prompt', prompt=text).get('context', '')

    def test_lists_notes_whose_files_fall_inside_the_repo(self):
        inside = self.save('Money is BigDecimal', 'Amounts use BigDecimal with scale 4. Never double.', files=['suite/src/money.py'])
        bug = self.save('Parser drops a trailing newline', 'The parser loses the last newline. Pad the input.', 'bug', files=['suite'])
        runbook = self.save('Release the suite', 'Tag, build, publish.', 'runbook', files=['suite/docs'])
        policy = self.save('No force pushes', 'Never force push to main.', 'policy', files=['suite'])
        self.save('Hive uses queues', 'Another repo entirely.', files=['hive/src'])
        self.save('Suite orientation', 'Background knowledge.', 'knowledge', files=['suite'])   # not a briefing type
        self.save('No files at all', 'A decision about nothing in particular.')
        self.capture_map()
        lines, refs = self.lines()
        self.assertTrue(lines[0].startswith('Notes about this repo'))
        self.assertCountEqual(refs, [inside, bug, runbook, policy])
        by_ref = {ref: line for ref, line in zip(refs, lines[1:])}
        self.assertEqual(by_ref[inside], f"- DECISION {short_ref(*inside.split(':'))} Money is BigDecimal — Amounts use BigDecimal with scale 4.")
        self.assertTrue(by_ref[bug].startswith('- BUG ') and by_ref[bug].endswith('— The parser loses the last newline.'))
        self.assertTrue(by_ref[runbook].startswith('- RUNBOOK ') and by_ref[policy].startswith('- POLICY '))

    def test_follows_the_maps_links_whatever_their_files(self):
        linked = self.save('Why we picked SQLite', 'A single file beats a server here.')   # no files: only the link finds it
        self.save('Unlinked decision', 'Nothing points here.')
        self.capture_map('--link', '[[Why we picked SQLite]]', '--link', 'https://example.com/spec')
        lines, refs = self.lines()
        self.assertEqual(refs, [linked])
        self.assertIn('Why we picked SQLite — A single file beats a server here.', lines[1])

    def test_newest_first_within_the_budget_and_one_line_each(self):
        refs = [self.save(f'Decision {i} about the suite', 'A reasonably long summary sentence that runs on and on, well past one hundred characters, so that it has to be cut. More.', files=['suite']) for i in range(12)]
        self.capture_map()
        lines, listed = self.lines()
        text = '\n'.join(lines)
        self.assertLessEqual(len(text), 900)
        self.assertLess(len(listed), 12)
        self.assertEqual(listed, refs[::-1][:len(listed)])   # the newest ones survive the cut
        self.assertTrue(all(len(l) < 220 and ' …' in l for l in lines[1:]), lines)
        short_lines, short = self.lines(budget=300)
        self.assertLessEqual(len('\n'.join(short_lines)), 300)
        self.assertEqual(self.lines(budget=40), ([], []))   # not even the heading and one line fit

    def test_notes_already_shown_or_excluded_are_left_out(self):
        a = self.save('Money is BigDecimal', 'Amounts use BigDecimal.', files=['suite'])
        b = self.save('Queues are bounded', 'Every queue has a cap.', files=['suite'])
        self.capture_map()
        revision = next(n for n in _rows('demo', self.root, ('decision',)) if 'demo:' + n['id'] == a)['revision']
        self.assertEqual(self.lines(shown={a: revision[:12]})[1], [b])
        self.assertIn(a, self.lines(shown={a: 'older'})[1])   # a new revision of a shown note is listed again
        self.assertEqual(self.lines(exclude={a, b})[1], [])
        self.assertEqual(self.lines(shown=[a])[1], [b])   # the legacy list form of shown works too

    def test_a_prompt_gets_the_summaries_after_the_map_once_and_records_the_usage_kind(self):
        a = self.save('Publish a build', 'Tag, build, publish. Then announce it.', 'runbook', files=['suite'])
        self.capture_map()
        self.hook('session-start')
        text = self.prompt('add a feature to suite')
        self.assertLess(text.index('Codebase map for suite'), text.index('Notes about this repo'))
        self.assertIn('Publish a build — Tag, build, publish.', text)
        self.assertNotIn('Publish a build', self.prompt('another feature for suite'))
        kinds = {(r['ref'], r['k']) for r in usage.events(self.root) if r['e'] == 'inject'}
        self.assertIn((a, 'map-brief'), kinds)
        self.assertIn('map-brief', usage.STANDING)
        # The one-liner is not a delivery of the note: retrieval can still inject it with its snippet.
        self.assertIn('Publish a build', self.prompt('how do I publish a build'))

    def test_a_decision_shown_as_guidance_and_a_gotcha_shown_at_session_start_are_not_repeated(self):
        self.save('Never run mvnw.cmd from Git Bash', 'It exits 0 and builds nothing. Run ./mvnw instead.', 'bug', files=['suite'])
        self.save('Money is BigDecimal', 'Amounts use BigDecimal.', files=['suite'])
        self.save('Release the suite', 'Tag, build, publish.', 'runbook', files=['suite'])
        self.capture_map()
        start = self.hook('session-start')['context']
        self.assertIn('Known gotchas', start)
        self.assertIn('Money is BigDecimal', start)
        text = self.prompt('add a feature to suite')
        self.assertIn('Release the suite', text)
        self.assertNotIn('Never run mvnw.cmd', text)
        self.assertNotIn('Money is BigDecimal', text)

    def test_the_project_brief_lists_the_notes_it_links_to(self):
        linked = self.save('Why a ledger rebuild', 'The legacy ledger cannot reconcile.', 'runbook')
        in_map = self.save('Money is BigDecimal', 'Amounts use BigDecimal.', 'runbook', files=['suite'])
        only_brief = self.save('Ledger scope', 'Billing and ledger only.', 'runbook')
        self.save('Unlinked decision', 'Nothing points here.', 'runbook')
        self.capture_map('--link', '[[Why a ledger rebuild]]')
        code, brief = self.cli('--domain', 'demo', 'capture', '--type', 'brief', '--file', '.', '--title', 'project brief', '--summary', 'A ledger rebuild.',
                               '--detail', '**Goals and scope:** Replace the legacy ledger.', '--link', '[[Why a ledger rebuild]]', '--link', '[[Money is BigDecimal]]',
                               '--link', '[[Ledger scope]]')
        self.assertEqual(code, 0, brief)
        self.hook('session-start')
        text = self.prompt('add a feature to suite')
        self.assertIn('Project brief —', text)
        self.assertEqual(text.count('Notes about this repo'), 2)   # one list after the brief, one after the map
        brief_part, map_part = text.split('Codebase map for suite')
        self.assertIn('Ledger scope — Billing and ledger only.', brief_part)
        self.assertNotIn('Why a ledger rebuild —', brief_part)   # the map lists these two, so the brief does not repeat them
        self.assertNotIn('Money is BigDecimal —', brief_part)
        self.assertIn('Why a ledger rebuild —', map_part)
        self.assertIn('Money is BigDecimal —', map_part)
        self.assertNotIn('Unlinked decision', text)
        refs = [r['ref'] for r in usage.events(self.root) if r['e'] == 'inject' and r['k'] == 'map-brief']
        self.assertCountEqual(refs, [linked, in_map, only_brief])

if __name__ == '__main__':
    unittest.main()
