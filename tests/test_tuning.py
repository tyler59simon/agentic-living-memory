"""Retrieval and signal defaults tuned with scripts/eval_retrieval.py on 2026-11-06 (Phase 3, slice 4)."""
import unittest
from test_core import StoreFixture
from core import runtime
from core.notes import capture
from core.search import search_domain

TAGS = ['subject/project', 'concern/decision']


class Tuning(StoreFixture):
    def context(self, prompt):
        return runtime.context(str(self.base / 'project'), prompt, self.root, 'demo')

    def test_a_weak_all_terms_match_does_not_hide_partial_title_matches(self):
        # Only the first note contains every term; the plan's title carries "phase" and "deploy".
        capture('Edit the source, deploy with install.py', 'Deploy after tests. The phase left over is docs.', TAGS, 'demo', self.root)
        plan = capture('Phase 2 deploy and wrap-up', 'Finish the move and close out.', TAGS, 'demo', self.root, type='plan', allow_similar=True)
        ids = [h['id'] for h in search_domain("what's left to do on the phase 2 deploy?", 'demo', 5, self.root)]
        self.assertIn(plan['id'], ids)
        injected = [ref.split(':', 1)[1] for ref in self.context("what's left to do on the phase 2 deploy?")[1]]
        self.assertIn(plan['id'], injected)

    def test_half_coverage_is_not_enough_for_long_prompts(self):
        capture('Codex review notes', 'A good review checks each variable that broke.', TAGS, 'demo', self.root)
        self.assertEqual(self.context("what's a good name for this python variable?")[1], [])  # 2 of 4 terms, body only
        self.assertTrue(self.context('good variable review')[1])  # 3 terms: two of them still count

    def test_short_prompts_keep_the_half_coverage_rule(self):
        capture('Feature comparison', 'Living Memory compared with claude-mem.', TAGS, 'demo', self.root)
        self.assertTrue(self.context('how does it compare with claude-mem?')[1])

    def test_questions_about_the_future_are_not_preferences(self):
        self.assertIsNone(runtime.durable_signal('what will phase 4 add in the future?'))
        self.assertEqual(runtime.durable_signal('in the future, use tabs'), 'preference')
        self.assertEqual(runtime.durable_signal('in future please run the tests first'), 'preference')


if __name__ == '__main__':
    unittest.main()
