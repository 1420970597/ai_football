from decimal import Decimal
import unittest

from app.review_period_intent import period_intent_rejection


class PeriodIntentTests(unittest.TestCase):
    def quote(self, **changes):
        return dict(market_kind='total', line=Decimal('3.75'), selection='over',
                    period='full_time', **changes)

    def test_explicit_matching_periods_and_both_totals_orders(self):
        for text, period in (('全场 3.5/4大', 'full_time'), ('上半大3.5/4', 'first_half'),
                             ('上半场大3.75', 'first_half'), ('下半場３.５／４大', 'second_half')):
            quote=self.quote();quote['period']=period
            self.assertIsNone(period_intent_rejection(text,quote))
            quote['period']='first_half' if period=='full_time' else 'full_time'
            self.assertEqual('source_period_intent_conflict',period_intent_rejection(text,quote))

    def test_ambiguous_shorthand_does_not_become_a_full_time_pick(self):
        for text in ('半大3.5/4 等水上','半场大3.75','半場３.５／４大'):
            for period in ('full_time','first_half','second_half'):
                quote=self.quote();quote['period']=period
                self.assertEqual('source_period_intent_ambiguous',period_intent_rejection(text,quote))

    def test_multiple_picks_are_scoped_to_the_same_line_and_direction(self):
        text='上半大1.5，全场3.5/4大'
        self.assertIsNone(period_intent_rejection(text,self.quote()))
        quote=self.quote();quote.update(line=Decimal('1.5'),period='first_half')
        self.assertIsNone(period_intent_rejection(text,quote))
        quote['period']='full_time'
        self.assertEqual('source_period_intent_conflict',period_intent_rejection(text,quote))
        self.assertIsNone(period_intent_rejection('上半小3.75',self.quote()))

    def test_unrelated_scores_images_stakes_and_invalid_splits_do_not_invent_period(self):
        for text in ('','上半1-1，下半还有时间','半注大3.5/4','大3.5/4','全场大3/4'):
            self.assertIsNone(period_intent_rejection(text,self.quote()))
        quote=self.quote();quote['market_kind']='handicap'
        self.assertIsNone(period_intent_rejection('半大3.5/4',quote))
