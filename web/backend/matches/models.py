from django.db import models
from django.utils import timezone


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Team(TimestampedModel):
    name = models.CharField(max_length=255)
    short_name = models.CharField(max_length=64, blank=True)
    external_id = models.CharField(max_length=64, blank=True, unique=True)
    league = models.CharField(max_length=128, blank=True)
    league_id = models.CharField(max_length=64, blank=True)
    country = models.CharField(max_length=128, blank=True)
    logo_url = models.URLField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return self.name


class Match(TimestampedModel):
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

    external_id = models.CharField(max_length=64, unique=True)
    business_date = models.DateField()
    match_datetime = models.DateTimeField()
    league = models.CharField(max_length=128)
    league_id = models.CharField(max_length=64, blank=True)
    venue = models.CharField(max_length=255, blank=True)
    round_name = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_SCHEDULED)

    home_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='home_matches')
    away_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='away_matches')

    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)

    odds_home = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    odds_draw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    odds_away = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    latest_article_synced_at = models.DateTimeField(null=True, blank=True)
    latest_ai_summary_at = models.DateTimeField(null=True, blank=True)

    raw_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['match_datetime']
        indexes = [
            models.Index(fields=['business_date']),
            models.Index(fields=['match_datetime']),
            models.Index(fields=['league']),
        ]

    def __str__(self) -> str:
        return f"{self.home_team} vs {self.away_team}"


class MatchDetail(TimestampedModel):
    match = models.OneToOneField(Match, on_delete=models.CASCADE, related_name='detail')
    statistics = models.JSONField(default=dict, blank=True)
    injuries = models.JSONField(default=dict, blank=True)
    shooters = models.JSONField(default=dict, blank=True)
    future_matches = models.JSONField(default=dict, blank=True)
    history = models.JSONField(default=dict, blank=True)
    odds_info = models.JSONField(default=dict, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"Detail for {self.match_id}"


class Article(TimestampedModel):
    STATUS_PENDING = 'pending'
    STATUS_FETCHED = 'fetched'
    STATUS_ANALYZED = 'analyzed'
    STATUS_FAILED = 'failed'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_FETCHED, 'Fetched'),
        (STATUS_ANALYZED, 'Analyzed'),
        (STATUS_FAILED, 'Failed'),
    ]

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='articles')
    title = models.CharField(max_length=512)
    url = models.URLField(unique=True)
    summary = models.TextField(blank=True)
    author = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=255, blank=True)
    publish_datetime = models.DateTimeField(null=True, blank=True)
    publish_time_text = models.CharField(max_length=64, blank=True)
    image_url = models.URLField(blank=True)
    content_html = models.TextField(blank=True)
    content_text = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-publish_datetime', '-created_at']

    def __str__(self) -> str:
        return self.title


class ArticleAnalysis(TimestampedModel):
    VERDICT_HOME = 'home_win'
    VERDICT_AWAY = 'away_win'
    VERDICT_DRAW = 'draw'
    VERDICT_UNKNOWN = 'unknown'

    VERDICT_CHOICES = [
        (VERDICT_HOME, 'Home Win'),
        (VERDICT_AWAY, 'Away Win'),
        (VERDICT_DRAW, 'Draw'),
        (VERDICT_UNKNOWN, 'Unknown'),
    ]

    article = models.OneToOneField(Article, on_delete=models.CASCADE, related_name='analysis')
    headline = models.CharField(max_length=512, blank=True)
    key_points = models.TextField(blank=True)
    verdict = models.CharField(max_length=16, choices=VERDICT_CHOICES, default=VERDICT_UNKNOWN)
    predicted_score = models.CharField(max_length=32, blank=True)
    total_goals = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    confidence = models.PositiveIntegerField(null=True, blank=True)
    model_name = models.CharField(max_length=128, blank=True)
    tokens_used = models.IntegerField(null=True, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"Analysis for {self.article_id}"


class AiInsight(TimestampedModel):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='ai_insights')
    headline = models.CharField(max_length=512, blank=True)
    key_points = models.TextField(blank=True)
    verdict = models.CharField(max_length=16, choices=ArticleAnalysis.VERDICT_CHOICES, default=ArticleAnalysis.VERDICT_UNKNOWN)
    predicted_score = models.CharField(max_length=32, blank=True)
    total_goals = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    confidence = models.PositiveIntegerField(null=True, blank=True)
    model_name = models.CharField(max_length=128, blank=True)
    provider = models.CharField(max_length=128, blank=True)
    risk_factors = models.TextField(blank=True)
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"AI insight for match {self.match_id}"


class MatchRecommendation(TimestampedModel):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='recommendations')
    title = models.CharField(max_length=255)
    summary = models.TextField()
    recommendation_points = models.JSONField(default=list, blank=True)
    confidence_level = models.CharField(max_length=64, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"Recommendation for match {self.match_id}"


class MatchTimelineEntry(TimestampedModel):
    EVENT_ARTICLE_SUMMARY = 'article_summary'
    EVENT_MATCH_SUMMARY = 'match_summary'
    EVENT_ALERT = 'alert'

    EVENT_CHOICES = [
        (EVENT_ARTICLE_SUMMARY, 'Article Summary'),
        (EVENT_MATCH_SUMMARY, 'Match Summary'),
        (EVENT_ALERT, 'Alert'),
    ]

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='timeline')
    event_type = models.CharField(max_length=32, choices=EVENT_CHOICES)
    headline = models.CharField(max_length=512, blank=True)
    details = models.TextField(blank=True)
    verdict = models.CharField(max_length=16, choices=ArticleAnalysis.VERDICT_CHOICES, default=ArticleAnalysis.VERDICT_UNKNOWN)
    predicted_score = models.CharField(max_length=32, blank=True)
    total_goals = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    confidence = models.PositiveIntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"Timeline {self.event_type} for match {self.match_id}"


class SiteConfiguration(TimestampedModel):
    singleton = models.BooleanField(default=True, editable=False, unique=True)
    ai_base_url = models.URLField(default='https://api.openai.com/v1/chat/completions')
    ai_api_key = models.CharField(max_length=255, blank=True)
    ai_model = models.CharField(max_length=128, default='gpt-4o-mini')
    article_prompt = models.TextField(blank=True)
    match_prompt = models.TextField(blank=True)
    consensus_prompt = models.TextField(blank=True)
    update_interval_minutes = models.PositiveIntegerField(default=180)
    last_update_at = models.DateTimeField(null=True, blank=True)
    last_article_sync_at = models.DateTimeField(null=True, blank=True)
    sogou_enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = 'Site Configuration'
        verbose_name_plural = 'Site Configuration'

    def __str__(self) -> str:
        return 'Site Configuration'

    @classmethod
    def load(cls) -> 'SiteConfiguration':
        obj, _ = cls.objects.get_or_create(singleton=True, defaults={
            'article_prompt': (
                'Read the football preview article below and extract key insights. '
                'Respond with JSON: {"headline": str, "key_points": str, "verdict": '
                '"home_win|away_win|draw|unknown", "predicted_score": str, "total_goals": number, '
                '"confidence": int, "risk_factors": str}'
            ),
            'match_prompt': (
                'Combine the article summaries and match statistics to produce an AI forecast. '
                'Respond with JSON: {"headline": str, "key_points": str, "verdict": '
                '"home_win|away_win|draw", "predicted_score": str, "total_goals": number, '
                '"confidence": int, "risk_factors": str}'
            ),
            'consensus_prompt': (
                'Given multiple article predictions and detailed match data, assess their consensus '
                'and deliver a final conclusion. Respond with JSON: {"headline": str, '
                '"justification": str, "verdict": "home_win|away_win|draw", "confidence": int, '
                '"predicted_score": str, "total_goals": number}'
            ),
        })
        return obj

    def update_timestamps(self, *, match_sync: bool = False, article_sync: bool = False) -> None:
        now = timezone.now()
        if match_sync:
            self.last_update_at = now
        if article_sync:
            self.last_article_sync_at = now
        self.save(update_fields=['last_update_at', 'last_article_sync_at', 'updated_at'])
