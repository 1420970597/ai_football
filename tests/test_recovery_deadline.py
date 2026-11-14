from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import AsyncMock
from app.review_recovery import market_recovery


class RecoveryDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_recovery_cannot_be_scheduled_past_original_cutoff(self):
        now=datetime.now(timezone.utc)
        for age in (0,8,19,22):
            job={'review_mode':'real_time','posted_at':now,'detected_at':now-timedelta(seconds=age)}
            conn=AsyncMock()
            result=await market_recovery(conn,job,'attempt',1)
            self.assertIsNone(result);conn.fetch.assert_not_awaited()

    async def test_historical_and_exhausted_still_do_not_recover(self):
        now=datetime.now(timezone.utc)
        conn=AsyncMock()
        self.assertIsNone(await market_recovery(conn,{'review_mode':'historical_replay','posted_at':now},'attempt',1))
        self.assertIsNone(await market_recovery(conn,{'review_mode':'real_time','posted_at':now},'attempt',2))
        conn.fetch.assert_not_awaited()
