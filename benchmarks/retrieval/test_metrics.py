import unittest

from benchmarks.retrieval.metrics import ranking
from benchmarks.retrieval.dataset import in_city,category


class MetricsTests(unittest.TestCase):
    def test_duplicates_cannot_inflate_recall(self):
        r=ranking(['a','a','b'],['a','c'])
        self.assertEqual(r['recall_at_20'],.5)
        self.assertEqual(r['precision_of_returned_at_20'],.5)
        self.assertEqual(r['precision_at_20'],.05)

    def test_reversed_ranking_lowers_ndcg(self):
        self.assertLess(ranking(['bad','good'],['good'])['ndcg_at_10'],1)
        self.assertEqual(ranking(['good','bad'],['good'])['ndcg_at_10'],1)

    def test_failed_nonempty_task_scores_zero(self):
        r=ranking([],['expected'])
        self.assertEqual(r['recall_at_20'],0)
        self.assertEqual(r['precision_at_20'],0)
        self.assertEqual(r['ndcg_at_10'],0)

    def test_no_relevant_data_is_not_perfect_recall(self):
        r=ranking([],[])
        self.assertIsNone(r['recall_at_20'])
        self.assertIsNone(r['ndcg_at_10'])
        self.assertTrue(r['correct_empty'])
        self.assertFalse(ranking(['unrelated'],[])['correct_empty'])

    def test_scope_oracle_has_word_boundaries(self):
        self.assertTrue(in_city({'province':'Hà Nội 100000'},'Hanoi'))
        self.assertFalse(in_city({'province':'Huế'},'Hanoi'))
        self.assertFalse(in_city({'province':'SomethingHueElse'},'Hue'))

    def test_category_oracle_preserves_subtypes(self):
        self.assertEqual(category({'category':'Vietnamese restaurant'}),'restaurant')
        self.assertEqual(category({'category':'Local history museum'}),'museum')
        self.assertIsNone(category({'category':'Museum Cafe Name','place_name':'Museum'}))


if __name__=='__main__':
    unittest.main()
