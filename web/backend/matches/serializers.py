from rest_framework import serializers

from .models import AIInsight, Article, Match, Recommendation, Team, TeamStat


class TeamStatSerializer(serializers.ModelSerializer):
    class Meta:
        model = TeamStat
        fields = [
            'season',
            'matches_played',
            'wins',
            'draws',
            'losses',
            'goals_for',
            'goals_against',
            'possession_average',
            'shots_average',
            'passing_accuracy',
            'form',
            'form_description',
        ]


class TeamSerializer(serializers.ModelSerializer):
    stats = TeamStatSerializer(many=True, read_only=True)

    class Meta:
        model = Team
        fields = [
            'id',
            'name',
            'short_name',
            'league',
            'country',
            'founded_year',
            'coach',
            'stadium',
            'crest_url',
            'stats',
        ]


class ArticleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Article
        fields = [
            'id',
            'title',
            'url',
            'source',
            'summary',
            'author',
            'published_at',
            'content',
            'created_at',
        ]


class AIInsightSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIInsight
        fields = [
            'id',
            'provider',
            'model_name',
            'verdict',
            'confidence',
            'predicted_score',
            'total_goals',
            'headline',
            'key_points',
            'risk_factors',
            'raw_payload',
            'created_at',
        ]


class RecommendationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recommendation
        fields = [
            'id',
            'title',
            'summary',
            'recommendation_points',
            'confidence_level',
            'risk_notes',
            'created_at',
        ]


class MatchListSerializer(serializers.ModelSerializer):
    home_team = TeamSerializer(read_only=True)
    away_team = TeamSerializer(read_only=True)
    latest_insight = serializers.SerializerMethodField()

    class Meta:
        model = Match
        fields = [
            'id',
            'league',
            'round_name',
            'venue',
            'match_datetime',
            'status',
            'home_team',
            'away_team',
            'home_score',
            'away_score',
            'odds_home',
            'odds_draw',
            'odds_away',
            'latest_insight',
        ]

    def get_latest_insight(self, obj):
        insight = obj.ai_insights.order_by('-created_at').first()
        if not insight:
            return None
        return AIInsightSerializer(insight).data


class MatchDetailSerializer(MatchListSerializer):
    articles = ArticleSerializer(many=True, read_only=True)
    ai_insights = AIInsightSerializer(many=True, read_only=True)
    recommendations = RecommendationSerializer(many=True, read_only=True)

    class Meta(MatchListSerializer.Meta):
        fields = MatchListSerializer.Meta.fields + [
            'articles',
            'ai_insights',
            'recommendations',
            'created_at',
            'updated_at',
        ]

