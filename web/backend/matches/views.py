from datetime import datetime

from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import AIInsight, Article, Match, Recommendation, Team
from .serializers import (
    AIInsightSerializer,
    ArticleSerializer,
    MatchDetailSerializer,
    MatchListSerializer,
    RecommendationSerializer,
    TeamSerializer,
)


class TeamViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Team.objects.all().prefetch_related('stats')
    serializer_class = TeamSerializer


class MatchViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Match.objects.select_related('home_team', 'away_team').prefetch_related(
        'ai_insights',
        'recommendations',
        Prefetch('articles', queryset=Article.objects.order_by('-published_at')),
    )

    def get_serializer_class(self):
        if self.action == 'list':
            return MatchListSerializer
        return MatchDetailSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        league = params.get('league')
        if league:
            qs = qs.filter(league__iexact=league)

        team = params.get('team')
        if team:
            qs = qs.filter(Q(home_team__name__icontains=team) | Q(away_team__name__icontains=team))

        status = params.get('status')
        if status:
            qs = qs.filter(status=status)

        date_from = params.get('date_from')
        if date_from:
            try:
                start = datetime.fromisoformat(date_from)
                qs = qs.filter(match_datetime__gte=start)
            except ValueError:
                pass

        date_to = params.get('date_to')
        if date_to:
            try:
                end = datetime.fromisoformat(date_to)
                qs = qs.filter(match_datetime__lte=end)
            except ValueError:
                pass

        upcoming = params.get('upcoming', 'true').lower() == 'true'
        if upcoming and not status:
            qs = qs.filter(match_datetime__gte=timezone.now()).order_by('match_datetime')
        else:
            qs = qs.order_by('-match_datetime')

        return qs

    @action(detail=True, methods=['get'])
    def timeline(self, request, *args, **kwargs):
        match = self.get_object()
        insights = match.ai_insights.order_by('-created_at')
        serializer = AIInsightSerializer(insights, many=True)
        return Response({
            'match_id': match.id,
            'insights': serializer.data,
        })


class ArticleViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Article.objects.select_related('match')
    serializer_class = ArticleSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        match_id = self.request.query_params.get('match')
        if match_id:
            qs = qs.filter(match_id=match_id)
        return qs.order_by('-published_at')


class AIInsightViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AIInsight.objects.select_related('match')
    serializer_class = AIInsightSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        match_id = self.request.query_params.get('match')
        if match_id:
            qs = qs.filter(match_id=match_id)
        return qs.order_by('-created_at')


class RecommendationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Recommendation.objects.select_related('match')
    serializer_class = RecommendationSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        match_id = self.request.query_params.get('match')
        if match_id:
            qs = qs.filter(match_id=match_id)
        return qs.order_by('-created_at')

