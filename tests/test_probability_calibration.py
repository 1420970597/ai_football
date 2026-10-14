import unittest

from core.probability_calibration import calibrate_probability, fit_logistic


class ProbabilityCalibrationTests(unittest.TestCase):
    def test_sparse_or_degenerate_data_falls_back_to_identity(self):
        artifact = fit_logistic([.2, .8], [0, 1])
        self.assertEqual(artifact['method'], 'identity')
        self.assertAlmostEqual(calibrate_probability(.7, artifact), .7)

    def test_logistic_calibration_is_finite_and_bounded(self):
        probs = [.2, .3, .4, .6, .7, .8] * 8
        outcomes = [0, 0, 0, 1, 1, 1] * 8
        artifact = fit_logistic(probs, outcomes)
        self.assertEqual(artifact['method'], 'logistic')
        self.assertTrue(0 < calibrate_probability(.5, artifact) < 1)
        self.assertTrue(0 < calibrate_probability(.99, artifact) < 1)

    def test_invalid_labels_and_probabilities_are_rejected_before_sparse_fallback(self):
        for target in (None, .5, -1, 2, '1'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                fit_logistic([.5], [target])
        for probability in (float('nan'), float('inf'), -.1, 1.1):
            with self.subTest(probability=probability):
                with self.assertRaises(ValueError):
                    fit_logistic([probability], [0])
                with self.assertRaises(ValueError):
                    calibrate_probability(probability, {'method': 'identity'})
                with self.assertRaises(ValueError):
                    calibrate_probability(probability, {'method': 'logistic', 'slope': 1, 'intercept': 0})


if __name__ == '__main__':
    unittest.main()
