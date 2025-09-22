from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Team(TimeStampedModel):
    name = models.CharField(max_length=128, unique=True)
    short_name = models.CharField(max_length=32, blank=True)
    league = models.CharField(max_length=128, blank=True)
    country = models.CharField(max_length=64, blank=True)
    founded_year = models.PositiveIntegerField(null=True, blank=True)
    coach = models.CharField(max_length=128, blank=True)
    stadium = models.CharField(max_length=128, blank=True)
    crest_url = models.URLField(blank=True)

    def __str__(self) -> str:
        return self.name


class TeamStat(TimeStampedModel):
    team = models.ForeignKey(Team, related_name='stats', on_delete=models.CASCADE)
    season = models.CharField(max_length=32)
    matches_played = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    draws = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    goals_for = models.PositiveIntegerField(default=0)
    goals_against = models.PositiveIntegerField(default=0)
    possession_average = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    shots_average = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    passing_accuracy = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    form = models.CharField(max_length=128, blank=True, help_text='Recent form string such as W-W-D-L-W')
    form_description = models.TextField(blank=True)

    class Meta:
        unique_together = ('team', 'season')
        ordering = ['-season']

    def __str__(self) -> str:
        return f"{self.team.name} {self.season}"


class Match(TimeStampedModel):
    STATUS_SCHEDULED = 'scheduled'
    STATUS_LIVE = 'live'
    STATUS_FINISHED = 'finished'
    STATUS_POSTPONED = 'postponed'
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, 'Scheduled'),
        (STATUS_LIVE, 'Live'),
        (STATUS_FINISHED, 'Finished'),
        (STATUS_POSTPONED, 'Postponed'),
    ]

    external_id = models.CharField(max_length=64, blank=True, null=True, unique=True)
    league = models.CharField(max_length=128)
    round_name = models.CharField(max_length=64, blank=True)
    venue = models.CharField(max_length=128, blank=True)
    match_datetime = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_SCHEDULED)
    home_team = models.ForeignKey(Team, related_name='home_matches', on_delete=models.PROTECT)
    away_team = models.ForeignKey(Team, related_name='away_matches', on_delete=models.PROTECT)
    home_score = models.PositiveIntegerField(null=True, blank=True)
    away_score = models.PositiveIntegerField(null=True, blank=True)
    odds_home = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    odds_draw = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    odds_away = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    last_checked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['match_datetime']
        indexes = [
            models.Index(fields=['match_datetime']),
            models.Index(fields=['league']),
        ]
        unique_together = (
            ('match_datetime', 'home_team', 'away_team'),
        )

    def __str__(self) -> str:
        return f"{self.home_team} vs {self.away_team}"


class Article(TimeStampedModel):
    match = models.ForeignKey(Match, related_name='articles', on_delete=models.CASCADE)
    title = models.CharField(max_length=256)
    url = models.URLField(unique=True)
    source = models.CharField(max_length=128, blank=True)
    summary = models.TextField(blank=True)
    author = models.CharField(max_length=128, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    content = models.TextField(blank=True)

    class Meta:
        ordering = ['-published_at', '-created_at']

    def __str__(self) -> str:
        return self.title


class AIInsight(TimeStampedModel):
    match = models.ForeignKey(Match, related_name='ai_insights', on_delete=models.CASCADE)
    provider = models.CharField(max_length=64, default='internal')
    model_name = models.CharField(max_length=128, blank=True)
    verdict = models.CharField(max_length=32, blank=True, help_text='home/draw/away')
    confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    predicted_score = models.CharField(max_length=32, blank=True)
    total_goals = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    headline = models.CharField(max_length=256, blank=True)
    key_points = models.TextField(blank=True)
    risk_factors = models.TextField(blank=True)
    raw_payload = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"AI insight for {self.match}"


class Recommendation(TimeStampedModel):
    match = models.ForeignKey(Match, related_name='recommendations', on_delete=models.CASCADE)
    title = models.CharField(max_length=128)
    summary = models.TextField(blank=True)
    recommendation_points = models.JSONField(default=list, blank=True)
    confidence_level = models.CharField(max_length=32, blank=True)
    risk_notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"Recommendation for {self.match}"

