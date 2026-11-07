from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


class StatisticsBenchmarkTests(unittest.TestCase):
    def benchmark(self):
        folder=Path(__file__).resolve().parent/'performance'
        spec=importlib.util.spec_from_file_location('isolated_performance_benchmark',folder/'benchmark.py')
        module=importlib.util.module_from_spec(spec)
        with patch.object(sys,'path',[str(folder),*sys.path]):
            spec.loader.exec_module(module)
        return module

    def test_statistics_query_uses_current_quality_and_settlement_evidence(self):
        module=self.benchmark()
        cases=module.statistics_cases(datetime.now(timezone.utc))
        query,args=cases['statistics_aggregation']
        self.assertIn('v2_signal_settlements',query)
        self.assertIn('v2_quote_evidence',query)
        self.assertIn('statistics_eligible',query)
        self.assertIn('settled_at',query)
        self.assertIn('WITH checked AS MATERIALIZED',query)
        self.assertEqual(6,len(args))
        self.assertIn('x.enabled',cases['statistics_source_options'][0])
        self.assertNotIn('v2_quote_evidence',cases['statistics_source_options'][0])

    def test_measurement_distribution_rejects_empty_negative_or_nonfinite_samples(self):
        module=self.benchmark()
        for values in ([],[-1],[float('nan')],[float('inf')]):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    module.distribution(values)
        value=module.distribution(list(range(1,21)))
        self.assertEqual((20,10,19),(value['samples'],value['p50_ms'],value['p95_ms']))
