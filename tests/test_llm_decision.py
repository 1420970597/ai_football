#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端与决策引擎的单元测试（T4/T5）。

覆盖：
  - 从 pi 配置加载（含缺失/占位符密钥的可操作报错）
  - base_url scheme 白名单（防 file:// 本地文件读取）
  - JSON 提取的**截断修复**（生产实况：推理模型预算耗尽）
  - content/reasoning_content 的区别（不把思考过程当答案）
  - 决策融合：小模型 + LLM → 融合概率 → edge/Kelly → 决策枚举
  - 诚实性约束：LLM 失败时必须降级为 no_llm 而非伪造 edge

不发起任何真实网络请求（全部 mock）。

    python3 -m unittest tests.test_llm_decision -v
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, Mapping
from unittest import mock

from core.models import OddsSnapshot, SnapshotState
from service.decision import (
    DECISION_AVOID,
    DECISION_BUY,
    DECISION_NO_LLM,
    DECISION_WATCH,
    DecisionConfig,
    DecisionEngine,
)
from service.llm import (
    LLMClient,
    LLMConfig,
    LLMError,
    LLMNotConfigured,
    _repair_truncated_json,
    extract_json,
    load_pi_config,
)

_KEY = "sk-test-key-not-real"
_BASE = "http://llm.test:8885/v1"


def _extract(text: str) -> Any:
    """取出 JSON 并断言非 None（类型安全，避免 Optional 下标告警）。"""
    got = extract_json(text)
    if got is None:
        raise AssertionError("期望能解析出 JSON，实际为 None: %r" % text[:80])
    return got


def _cfg(**kw: Any) -> LLMConfig:
    base = dict(base_url=_BASE, model="test-model", provider="p", api_key=_KEY)
    base.update(kw)
    return LLMConfig(**base)  # type: ignore[arg-type]


def _resp(content: str = "", reasoning: str = "", finish: str = "stop") -> Dict[str, Any]:
    msg: Dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return {"choices": [{"index": 0, "message": msg, "finish_reason": finish}]}


def _snap(odds=(2.10, 3.40, 3.60), outcomes=("home", "draw", "away"),
          market="HAD", state=SnapshotState.ACTIVE) -> OddsSnapshot:
    return OddsSnapshot(
        match_id="m1", league="测试联赛", home="主队", away="客队",
        market=market, outcomes=outcomes, odds=odds,
        state=state, captured_at=datetime.now(timezone.utc), source="乐鱼API",
    )


# --------------------------------------------------------------------------- #
# JSON 提取
# --------------------------------------------------------------------------- #

class TestExtractJson(unittest.TestCase):
    def test_plain_object(self) -> None:
        self.assertEqual(extract_json('{"a":1}'), {"a": 1})

    def test_fenced_json(self) -> None:
        self.assertEqual(extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_json_with_prose_around_it(self) -> None:
        self.assertEqual(extract_json('说明 {"a":1} 结尾'), {"a": 1})

    def test_non_json_returns_none(self) -> None:
        self.assertIsNone(extract_json("我无法判断"))
        self.assertIsNone(extract_json(""))


class TestTruncatedRepair(unittest.TestCase):
    """推理模型预算耗尽会截断 JSON —— 这是生产实况，必须能救回。"""

    def test_production_truncation(self) -> None:
        # 实测截断：probabilities 完整，但 confidence 的键没写完
        text = ('{"probabilities": {"home": 0.55, "draw": 0.23, "away": 0.22}, "confid')
        got = _extract(text)
        self.assertIsInstance(got, dict)
        self.assertEqual(got["probabilities"],
                         {"home": 0.55, "draw": 0.23, "away": 0.22})

    def test_trailing_comma(self) -> None:
        got = _extract('{"p":{"h":0.5},"c":0.4,')
        self.assertEqual(got, {"p": {"h": 0.5}, "c": 0.4})

    def test_half_written_string_value(self) -> None:
        got = _extract('{"probabilities":{"home":0.5},"reason":"主队')
        self.assertIsInstance(got, dict)
        self.assertIn("probabilities", got)

    def test_nested_unclosed(self) -> None:
        got = _extract('{"probabilities":{"home":0.5,"draw":0.2')
        self.assertEqual(got, {"probabilities": {"home": 0.5}})

    def test_repair_does_not_invent_values(self) -> None:
        """只补齐结构，绝不编造缺失的数值（编造会污染决策）。"""
        out = _repair_truncated_json('{"probabilities": {"home": 0.5}, "confidence"')
        assert out is not None
        parsed = json.loads(out)
        # confidence 未给出 → 结果里不应出现该键
        self.assertNotIn("confidence", parsed)

    def test_repair_returns_none_when_nothing_to_do(self) -> None:
        self.assertIsNone(_repair_truncated_json("no braces here"))


# --------------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------------- #

class TestLoadPiConfig(unittest.TestCase):
    def _mock_files(self, settings: Mapping[str, Any],
                    models: Mapping[str, Any],
                    auth: Mapping[str, Any]):
        def fake(path):  # noqa: ANN001
            name = str(path).rsplit("/", 1)[-1]
            return {"settings.json": settings, "models.json": models,
                    "auth.json": auth}.get(name, {})
        return mock.patch("service.llm._read_json", side_effect=fake)

    def test_reads_pi_config(self) -> None:
        models = {"providers": {"p": {"baseUrl": _BASE,
                                      "models": [{"id": "m-x"}]}}}
        with self._mock_files({"defaultProvider": "p", "defaultModel": "m-x"},
                              models, {"p": {"key": _KEY}}):
            cfg = load_pi_config(env={})
        self.assertEqual(cfg.model, "m-x")
        self.assertEqual(cfg.provider, "p")
        self.assertTrue(cfg.api_key)

    def test_vendor_prefixed_model_is_normalised(self) -> None:
        """pi 的 defaultModel 可能是 vendor/id，而 provider 用裸 id。"""
        models = {"providers": {"p": {"baseUrl": _BASE, "models": [{"id": "m-x"}]}}}
        with self._mock_files({"defaultProvider": "p", "defaultModel": "vendor/m-x"},
                              models, {"p": {"key": _KEY}}):
            self.assertEqual(load_pi_config(env={}).model, "m-x")

    def test_env_overrides(self) -> None:
        models = {"providers": {"p": {"baseUrl": _BASE, "models": [{"id": "m-x"}]}}}
        with self._mock_files({}, models, {}):
            cfg = load_pi_config(env={"PI_LLM_PROVIDER": "p", "PI_LLM_MODEL": "m-x",
                                      "PI_LLM_API_KEY": _KEY})
        self.assertEqual(cfg.model, "m-x")

    def test_missing_provider_is_actionable(self) -> None:
        with self._mock_files({}, {}, {}):
            with self.assertRaises(LLMNotConfigured) as ctx:
                load_pi_config(env={})
        self.assertIn("PI_LLM_PROVIDER", str(ctx.exception))

    def test_placeholder_key_rejected(self) -> None:
        """占位符密钥必须被识别（含大小写变体），不得当作可用凭据。"""
        models = {"providers": {"p": {"baseUrl": _BASE, "models": [{"id": "m"}]}}}
        for placeholder in ("YOUR_API_KEY", "your_api_key", "sk-xxx", "changeme"):
            with self.subTest(placeholder=placeholder):
                with self._mock_files({"defaultProvider": "p"}, models,
                                      {"p": {"key": placeholder}}):
                    with self.assertRaises(LLMNotConfigured):
                        load_pi_config(env={})

    def test_missing_base_url_rejected(self) -> None:
        models = {"providers": {"p": {"models": [{"id": "m"}]}}}
        with self._mock_files({"defaultProvider": "p"}, models,
                              {"p": {"key": _KEY}}):
            with self.assertRaises(LLMNotConfigured):
                load_pi_config(env={})


class TestConfigSafety(unittest.TestCase):
    def test_rejects_non_http_scheme(self) -> None:
        """防配置注入：file:// 会导致任意本地文件读取。"""
        for bad in ("file:///etc/passwd", "ftp://x", "", "gopher://x"):
            with self.subTest(bad=bad):
                with self.assertRaises(LLMNotConfigured):
                    _cfg(base_url=bad)

    def test_chat_url_appends_path(self) -> None:
        self.assertEqual(_cfg().chat_url, _BASE + "/chat/completions")

    def test_chat_url_respects_full_endpoint(self) -> None:
        cfg = _cfg(base_url=_BASE + "/chat/completions")
        self.assertEqual(cfg.chat_url, _BASE + "/chat/completions")

    def test_masked_hides_key(self) -> None:
        d = _cfg().masked()
        self.assertNotIn(_KEY, json.dumps(d))
        self.assertTrue(d["has_key"])


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

class TestLLMClient(unittest.TestCase):
    def test_returns_content(self) -> None:
        c = LLMClient(_cfg())
        with mock.patch.object(c, "_post", return_value=_resp('{"a":1}')):
            self.assertEqual(c.complete("hi"), '{"a":1}')

    def test_reasoning_only_raises_not_returns_thinking(self) -> None:
        """关键回归：不能把 reasoning_content（思考）当成答案返回。

        推理型模型在预算不足时 content 为空、只有 reasoning；
        早期实现会把思考内容当答案，污染 JSON 解析。
        """
        c = LLMClient(_cfg())
        with mock.patch.object(c, "_post",
                               return_value=_resp("", "思考中...")):
            with self.assertRaises(LLMError) as ctx:
                c.complete("hi")
        msg = str(ctx.exception)
        self.assertIn("正文为空", msg)
        self.assertIn("max_tokens", msg)

    def test_missing_choices_raises(self) -> None:
        c = LLMClient(_cfg())
        with mock.patch.object(c, "_post", return_value={}):
            with self.assertRaises(LLMError):
                c.complete("hi")

    def test_retries_on_5xx(self) -> None:
        import urllib.error

        c = LLMClient(_cfg(max_retries=3))
        err = urllib.error.HTTPError(_BASE, 502, "bad", {}, None)  # type: ignore[arg-type]
        with mock.patch.object(c, "_post", side_effect=[err, _resp('{"ok":1}')]):
            with mock.patch("service.llm.time.sleep"):
                self.assertEqual(c.complete("hi"), '{"ok":1}')

    def test_4xx_does_not_retry(self) -> None:
        import urllib.error

        c = LLMClient(_cfg(max_retries=3))
        err = urllib.error.HTTPError(_BASE, 400, "bad", {}, None)  # type: ignore[arg-type]
        with mock.patch.object(c, "_post", side_effect=err) as m:
            with self.assertRaises(LLMError):
                c.complete("hi")
        self.assertEqual(m.call_count, 1, "4xx 重试无意义，应只调用一次")

    def test_complete_json_parses(self) -> None:
        c = LLMClient(_cfg())
        with mock.patch.object(c, "complete", return_value='{"a":1}'):
            self.assertEqual(c.complete_json("hi"), {"a": 1})

    def test_complete_json_raises_on_garbage(self) -> None:
        c = LLMClient(_cfg())
        with mock.patch.object(c, "complete", return_value="没有 JSON"):
            with self.assertRaises(LLMError):
                c.complete_json("hi")

    def test_health_never_leaks_key(self) -> None:
        c = LLMClient(_cfg())
        self.assertNotIn(_KEY, json.dumps(c.health()))


# --------------------------------------------------------------------------- #
# 决策引擎
# --------------------------------------------------------------------------- #

class TestDecisionEngineSmallModel(unittest.TestCase):
    def test_no_llm_yields_no_llm_decision(self) -> None:
        """诚实性：没有独立信息源就不给买入建议。"""
        e = DecisionEngine(DecisionConfig(use_llm=False))
        d = e.decide(_snap())
        self.assertEqual(d.decision, DECISION_NO_LLM)

    def test_llm_failure_degrades_to_no_llm(self) -> None:
        c = LLMClient(_cfg())
        e = DecisionEngine(DecisionConfig())
        e.attach_llm(c)
        with mock.patch.object(c, "complete_json", side_effect=LLMError("上游 502")):
            d = e.decide(_snap())
        self.assertEqual(d.decision, DECISION_NO_LLM)
        self.assertTrue(any("502" in n for n in d.notes))

    def test_market_probability_gives_negative_edge(self) -> None:
        """用市场自己的去水概率算 edge 必然 ≤0 —— 这是报告 §8.2 的核心结论。"""
        e = DecisionEngine(DecisionConfig())
        d = e.decide(_snap())
        for c in d.candidates:
            self.assertLessEqual(c.edge, 1e-9)

    def test_suspended_snapshot_is_avoided(self) -> None:
        e = DecisionEngine(DecisionConfig())
        d = e.decide(_snap(state=SnapshotState.SUSPENDED))
        self.assertEqual(d.decision, DECISION_AVOID)

    def test_delisted_snapshot_is_avoided(self) -> None:
        e = DecisionEngine(DecisionConfig())
        d = e.decide(_snap(state=SnapshotState.DELISTED))
        self.assertEqual(d.decision, DECISION_AVOID)


class TestDecisionFusion(unittest.TestCase):
    """LLM 提供独立信息 → 融合 → 可能产生正 edge。"""

    def _llm_agreeing(self, probs: Mapping[str, float],
                      confidence: float = 0.9,
                      reason: str = "测试理由") -> LLMClient:
        """构造一个返回指定概率的 LLM 客户端（mock 掉真实调用）。"""
        c = LLMClient(_cfg())
        payload = {"probabilities": dict(probs), "confidence": confidence,
                   "reason": reason}
        mock.patch.object(c, "complete_json", return_value=payload).start()
        self.addCleanup(mock.patch.stopall)
        return c

    def test_high_edge_with_confidence_is_buy(self) -> None:
        c = LLMClient(_cfg())
        # 给 home 极高概率，制造明显正 edge
        payload = {"probabilities": {"home": 0.90, "draw": 0.05, "away": 0.05},
                   "confidence": 0.9, "reason": "主队强势"}
        e = DecisionEngine(DecisionConfig(min_edge=0.01, min_confidence=0.3))
        e.attach_llm(c)
        with mock.patch.object(c, "complete_json", return_value=payload):
            d = e.decide(_snap(odds=(2.10, 3.40, 3.60)))
        self.assertEqual(d.decision, DECISION_BUY)
        self.assertEqual(d.pick, "home")
        self.assertGreater(d.edge, 0)
        self.assertGreater(d.kelly, 0)
        self.assertEqual(d.llm_reason, "主队强势")

    def test_low_confidence_is_watch_not_buy(self) -> None:
        c = LLMClient(_cfg())
        payload = {"probabilities": {"home": 0.90, "draw": 0.05, "away": 0.05},
                   "confidence": 0.20, "reason": "不确定"}
        e = DecisionEngine(DecisionConfig(min_edge=0.01, min_confidence=0.5))
        e.attach_llm(c)
        with mock.patch.object(c, "complete_json", return_value=payload):
            d = e.decide(_snap())
        self.assertEqual(d.decision, DECISION_WATCH)

    def test_missing_outcome_in_llm_is_failure(self) -> None:
        """LLM 少给一个结果 → 不能猜，必须判为失败。"""
        c = LLMClient(_cfg())
        payload = {"probabilities": {"home": 0.6, "draw": 0.4}, "confidence": 0.9}
        e = DecisionEngine(DecisionConfig())
        e.attach_llm(c)
        with mock.patch.object(c, "complete_json", return_value=payload):
            d = e.decide(_snap(odds=(2.1, 3.4, 3.6)))
        self.assertEqual(d.decision, DECISION_NO_LLM)
        self.assertTrue(any("缺失" in n for n in d.notes))

    def test_llm_weight_capped(self) -> None:
        c = LLMClient(_cfg())
        payload = {"probabilities": {"home": 0.9, "draw": 0.05, "away": 0.05},
                   "confidence": 1.0, "reason": ""}
        e = DecisionEngine(DecisionConfig(max_llm_weight=0.5))
        e.attach_llm(c)
        with mock.patch.object(c, "complete_json", return_value=payload):
            d = e.decide(_snap())
        self.assertLessEqual(d.llm_weight, 0.5)

    def test_probabilities_are_normalised(self) -> None:
        c = LLMClient(_cfg())
        # 和为 2 的非法输入应被归一化
        payload = {"probabilities": {"home": 1.0, "draw": 0.5, "away": 0.5},
                   "confidence": 0.8}
        e = DecisionEngine(DecisionConfig())
        e.attach_llm(c)
        with mock.patch.object(c, "complete_json", return_value=payload):
            d = e.decide(_snap())
        tot = sum(x.p_combined for x in d.candidates)
        self.assertAlmostEqual(tot, 1.0, places=6)

    def test_two_way_market_supported(self) -> None:
        c = LLMClient(_cfg())
        payload = {"probabilities": {"over": 0.6, "under": 0.4}, "confidence": 0.8}
        e = DecisionEngine(DecisionConfig())
        e.attach_llm(c)
        with mock.patch.object(c, "complete_json", return_value=payload):
            d = e.decide(_snap(odds=(1.9, 2.0), outcomes=("over", "under"),
                               market="OU(2.5)"))
        self.assertEqual(len(d.candidates), 2)

    def test_kelly_is_capped(self) -> None:
        c = LLMClient(_cfg())
        payload = {"probabilities": {"home": 0.97, "draw": 0.02, "away": 0.01},
                   "confidence": 0.95}
        e = DecisionEngine(DecisionConfig(max_stake_pct=0.05))
        e.attach_llm(c)
        with mock.patch.object(c, "complete_json", return_value=payload):
            d = e.decide(_snap(odds=(2.1, 3.4, 3.6)))
        self.assertLessEqual(d.kelly, 0.05 + 1e-9)

    def test_decide_many_sorted_by_rank(self) -> None:
        e = DecisionEngine(DecisionConfig(use_llm=False))
        items = [(_snap(), None, None, 1) for _ in range(3)]
        out = e.decide_many(items)
        self.assertEqual(len(out), 3)
        scores = [d.rank_score for d in out]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_as_dict_is_json_serialisable(self) -> None:
        e = DecisionEngine(DecisionConfig(use_llm=False))
        d = e.decide(_snap())
        json.dumps(d.as_dict())  # 不应抛异常

    def test_engine_health_reports_config(self) -> None:
        e = DecisionEngine(DecisionConfig(use_llm=False))
        h = e.health()
        self.assertIn("config", h)
        self.assertFalse(h["llm"]["available"])


class TestDecisionTrend(unittest.TestCase):
    """盘口走势修正：降赔 = 资金流入 = 概率上调。"""

    def test_down_trend_raises_probability(self) -> None:
        e = DecisionEngine(DecisionConfig(use_llm=False))
        snap = _snap()
        base, _, _ = e._small_model(snap, None)
        trend = {"markets": [{"chpid": "1", "hv": "", "n": 3,
                              "last": {"ot": "home", "delta_pct": -8.0}}]}
        adj, _, _ = e._small_model(snap, trend)
        self.assertGreater(adj[0], base[0])

    def test_up_trend_lowers_probability(self) -> None:
        e = DecisionEngine(DecisionConfig(use_llm=False))
        snap = _snap()
        base, _, _ = e._small_model(snap, None)
        trend = {"markets": [{"chpid": "1", "hv": "", "n": 3,
                              "last": {"ot": "home", "delta_pct": 8.0}}]}
        adj, _, _ = e._small_model(snap, trend)
        self.assertLess(adj[0], base[0])

    def test_trend_adjust_is_capped(self) -> None:
        e = DecisionEngine(DecisionConfig(max_trend_adjust=0.05, use_llm=False))
        snap = _snap()
        base, _, _ = e._small_model(snap, None)
        # 极端走势也不应把概率改动超过上限
        trend = {"markets": [{"chpid": "1", "hv": "", "n": 9,
                              "last": {"ot": "home", "delta_pct": -99.0}}]}
        adj, _, _ = e._small_model(snap, trend)
        self.assertLessEqual(abs(adj[0] - base[0]), 0.05 + 1e-6)

    def test_probabilities_stay_normalised(self) -> None:
        e = DecisionEngine(DecisionConfig(use_llm=False))
        trend = {"markets": [
            {"chpid": "1", "hv": "", "n": 1, "last": {"ot": "home", "delta_pct": -20.0}},
            {"chpid": "2", "hv": "", "n": 1, "last": {"ot": "away", "delta_pct": -20.0}},
        ]}
        adj, _, _ = e._small_model(_snap(), trend)
        self.assertAlmostEqual(sum(adj), 1.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
