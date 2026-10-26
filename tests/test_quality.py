import copy
import hashlib
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys
import unittest

from infra.quality.cli import ROOT, project_path
from infra.quality.evaluate import compare, evaluate
from infra.quality.formats import FIELDS, QualityError
from infra.quality.matching import _hungarian

SAMPLES = Path(__file__).resolve().parents[1] / 'infra/quality/samples'


def fixture(name):
    return json.loads((SAMPLES / (name + '.json')).read_text())


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.labels, self.baseline, self.candidate = [fixture(name) for name in ('labels', 'baseline', 'candidate')]

    def single(self, sample_id='synthetic-half'):
        labels, run = copy.deepcopy(self.labels), copy.deepcopy(self.candidate)
        labels['samples'] = [row for row in labels['samples'] if row['sample_id'] == sample_id]
        run['samples'] = [row for row in run['samples'] if row['sample_id'] == sample_id]
        return labels, run

    def test_baseline_separates_binding_misses_false_acceptance_duplicates_and_missing_run(self):
        report = evaluate(self.labels, self.baseline)
        metric = report['metrics']
        self.assertEqual({'correct': 2, 'missed': 2, 'false_accepted': 2, 'unassessed_expected': 1}, metric['exact_binding'])
        self.assertEqual(1, metric['duplicates']['outputs'])
        self.assertEqual(1, metric['duplicates']['acknowledgements'])
        self.assertEqual(1, metric['decisions']['false_accept_messages'])
        self.assertEqual(1, metric['decisions']['correct_reject_messages'])
        self.assertEqual(1, metric['multiple_recommendations']['missing_by_count'])
        self.assertEqual(1, metric['coverage']['missing'])
        self.assertFalse(report['real_world_accuracy_available'])
        self.assertFalse(report['profitability_assessed'])
        self.assertNotIn('total_score', report)
        self.assertNotIn('accuracy', report)

    def test_candidate_preserves_execution_error_instead_of_counting_it_correct_rejection(self):
        report = evaluate(self.labels, self.candidate)
        self.assertEqual(5, report['metrics']['exact_binding']['correct'])
        self.assertEqual(1, report['metrics']['coverage']['execution_error'])
        self.assertEqual(1, report['metrics']['decisions']['correct_reject_messages'])
        self.assertEqual(1, report['metrics']['decisions']['unassessed_rejections'])
        self.assertEqual(1, report['metrics']['multiple_recommendations']['all_exact'])
        self.assertEqual(1, report['groups']['phase']['mixed']['coverage']['samples'])

    def test_each_binding_dimension_is_independently_diagnosed(self):
        replacements = {'fixture_id': 'synthetic-wrong', 'market_id': 'synthetic-wrong-market', 'market_kind': 'total',
                        'period': 'full_time', 'line': '0', 'selection': 'home', 'phase': 'scheduled'}
        for changed, value in replacements.items():
            labels, run = self.single()
            run['samples'][0]['accepted'][0][changed] = value
            metric = evaluate(labels, run)['metrics']
            self.assertEqual(1, metric['exact_binding']['false_accepted'])
            for field in FIELDS:
                self.assertEqual(int(field == changed), metric['dimensions'][field]['incorrect'])
                self.assertEqual(int(field != changed), metric['dimensions'][field]['correct'])

    def test_missing_and_invalid_line_never_turn_into_zero(self):
        for value in (None, 'not-a-line', 'NaN', 'Infinity', True, '0/0.5'):
            labels, run = self.single()
            run['samples'][0]['accepted'][0]['line'] = value
            report = evaluate(labels, run)
            self.assertEqual(1, report['metrics']['dimensions']['line']['missing'])
            self.assertEqual('0.000000', report['metrics']['dimensions']['line']['correct_per_expected'])
            self.assertTrue(any('.line:' in value for value in report['samples'][0]['flags']))
            self.assertEqual(0, report['metrics']['exact_binding']['correct'])

    def test_decimal_equivalence_and_reversed_dual_picks_are_order_independent(self):
        labels, run = self.single('synthetic-dual')
        run['samples'][0]['accepted'][0]['line'] = '2.5000'
        first = evaluate(labels, run)
        run['samples'][0]['accepted'].reverse()
        second = evaluate(labels, run)
        self.assertEqual(2, first['metrics']['exact_binding']['correct'])
        self.assertEqual(first['metrics'], second['metrics'])

    def test_duplicate_output_cannot_replace_second_recommendation(self):
        labels, run = self.single('synthetic-dual')
        actual = run['samples'][0]['accepted'][0]
        run['samples'][0]['accepted'] = [copy.deepcopy(actual), copy.deepcopy(actual)]
        metric = evaluate(labels, run)['metrics']
        self.assertEqual(1, metric['duplicates']['outputs'])
        self.assertEqual(1, metric['exact_binding']['correct'])
        self.assertEqual(1, metric['multiple_recommendations']['missing_by_count'])
        self.assertEqual(0, metric['multiple_recommendations']['all_exact'])
        self.assertEqual(2, metric['dimensions']['fixture_id']['expected'])
        self.assertEqual('0.500000', metric['dimensions']['fixture_id']['correct_per_expected'])

    def test_equal_optimum_alignment_is_unassessed_per_field(self):
        labels, run = self.single('synthetic-dual')
        first = labels['samples'][0]['expected']['recommendations'][0]
        second = copy.deepcopy(first)
        second.update(intent_id='synthetic-second', fixture_id='synthetic-other-gold')
        labels['samples'][0]['expected']['recommendations'] = [first, second]
        actual = copy.deepcopy(first)
        other = copy.deepcopy(first)
        actual['fixture_id'], other['fixture_id'] = 'synthetic-wrong-one', 'synthetic-wrong-two'
        run['samples'][0]['accepted'] = [actual, other]
        report = evaluate(labels, run)
        self.assertIn('diagnostic_alignment_ambiguous', report['samples'][0]['flags'])
        self.assertEqual(2, report['metrics']['exact_binding']['false_accepted'])
        for metric in report['metrics']['dimensions'].values():
            self.assertEqual(2, metric['ambiguous'])
            self.assertEqual(0, metric['correct'])

    def test_hungarian_cost_and_ambiguity_match_independent_brute_force(self):
        rng = random.Random(47)
        for size in range(1, 5):
            for _ in range(20):
                cost = [[rng.randrange(4) for _ in range(size)] for _ in range(size)]
                assignment, ambiguous = _hungarian(cost)
                permutations = list(itertools.permutations(range(size)))
                minimum = min(sum(cost[row][columns[row]] for row in range(size)) for columns in permutations)
                optimum = [columns for columns in permutations if sum(cost[row][columns[row]] for row in range(size)) == minimum]
                self.assertEqual(minimum, sum(cost[row][column] for row, column in assignment.items()))
                self.assertEqual({row for row in range(size) if any(columns[row] != assignment[row] for columns in optimum)}, ambiguous)

    def test_source_revision_or_hash_mismatch_is_not_a_correct_match(self):
        for field, value in (('source_content_sha256', 'f' * 64), ('message_revision', 2), ('message_revision', True)):
            labels, run = self.single()
            run['samples'][0][field] = value
            report = evaluate(labels, run)
            self.assertEqual(0, report['metrics']['exact_binding']['correct'])
            self.assertEqual(1, report['metrics']['coverage']['unassessed'])
            self.assertEqual(1, report['metrics']['coverage']['unbound_accepted'])
            self.assertEqual(1, report['metrics']['dimensions']['fixture_id']['unassessed'])

    def test_missing_wrong_or_reordered_media_manifest_cannot_claim_visual_quality(self):
        for observed in (None, [], ['f' * 64]):
            labels, run = self.single('synthetic-dual')
            if observed is None:
                del run['samples'][0]['source_media_sha256']
            else:
                run['samples'][0]['source_media_sha256'] = observed
            report = evaluate(labels, run)
            self.assertEqual(0, report['metrics']['exact_binding']['correct'])
            self.assertEqual(1, report['metrics']['coverage']['unassessed'])
        labels, run = self.single('synthetic-dual')
        labels['samples'][0]['source']['media_sha256'] *= 2
        with self.assertRaises(QualityError):
            bad = copy.deepcopy(labels)
            bad['samples'][0]['source']['media_sha256'] = []
            evaluate(bad, run)
        self.assertEqual(1, evaluate(labels, run)['metrics']['coverage']['input_mismatch'])
        labels, run = self.single('synthetic-dual')
        labels['samples'][0]['source']['media_sha256'].append('b' * 64)
        run['samples'][0]['source_media_sha256'] = list(reversed(labels['samples'][0]['source']['media_sha256']))
        self.assertEqual(1, evaluate(labels, run)['metrics']['coverage']['input_mismatch'])

    def test_one_signal_identity_cannot_satisfy_two_different_recommendations(self):
        labels, run = self.single('synthetic-dual')
        for accepted in run['samples'][0]['accepted']:
            accepted['signal_id'] = 'synthetic-same-signal'
        with self.assertRaisesRegex(QualityError, 'same_signal_id_has_conflicting_bindings'):
            evaluate(labels, run)

    def test_missing_records_lists_and_status_never_inflate_correct_rejection(self):
        for change in ('missing_record', 'missing_list', 'missing_status', 'error'):
            labels, run = self.single('synthetic-ambiguous')
            if change == 'missing_record':
                run['samples'] = []
            elif change == 'missing_list':
                del run['samples'][0]['accepted']
            elif change == 'missing_status':
                del run['samples'][0]['status']
            else:
                run['samples'][0]['status'] = 'error'
            metric = evaluate(labels, run)['metrics']
            self.assertEqual(0, metric['decisions']['correct_reject_messages'])
            self.assertEqual(1, metric['decisions']['unassessed_rejections'])

    def test_acceptance_before_later_execution_error_still_counts_false_accept_risk(self):
        labels, run = self.single('synthetic-ambiguous')
        run['samples'][0]['status'] = 'error'
        run['samples'][0]['accepted'] = copy.deepcopy(self.candidate['samples'][0]['accepted'])
        report = evaluate(labels, run)
        self.assertEqual(1, report['metrics']['coverage']['execution_error'])
        self.assertEqual(1, report['metrics']['decisions']['false_accept_messages'])
        self.assertEqual(2, report['metrics']['exact_binding']['false_accepted'])

    def test_explicit_positive_rejection_and_unexplained_negative_rejection_are_separate(self):
        labels, run = self.single()
        run['samples'][0]['accepted'] = []
        self.assertEqual(1, evaluate(labels, run)['metrics']['decisions']['false_reject_messages'])
        labels, run = self.single('synthetic-ambiguous')
        del run['samples'][0]['rejection_reason']
        metric = evaluate(labels, run)['metrics']
        self.assertEqual(1, metric['decisions']['correct_reject_messages'])
        self.assertEqual(1, metric['decisions']['rejection_reason_missing'])

    def test_missing_provenance_and_orphan_predictions_remain_visible(self):
        self.candidate['provenance'] = {'prompt_sha256': 'invalid'}
        orphan = copy.deepcopy(self.candidate['samples'][0])
        orphan['sample_id'] = 'synthetic-no-gold'
        self.candidate['samples'].append(orphan)
        report = evaluate(self.labels, self.candidate)
        self.assertFalse(report['provenance_complete'])
        self.assertIn('model_id', report['provenance_missing'])
        self.assertIn('prompt_sha256_invalid', report['provenance_missing'])
        self.assertEqual([{'sample_id': 'synthetic-no-gold', 'accepted_count': 2}], report['orphan_predictions'])

    def test_duplicate_gold_or_prediction_ids_are_rejected(self):
        labels, run = self.single()
        labels['samples'][0]['expected']['recommendations'] *= 2
        with self.assertRaises(QualityError):
            evaluate(labels, run)
        run['samples'] *= 2
        with self.assertRaises(QualityError):
            evaluate(self.labels, run)
        duplicate = copy.deepcopy(self.labels)
        duplicate['samples'].append(copy.deepcopy(duplicate['samples'][0]))
        with self.assertRaises(QualityError):
            evaluate(duplicate, self.candidate)

    def test_real_or_unmarked_samples_and_cross_dataset_runs_are_rejected(self):
        for mutate in ('root', 'sample', 'source', 'run', 'dataset'):
            labels, run = copy.deepcopy(self.labels), copy.deepcopy(self.candidate)
            if mutate == 'root':
                labels['synthetic'] = False
            elif mutate == 'sample':
                labels['samples'][0]['synthetic'] = False
            elif mutate == 'source':
                labels['samples'][0]['source']['level'] = 'real_telegram'
            elif mutate == 'run':
                run['synthetic'] = False
            else:
                run['dataset_id'] = 'different-dataset'
            with self.assertRaises(QualityError):
                evaluate(labels, run)

    def test_version_comparison_keeps_regression_even_when_rejection_total_is_unchanged(self):
        report = compare(self.labels, self.baseline, self.candidate)
        self.assertFalse(report['automatic_promotion'])
        self.assertEqual(6, report['changed_samples'])
        self.assertEqual(3, report['metric_changes']['exact_binding']['correct'])
        self.assertEqual(0, report['metric_changes']['decisions']['correct_reject_messages'])
        regression = next(row for row in report['review_queue'] if row['sample_id'] == 'synthetic-unsupported-image')
        self.assertEqual(1, regression['metric_changes']['coverage']['execution_error'])
        self.assertEqual(-1, regression['metric_changes']['decisions']['correct_reject_messages'])
        self.assertTrue(regression['human_review_required'])
        unchanged = compare(self.labels, self.candidate, self.candidate)
        self.assertEqual(0, unchanged['changed_samples'])

    def test_cli_paths_are_project_scoped_and_do_not_overwrite_existing_labels(self):
        self.assertEqual(SAMPLES, project_path(SAMPLES))
        with self.assertRaises(QualityError):
            project_path(ROOT.parent / 'outside')
        path = SAMPLES / 'labels.json'
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = subprocess.run([sys.executable, '-m', 'infra.quality.cli', 'evaluate', '--labels', str(path),
                                 '--run', str(SAMPLES / 'candidate.json'), '--output', str(path)], capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertEqual('FileExistsError', json.loads(result.stdout)['error'])
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_cli_comparison_runs_without_network_or_model_configuration(self):
        result = subprocess.run([sys.executable, '-m', 'infra.quality.cli', 'compare', '--labels', str(SAMPLES / 'labels.json'),
                                 '--baseline', str(SAMPLES / 'baseline.json'), '--candidate', str(SAMPLES / 'candidate.json')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(6, report['changed_samples'])
        self.assertTrue(report['synthetic_only'])


if __name__ == '__main__':
    unittest.main()
