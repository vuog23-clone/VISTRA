import unittest
from benchmarks.costs import estimate


class CostTests(unittest.TestCase):
    def test_cached_input_is_not_charged_twice(self):
        value=estimate('gpt-5-nano',{'input_tokens':1000,'cached_input_tokens':400,'output_tokens':100})
        self.assertAlmostEqual(value['estimated_usd'],.000072)

    def test_cache_writes_have_their_own_rate(self):
        value=estimate('gpt-6-luna',{'input_tokens':1000000,'output_tokens':200000,
            'input_tokens_details':{'cached_tokens':400000,'cache_write_tokens':100000}})
        self.assertAlmostEqual(value['estimated_usd'],.283)
        self.assertTrue(value['long_context_rate'])

    def test_missing_luna_cache_write_count_is_unknown(self):
        self.assertIsNone(estimate('gpt-6-luna',{'input_tokens':1000,'cached_input_tokens':0,'output_tokens':20})['estimated_usd'])

    def test_no_usage_is_not_zero(self):
        self.assertIsNone(estimate('gpt-5-nano',None)['estimated_usd'])

    def test_invalid_cached_counts_rejected(self):
        with self.assertRaises(ValueError):
            estimate('gpt-5-nano',{'input_tokens':10,'cached_input_tokens':20,'output_tokens':2})

    def test_unverified_service_tier_not_given_standard_cost(self):
        self.assertIsNone(estimate('gpt-5-nano',{'input_tokens':1000,'cached_input_tokens':0,'output_tokens':20},service_tier='priority')['estimated_usd'])
