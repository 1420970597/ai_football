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


if __name__ == '__main__':
    unittest.main()
