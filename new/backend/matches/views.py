from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from matches.models import Match, MatchTimelineEntry, SiteConfiguration
from matches.runtime import get_update_coordinator
from matches.serializers import (
    MatchDetailSerializer,
    MatchListSerializer,
    MatchTimelineSerializer,
    SiteConfigurationSerializer,
)


class MatchViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = (
        Match.objects.select_related('home_team', 'away_team', 'detail')
        .prefetch_related('ai_insights', 'articles', 'recommendations')
        .annotate(articles_count=Count('articles'))
    )
    serializer_class = MatchListSerializer
    ordering = ['match_datetime']

    def get_queryset(self):
        qs = super().get_queryset()
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        qs = qs.filter(business_date__in=[today.isoformat(), tomorrow.isoformat()])

        league = self.request.query_params.get('league')
        if league:
            qs = qs.filter(league__icontains=league)

        team = self.request.query_params.get('team')
        if team:
            qs = qs.filter(Q(home_team__name__icontains=team) | Q(away_team__name__icontains=team))

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        upcoming = self.request.query_params.get('upcoming')
        if upcoming in {'true', '1'}:
            qs = qs.filter(match_datetime__gte=timezone.now())

        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(match_datetime__date__gte=date_from)
        if date_to:
            qs = qs.filter(match_datetime__date__lte=date_to)

        return qs.order_by('match_datetime')

    def list(self, request, *args, **kwargs):  # pylint: disable=arguments-differ
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page or queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response({'results': serializer.data, 'count': len(serializer.data)})

    def retrieve(self, request, *args, **kwargs):  # pylint: disable=arguments-differ
        match = self.get_object()
        serializer = MatchDetailSerializer(match)
        detail = getattr(match, 'detail', None)
        payload = {
            'match': serializer.data,
            'detail': {}
        }
        if detail:
            payload['detail'] = {
                'statistics': detail.statistics,
                'injuries': detail.injuries,
                'shooters': detail.shooters,
                'future_matches': detail.future_matches,
                'history': detail.history,
            }
        return Response(payload)

    @action(detail=True, methods=['get'])
    def timeline(self, request, pk=None):  # pylint: disable=unused-argument
        match = self.get_object()
        entries = MatchTimelineEntry.objects.filter(match=match).order_by('-created_at')
        serializer = MatchTimelineSerializer(entries, many=True)
        return Response({'insights': serializer.data})


class SiteConfigurationView(APIView):
    def get(self, request):
        config = SiteConfiguration.load()
        serializer = SiteConfigurationSerializer(config)
        return Response(serializer.data)

    def put(self, request):
        config = SiteConfiguration.load()
        serializer = SiteConfigurationSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ManualUpdateTriggerView(APIView):
    def post(self, request):
        try:
            coordinator = get_update_coordinator()
        except RuntimeError:
            return Response({'detail': 'update services not running'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        coordinator.trigger_now()
        return Response({'detail': 'update triggered'})
