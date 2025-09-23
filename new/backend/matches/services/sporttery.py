from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

import requests
from django.utils import timezone

from matches.models import Match

logger = logging.getLogger(__name__)


@dataclass
class TeamPayload:
    external_id: int
    name: str
    short_name: str
    league: str
    league_id: str


@dataclass
class MatchPayload:
    match_id: int
    business_date: str
    match_datetime: datetime
    league: str
    league_id: str
    venue: str
    round_name: str
    status: str
    home: TeamPayload
    away: TeamPayload
    odds: Dict[str, float]
    raw: Dict


class SportteryClient:
    MATCH_LIST_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchListV1.qry"
    ANALYSIS_ENDPOINTS = {
        'history': "https://webapi.sporttery.cn/gateway/uniform/football/getResultHistoryV1.qry",
        'tables': "https://webapi.sporttery.cn/gateway/uniform/football/getMatchTablesV2.qry",
        'future': "https://webapi.sporttery.cn/gateway/uniform/football/getFutureMatchesV1.qry",
        'players': "https://webapi.sporttery.cn/gateway/uniform/football/getMatchPlayerV1.qry",
        'injury': "https://webapi.sporttery.cn/gateway/uniform/football/getInjurySuspensionV1.qry",
        'feature': "https://webapi.sporttery.cn/gateway/uniform/football/getMatchFeatureV1.qry",
    }

    STATUS_MAPPING = {
        'Selling': Match.STATUS_SCHEDULED,
        'Open': Match.STATUS_SCHEDULED,
        'UnSale': Match.STATUS_SCHEDULED,
        'Waiting': Match.STATUS_SCHEDULED,
        'Playing': Match.STATUS_LIVE,
        'Final': Match.STATUS_FINISHED,
        'Cancle': Match.STATUS_POSTPONED,
        'Cancel': Match.STATUS_POSTPONED,
        'Delay': Match.STATUS_POSTPONED,
        'Delayed': Match.STATUS_POSTPONED,
        'Suspend': Match.STATUS_POSTPONED,
        'Suspended': Match.STATUS_POSTPONED,
        'Resulted': Match.STATUS_FINISHED,
    }

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; AIFootballBot/1.0)'
        })

    def _make_datetime(self, date_str: str, time_str: str) -> datetime:
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        return timezone.make_aware(dt, timezone.get_current_timezone())

    def _map_status(self, status: Optional[str]) -> str:
        if not status:
            return Match.STATUS_SCHEDULED
        return self.STATUS_MAPPING.get(status, Match.STATUS_SCHEDULED)

    def fetch_schedule(self) -> List[MatchPayload]:
        params = {'clientCode': '3001'}
        response = self.session.get(self.MATCH_LIST_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        value = data.get('value') or {}
        match_info_list: Iterable[Dict] = value.get('matchInfoList') or []

        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        target_dates = {today.isoformat(), tomorrow.isoformat()}

        matches: List[MatchPayload] = []

        for day_info in match_info_list:
            business_date = day_info.get('businessDate')
            if business_date not in target_dates:
                continue
            for match in day_info.get('subMatchList', []):
                try:
                    matches.append(self._convert_match(match, business_date))
                except Exception as exc:  # pylint: disable=broad-except
                    logger.exception("Failed to convert match payload", exc_info=exc)
        return matches

    def _convert_match(self, payload: Dict, business_date: str) -> MatchPayload:
        match_id = int(payload['matchId'])
        league = payload.get('leagueAllName') or payload.get('leagueShortName') or ''
        league_id = str(payload.get('leagueId') or '')
        venue = payload.get('venueName', '')
        round_name = payload.get('matchWeek', '')
        status = self._map_status(payload.get('matchStatus'))

        match_datetime = self._make_datetime(
            payload.get('matchDate', business_date),
            payload.get('matchTime', '00:00')
        )

        odds = self._parse_odds(payload.get('oddsList', []))

        home = TeamPayload(
            external_id=int(payload.get('homeTeamId') or 0),
            name=payload.get('homeTeamAllName') or payload.get('homeTeamAbbName') or 'Unknown',
            short_name=payload.get('homeTeamAbbName') or '',
            league=league,
            league_id=league_id,
        )
        away = TeamPayload(
            external_id=int(payload.get('awayTeamId') or 0),
            name=payload.get('awayTeamAllName') or payload.get('awayTeamAbbName') or 'Unknown',
            short_name=payload.get('awayTeamAbbName') or '',
            league=league,
            league_id=league_id,
        )

        return MatchPayload(
            match_id=match_id,
            business_date=business_date,
            match_datetime=match_datetime,
            league=league,
            league_id=league_id,
            venue=venue or '',
            round_name=round_name or '',
            status=status,
            home=home,
            away=away,
            odds=odds,
            raw=payload,
        )

    def _parse_odds(self, odds_list: Iterable[Dict]) -> Dict[str, float]:
        odds_map: Dict[str, float] = {}
        for odds in odds_list or []:
            pool_code = odds.get('poolCode')
            if pool_code not in {'HAD', 'HHAD'}:
                continue
            try:
                if odds.get('h'):
                    odds_map['home'] = float(odds['h'])
                if odds.get('d'):
                    odds_map['draw'] = float(odds['d'])
                if odds.get('a'):
                    odds_map['away'] = float(odds['a'])
            except (TypeError, ValueError):
                logger.debug("Invalid odds payload: %s", odds)
        return odds_map

    def fetch_match_analysis(self, match_id: int) -> Dict[str, Dict]:
        results: Dict[str, Dict] = {}
        for key, endpoint in self.ANALYSIS_ENDPOINTS.items():
            params: Dict[str, str]
            if key == 'tables':
                params = {'gmMatchId': str(match_id)}
            else:
                params = {'sportteryMatchId': str(match_id), 'termLimits': '10'}
            try:
                response = self.session.get(endpoint, params=params, timeout=15)
                response.raise_for_status()
                payload = response.json()
                results[key] = payload
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("Fetch %s failed for match %s: %s", key, match_id, exc)
                results[key] = {
                    'success': False,
                    'error': str(exc),
                }
        return results


sporttery_client = SportteryClient()
