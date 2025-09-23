from __future__ import annotations

from typing import Any, Dict, List, Optional

from django.utils import timezone
from rest_framework import serializers

from matches.models import AiInsight, Article, Match, MatchDetail, MatchRecommendation, MatchTimelineEntry, SiteConfiguration, Team


class TeamBasicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = ['id', 'name', 'short_name', 'league', 'league_id']


class AiInsightSerializer(serializers.ModelSerializer):
    class Meta:
        model = AiInsight
        fields = ['id', 'headline', 'key_points', 'verdict', 'predicted_score', 'total_goals', 'confidence', 'model_name', 'provider', 'risk_factors', 'created_at']


class ArticleSerializer(serializers.ModelSerializer):
    published_at = serializers.SerializerMethodField()

    class Meta:
        model = Article
        fields = ['id', 'title', 'url', 'summary', 'author', 'source', 'published_at', 'publish_time_text', 'image_url']

    def get_published_at(self, obj: Article) -> Optional[str]:
        if obj.publish_datetime:
            dt = obj.publish_datetime
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return dt.isoformat()
        return None


class MatchRecommendationSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchRecommendation
        fields = ['id', 'title', 'summary', 'recommendation_points', 'confidence_level']


class MatchTimelineSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchTimelineEntry
        fields = ['id', 'event_type', 'headline', 'details', 'verdict', 'predicted_score', 'total_goals', 'confidence', 'created_at', 'payload']


class TeamWithStatsSerializer(TeamBasicSerializer):
    stats = serializers.SerializerMethodField()

    class Meta(TeamBasicSerializer.Meta):
        fields = TeamBasicSerializer.Meta.fields + ['stats']

    def get_stats(self, obj: Team) -> List[Dict[str, Any]]:
        match: Match = self.context.get('match')
        side = self.context.get('side')
        if not match or not side:
            return []
        detail = getattr(match, 'detail', None)
        if not detail or not detail.statistics:
            return []
        return build_team_stats(detail.statistics, side)


class MatchListSerializer(serializers.ModelSerializer):
    home_team = TeamBasicSerializer()
    away_team = TeamBasicSerializer()
    latest_insight = serializers.SerializerMethodField()
    articles_count = serializers.IntegerField(source='articles_count', read_only=True)

    class Meta:
        model = Match
        fields = [
            'id', 'external_id', 'league', 'match_datetime', 'status', 'venue',
            'home_team', 'away_team', 'home_score', 'away_score',
            'odds_home', 'odds_draw', 'odds_away', 'articles_count', 'latest_insight'
        ]

    def get_latest_insight(self, obj: Match) -> Optional[Dict[str, Any]]:
        insight = obj.ai_insights.order_by('-created_at').first()
        if not insight:
            return None
        return AiInsightSerializer(insight).data


class MatchDetailSerializer(serializers.ModelSerializer):
    home_team = serializers.SerializerMethodField()
    away_team = serializers.SerializerMethodField()
    ai_insights = AiInsightSerializer(many=True, read_only=True)
    articles = ArticleSerializer(many=True, read_only=True)
    recommendations = MatchRecommendationSerializer(many=True, read_only=True)

    class Meta:
        model = Match
        fields = [
            'id', 'external_id', 'league', 'match_datetime', 'status', 'venue', 'round_name',
            'home_team', 'away_team', 'home_score', 'away_score',
            'odds_home', 'odds_draw', 'odds_away', 'ai_insights', 'articles', 'recommendations'
        ]

    def get_home_team(self, obj: Match) -> Dict[str, Any]:
        serializer = TeamWithStatsSerializer(obj.home_team, context={'match': obj, 'side': 'home'})
        data = serializer.data
        data['stats'] = data.get('stats') or []
        return data

    def get_away_team(self, obj: Match) -> Dict[str, Any]:
        serializer = TeamWithStatsSerializer(obj.away_team, context={'match': obj, 'side': 'away'})
        data = serializer.data
        data['stats'] = data.get('stats') or []
        return data


class MatchDetailResponseSerializer(serializers.Serializer):
    match = MatchDetailSerializer()
    detail = serializers.SerializerMethodField()

    def get_detail(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        match: Match = obj['match']
        detail: Optional[MatchDetail] = getattr(match, 'detail', None)
        if not detail:
            return {}
        return {
            'statistics': detail.statistics,
            'injuries': detail.injuries,
            'shooters': detail.shooters,
            'future_matches': detail.future_matches,
            'history': detail.history,
        }


class SiteConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteConfiguration
        fields = [
            'ai_base_url', 'ai_api_key', 'ai_model',
            'article_prompt', 'match_prompt', 'consensus_prompt',
            'update_interval_minutes', 'last_update_at', 'last_article_sync_at', 'sogou_enabled'
        ]
        extra_kwargs = {
            'ai_api_key': {'write_only': True},
        }


def build_team_stats(statistics: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    value = statistics.get('value') or {}
    prefix = 'home' if side == 'home' else 'away'

    def pick(block: str, field: str):
        data = value.get(block) or {}
        return data.get(f"{prefix}{field}") or data.get(field)

    total_matches = (value.get('eachHomeAway') or {}).get('totalLegCnt')
    goal_avg = pick('goalAvg', 'GoalAvgCnt')
    loss_avg = pick('lossGoalAvg', 'LossGoalAvgCnt')

    stats_card = {
        'season': '\u8fd110\u573a',
        'wins': pick('eachHomeAway', 'WinGoalMatchCnt') or 0,
        'draws': pick('eachHomeAway', 'DrawMatchCnt') or 0,
        'losses': pick('eachHomeAway', 'LossGoalMatchCnt') or 0,
        'shots_average': goal_avg,
        'conceded_average': loss_avg,
        'possession_average': None,
        'passing_accuracy': None,
        'form': f"\u573a\u6b21 {total_matches or 0}\uff0c\u573a\u5747\u8fdb\u7403 {goal_avg or '-'} \uff0c\u573a\u5747\u5931\u7403 {loss_avg or '-'}"
    }

    same_home = {
        'season': '\u540c\u4e3b\u5ba2\u573a',
        'wins': pick('sameHomeAway', 'WinGoalMatchCnt') or 0,
        'draws': pick('sameHomeAway', 'DrawMatchCnt') or 0,
        'losses': pick('sameHomeAway', 'LossGoalMatchCnt') or 0,
        'shots_average': pick('goalAvg', 'GoalAvgCnt'),
        'conceded_average': pick('lossGoalAvg', 'LossGoalAvgCnt'),
        'possession_average': None,
        'passing_accuracy': None,
        'form': f"\u5f97\u5206\u5360\u6bd4 {pick('goalAvg', 'GoalAvgCntRatio') or '-'}\uff05\uff0c\u5931\u5206\u5360\u6bd4 {pick('lossGoalAvg', 'LossGoalAvgCntRatio') or '-'}\uff05"
    }

    recent_form = {
        'season': '\u8fd1\u671f\u8d70\u52bf',
        'wins': pick('last', 'WinGoalMatchCnt') or 0,
        'draws': pick('last', 'DrawMatchCnt') or 0,
        'losses': pick('last', 'LossGoalMatchCnt') or 0,
        'shots_average': None,
        'conceded_average': None,
        'possession_average': None,
        'passing_accuracy': None,
        'form': f"\u8fdb\u7403\u5360\u6bd4 {pick('last', 'ScoreRatio') or '-'}\uff05"
    }

    return [stats_card, same_home, recent_form]
