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
        self.assertEqual('eb6a7f03068c80a8f84aad0b3810c4e011e0cbae82b47d767b2a7fb2f5b2975f',
                         __import__('hashlib').sha256(prompt.encode()).hexdigest())

    async def test_complete_source_stop_guidance_is_per_attempt_and_preserves_attested_prompt(self):
        from datetime import datetime, timezone
        message={'source_name':'isolated','display_nickname':'isolated','posted_at':datetime.now(timezone.utc),
                 'raw_text':'没有唯一身份的原文','media_objects':[],'reply_context':{}}
        parts=await hermes_client._content_parts(message,uuid.uuid4(),uuid.uuid4())
        self.assertIn('bounded_source_search.complete=true且retry_same_scope=false',parts[0]['text'])
        self.assertIn('不重复resolve、改名或扩大日期',parts[0]['text'])
        self.assertIn('其它独立项继续核验',parts[0]['text'])

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

    async def test_skill_matches_native_single_and_team_total_batch_contract(self):
        root = Path(__file__).resolve().parents[1]
        skill = (root / 'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        for phrase in ('只有1项也直接批量', '精确total/handicap/team_total',
                       'total及team_total传原样market_query', '单队进球必须用team_total',
                       'source_period_unknown时不重试读盘', '不去掉原期间、方向或线值反复刷新'):
            self.assertIn(phrase, skill)
        self.assertNotIn('仅total传原样market_query', skill)
        self.assertIn('单场或多场完整total/handicap/team_total必须首查直接review_match_batch',skill)
        self.assertIn('图片推荐也适用，只有1项同样使用',skill)
        self.assertIn('首查review_match_batch已完整处理的对阵不再使用本节手动查询',skill)
