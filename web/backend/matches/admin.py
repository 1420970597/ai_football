from django.contrib import admin

from matches.models import AiInsight, Article, Match, MatchDetail, MatchRecommendation, MatchTimelineEntry, SiteConfiguration, Team


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('name', 'short_name', 'league', 'league_id')
    search_fields = ('name', 'short_name', 'league')


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ('id', 'league', 'match_datetime', 'home_team', 'away_team', 'status')
    list_filter = ('league', 'status')
    search_fields = ('home_team__name', 'away_team__name', 'league')
    autocomplete_fields = ('home_team', 'away_team')


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ('title', 'match', 'source', 'publish_datetime', 'status')
    list_filter = ('status', 'source')
    search_fields = ('title', 'summary', 'match__league')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AiInsight)
class AiInsightAdmin(admin.ModelAdmin):
    list_display = ('match', 'headline', 'verdict', 'confidence', 'model_name', 'created_at')
    list_filter = ('verdict', 'model_name')
    search_fields = ('headline', 'match__home_team__name', 'match__away_team__name')


@admin.register(MatchDetail)
class MatchDetailAdmin(admin.ModelAdmin):
    list_display = ('match', 'updated_at')


@admin.register(MatchTimelineEntry)
class MatchTimelineAdmin(admin.ModelAdmin):
    list_display = ('match', 'event_type', 'headline', 'created_at')
    list_filter = ('event_type',)


@admin.register(MatchRecommendation)
class MatchRecommendationAdmin(admin.ModelAdmin):
    list_display = ('match', 'title', 'confidence_level', 'created_at')


@admin.register(SiteConfiguration)
class SiteConfigurationAdmin(admin.ModelAdmin):
    list_display = ('ai_model', 'update_interval_minutes', 'last_update_at')
