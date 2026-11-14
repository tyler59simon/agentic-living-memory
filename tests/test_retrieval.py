"""Prompt retrieval relevance, session context, capture ergonomics and screening."""
import io
import json
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

from test_core import StoreFixture
from core import runtime, session_start
from core.cli import main as cli
from core.init_domain import init_domain
from core.notes import capture, similar_title, title_similarity, find_note
from core.repository import find_secret
from core.search import search_domain, snippet

TAGS = ['subject/project', 'concern/decision']


class Retrieval(StoreFixture):
    def prompt(self, text, session='s'):
        payload = {'cwd': str(self.base / 'project'), 'session_id': session, 'prompt': text}
        return runtime.process('claude', 'user-prompt', payload, self.root)['context']

    def test_plurals_and_conversational_filler_still_match(self):
        self.note('Webcam diagnosis', 'Camera driver is the inbox one.')
        self.assertIn('Webcam diagnosis', self.prompt('webcams', 'a'))
        self.assertIn('Webcam diagnosis', self.prompt('how is the webcam issue going', 'b'))

    def test_hit_lines_show_type_date_and_reference(self):
        n = capture('Build pipeline status', 'Green on main.', TAGS, 'demo', self.root, type='status')
        line = self.prompt('build pipeline').splitlines()[2]
        self.assertTrue(line.startswith('- [STATUS 20'), line)
        self.assertIn('demo:' + n['id'][:8] + ' ', line)

    def test_similar_plan_titles_rank_by_relevance_not_date(self):
        # Two shared title words do not make the newer plan a replacement for the older one.
        auth = capture('Payment API authentication', 'Rotate the signing tokens for payment authentication.', TAGS, 'demo', self.root, type='plan')
        capture('Payment API pagination', 'Cursor pagination for the payment list endpoint.', TAGS, 'demo', self.root, type='plan')
        hits = search_domain('payment api authentication tokens', 'demo', 5, self.root)
        self.assertEqual(hits[0]['id'], auth['id'])

    def test_notes_are_injected_once_per_session(self):
        self.note()
        self.assertIn('Cache', self.prompt('how is the cache rebuilt'))
        self.assertNotIn('Cache', self.prompt('the cache again'))
        self.assertIn('Cache', self.prompt('how is the cache rebuilt', 'other-session'))
        runtime.process('claude', 'session-start', {'cwd': str(self.base / 'project'), 'session_id': 's'}, self.root)
        self.assertIn('Cache', self.prompt('the cache again'))  # new or compacted context

    def test_harness_messages_are_not_searched(self):
        self.note()
        self.assertNotIn('Cache', self.prompt('<agent-message from="x">The cache report</agent-message>'))
        self.assertNotIn('Cache', self.prompt('[SYSTEM NOTIFICATION - NOT USER INPUT] cache task finished'))

    def test_long_prompts_keep_the_question_at_the_end(self):
        self.note('Deployment checklist', 'Run migrations before switching traffic.')
        pasted = ' '.join(f'logline{i}' for i in range(60))
        self.assertIn('Deployment checklist', self.prompt(pasted + ' what is our deployment checklist'))

    def test_parent_domain_notes_are_found_from_a_nested_project(self):
        init_domain('child', [str(self.base / 'project' / 'child')], self.root)
        self.note('Webcam conclusion', 'Likely hardware.')
        payload = {'cwd': str(self.base / 'project' / 'child'), 'session_id': 'c', 'prompt': 'webcam conclusion'}
        self.assertIn('Webcam conclusion', runtime.process('claude', 'user-prompt', payload, self.root)['context'])
        self.assertEqual(runtime.parent_domains('child', json.loads((self.root / 'bindings.json').read_text())), ['demo'])

    def test_no_match_offers_domain_titles_once_when_no_global_domain(self):
        self.note('Webcam conclusion', 'Likely hardware.')
        first = self.prompt('weather forecast tomorrow')
        self.assertIn('Recent titles in this domain', first)
        self.assertIn('Webcam conclusion', first)
        self.assertNotIn('No lexical match', self.prompt('holiday travel plans'))  # once per session
        self.assertNotIn('No lexical match', self.prompt('continue', 'one-word'))  # one term is not a question

    def test_no_match_prefers_global_titles_when_a_global_domain_exists(self):
        init_domain('_global', [str(self.base / 'elsewhere')], self.root)
        capture('Prefer concise answers', 'Keep replies short.', TAGS, '_global', self.root, type='preference')
        self.note('Webcam conclusion', 'Likely hardware.')
        context = self.prompt('weather forecast tomorrow')
        self.assertIn('Global titles', context)
        self.assertIn('Prefer concise answers', context)
        self.assertNotIn('Webcam conclusion', context)

    def test_doctor_warns_about_missing_scope_domains(self):
        def warnings():
            out = io.StringIO()
            with redirect_stdout(out):
                cli(['--root', str(self.root), 'doctor', '--cwd', str(self.base / 'project')])
            return json.loads(out.getvalue())['warnings']
        # _global's folder is created by the first `capture --global`, so its absence is expected.
        self.assertFalse(any('global_domains' in w for w in warnings()))
        config = json.loads((self.root / 'bindings.json').read_text());config['search_scope']['global_domains'] = ['shared']
        (self.root / 'bindings.json').write_text(json.dumps(config))
        self.assertTrue(any("global_domains names 'shared'" in w and 'migration-plan' in w for w in warnings()))

    def test_snippets_respect_word_boundaries(self):
        body = ('filler words here ' * 40) + 'the important latest result is here ' + ('more filler text ' * 40)
        text = snippet(body, ['important'], 120)
        self.assertIn('important', text)
        self.assertTrue(text.startswith('… '))
        inner = text.strip('… ').split(' ')
        self.assertTrue(all(word in body.split() for word in inner))


class SessionContext(StoreFixture):
    def start(self):
        payload = {'cwd': str(self.base / 'project'), 'session_id': 'sess'}
        return runtime.process('claude', 'session-start', payload, self.root)['context']

    def test_protocol_lists_tags_and_session_and_stays_small(self):
        for i in range(6):
            capture(f'Decision {i} about caching', 'x' * 600, TAGS, 'demo', self.root, type='decision', allow_similar=True)
        text = self.start()
        self.assertIn('concern/{architecture,decision}', text)
        self.assertIn('--session sess', text)
        self.assertNotIn('--subject project --concern knowledge', text)
        # Measure the protocol, not this machine's paths: the store, the interpreter and the script all appear
        # in the command line, and a longer checkout or Python path must not decide whether this passes.
        for path in (str(self.root), sys.executable, str(runtime.SCRIPT)):
            text = text.replace(path, '')
        self.assertLess(len(text), 5440)   # the fallback injects the whole protocol; the user-brief, review and log sentences grew it by ~250 characters, the Save rule by ~280 characters, the project-brief sentence by ~225, the ask sentence and usage line by ~120, the several-questions sentence by ~75, the writing rule and per-type sections by ~590, and the found-yourself sentence by ~220, and the maintenance exception by ~140

    def test_a_user_brief_keeps_the_fallback_context_inside_the_cap(self):
        for i in range(6):
            capture(f'Decision {i} about caching', 'x' * 600, TAGS, 'demo', self.root, type='decision', allow_similar=True)
        capture('User brief', 'A terse developer on Windows.\n\n## Details\n- **Working style:** ' + 'Short answers first, ask before large refactors. ' * 30 + '\n- **Current focus:** The user brief.',
                ['subject/project', 'concern/decision'], '_global', self.root, type='brief')
        text = self.start()
        self.assertIn('User brief: _global:', text)   # the fallback protocol leaves the minimum body, so the brief is down to its pointer line
        body = text.split('Living Memory session=sess\n', 1)[1]
        self.assertLessEqual(len(body), session_start.MIN_BUDGET)   # the brief takes its share of the body, never more than the budget

    def test_guidance_notes_come_first(self):
        capture('Prefer small commits', 'Keep commits small.', TAGS, 'demo', self.root, type='preference')
        for i in range(3):
            capture(f'Status {i}', f'Progress {i}.', TAGS, 'demo', self.root, type='status', allow_similar=True)
        lines = [l for l in self.start().splitlines() if l.startswith('- [')]
        self.assertTrue(lines[0].startswith('- [PREFERENCE'), lines)

    def test_root_is_omitted_for_the_configured_store(self):
        with patch('core.runtime._configured_root', return_value=self.root.resolve()):
            self.assertNotIn('--root', runtime.command(self.root, 'demo'))
        self.assertIn('--root', runtime.command(self.root, 'demo'))


class CaptureErgonomics(StoreFixture):
    def cmd(self, rest):
        return f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo {rest}'

    def test_single_quoted_text_may_contain_shell_characters(self):
        allowed = self.cmd("capture --session s --type decision --subject project --concern decision 'A -> B' 'Uses $HOME | grep; x & y\nline two'")
        self.assertEqual(runtime.memory_subcommand(allowed), 'capture')
        self.assertEqual(runtime.memory_subcommand('& ' + self.cmd("capture 'It''s fine' 'ok'")), 'capture')
        for unsafe in ('capture "uses $HOME" body', "capture 'a' 'b'; rm -rf x", 'capture `whoami` body', "capture 'a' 'b' | tee x"):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(runtime.memory_subcommand(self.cmd(unsafe)))

    def test_turn_defaults_to_the_latest_prompt(self):
        runtime.process('claude', 'user-prompt', {'cwd': str(self.base / 'project'), 'session_id': 'sx', 'turn_id': 't-42', 'prompt': 'hi'}, self.root)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli(['--root', str(self.root), '--domain', 'demo', 'capture', '--session', 'sx', '--type', 'decision',
                                  '--subject', 'project', '--concern', 'decision', 'Use WAL mode', 'SQLite WAL mode for the index.']), 0)
        from core.capture import status
        self.assertTrue(status('sx', 't-42', self.root))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(['--root', str(self.root), 'capture-finalize', '--session', 'sx']), 0)

    def test_near_miss_titles_are_warned_not_rejected(self):
        capture('Hook timeout settings', 'Fifteen seconds.', TAGS, 'demo', self.root)
        result = capture('Hook timeout for Codex sessions', 'Thirty seconds.', TAGS, 'demo', self.root)
        self.assertEqual(result['status'], 'created')
        self.assertEqual(len(result.get('similar', [])), 1)

    def test_duplicate_titles(self):
        self.assertGreaterEqual(title_similarity('Hook latency baseline', 'Hook latency baseline measured'), 0.8)
        self.assertGreaterEqual(title_similarity('Hook timeout', 'Hook timeouts'), 0.8)
        self.assertLess(title_similarity('How we deploy', 'Why we deploy'), 0.8)
        self.assertLess(title_similarity('Parallel 0', 'Parallel 1'), 0.8)


class SecretScreening(StoreFixture):
    def test_real_credentials_are_caught(self):
        for text in ('DB_PASSWORD=hunter2hunter2', 'aws_secret_access_key = Zr8pQ2vLx9TmW4yNb7KcHs1Fd6GjA3uE5oRi0Ytq',
                     'client_secret: 8f3k2m9x7q1w', 'sk_live_51HxxxxABCDEF0123456789', 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345',
                     'glpat-abcdefghijklmnopqrst', 'postgres://admin:s3cretpw@db.internal/app',
                     'DefaultEndpointsProtocol=https;AccountName=x;AccountKey=abcdefghijklmnopqrstuvwxyz0123456789+/=='):
            with self.subTest(text=text):
                self.assertIsNotNone(find_secret(text))

    def test_placeholders_and_prose_are_allowed(self):
        for text in ('password=<REDACTED>', 'API_KEY=${API_KEY}', 'api_key=os.environ["API_KEY"]', 'The client secret: configured via Key Vault',
                     'token: ******', 'Set password: changeme in the example config'):
            with self.subTest(text=text):
                self.assertIsNone(find_secret(text))

    VENDOR = {
        'Hugging Face token': 'hf_' + 'aB3' * 12,
        'Docker token': 'dckr_pat_' + 'aB3_' * 6,
        'Linear key': 'lin_api_' + 'aB3' * 12,
        'Notion token': 'ntn_' + 'aB3' * 15,
        'SendGrid key': 'SG.' + 'aB3_' * 5 + '.' + 'aB3-' * 10,
        'DigitalOcean token': 'dop_v1_' + 'a1b2' * 16,
        'Shopify token': 'shpat_' + 'a1B2' * 8,
        'PyPI token': 'pypi-AgE' + 'aB3_' * 15,
        'Databricks token': 'dapi' + 'a1b2' * 8,
        'Vault token': 'hvs.' + 'aB3_' * 7,
        'Groq key': 'gsk_' + 'aB3' * 15,
        'xAI key': 'xai-' + 'aB3' * 15,
        'Supabase token': 'sbp_' + 'a1b2' * 10,
        'Sentry token': 'sntrys_' + 'aB3=' * 12,
        'Slack webhook': 'https://hooks.slack.com/services/T0ABC123/B0DEF456/' + 'aB3' * 8,
        'Discord webhook': 'https://discord.com/api/webhooks/123456789/' + 'aB3_' * 8,
        'Google OAuth token': 'ya29.' + 'aB3_' * 8,
        'GitLab token': 'glrt-' + 'aB3_' * 6,
        'basic auth credentials': 'Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==',
    }

    def test_vendor_tokens_are_caught_by_name(self):
        for kind, text in self.VENDOR.items():
            with self.subTest(kind=kind):
                self.assertEqual(find_secret('Config: ' + text + ' end')[0], kind)

    def test_vendor_prefixes_in_prose_are_allowed(self):
        for text in ('Hugging Face tokens start with hf_ and are long', 'Docker PATs look like dckr_pat_ plus text',
                     'Use the SG. prefix for SendGrid', 'xai- keys come from the console', 'Basic authentication is disabled',
                     'Basic auth over HTTPS only', 'the secret_key setting', 'ya29. tokens expire hourly',
                     'shpat_ tokens are per store', 'see hooks.slack.com/services for webhooks', 'dapi tokens are per workspace'):
            with self.subTest(text=text):
                self.assertIsNone(find_secret(text))

    def test_rejection_names_what_matched(self):
        with self.assertRaisesRegex(ValueError, r'credential assignment at character \d+'):
            capture('Database', 'Use DB_PASSWORD=hunter2hunter2 locally.', TAGS, 'demo', self.root)


if __name__ == '__main__':
    import unittest
    unittest.main()
