"""Compact request context must retain the full preloaded review contract."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid

import httpx
from app import hermes_client


class HermesPromptBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_wire_prompt_preserves_multimodal_initial_input(self):
        captured = []

        async def handler(request):
            captured.append(request)
            return httpx.Response(200, json={'choices': [
                {'finish_reason': 'stop', 'message': {'content': '当前证据不足，未解决。'}}]})

        settings = SimpleNamespace(hermes_api_url='http://hermes.invalid/v1',
                                   hermes_api_key='isolated-key', hermes_model='isolated-model')
        review, attempt = uuid.uuid4(), uuid.uuid4()
        parts = [{'type': 'text', 'text': f'review_id: {review}\nattempt_id: {attempt}\n'
                  '原文：追4.5/5大\n回复：本场下半场'},
                 {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,isolated-original'}},
                 {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,isolated-reply'}}]
        prepared = hermes_client.PreparedReviewInput(str(review), str(attempt), parts, 1.25)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await hermes_client.review_message(settings, {}, review, attempt, client=client, prepared_input=prepared)
        self.assertEqual(1, len(captured))
        body = json.loads(captured[0].content)
        self.assertEqual(parts, body['messages'][-1]['content'])
        self.assertEqual('system', body['messages'][0]['role'])
        prompt = body['messages'][0]['content']
        self.assertEqual(hermes_client.SYSTEM_PROMPT, prompt)
        self.assertLessEqual(len(prompt.encode('utf-8')), 2000)
        self.assertIn('expert-review/preload-v1', prompt)
        self.assertIn('预载不完整时停止审核', prompt)
        self.assertIn('review_id', prompt)
        self.assertIn('attempt_id', prompt)
        self.assertIn('不可信', prompt)

    async def test_full_skill_keeps_visual_identity_period_and_receipt_rules(self):
        root = Path(__file__).resolve().parents[1]
        skill = (root / 'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        for phrase in ('expert-review/preload-v1', '正文为空不等于没有推荐', '2x1 可單可串',
                       '正文无需再次写方向或盘口', '缺有效报价不等于没有推荐意图',
                       '原消息附近不超过7天', 'matching_candidates_complete=false',
                       'search_complete=true且结构化resolution.resolved=true',
                       'market_query=小3.5', 'complete_for_filter=true', 'selection_line=-line',
                       'rejection_guidance.retry_same_evidence=false', 'historical_replay',
                       '欧洲盘／歐洲盤／亚洲盘', '未知源时间', 'accepted'):
            with self.subTest(rule=phrase):
                self.assertIn(phrase, skill)

    async def test_skill_never_promotes_candidate_name_into_source_identity(self):
        root = Path(__file__).resolve().parents[1]
        skill = (root / 'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        self.assertIn('team_a/team_b始终保留原文', skill)
        self.assertIn('禁止把候选home_team/away_team反填为来源', skill)
        self.assertNotIn('优先把候选实际返回的标准home_team/away_team原样传给解析工具', skill)
