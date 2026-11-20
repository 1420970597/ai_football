import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

from infra.profitability.collect_analysis import prepare_query


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = os.environ.get('TEST_DATABASE_ADMIN_URL')


@unittest.skipUnless(TEST_DATABASE, 'isolated Docker PostgreSQL required')
class ProfitabilityOpportunityCoverageTest(unittest.IsolatedAsyncioTestCase):
    async def test_reports_only_the_bound_automatic_enforced_attempt(self):
        parts = urlsplit(TEST_DATABASE)
        if (parts.scheme, parts.username, parts.hostname, parts.path) != (
                'postgresql', 'postgres', 'ai-football-v2-pgverify', '/postgres'):
            raise RuntimeError('Opportunity coverage test requires the isolated Docker test database')
        begin = datetime(2026, 11, 18, 2, tzinfo=timezone.utc)
        sql = (ROOT / 'infra/profitability/opportunity_coverage.sql').read_text()
        query, args = prepare_query(sql, {'begin': begin})
        fixture = ROOT / 'tests/integration/profitability_opportunity_coverage.sql'
        conn = await asyncpg.connect(TEST_DATABASE, timeout=5, command_timeout=7)
        try:
            await conn.fetchval("SELECT set_config('profitability_test.starts_at', $1, false)", begin.isoformat())
            await conn.execute(fixture.read_text())
            async with conn.transaction(isolation='repeatable_read', readonly=True):
                result = json.loads(await conn.fetchval(query, *args))
        finally:
            await conn.close()

        common = dict(sport='football', competition_id='league-1', competition_name='League One',
                      phase='live', orders=1, fixtures=1)
        self.assertEqual([
            dict(common, status='below_minimum_odds', reason=None),
            dict(common, status='reference_ineligible', reason='other_recorded'),
            dict(common, status='reference_ineligible', reason='reference_contract_missing'),
        ], result['automatic_price_blockers'])
        overall = {r['reason']: r['orders'] for r in result['automatic_order_stops'] if r['all_markets']}
        self.assertEqual(2, overall['reference_ineligible'])
        self.assertEqual(1, overall['below_minimum_odds'])
