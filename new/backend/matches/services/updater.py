from __future__ import annotations

import logging
import threading
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from matches.models import Match, MatchDetail, SiteConfiguration, Team
from matches.services.articles import ArticleCrawlerPool, sync_articles_for_match
from matches.services.sporttery import MatchPayload, TeamPayload, sporttery_client

logger = logging.getLogger(__name__)


class MatchDataUpdater:
    def __init__(self, article_pool: ArticleCrawlerPool) -> None:
        self.article_pool = article_pool

    def sync(self) -> None:
        logger.info("Starting match data sync")
        matches = sporttery_client.fetch_schedule()
        config = SiteConfiguration.load()

        for payload in matches:
            match = self._upsert_match(payload)
            self._sync_details(match)
            try:
                sync_articles_for_match(match, pool=self.article_pool)
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Article sync failed for match %s", match.id, exc_info=exc)

        config.update_timestamps(match_sync=True, article_sync=True)
        logger.info("Match sync completed")

    def _upsert_match(self, payload: MatchPayload) -> Match:
        home = self._get_or_create_team(payload.home)
        away = self._get_or_create_team(payload.away)

        odds_home = self._to_decimal(payload.odds.get('home'))
        odds_draw = self._to_decimal(payload.odds.get('draw'))
        odds_away = self._to_decimal(payload.odds.get('away'))

        defaults = {
            'business_date': payload.business_date,
            'match_datetime': payload.match_datetime,
            'league': payload.league,
            'league_id': payload.league_id,
            'venue': payload.venue,
            'round_name': payload.round_name,
            'status': payload.status,
            'home_team': home,
            'away_team': away,
            'odds_home': odds_home,
            'odds_draw': odds_draw,
            'odds_away': odds_away,
            'raw_payload': payload.raw,
        }

        match, created = Match.objects.update_or_create(
            external_id=str(payload.match_id),
            defaults=defaults,
        )
        if created:
            logger.info("Created match %s", match)
        return match

    def _get_or_create_team(self, payload: TeamPayload) -> Team:
        defaults = {
            'name': payload.name,
            'short_name': payload.short_name,
            'league': payload.league,
            'league_id': payload.league_id,
        }
        team, _ = Team.objects.update_or_create(
            external_id=str(payload.external_id),
            defaults=defaults,
        )
        return team

    def _sync_details(self, match: Match) -> None:
        analysis = sporttery_client.fetch_match_analysis(int(match.external_id))
        defaults = {
            'statistics': analysis.get('feature', {}),
            'injuries': analysis.get('injury', {}),
            'shooters': analysis.get('players', {}),
            'future_matches': analysis.get('future', {}),
            'history': analysis.get('history', {}),
            'odds_info': analysis.get('tables', {}),
            'extra': {'source': 'sporttery', 'synced_at': timezone.now().isoformat()},
        }
        MatchDetail.objects.update_or_create(match=match, defaults=defaults)

    @staticmethod
    def _to_decimal(value: Optional[float]) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (ValueError, TypeError):
            return None


class UpdateCoordinator:
    def __init__(self, article_pool: ArticleCrawlerPool) -> None:
        self.article_pool = article_pool
        self.running_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name='match-update-coordinator', daemon=True)
        self.thread.start()
        logger.info("Update coordinator started")

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
            logger.info("Update coordinator stopped")

    def trigger_now(self) -> None:
        threading.Thread(target=self._execute_once, daemon=True).start()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._execute_if_due()
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Match update loop error", exc_info=exc)
            self.stop_event.wait(timeout=60)

    def _execute_if_due(self) -> None:
        config = SiteConfiguration.load()
        interval = timedelta(minutes=config.update_interval_minutes or settings.MATCH_UPDATE_DEFAULT_INTERVAL)
        last = config.last_update_at or timezone.now() - interval * 2
        if timezone.now() - last >= interval:
            self._execute_once()

    def _execute_once(self) -> None:
        if not self.running_lock.acquire(blocking=False):
            logger.debug("Update already running; skipping")
            return
        try:
            updater = MatchDataUpdater(article_pool=self.article_pool)
            with transaction.atomic():
                updater.sync()
        finally:
            self.running_lock.release()


__all__ = ['MatchDataUpdater', 'UpdateCoordinator']
