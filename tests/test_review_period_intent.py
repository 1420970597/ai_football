from decimal import Decimal
from datetime import datetime, timedelta, timezone
import unittest

from app.review_period_intent import period_intent_rejection, verified_early_first_half_context, premarket_period_rejection


class PeriodIntentTests(unittest.TestCase):
    def quote(self, **changes):
        value = dict(market_kind='total', line=Decimal('3.75'), selection='over', period='full_time')
        value.update(changes)
        return value

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

    def test_adjacent_line_list_inherits_period_and_direction_for_every_line(self):
        for text in ('半大0.75 0.5   来开门红', '半场0.5/1大 0.5', '半場大０.７５\t０.５'):
            for line in (Decimal('.75'), Decimal('.5')):
                for period in ('full_time', 'first_half', 'second_half'):
                    with self.subTest(text=text, line=line, period=period):
                        self.assertEqual('source_period_intent_ambiguous', period_intent_rejection(
                            text, self.quote(line=line, period=period)))
        for text, period in (('上半大0.75 0.5', 'first_half'),
                             ('下半0.75大 0.5', 'second_half'),
                             ('全场小0.75 0.5', 'full_time')):
            selection = 'under' if '小' in text else 'over'
            quote = self.quote(line=Decimal('.5'), period=period, selection=selection)
            self.assertIsNone(period_intent_rejection(text, quote))
            quote['period'] = 'first_half' if period == 'full_time' else 'full_time'
            self.assertEqual('source_period_intent_conflict', period_intent_rejection(text, quote))

    def test_line_list_does_not_inherit_across_amounts_odds_directions_or_newlines(self):
        for text in ('半大0.75 0.5注', '半大0.75 0.5 注', '半大0.75 0.5u',
                     '半大0.75 0.5 倍', '半大0.75 1.92 0.5',
                     '半大0.75\n0.5', '半大0.75；0.5', '半大0.75 小0.5',
                     '半大0.75 0.5/1.5', '半大0.75 0.5大'):
            with self.subTest(text=text):
                self.assertIsNone(period_intent_rejection(text, self.quote(line=Decimal('.5'))))
        self.assertIsNone(period_intent_rejection('半小0.75 0.5', self.quote(line=Decimal('.5'))))

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

    def test_verified_early_leyu_first_half_context_resolves_bare_half(self):
        now = datetime.now(timezone.utc)
        quote = self.quote(period='first_half', fixture_id='leyu:5821685', source_id='leyu:5821685',
                           phase='live', fixture_sport='football', fixture_match_state='6',
                           fixture_clock_minute=15, fixture_received_at=now,
                           intent_candidate={'id':'leyu:5821685','source_id':'leyu:5821685',
                               'sport':'football','phase':'live','match_state':'6','clock_minute':15,
                               'received_at':(now-timedelta(seconds=5)).isoformat()})
        posted = now - timedelta(seconds=10)
        self.assertIsNone(period_intent_rejection('半场大3.75', quote, posted_at=posted, now=now))
        for line in (Decimal('.75'), Decimal('.5')):
            quote['line'] = line
            self.assertIsNone(period_intent_rejection('半大0.75 0.5 来开门红', quote, posted_at=posted, now=now))
            self.assertIsNone(premarket_period_rejection('半大0.75 0.5 来开门红',quote,
                {'algorithm':'premarket-source-intent-v1','period':None},has_visual_source=True,
                posted_at=posted,now=now))
            evidence = verified_early_first_half_context('半大0.75 0.5 来开门红', quote, posted_at=posted, now=now)
            self.assertEqual('leyu-early-first-half-stage-v2', evidence['algorithm'])
            stale_posted = now - timedelta(minutes=10)
            self.assertEqual('source_period_intent_ambiguous', period_intent_rejection(
                '半大0.75 0.5 来开门红', quote, posted_at=stale_posted, now=now))
            self.assertIsNone(verified_early_first_half_context(
                '半大0.75 0.5 来开门红', quote, posted_at=stale_posted, now=now))

    def test_early_context_does_not_resolve_stale_or_non_leyu_half(self):
        now = datetime.now(timezone.utc)
        quote = self.quote(period='first_half', fixture_id='source:1', source_id='source:1',
                           phase='live', fixture_sport='football', fixture_match_state='6',
                           fixture_clock_minute=15, fixture_received_at=now,
                           intent_candidate={'id':'source:1','source_id':'source:1','sport':'football',
                               'phase':'live','match_state':'6','clock_minute':15,
                               'received_at':(now-timedelta(seconds=5)).isoformat()})
        self.assertEqual('source_period_intent_ambiguous', period_intent_rejection(
            '半场大3.75', quote, posted_at=now-timedelta(seconds=10), now=now))
