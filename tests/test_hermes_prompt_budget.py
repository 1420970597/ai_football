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

    async def test_corner_intent_never_reads_goal_totals_or_invents_supported_type(self):
        root=Path(__file__).resolve().parents[1]
        skill=(root/'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        for rule in ('角球大小与进球大小是不同玩法','禁止用market_kind=total',
                     '不得发明corner_total','market_query=角球',
                     '返回unsupported_markets时准确说明系统当前不支持该原生玩法',
                     '不提交任何quote'):
            self.assertIn(rule,skill)

    async def test_unknown_period_uses_native_identity_only_batch_and_short_receipt(self):
        root=Path(__file__).resolve().parents[1]
        skill=(root/'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        for phrase in ('期间未知的单场或多场大小球也首查review_match_batch',
                       'period=null、source_periods=[]','只唯一识别、不读盘、不录入',
                       '不为凑参数猜期间','不删改来源联赛','通常一行即可',
                       '不以简短输出跳过任何原文或图片'):
            self.assertIn(phrase,skill)

    async def test_correct_score_selection_is_not_current_score_or_goal_total(self):
        root=Path(__file__).resolve().parents[1]
        skill=(root/'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        for phrase in ('波胆／波膽／正确比分单据也是推荐意图',
                       '不要求正文复述、投注金额或确认单',
                       '不是即时比分，不与工具当前0-1作相等检查',
                       '禁止改成total','不读盘、不提交quote'):
            self.assertIn(phrase,skill)

    async def test_breakthrough_short_text_scoreboard_does_not_invent_period_or_half_goal_line(self):
        root=Path(__file__).resolve().parents[1]
        skill=(root/'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        for phrase in ('“破／破蛋”加普通滚球比分条不是已选盘口',
                       '不能补成全场大0.5','0-0是当前比分',
                       'market_query保留原短句“破”、period=null、source_periods=[]',
                       '不补数字、不读盘、不录入','来源另外明确半场大0.5或全场大0.5时仍按该明确所选项核验',
                       '看到任何候选/库存之前保存来源缺期间及缺数字线'):
            self.assertIn(phrase,skill)

    async def test_empty_current_score_reply_does_not_reapply_archived_pick(self):
        root=Path(__file__).resolve().parents[1]
        skill=(root/'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        for phrase in ('空正文回复旧推荐且本条只有比分行时',
                       '回复关系本身不代表再次采用旧推荐',
                       '禁止把reply_text当成本条正文','禁止按旧单延续核验当前盘口',
                       '不能把旧单本来缺失的期间补成全场',
                       '不search、不读盘、不录入','不能一律跳过空正文或回复消息'):
            self.assertIn(phrase,skill)

    async def test_esports_pick_requires_native_identity_without_inventing_supported_period(self):
        root=Path(__file__).resolve().parents[1]
        skill=(root/'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        for phrase in ('电子竞技明确选项也必须先查原生赛事',
                       '不得未经查询就声称工具只覆盖足球篮球、不包含电竞',
                       'sport=esports调用review_match_batch',
                       'market_kind保留原文“击杀总数”、period=null/source_periods=[]',
                       '“第一局”不是full_time或上下半场，不借BO5推断全场',
                       '不能推导整个平台不支持该项目',
                       '禁止把击杀大小改成足球进球total'):
            self.assertIn(phrase,skill)

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
