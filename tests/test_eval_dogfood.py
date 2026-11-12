"""scripts/eval_dogfood.py: scoring of cited excerpts against expected files, on a tiny repo (real sync and ask, no model)."""
import importlib.util,json,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('eval_dogfood',ROOT/'scripts'/'eval_dogfood.py')
EVAL=importlib.util.module_from_spec(spec);spec.loader.exec_module(EVAL)

FILES={
    'README.md':'# Gardening Notes\n\nThis repo keeps small notes about a vegetable garden.\n\n## Watering\n\nTomatoes need deep watering twice a week in summer, early in the morning.\n',
    'docs/compost.md':'# Compost\n\n## Turning the pile\n\nTurn the compost pile every fortnight so the heap stays aerated and warm.\n',
    'src/planner.py':'def plan_harvest(crop):\n    """Pick the harvest week for a crop from its sowing date and days to maturity."""\n    return crop.sown + crop.days_to_maturity\n',
    'src/unrelated.py':'def checksum(data):\n    """Rolling checksum over a byte buffer."""\n    return sum(data) % 255\n',
}

class DogfoodScoring(unittest.TestCase):
    def test_score_is_the_rank_of_the_first_expected_path(self):
        q={'q':'x','expect':['a.py','b.md']}
        self.assertEqual(EVAL.score(q,['c.py','b.md','a.py']),2)
        self.assertEqual(EVAL.score(q,['c.py','d.py']),None)
        self.assertEqual(EVAL.score({'q':'x','expect':[r'docs\a.md']},['docs/a.md']),1)
    def test_percentile(self):
        self.assertEqual(EVAL.percentile([5,1,3],0.5),3);self.assertIsNone(EVAL.percentile([],0.5))
    def test_load_questions_rejects_a_question_without_expectations(self):
        path=Path(self.enterContext(tempfile.TemporaryDirectory()))/'q.json'
        path.write_text(json.dumps({'questions':[{'q':'where?','expect':[]}]}),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'expect'):EVAL.load_questions(path)
    def test_shipped_fixture_is_well_formed(self):
        questions=EVAL.load_questions(EVAL.DEFAULT_QUESTIONS);self.assertGreaterEqual(len(questions),20)
        for q in questions:
            for rel in q['expect']:self.assertTrue((ROOT/rel).is_file(),f'{rel} expected by {q["q"]!r} does not exist')

class DogfoodRun(unittest.TestCase):
    def test_hits_and_misses_are_scored_against_a_real_sync_and_ask(self):
        tmp=Path(self.enterContext(tempfile.TemporaryDirectory()));repo=tmp/'repo'
        for rel,text in FILES.items():
            (repo/rel).parent.mkdir(parents=True,exist_ok=True);(repo/rel).write_text(text,encoding='utf-8')
        (repo/'blob.bin').write_bytes(b'\x00\x01compost watering harvest')   # a binary is neither copied nor indexed
        questions=[
            {'q':'How often should the tomatoes be watered?','expect':['README.md']},
            {'q':'How do I pick the harvest week for a crop?','expect':['src/planner.py']},
            {'q':'How often do I turn the compost pile?','expect':['src/unrelated.py']},   # a deliberate miss: the answer is in docs/compost.md
        ]
        report=EVAL.evaluate(repo,questions,limit=5)
        by_q={r['q']:r for r in report['results']}
        self.assertEqual(report['questions'],3);self.assertEqual(report['files_copied'],4)
        self.assertTrue(by_q[questions[0]['q']]['hit']);self.assertTrue(by_q[questions[1]['q']]['hit'])
        miss=by_q[questions[2]['q']];self.assertFalse(miss['hit']);self.assertIsNone(miss['rank']);self.assertIn('docs/compost.md',miss['cited'])
        self.assertNotIn('blob.bin',{p for r in report['results'] for p in r['cited']})
        self.assertEqual(report['hit@5'],round(2/3,3));self.assertFalse(report['target_met'])
        self.assertEqual(report['mrr'],0.667)
        self.assertGreater(report['sync_ms'],0);self.assertIn('Target hit@5',EVAL.render(report));self.assertIn('FAIL',EVAL.render(report))
    def test_the_questions_file_is_left_out_of_the_copy(self):
        tmp=Path(self.enterContext(tempfile.TemporaryDirectory()));repo=tmp/'repo';(repo/'tests').mkdir(parents=True)
        (repo/'README.md').write_text('# Plain\n\nNothing about lemons here.\n',encoding='utf-8')
        (repo/'tests'/'q.json').write_text('{"questions": [{"q": "zebra migration routes"}]}',encoding='utf-8')
        dest=tmp/'copy';dest.mkdir()
        self.assertEqual(EVAL.copy_repo(repo,dest,[repo/'tests'/'q.json']),1);self.assertFalse((dest/'tests'/'q.json').exists())

if __name__=='__main__':unittest.main()
