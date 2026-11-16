"""Проверки методики: обрывы не превращаются в успешные ответы и p95."""
import unittest
from report import quantile, summarize

class AnalysisChecks(unittest.TestCase):
    def test_aborted_http_200_is_excluded_from_success_quantile(self):
        common={'pod':'p','send_lag_seconds':0,'cache_hit':False}
        rows=[{**common,'outcome':'completed','status':200,'ttfc_seconds':1,'total_seconds':2},
              {**common,'outcome':'aborted','status':200,'ttfc_seconds':100,'total_seconds':200},
              {**common,'outcome':'rejected','status':429,'ttfc_seconds':None,'total_seconds':.1}]
        result=summarize(rows)
        self.assertEqual(result['successful_n'],1)
        self.assertEqual(result['p95_ttfc'],1)
        self.assertAlmostEqual(result['error_fraction'],2/3)

    def test_empty_observations_have_no_quantile(self):
        self.assertIsNone(quantile([],.95))
        self.assertIsNone(summarize([])['error_fraction'])
