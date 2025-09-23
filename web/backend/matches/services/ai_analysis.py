from __future__ import annotations

import json

import logging

import re

from dataclasses import dataclass

from typing import Dict, Iterable, List, Optional

import requests

from django.conf import settings

from django.db import transaction

from django.utils import timezone

from matches.models import (

    AiInsight,

    Article,

    ArticleAnalysis,

    Match,

    MatchDetail,

    MatchRecommendation,

    MatchTimelineEntry,

    SiteConfiguration,

)

logger = logging.getLogger(__name__)

JSON_BLOCK_REGEX = re.compile(r"\{.*\}", re.DOTALL)

@dataclass

class AiResult:

    content: str

    usage: Dict[str, int]

    model: str

class AiModelClient:

    def __init__(self, config: SiteConfiguration):

        self.config = config

        self.base_url = config.ai_base_url

        self.api_key = config.ai_api_key

        self.model = config.ai_model

    def is_enabled(self) -> bool:

        return bool(self.api_key and self.base_url and self.model)

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.6, max_tokens: int = 1024) -> Optional[AiResult]:

        if not self.is_enabled():

            logger.debug("AI client disabled; skipping request")

            return None

        payload = {

            'model': self.model,

            'messages': messages,

            'temperature': temperature,

            'max_tokens': max_tokens,

        }

        headers = {

            'Authorization': f'Bearer {self.api_key}',

            'Content-Type': 'application/json',

        }

        try:

            response = requests.post(

                self.base_url,

                json=payload,

                headers=headers,

                timeout=settings.AI_REQUEST_TIMEOUT,

            )

            response.raise_for_status()

            result = response.json()

            choice = (result.get('choices') or [{}])[0]

            message = choice.get('message') or {}

            content = message.get('content', '').strip()

            usage = result.get('usage') or {}

            model = result.get('model', self.model)

            return AiResult(content=content, usage=usage, model=model)

        except Exception as exc:  # pylint: disable=broad-except

            logger.exception("AI request failed: %s", exc)

            return None

    @staticmethod

    def parse_json_block(text: str) -> Optional[Dict]:

        if not text:

            return None

        try:

            return json.loads(text)

        except json.JSONDecodeError:

            match = JSON_BLOCK_REGEX.search(text)

            if match:

                try:

                    return json.loads(match.group())

                except json.JSONDecodeError:

                    return None

        return None

class ArticleAnalyzer:

    def __init__(self, client: AiModelClient, config: SiteConfiguration):

        self.client = client

        self.config = config

    def analyze(self, article: Article, plain_text: str) -> Optional[ArticleAnalysis]:

        if not self.client.is_enabled():

            return None

        prompt = self.config.article_prompt or (

            "and respond in JSON format: "

            '{"headline": str, "key_points": str, "verdict": "home_win|away_win|draw|unknown", '

            '"predicted_score": str, "total_goals": number, "confidence": int, "risk_factors": str}'

        )

        messages = [

            {

                'role': 'system',

                'content': ''

            },

            {

                'role': 'user',

                'content': (

                    f"Article title: {article.title}\n"

                    f"Original link: {article.url}\n"

                    f"Article content: \n{plain_text}\n\n{prompt}"

                )

            }

        ]

        result = self.client.chat(messages, temperature=0.4, max_tokens=1024)

        if not result:

            return None

        payload = self.client.parse_json_block(result.content) or {}

        analysis, _ = ArticleAnalysis.objects.update_or_create(

            article=article,

            defaults={

                'headline': payload.get('headline', '')[:512],

                'key_points': payload.get('key_points', '')[:2000],

                'verdict': payload.get('verdict', ArticleAnalysis.VERDICT_UNKNOWN),

                'predicted_score': str(payload.get('predicted_score', '')).strip(),

                'total_goals': self._safe_float(payload.get('total_goals')),

                'confidence': self._safe_int(payload.get('confidence')),

                'model_name': result.model,

                'tokens_used': self._safe_int((result.usage or {}).get('total_tokens')),

                'raw_response': {'model_output': result.content, 'parsed': payload},

            }

        )

        MatchTimelineEntry.objects.create(

            match=article.match,

            event_type=MatchTimelineEntry.EVENT_ARTICLE_SUMMARY,

            headline=analysis.headline or article.title,

            details=analysis.key_points,

            verdict=analysis.verdict,

            predicted_score=analysis.predicted_score,

            total_goals=analysis.total_goals,

            confidence=analysis.confidence,

            payload={'article_id': article.id},

        )

        return analysis

    @staticmethod

    def _safe_float(value) -> Optional[float]:

        try:

            return float(value)

        except (TypeError, ValueError):

            return None

    @staticmethod

    def _safe_int(value) -> Optional[int]:

        try:

            return int(value)

        except (TypeError, ValueError):

            return None

class MatchInsightBuilder:

    def __init__(self, client: AiModelClient, config: SiteConfiguration):

        self.client = client

        self.config = config

    def build(self, match: Match) -> Optional[AiInsight]:

        analyses = list(

            ArticleAnalysis.objects.filter(article__match=match).order_by('-created_at')

        )

        if not analyses:

            logger.debug("No article analyses available for match %s", match.id)

            return None

        verdict_counts = self._aggregate_verdicts(analyses)

        dominant_verdict, confidence_ratio = self._pick_verdict(verdict_counts, len(analyses))

        insight_payload = {

            'headline': '',

            'key_points': '',

            'verdict': dominant_verdict,

            'predicted_score': '',

            'total_goals': None,

            'confidence': int(confidence_ratio * 100) if confidence_ratio else None,

            'risk_factors': '',

        }

        if self.client.is_enabled():

            ai_payload = self._call_match_summary(match, analyses, verdict_counts, confidence_ratio)

            if ai_payload:

                insight_payload.update(ai_payload)

        else:

            insight_payload['headline'] = f"{len(analyses)}"

            insight_payload['key_points'] = self._build_majority_summary(verdict_counts, analyses)

        with transaction.atomic():

            insight = AiInsight.objects.create(

                match=match,

                headline=insight_payload.get('headline', '')[:512],

                key_points=insight_payload.get('key_points', '')[:2000],

                verdict=insight_payload.get('verdict', dominant_verdict),

                predicted_score=insight_payload.get('predicted_score', ''),

                total_goals=insight_payload.get('total_goals'),

                confidence=insight_payload.get('confidence'),

                model_name=self.client.config.ai_model if self.client.is_enabled() else 'majority-vote',

                provider='ai-aggregator' if self.client.is_enabled() else 'fallback-majority',

                risk_factors=insight_payload.get('risk_factors', ''),

                extra={

                    'verdict_counts': verdict_counts,

                    'article_ids': [analysis.article_id for analysis in analyses],

                },

            )

            MatchTimelineEntry.objects.create(

                match=match,

                event_type=MatchTimelineEntry.EVENT_MATCH_SUMMARY,

                headline=insight.headline or 'AI ',

                details=insight.key_points,

                verdict=insight.verdict,

                predicted_score=insight.predicted_score,

                total_goals=insight.total_goals,

                confidence=insight.confidence,

                payload={'insight_id': insight.id},

            )

            match.latest_ai_summary_at = timezone.now()

            match.save(update_fields=['latest_ai_summary_at', 'updated_at'])

        return insight

    def _aggregate_verdicts(self, analyses: Iterable[ArticleAnalysis]) -> Dict[str, int]:

        counts: Dict[str, int] = {

            ArticleAnalysis.VERDICT_HOME: 0,

            ArticleAnalysis.VERDICT_AWAY: 0,

            ArticleAnalysis.VERDICT_DRAW: 0,

            ArticleAnalysis.VERDICT_UNKNOWN: 0,

        }

        for analysis in analyses:

            counts[analysis.verdict] = counts.get(analysis.verdict, 0) + 1

        return counts

    def _pick_verdict(self, counts: Dict[str, int], total: int) -> (str, float):

        verdict = ArticleAnalysis.VERDICT_UNKNOWN

        ratio = 0.0

        for key, value in counts.items():

            if key == ArticleAnalysis.VERDICT_UNKNOWN:

                continue

            if value > ratio * total:

                verdict = key

                ratio = value / total

        if verdict == ArticleAnalysis.VERDICT_UNKNOWN:

            ratio = 0.0

        return verdict, ratio

    def _build_majority_summary(self, counts: Dict[str, int], analyses: Iterable[ArticleAnalysis]) -> str:

        parts = [f" {len(list(analyses))} "]

        if counts.get(ArticleAnalysis.VERDICT_HOME):

            parts.append(f" {counts[ArticleAnalysis.VERDICT_HOME]} ")

        if counts.get(ArticleAnalysis.VERDICT_DRAW):

            parts.append(f" {counts[ArticleAnalysis.VERDICT_DRAW]} ")

        if counts.get(ArticleAnalysis.VERDICT_AWAY):

            parts.append(f" {counts[ArticleAnalysis.VERDICT_AWAY]} ")

        return ''.join(parts)

    def _call_match_summary(

        self,

        match: Match,

        analyses: List[ArticleAnalysis],

        verdict_counts: Dict[str, int],

        confidence_ratio: float,

    ) -> Optional[Dict]:

        if not self.client.is_enabled():

            return None

        summary_prompt = self.config.match_prompt or (

            " JSON"

            '{"headline": str, "key_points": str, "verdict": "home_win|away_win|draw", '

            '"predicted_score": str, "total_goals": number, "confidence": int, "risk_factors": str}'

        )

        detail_payload = self._build_match_detail_payload(match)

        article_payload = [

            {

                'headline': analysis.headline,

                'key_points': analysis.key_points,

                'verdict': analysis.verdict,

                'predicted_score': analysis.predicted_score,

                'confidence': analysis.confidence,

            }

            for analysis in analyses

        ]

        messages = [

            {

                'role': 'system',

                'content': ''

            },

            {

                'role': 'user',

                'content': (

                    f"{json.dumps(detail_payload, ensure_ascii=False)}\n"

                    f"{json.dumps(article_payload, ensure_ascii=False)}\n"

                    f"{json.dumps(verdict_counts, ensure_ascii=False)}"

                    f"\n{confidence_ratio:.2f}\n\n{summary_prompt}"

                )

            }

        ]

        result = self.client.chat(messages, temperature=0.2, max_tokens=1024)

        if not result:

            return None

        payload = self.client.parse_json_block(result.content)

        if not payload:

            return None

        payload['confidence'] = self._safe_int(payload.get('confidence'))

        payload['total_goals'] = self._safe_float(payload.get('total_goals'))

        payload['model'] = result.model

        return payload

    def _build_match_detail_payload(self, match: Match) -> Dict:

        detail: Optional[MatchDetail] = getattr(match, 'detail', None)

        return {

            'match_id': match.external_id,

            'home_team': match.home_team.name,

            'away_team': match.away_team.name,

            'league': match.league,

            'datetime': match.match_datetime.isoformat(),

            'status': match.status,

            'statistics': detail.statistics if detail else {},

            'injuries': detail.injuries if detail else {},

            'history': detail.history if detail else {},

        }

    @staticmethod

    def _safe_float(value) -> Optional[float]:

        try:

            return float(value)

        except (TypeError, ValueError):

            return None

    @staticmethod

    def _safe_int(value) -> Optional[int]:

        try:

            return int(value)

        except (TypeError, ValueError):

            return None

__all__ = ['AiModelClient', 'ArticleAnalyzer', 'MatchInsightBuilder']

