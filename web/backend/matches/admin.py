from django.contrib import admin

from .models import AIInsight, Article, Match, Recommendation, Team, TeamStat


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('name', 'league', 'country', 'coach')
    search_fields = ('name', 'league', 'country')


@admin.register(TeamStat)
class TeamStatAdmin(admin.ModelAdmin):
    list_display = ('team', 'season', 'matches_played', 'wins', 'draws', 'losses')
    list_filter = ('season',)
    search_fields = ('team__name',)


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ('league', 'match_datetime', 'home_team', 'away_team', 'status')
    search_fields = ('league', 'home_team__name', 'away_team__name')
    list_filter = ('league', 'status')
    date_hierarchy = 'match_datetime'


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ('title', 'match', 'source', 'published_at')
    search_fields = ('title', 'source')
    list_filter = ('source',)


@admin.register(AIInsight)
class AIInsightAdmin(admin.ModelAdmin):
    list_display = ('match', 'provider', 'verdict', 'confidence', 'created_at')
    search_fields = ('match__home_team__name', 'match__away_team__name', 'provider')
    list_filter = ('provider',)


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = ('match', 'title', 'confidence_level', 'created_at')
    search_fields = ('title', 'match__home_team__name', 'match__away_team__name')
    list_filter = ('confidence_level',)

