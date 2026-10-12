"""Check financial and precision illustrations against independent known values."""
import math
import tempfile
import unittest
from pathlib import Path

from make_figures import expected_return, iid_half_width, write_csv


class AnalysisTests(unittest.TestCase):
    def test_high_hit_rate_can_lose(self):
        self.assertAlmostEqual(expected_return(.78, 1.20), -.064)
        self.assertAlmostEqual(expected_return(.52, 2.05), .066)

    def test_half_and_zero_paid_stake(self):
        self.assertAlmostEqual(expected_return(.6, 2, .5), .1)
        self.assertEqual(expected_return(.6, 2, 0), 0)

    def test_invalid_financial_parameters(self):
        for args in ((math.nan, 2), (.5, 1), (1.1, 2), (.5, 2, -1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                expected_return(*args)

    def test_known_precision_and_scaling(self):
        self.assertAlmostEqual(iid_half_width(.6, 100), .09601999791710056)
        self.assertAlmostEqual(iid_half_width(.6, 400), iid_half_width(.6, 100) / 2)

    def test_invalid_precision(self):
        for probability, count in ((.6, 0), (.6, -1), (math.inf, 100), (1.1, 100)):
            with self.subTest(count=count), self.assertRaises(ValueError):
                iid_half_width(probability, count)

    def test_csv_empty_and_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'example.csv'
            with self.assertRaises(ValueError):
                write_csv(path, ('value',), [])
            write_csv(path, ('value',), [{'value': 1}])
            self.assertEqual(path.read_bytes(), b'value\n1\n')


if __name__ == '__main__':
    unittest.main()
