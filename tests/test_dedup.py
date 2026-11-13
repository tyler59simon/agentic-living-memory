import json,unittest
from pathlib import Path
from test_core import StoreFixture
from core.notes import TitleScorer,title_similarity,similar_title,near_titles,pair_score,LENIENT_CAP

FIXTURE=json.loads((Path(__file__).parent/'fixtures'/'title_pairs.json').read_text(encoding='utf-8'))
THRESHOLD=0.8

def judged(score_pair):
    """(precision, recall, wrong pairs) of a scoring function over the labelled fixture."""
    tp=fp=fn=0;wrong=[]
    for pair in FIXTURE['pairs']:
        predicted=score_pair(pair)>=THRESHOLD;actual=pair['label']=='duplicate'
        tp+=predicted and actual;fp+=predicted and not actual;fn+=actual and not predicted
        if predicted!=actual:wrong.append(pair)
    return tp/max(1,tp+fp),tp/max(1,tp+fn),wrong

def new_rule(pair):
    corpus=[{'title':t} for t in FIXTURE['corpus'] if t!=pair['existing']]+[{'title':pair['existing']}]
    return TitleScorer(corpus).score(pair['new'],pair['existing'])

class Dedup(unittest.TestCase):
    def test_fixture_is_labelled_correctly_by_the_new_rule(self):
        precision,recall,wrong=judged(new_rule)
        self.assertEqual(wrong,[],'misjudged: '+'; '.join(f"{p['new']!r} vs {p['existing']!r}" for p in wrong))
        self.assertEqual((precision,recall),(1.0,1.0))
    def test_new_rule_is_no_worse_than_the_overlap_rule(self):
        old=judged(lambda p:title_similarity(p['new'],p['existing']));new=judged(new_rule)
        print(f"\ntitle dedup fixture: overlap rule precision {old[0]:.2f} recall {old[1]:.2f}; idf rule precision {new[0]:.2f} recall {new[1]:.2f}")
        self.assertGreaterEqual(new[0],old[0]);self.assertGreaterEqual(new[1],old[1])
    def test_only_inflections_fold(self):
        scorer=TitleScorer([])  # even at cold start, folding must not make different words equal
        self.assertLess(scorer.score('File patch handling','File path handling'),1.0)
        self.assertEqual(TitleScorer._base('using'),TitleScorer._base('use'))
    def test_cold_start_uses_the_overlap_rule(self):
        few=[{'title':'rdlp-plugin release checklist'}]
        self.assertEqual(TitleScorer(few).score('rdlp-api release checklist','rdlp-plugin release checklist'),
                         title_similarity('rdlp-api release checklist','rdlp-plugin release checklist'))
    def test_best_match_wins(self):
        corpus=[{'title':t} for t in FIXTURE['corpus'] if not t.startswith('Obsidian')]
        weaker={'title':'Obsidian vault sync conflicts and merges'};stronger={'title':'Obsidian vault sync conflict'}
        self.assertIs(similar_title('Obsidian vault sync conflicts',corpus+[weaker,stronger]),stronger)
    def test_near_titles_are_best_first(self):
        corpus=[{'title':t} for t in FIXTURE['corpus']]
        near=near_titles('Session start budget',corpus)
        self.assertEqual(near[:1],[{'title':'Session start budget and degradation order'}])

class CaptureUsesIt(StoreFixture):
    def test_distinct_component_is_not_rejected_once_the_domain_is_large(self):
        for title in FIXTURE['corpus']:self.note(title,'Body for '+title,allow_similar=True)
        self.note('rdlp-plugin release checklist','Plugin steps.')
        self.assertEqual(self.note('rdlp-api release checklist','API steps.')['status'],'created')
        with self.assertRaisesRegex(ValueError,'possible_duplicate'):self.note('Hook timeout','A different body.')

class NumberedTitles(StoreFixture):
    def fill(self):
        for title in FIXTURE['corpus']:self.note(title,'Body for '+title,allow_similar=True)
    def test_numbered_status_titles_are_advisory_similar_never_a_refusal(self):
        self.fill();self.note('Sprint 3 status','Sprint 3 closed two tickets.',type='status')
        result=self.note('Sprint 4 status','Sprint 4 closed five tickets.',type='status')
        self.assertEqual(result['status'],'created');self.assertEqual([s['title'] for s in result['similar']],['Sprint 3 status'])
    def test_numbered_plan_titles_keep_the_identifier_veto(self):
        self.fill();self.note('Phase 1 plan','Phase one steps.',type='plan')
        result=self.note('Phase 2 plan','Phase two steps.',type='plan')
        self.assertEqual(result['status'],'created');self.assertNotIn('similar',result)
    def test_lenient_scores_stay_below_the_refusal_threshold(self):
        corpus=[{'title':t} for t in FIXTURE['corpus']]+[{'title':'Sprint 3 status'}]
        scorer=TitleScorer(corpus)
        self.assertEqual(scorer.score('Sprint 4 status','Sprint 3 status'),0.0)
        self.assertEqual(scorer.score('Sprint 4 status','Sprint 3 status',lenient=True),1.0)
        self.assertEqual(pair_score(scorer,'Sprint 4 status','Sprint 3 status','status','status'),LENIENT_CAP)
        self.assertEqual(pair_score(scorer,'Sprint 4 status','Sprint 3 status','plan','plan'),0.0)

if __name__=='__main__':unittest.main()
